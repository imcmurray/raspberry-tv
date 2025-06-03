import os
import time
import struct
import fcntl
import mmap
import logging
import configparser
import requests
import json
import threading
import datetime
import hashlib
import tempfile # Ensure tempfile is imported
import cv2
import numpy as np
import io # For io.BytesIO
# time is already imported globally
from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
# from selenium.webdriver.common.by import By
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException as SeleniumTimeoutException
from PIL import UnidentifiedImageError


# Basic logging setup
# Consider making the log level configurable (e.g., from config file or env var)
# For production, INFO might be too verbose for some operations, DEBUG is for development.
logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO").upper(), 
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)

FB_DEVICE = "/dev/fb1"
CONFIG_FILE_PATH = '/etc/slideshow.conf'
CHROME_DRIVER_PATH = os.environ.get('CHROME_DRIVER_PATH', '/usr/bin/chromedriver')
WEBDRIVER_LOG_PATH = '/tmp/chromedriver.log'


need_refetch = threading.Event()
processed_slides_global_for_cleanup = [] # For atexit cleanup

# Font and Text Cache Configuration
FONT_PATH_PRIMARY = "freesansbold.ttf" 
FONT_PATH_FALLBACK = "DejaVuSans.ttf" 
TEXT_CACHE_MAX_SIZE = 50
text_cache = {}
last_datetime_minute = None

# Website Screenshot Cache
website_screenshot_cache = {}
WEBSITE_CACHE_EXPIRY_SECONDS = 3600 # Cache website screenshots for 1 hour
MAX_WEBSITE_CACHE_ENTRIES = 10 # Max number of website screenshots to keep in cache

# Transition Constants
FADE_STEPS = 25 # Number of steps for fade animation
DEFAULT_TRANSITION_TIME_MS = 500 # Default duration for slide transitions in milliseconds

# Main Loop Behavior
SCROLL_FPS = 30  # Target frames per second for scrolling text animation
SCROLL_SPEED_PPS = 100  # Scroll speed in pixels per second for scrolling text
DEFAULT_SLIDE_DURATION_S = 10  # Default duration for a slide in seconds


# Helper to convert hex color to RGB/RGBA tuple
def hex_to_rgb(hex_color, alpha=None):
    """Converts a hex color string (e.g., "#RRGGBB") to an RGB or RGBA tuple."""
    hex_color = hex_color.lstrip('#')
    lv = len(hex_color)
    rgb = tuple(int(hex_color[i:i + lv // 3], 16) for i in range(0, lv, lv // 3))
    if alpha is not None:
        return rgb + (alpha,)
    return rgb


def get_font(size):
    """
    Loads a TrueType font from specified paths or a default Pillow font.

    Args:
        size (int): The desired font size.

    Returns:
        ImageFont.FreeTypeFont or ImageFont.ImageFont: The loaded font object, 
                                                       or None if all loading attempts fail.
    """
    try:
        return ImageFont.truetype(FONT_PATH_PRIMARY, size)
    except IOError:
        logging.warning(f"Primary font '{FONT_PATH_PRIMARY}' not found at size {size}. Trying fallback.")
        try:
            return ImageFont.truetype(FONT_PATH_FALLBACK, size)
        except IOError:
            logging.warning(f"Fallback font '{FONT_PATH_FALLBACK}' not found at size {size}. Using Pillow's default.")
            try:
                # Pillow's default font is very basic and might not support sizes well for direct size setting.
                # ImageFont.load_default() returns a built-in bitmap font that's not scalable by size parameter here.
                # For a scalable default if truetype fonts fail, consider bundling a known-good .ttf file.
                # Or, use a very simple truetype font if available on most systems e.g. "arial.ttf" on Windows.
                # For now, if specific fonts fail, load_default() is the last resort.
                return ImageFont.load_default() 
            except Exception as e:
                 logging.error(f"Could not load any font, including Pillow's default: {e}", exc_info=True)
                 return None 

def render_text_to_surface(text_content, font_size_str, text_color_hex, text_bg_color_hex=None, text_align_surface_width=None, text_padding=5):
    """
    Renders text onto a new Pillow Image surface, typically with a transparent background.
    The surface width can be fixed (e.g., for non-scrolling text aligned within a zone) 
    or determined by text content (e.g., for scrolling text).

    Args:
        text_content (str): The text to render.
        font_size_str (str): Symbolic font size ("small", "medium", "large", "xlarge").
        text_color_hex (str): Hex string for text color (e.g., "#FFFFFF").
        text_bg_color_hex (str, optional): Hex string for background color. Defaults to None (transparent).
        text_align_surface_width (int, optional): If provided, fixes the surface width for text alignment.
                                                Used for non-scrolling text that needs to align within a zone.
                                                If None, surface width fits the text content (for scrolling).
        text_padding (int): Padding around the text within its surface.

    Returns:
        tuple(Image.Image, int): A tuple containing the rendered Pillow Image object (RGBA) 
                                 and the original width of the text content itself (before padding).
                                 Returns (None, 0) if rendering fails.
    """
    font_size_mapping = {"small": 24, "medium": 36, "large": 48, "xlarge": 60} # XL added
    pixel_size = font_size_mapping.get(font_size_str.lower(), 36) # Default to medium
    
    font = get_font(pixel_size)
    if not font:
        logging.error("Cannot render text: Font not loaded.")
        return None, 0

    text_color_rgb = hex_to_rgb(text_color_hex)

    # Get text dimensions using getbbox
    try:
        bbox = font.getbbox(text_content) # (left, top, right, bottom)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1] # Height of the text content itself
        # The actual height needed for the surface might be bbox[3] if top (bbox[1]) is negative (descenders)
        # or more generally, bbox[3] - min(0, bbox[1]) to account for text going below baseline.
        # For simplicity, using text_height based on bbox difference, and adding padding.
        # A more robust way is to use font.getmask(text_content).size for exact pixel dimensions.
    except AttributeError: # Older Pillow might not have getbbox on font
        try:
            text_width, text_height = font.getsize(text_content) # Deprecated but fallback
        except AttributeError:
             logging.error("Font object does not support getbbox or getsize. Cannot measure text.")
             return None, 0


    surface_height = text_height + (2 * text_padding)
    
    # Determine surface width
    if screen_width_for_scrolling and text_width < screen_width_for_scrolling:
        # For non-scrolling text that should fit a line, or scrolling text shorter than screen
        surface_width = screen_width_for_scrolling
    else:
        surface_width = text_width + (2 * text_padding) # Pad for text that might be slightly wider due to hinting

    original_text_width = text_width # Store the actual text width before padding or screen fitting

    txt_surface = Image.new('RGBA', (surface_width, surface_height), (0,0,0,0))
    draw = ImageDraw.Draw(txt_surface)

    if text_bg_color_hex:
        bg_color_rgba = hex_to_rgb(text_bg_color_hex, alpha=200) # Default 200/255 alpha for background
        # Adjust radius based on surface height for a pleasant look
        radius = min(10, surface_height // 3) 
        try:
            draw.rounded_rectangle([(0,0), (surface_width, surface_height)], radius=radius, fill=bg_color_rgba)
        except TypeError: # Older Pillow might not support float radius in rounded_rectangle or specific args
            draw.rectangle([(0,0), (surface_width, surface_height)], fill=bg_color_rgba)


    # Position text within its surface (centered vertically, left-aligned horizontally)
    # text_x = text_padding
    # text_y = (surface_height - text_height) / 2 # This centers based on measured text_height
    # A common way to draw text is to align based on the top of the text:
    text_x = text_padding
    text_y = text_padding # Align to top-left with padding. font.getbbox accounts for ascenders/descenders.

    draw.text((text_x, text_y), text_content, font=font, fill=text_color_rgb)
    
    logging.info(f"Rendered text '{text_content[:30]}...' to {surface_width}x{surface_height} surface.")
    return txt_surface, original_text_width


def get_cached_text_surface(text_params, screen_width_for_text_area, force_refresh=False):
    """
    Retrieves a cached text surface or renders and caches it.
    text_params is a dict from slide_definition's 'text_overlays' list.
    """
    global last_datetime_minute, text_cache

    raw_text = text_params.get('text', '')
    current_text = raw_text

    if "{datetime}" in raw_text:
        now = datetime.datetime.now()
        current_minute = now.minute
        if current_minute != last_datetime_minute:
            force_refresh = True
            last_datetime_minute = current_minute
            logging.info("Datetime minute changed, forcing text refresh for datetime templates.")
        current_text = raw_text.replace("{datetime}", now.strftime("%Y-%m-%d %H:%M"))

    font_size = text_params.get('size', 'medium')
    color = text_params.get('color', '#FFFFFF')
    bg_color = text_params.get('bg_color') # Can be None
    is_scrolling = text_params.get('scroll', False)
    
    # Use screen_width_for_text_area if text is not scrolling or if it's shorter than this width
    # This helps in creating surfaces that fit the intended display line for non-scrolling text.
    # For scrolling text, if it's very long, its own width will be used by render_text_to_surface.
    # text_align_surface_width is passed to render_text_to_surface as text_align_surface_width.
    
    cache_key_parts = [current_text, font_size, color, bg_color, screen_width_for_text_area, text_params.get('padding', 5)]
    # If it's for scrolling, the screen_width_for_text_area is not used for rendering width, so differentiate cache key
    # The render_width_param was simplified. Let's use text_align_surface_width directly in render_text_to_surface
    # and make cache key depend on whether it was used.
    # If text_align_surface_width is passed to render_text_to_surface, it means fixed width rendering.
    # If it's None, it means auto-width for scrolling.
    # The `is_scrolling` flag helps determine this.
    
    # Revised cache key logic:
    # The actual rendered surface depends on text_align_surface_width if provided to render_text_to_surface.
    # If is_scrolling is True, text_align_surface_width is typically None for render_text_to_surface,
    # letting text width define surface width.
    # If is_scrolling is False, text_align_surface_width (e.g. screen_width) is passed to render_text_to_surface
    # to ensure static text is rendered in a surface of that fixed width for alignment.
    
    effective_render_width = screen_width_for_text_area if not is_scrolling else None
    cache_key_parts = [current_text, font_size, color, bg_color, effective_render_width, text_params.get('padding', 5)]
    cache_key = tuple(cache_key_parts)

    if not force_refresh and cache_key in text_cache:
        logging.debug(f"Returning cached text surface for: {current_text[:30]}... with key {cache_key}")
        return text_cache[cache_key]

    # Render the text
    # If scrolling, render_width_param (text_align_surface_width for render_text_to_surface) should be None
    # so the surface is sized to the text.
    # If not scrolling, it should be screen_width_for_text_area.
    text_surface, original_text_width = render_text_to_surface(
        current_text, 
        font_size, 
        color, 
        bg_color, 
        text_align_surface_width=effective_render_width, # Pass the width for fixed-size surface if not scrolling
        text_padding=text_params.get('padding', 5)
    )

    if text_surface:
        if len(text_cache) >= TEXT_CACHE_MAX_SIZE:
            text_cache.pop(next(iter(text_cache))) # Remove the oldest item (FIFO)
            logging.info("Text cache full. Removed oldest entry.")
        text_cache[cache_key] = (text_surface, original_text_width)
        logging.debug(f"Cached new text surface for: {current_text[:30]}...")
        return text_surface, original_text_width
    
    return None, 0


def get_requests_session():
    """Creates a requests session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def fetch_document(couchdb_url, tv_uuid):
    """Fetches the slideshow document from CouchDB."""
    doc_url = f"{couchdb_url.rstrip('/')}/{tv_uuid}"
    logging.info(f"Fetching document from: {doc_url}")
    session = get_requests_session()
    try:
        response = session.get(doc_url, timeout=10) # 10 seconds timeout
        response.raise_for_status() # Raises HTTPError for bad responses (4XX or 5XX)
        doc = response.json()
        logging.info(f"Successfully fetched document for TV UUID: {tv_uuid}")
        return doc
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error fetching document {doc_url}: {e.response.status_code} {e.response.reason}")
        if e.response.status_code == 404:
            logging.error(f"Document not found for TV UUID: {tv_uuid}")
        elif e.response.status_code == 401:
            logging.error(f"Unauthorized access to document for TV UUID: {tv_uuid}. Check credentials/permissions.")
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error fetching document {doc_url}: {e}", exc_info=True)
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout fetching document {doc_url}: {e}", exc_info=True)
    except requests.exceptions.RequestException as e: # Catch other requests-related errors
        logging.error(f"Generic error fetching document {doc_url}: {e}", exc_info=True)
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from document {doc_url}: {e}", exc_info=True)
    except Exception as e: # Catch any other unexpected errors
        logging.critical(f"Unexpected error in fetch_document for {doc_url}: {e}", exc_info=True)
    return None

def upload_attachment_to_couchdb(db_url, doc_id, doc_rev, attachment_name, attachment_data_bytes, content_type):
    """
    Uploads a data attachment to a CouchDB document.

    Args:
        db_url (str): The base URL of the CouchDB database (e.g., http://host/slideshows).
        doc_id (str): The ID of the document.
        doc_rev (str): The current revision of the document.
        attachment_name (str): The name for the new attachment.
        attachment_data_bytes (bytes): The binary data of the attachment.
        content_type (str): The MIME type of the attachment (e.g., "image/png").

    Returns:
        str: The new document revision string if successful, None otherwise.
    """
    if not all([db_url, doc_id, doc_rev, attachment_name, attachment_data_bytes, content_type]):
        logging.error("upload_attachment_to_couchdb: Missing one or more required arguments.")
        return None

    attachment_url = f"{db_url.rstrip('/')}/{doc_id}/{attachment_name}?rev={doc_rev}"
    headers = {
        "Content-Type": content_type
    }

    session = get_requests_session() # Use existing session getter for retries etc.
    logging.info(f"Attempting to upload attachment '{attachment_name}' to {doc_id} at rev {doc_rev}")

    try:
        response = session.put(
            attachment_url,
            data=attachment_data_bytes,
            headers=headers,
            timeout=30 # Set a reasonable timeout for uploads
        )
        response.raise_for_status() # Raise HTTPError for bad responses (4XX or 5XX)

        response_json = response.json()
        if response_json.get("ok"):
            new_rev = response_json.get("rev")
            logging.info(f"Successfully uploaded attachment '{attachment_name}' to document '{doc_id}'. New revision: {new_rev}")
            return new_rev
        else:
            logging.error(f"Failed to upload attachment '{attachment_name}'. Response: {response.text}")
            return None

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error uploading attachment {attachment_name}: {e.response.status_code} {e.response.reason} - {e.response.text}")
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error uploading attachment {attachment_name}: {e}")
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout uploading attachment {attachment_name}: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Generic error uploading attachment {attachment_name}: {e}")
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON response after uploading {attachment_name}: {e}")
    except Exception as e:
        logging.critical(f"Unexpected error in upload_attachment_to_couchdb for {attachment_name}: {e}", exc_info=True)

    return None

def update_tv_status_document(db_url, status_doc_id, tv_uuid, current_slide_name):
    """
    Creates or updates the TV status document in CouchDB.

    Args:
        db_url (str): The base URL of the CouchDB database (e.g., http://host/slideshows).
        status_doc_id (str): The ID for the status document (e.g., "status_YOUR_TV_UUID").
        tv_uuid (str): The UUID of the TV.
        current_slide_name (str): The name/ID of the currently displayed slide.
    """
    logging.info(f"Attempting to update TV status for doc '{status_doc_id}' with slide '{current_slide_name}'.")

    # Prepare the main payload
    payload = {
        "type": "tv_status",
        "tv_uuid": tv_uuid,
        "current_slide_id": current_slide_name,
        "current_slide_filename": current_slide_name, # Assuming name is the filename
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

    # Attempt to fetch the existing status document to get its _rev
    existing_doc = fetch_document(db_url, status_doc_id) # fetch_document handles its own logging

    if existing_doc and existing_doc.get('_rev'):
        payload['_rev'] = existing_doc['_rev']
        logging.debug(f"Updating existing status doc '{status_doc_id}' at revision {payload['_rev']}.")
    elif existing_doc: # Document exists but no _rev? Should not happen if fetch_document is robust.
        logging.warning(f"Status doc '{status_doc_id}' fetched but has no _rev. Attempting to update without it.")
    else: # Document likely doesn't exist (fetch_document returned None or a non-doc)
        logging.info(f"Status document '{status_doc_id}' not found or failed to fetch. Will attempt to create new.")
        # No _rev in payload, so it will be a new document creation if status_doc_id doesn't exist,
        # or fail if it does exist but we couldn't get _rev (CouchDB prevents overwrite without _rev).

    session = get_requests_session()
    target_url = f"{db_url.rstrip('/')}/{status_doc_id}"

    try:
        response = session.put(target_url, json=payload, timeout=10) # Standard timeout
        response.raise_for_status() # Raise HTTPError for bad responses (4XX or 5XX)

        response_json = response.json()
        if response_json.get("ok"):
            new_rev = response_json.get("rev")
            logging.info(f"Successfully updated/created TV status doc '{status_doc_id}'. New revision: {new_rev}.")
            return True # Indicate success
        else:
            logging.error(f"Failed to update/create TV status doc '{status_doc_id}'. Response: {response.text}")
            return False

    except requests.exceptions.HTTPError as e:
        # Log specific CouchDB errors if possible (e.g., conflict)
        if e.response.status_code == 409: # Conflict
            logging.warning(f"Conflict (409) updating TV status doc '{status_doc_id}'. Outdated revision? {e.response.text}")
        else:
            logging.error(f"HTTP error {e.response.status_code} updating TV status doc '{status_doc_id}': {e.response.reason} - {e.response.text}")
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error updating TV status doc '{status_doc_id}': {e}")
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout updating TV status doc '{status_doc_id}': {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Generic error updating TV status doc '{status_doc_id}': {e}")
    except json.JSONDecodeError as e: # Should be caught by session.put if response is not JSON
        logging.error(f"Error decoding JSON response after updating TV status doc '{status_doc_id}': {e}")
    except Exception as e:
        logging.critical(f"Unexpected error in update_tv_status_document for '{status_doc_id}': {e}", exc_info=True)

    return False # Indicate failure

# def watch_changes(couchdb_url, tv_uuid, need_refetch_event):
#     """Watches the CouchDB _changes feed for the specific document."""
#     changes_url = f"{couchdb_url.rstrip('/')}/_changes"
#     params = {
#         "feed": "continuous",
#         "heartbeat": 30000, # 30 seconds in milliseconds
#         "doc_ids": json.dumps([tv_uuid]),
#         "since": "now" # Start from the current state
#     }
#     logging.info(f"Starting to watch changes feed for doc ID {tv_uuid} at {changes_url}")
#
#     while True:
#         session = get_requests_session()
#         try:
#             with session.get(changes_url, params=params, stream=True, timeout=45) as response: # Slightly longer timeout for heartbeat
#                 response.raise_for_status()
#                 logging.info(f"Successfully connected to changes feed for {tv_uuid}")
#                 for line in response.iter_lines():
#                     if line:
#                         try:
#                             decoded_line = line.decode('utf-8').strip()
#                             if not decoded_line: # Skip empty lines (heartbeats)
#                                 logging.debug("Received heartbeat or empty line from changes feed.")
#                                 continue
#                             if decoded_line.startswith('{'): # Ensure it's a JSON object
#                                 change = json.loads(decoded_line)
#                                 logging.info(f"Received change: {json.dumps(change)}")
#                                 if change.get("id") == tv_uuid:
#                                     logging.info(f"Change detected for document {tv_uuid}. Triggering refetch.")
#                                     need_refetch_event.set()
#                             else:
#                                 logging.debug(f"Received non-JSON line from changes feed: {decoded_line}")
#                         except json.JSONDecodeError as e:
#                             logging.warning(f"Error decoding JSON from changes feed line: '{line.decode('utf-8', errors='ignore')}': {e}", exc_info=True)
#                         except Exception as e:
#                             logging.error(f"Unexpected error processing change line: {e}", exc_info=True)
#         except requests.exceptions.HTTPError as e: # Specific HTTP error
#             logging.error(f"HTTP error {e.response.status_code} watching changes feed ({changes_url}): {e.response.reason}. Retrying in 30s.", exc_info=True)
#         except requests.exceptions.ConnectionError as e: # Connection error
#             logging.error(f"Connection error watching changes feed ({changes_url}): {e}. Retrying in 30s.", exc_info=True)
#         except requests.exceptions.Timeout as e: # This can include read timeouts on the stream
#             logging.warning(f"Timeout watching changes feed ({changes_url}): {e}. Will attempt to reconnect.", exc_info=True)
#         except requests.exceptions.RequestException as e: # Other requests errors
#             logging.error(f"RequestException error watching changes feed ({changes_url}): {e}. Retrying in 30s.", exc_info=True)
#         except Exception as e: # Catch any other unexpected errors
#             logging.critical(f"Unexpected critical error in watch_changes loop ({changes_url}): {e}. Retrying in 30s.", exc_info=True)
#
#         logging.info(f"Attempting to reconnect to changes feed ({changes_url}) after 30 seconds...")
#         time.sleep(30)

def fetch_and_process_image_slide(slide_doc, couchdb_url, tv_uuid, screen_width, screen_height):
    """
    Fetches image attachment, scales/centers it. 
    Then, processes and applies defined text overlays (both static and prepares for scrolling).
    
    Args:
        slide_doc (dict): The slide definition dictionary.
        couchdb_url (str): Base URL for CouchDB.
        tv_uuid (str): The UUID of the TV document (used as part of the attachment URL).
        screen_width (int): Width of the target display screen.
        screen_height (int): Height of the target display screen.

    Returns:
        dict: The updated slide_doc with 'processed_image' (Pillow Image with static text) 
              and 'scrolling_texts' (list of surfaces for scrolling text), or None if processing fails.
    """
    content_name = slide_doc.get('name')
    slide_name = slide_doc.get('name', 'Unnamed Image Slide') # Retain slide_name for logging, even if content_name is now derived from 'name'

    if not content_name: # This now checks if 'name' (used as content_name) is missing
        logging.warning(f"Image slide '{slide_name}' (which should be content_name) is missing 'name' attribute. Skipping.")
        return None

    attachment_url = f"{couchdb_url.rstrip('/')}/{tv_uuid}/{content_name}"
    logging.info(f"Fetching image attachment for slide '{slide_name}' (using attachment key '{content_name}') from: {attachment_url}")
    
    session = get_requests_session()
    try:
        response = session.get(attachment_url, timeout=15) # Increased timeout for image download
        response.raise_for_status()

        image = Image.open(BytesIO(response.content))
        img_width, img_height = image.size

        if img_width == 0 or img_height == 0:
            logging.warning(f"Image '{content_name}' (attachment key) for slide '{slide_name}' has zero dimension. Skipping.")
            return None
        
        # Convert to RGB if it's not (e.g. RGBA, P, L) to ensure compatibility with background
        if image.mode not in ('RGB', 'L'): # Allow L mode (grayscale) as it can be pasted on RGB
             if image.mode == 'RGBA':
                 logging.debug(f"Image '{content_name}' (attachment key) is RGBA, creating RGB canvas for it before pasting on main canvas.")
                 # Create an RGB canvas for the RGBA image to handle transparency
                 rgb_image = Image.new("RGB", image.size, (0,0,0)) # Black background for this intermediate step
                 rgb_image.paste(image, (0,0), mask=image.split()[3]) # Paste using alpha channel as mask
                 image = rgb_image
             elif image.mode == 'P': # Palette mode
                 logging.debug(f"Image '{content_name}' (attachment key) is P (Palette) mode, converting to RGB.")
                 image = image.convert('RGB')
             else: # Other modes like LA (Luminance Alpha)
                 logging.debug(f"Image '{content_name}' (attachment key) is in mode {image.mode}, converting to RGB.")
                 image = image.convert('RGB')


        # Calculate scaling factor
        img_aspect_ratio = img_width / img_height
        screen_aspect_ratio = screen_width / screen_height

        if img_aspect_ratio > screen_aspect_ratio: # Image is wider than screen
            scale_factor = screen_width / img_width
        else: # Image is taller or same aspect ratio
            scale_factor = screen_height / img_height
        
        new_width = int(img_width * scale_factor)
        new_height = int(img_height * scale_factor)

        if new_width <= 0 or new_height <= 0:
            logging.warning(f"Calculated new dimensions for '{content_name}' (attachment key) are invalid ({new_width}x{new_height}). Skipping.")
            return None

        logging.info(f"Scaling image '{content_name}' (attachment key) from {img_width}x{img_height} to {new_width}x{new_height} for screen {screen_width}x{screen_height}")
        scaled_image = image.resize((new_width, new_height), Image.LANCZOS)

        # Create canvas and paste centered image
        base_canvas = Image.new('RGB', (screen_width, screen_height), (0, 0, 0)) # Black background
        x_img = (screen_width - new_width) // 2
        y_img = (screen_height - new_height) // 2
        base_canvas.paste(scaled_image, (x_img, y_img))

        slide_doc['processed_image'] = base_canvas # This is the image with static text overlays applied
        slide_doc['scrolling_texts'] = [] # Initialize for potential scrolling text

        # Process text overlays
        text_overlays = slide_doc.get('text_overlays', [])
        if not isinstance(text_overlays, list): text_overlays = []

        for text_params in text_overlays:
            text_content = text_params.get('text')
            if not text_content:
                logging.warning(f"Slide '{slide_name}' has text_overlay without 'text'. Skipping.")
                continue

            # Determine the width of the area available for this text (e.g., full screen or a column)
            # For now, assume text can use full screen_width if scrolling, or is placed relative to it.
            text_render_surface, text_original_width = get_cached_text_surface(
                text_params, 
                screen_width_for_text_area=screen_width 
            )

            if not text_render_surface:
                logging.warning(f"Could not render text: '{text_content[:30]}...' for slide '{slide_name}'.")
                continue
            
            if text_params.get('scroll', False):
                slide_doc['scrolling_texts'].append({
                    'surface': text_render_surface,
                    'original_width': text_original_width,
                    'params': text_params # Store original params for positioning, speed etc.
                })
                logging.info(f"Stored scrolling text surface '{text_content[:30]}...' for slide '{slide_name}'.")
            else:
                # Non-scrolling: Composite directly onto the 'processed_image'
                # Determine position for non-scrolling text
                pos_x, pos_y = 0, 0 # Default to top-left
                text_align = text_params.get('align', 'bottom_center') # e.g., top_left, center, bottom_right
                margin = text_params.get('margin', 10)

                surf_width, surf_height = text_render_surface.size

                if text_align == 'top_left':
                    pos_x, pos_y = margin, margin
                elif text_align == 'top_center':
                    pos_x, pos_y = (screen_width - surf_width) // 2, margin
                elif text_align == 'top_right':
                    pos_x, pos_y = screen_width - surf_width - margin, margin
                elif text_align == 'center_left':
                    pos_x, pos_y = margin, (screen_height - surf_height) // 2
                elif text_align == 'center':
                    pos_x, pos_y = (screen_width - surf_width) // 2, (screen_height - surf_height) // 2
                elif text_align == 'center_right':
                    pos_x, pos_y = screen_width - surf_width - margin, (screen_height - surf_height) // 2
                elif text_align == 'bottom_left':
                    pos_x, pos_y = margin, screen_height - surf_height - margin
                elif text_align == 'bottom_center':
                    pos_x, pos_y = (screen_width - surf_width) // 2, screen_height - surf_height - margin
                elif text_align == 'bottom_right':
                    pos_x, pos_y = screen_width - surf_width - margin, screen_height - surf_height - margin
                
                logging.info(f"Compositing non-scrolling text '{text_content[:30]}...' at ({pos_x},{pos_y}) on slide '{slide_name}'.")
                # Ensure processed_image is RGBA if it's not already for alpha_composite
                if slide_doc['processed_image'].mode != 'RGBA':
                     slide_doc['processed_image'] = slide_doc['processed_image'].convert('RGBA')
                
                # Create a temporary canvas to composite onto, if text_render_surface is smaller
                # This ensures correct alpha blending if text_render_surface has its own background
                temp_composite_layer = Image.new('RGBA', slide_doc['processed_image'].size, (0,0,0,0))
                temp_composite_layer.paste(text_render_surface, (pos_x, pos_y))
                slide_doc['processed_image'] = Image.alpha_composite(slide_doc['processed_image'], temp_composite_layer)
                # Convert back to RGB if framebuffer doesn't handle alpha well (common)
                slide_doc['processed_image'] = slide_doc['processed_image'].convert('RGB')


        logging.info(f"Successfully processed image and text for slide '{slide_name}' with image attachment key '{content_name}'.")
        return slide_doc

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error {e.response.status_code} fetching image {attachment_url} (attachment key '{content_name}') for slide '{slide_name}': {e.response.reason}", exc_info=True)
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error fetching image {attachment_url} (attachment key '{content_name}') for slide '{slide_name}': {e}", exc_info=True)
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout fetching image {attachment_url} (attachment key '{content_name}') for slide '{slide_name}': {e}", exc_info=True)
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error fetching image {attachment_url} (attachment key '{content_name}') for slide '{slide_name}': {e}", exc_info=True)
    except (IOError, UnidentifiedImageError) as e: 
        logging.error(f"Pillow error processing image with attachment key '{content_name}' for slide '{slide_name}': {e}", exc_info=True)
    except Exception as e:
        logging.critical(f"Unexpected error processing image slide '{slide_name}' (attachment key '{content_name}'): {e}", exc_info=True)
    
    return None

def perform_fade_transition(fb_path, screen_width, screen_height, bpp, img_mode, outgoing_image_canvas, incoming_image_canvas, duration_ms):
    """
    Performs a fade transition between two Pillow Image canvases by blending them
    incrementally and writing each step to the framebuffer.

    Args:
        fb_path (str): Path to the framebuffer device (used by write_to_framebuffer placeholder).
        screen_width (int): Width of the screen.
        screen_height (int): Height of the screen.
        bpp (int): Bits per pixel of the screen (used by write_to_framebuffer placeholder).
        img_mode (str): Pillow image mode for framebuffer (e.g. 'RGB', 'RGBA').
        outgoing_image_canvas (Image.Image, optional): The image currently displayed, fading out.
        incoming_image_canvas (Image.Image): The new image to fade in.
        duration_ms (int): Total duration of the fade transition in milliseconds.
    """
    # Note: fb_obj from fb_info is not used here as write_to_framebuffer is a placeholder.
    # A real implementation might pass fb_info['fb_obj'].
    logging.info(f"Performing fade transition: duration {duration_ms}ms, from {'image' if outgoing_image_canvas else 'None'} to {'image' if incoming_image_canvas else 'None'}")

    if incoming_image_canvas is None:
        logging.warning("Fade transition requested but incoming_image_canvas is None. Nothing to display.")
        return

    if duration_ms <= 0:
        logging.info("Transition duration is 0 or less, displaying incoming image directly.")
        write_to_framebuffer(None, incoming_image_canvas, screen_width, screen_height, bpp, img_mode) # Pass None for fb_obj for placeholder
        return

    black_canvas = Image.new('RGB', (screen_width, screen_height), (0, 0, 0))
    # Ensure canvases are in 'RGB' mode for Image.blend, if not already.
    # (This should be guaranteed by how they are created/processed before this function)
    if outgoing_image_canvas and outgoing_image_canvas.mode != 'RGB':
        outgoing_image_canvas = outgoing_image_canvas.convert('RGB')
    if incoming_image_canvas.mode != 'RGB':
        incoming_image_canvas = incoming_image_canvas.convert('RGB')


    delay_per_step = (duration_ms / FADE_STEPS) / 1000.0 

    # Fade Out Logic
    if outgoing_image_canvas:
        logging.debug("Fade Out phase...")
        for i in range(FADE_STEPS + 1):
            alpha = i / FADE_STEPS 
            try:
                blended_image = Image.blend(outgoing_image_canvas, black_canvas, alpha)
                write_to_framebuffer(None, blended_image, screen_width, screen_height, bpp, img_mode)
            except ValueError as e: 
                logging.error(f"Error blending images during fade out (slide: {slide_name if 'slide_name' in locals() else 'N/A'}): {e}. Using black_canvas.", exc_info=True)
                write_to_framebuffer(None, black_canvas, screen_width, screen_height, bpp, img_mode)
                break # Skip to fade in if blending fails
            if i < FADE_STEPS: time.sleep(delay_per_step) # No sleep on the very last step of fade out
    else:
        logging.debug("No outgoing image, ensuring screen is black before fade in.")
        write_to_framebuffer(None, black_canvas, screen_width, screen_height, bpp, img_mode)
        # A small, fixed pause could be added here if desired, but the fade-in loop will provide gradual change.

    # Fade In Logic
    logging.debug("Fade In phase...")
    for i in range(FADE_STEPS + 1):
        alpha = i / FADE_STEPS
        try:
            blended_image = Image.blend(black_canvas, incoming_image_canvas, alpha)
            write_to_framebuffer(None, blended_image, screen_width, screen_height, bpp, img_mode)
        except ValueError as e:
            logging.error(f"Error blending images during fade in (slide: {slide_name if 'slide_name' in locals() else 'N/A'}): {e}. Using incoming_image_canvas directly.", exc_info=True)
            write_to_framebuffer(None, incoming_image_canvas, screen_width, screen_height, bpp, img_mode) 
            break 
        if i < FADE_STEPS: time.sleep(delay_per_step) # No sleep on the very last step of fade in

    # Final display of the incoming_image_canvas is implicitly handled by the loop's last step.
    logging.info("Fade transition complete.")


def cv2_to_pillow(cv2_frame):
    """Converts an OpenCV frame (BGR NumPy array) to a Pillow Image (RGB)."""
    if cv2_frame is None:
        return None
    try:
        # Check if frame is empty
        if cv2_frame.size == 0:
            logging.warning("Attempted to convert an empty OpenCV frame.")
            return None
        rgb_frame = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb_frame)
    except cv2.error as e:
        logging.error(f"OpenCV error during color conversion: {e}")
        return None
    except Exception as e:
        logging.error(f"Error converting OpenCV frame to Pillow image: {e}")
        return None

def fetch_and_prepare_video_slide(slide_doc, couchdb_url, tv_uuid, config_unused):
    """
    Fetches video attachment, stores it in a temp file, extracts video properties,
    and prepares text overlay surfaces for the video.
    """
    slide_name = slide_doc.get('name', 'Unnamed Video Slide')
    content_name = slide_doc.get('content_name')

    if not content_name:
        logging.error(f"Video slide '{slide_name}' is missing 'content_name'. Skipping.")
        return None

    attachment_url = f"{couchdb_url.rstrip('/')}/{tv_uuid}/{content_name}"
    logging.info(f"Fetching video attachment for slide '{slide_name}' from: {attachment_url}")
    
    session = get_requests_session()
    temp_file_path_actual = None # To store the actual path for cleanup if NamedTemporaryFile object is lost
    
    try:
        response = session.get(attachment_url, timeout=120, stream=True) # Increased timeout for potentially large videos
        response.raise_for_status()

        file_suffix = os.path.splitext(content_name)[1] if os.path.splitext(content_name)[1] else '.mp4'

        with tempfile.NamedTemporaryFile(delete=False, suffix=file_suffix) as temp_file:
            for chunk in response.iter_content(chunk_size=8192*4): # Larger chunk size for videos
                temp_file.write(chunk)
            temp_file_path_actual = temp_file.name 
        
        logging.info(f"Video '{content_name}' saved to temporary file: {temp_file_path_actual}")
        slide_doc['video_temp_path'] = temp_file_path_actual

        video_cap = cv2.VideoCapture(temp_file_path_actual)
        if not video_cap.isOpened():
            logging.error(f"OpenCV could not open video file: {temp_file_path_actual} for slide '{slide_name}'.")
            if os.path.exists(temp_file_path_actual):
                os.unlink(temp_file_path_actual)
            return None

        slide_doc['video_fps'] = video_cap.get(cv2.CAP_PROP_FPS)
        slide_doc['video_width'] = int(video_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        slide_doc['video_height'] = int(video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        slide_doc['frame_count'] = int(video_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_cap.release()

        if slide_doc['video_fps'] is None or slide_doc['video_fps'] == 0 or \
           slide_doc['video_width'] == 0 or slide_doc['video_height'] == 0:
             logging.warning(f"Video '{content_name}' has invalid metadata (fps/width/height is 0 or None). May not play correctly.")
        
        slide_doc['content_type'] = 'video'
        
        # Prepare text overlays for video (static and scrolling)
        slide_doc['static_text_surfaces'] = [] # To store {surface, params} for static text
        slide_doc['scrolling_texts'] = []      # To store {surface, original_width, params} for scrolling

        text_overlays_def = slide_doc.get('text_overlays', [])
        if not isinstance(text_overlays_def, list): text_overlays_def = []

        # Screen width is needed for get_cached_text_surface if text is not scrolling
        # This is a bit problematic as fetch_and_prepare_video_slide doesn't know screen_width.
        # For now, we'll assume text for video will mostly be scrolling or use its own width.
        # A better solution would be to pass screen_width here or make text rendering more flexible.
        # Let's assume for video, non-scrolling text surfaces are rendered to their own width.
        # This means text_align_surface_width will be None for get_cached_text_surface.
        
        for text_params in text_overlays_def:
            text_content = text_params.get('text')
            if not text_content:
                logging.warning(f"Video slide '{slide_name}' has text_overlay without 'text'. Skipping this overlay.")
                continue
            
            # For video, pass None for screen_width_for_text_area to render text to its own width initially.
            # Alignment will be handled during per-frame composition.
            text_render_surface, text_original_width = get_cached_text_surface(text_params, None)

            if not text_render_surface:
                logging.warning(f"Could not render text: '{text_content[:30]}...' for video slide '{slide_name}'.")
                continue

            if text_params.get('scroll', False):
                slide_doc['scrolling_texts'].append({
                    'surface': text_render_surface,
                    'original_width': text_original_width,
                    'params': text_params
                })
            else: # Static text for video
                slide_doc['static_text_surfaces'].append({
                    'surface': text_render_surface,
                    'params': text_params 
                })
        
        logging.info(f"Successfully prepared video slide '{slide_name}' from '{content_name}' with text overlays.")
        
        slide_doc['cleanup_func'] = lambda path=temp_file_path_actual: (
            logging.info(f"Cleaning up temporary video file: {path}"),
            os.unlink(path) if os.path.exists(path) else None
        )
        return slide_doc

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error {e.response.status_code} fetching video {attachment_url} for slide '{slide_name}': {e.response.reason}", exc_info=True)
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error fetching video {attachment_url} for slide '{slide_name}': {e}", exc_info=True)
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout fetching video {attachment_url} for slide '{slide_name}': {e}", exc_info=True)
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error fetching video {attachment_url} for slide '{slide_name}': {e}", exc_info=True)
    except IOError as e: 
        logging.error(f"IOError saving video {content_name} for slide '{slide_name}': {e}", exc_info=True)
    except cv2.error as e:
        logging.error(f"OpenCV error processing video {content_name} for slide '{slide_name}': {e}", exc_info=True)
    except Exception as e:
        logging.critical(f"Unexpected error preparing video slide '{slide_name}' (video: {content_name}): {e}", exc_info=True)
    
    if temp_file_path_actual and os.path.exists(temp_file_path_actual):
        try:
            logging.debug(f"Cleaning up orphaned temp video file due to error: {temp_file_path_actual}")
            os.unlink(temp_file_path_actual)
        except OSError as unlink_e:
            logging.error(f"Error unlinking orphaned temp video file {temp_file_path_actual} during error handling: {unlink_e}", exc_info=True)
    return None

def capture_website(url, target_screen_width, target_screen_height, timeout=30):
    """
    Captures a website screenshot using Selenium, processes it to an intermediate size (1920x1080),
    then scales and centers it onto a canvas matching target_screen_width/height.
    Returns a Pillow Image object or None.
    """
    logging.info(f"Attempting to capture website: {url}")
    options = ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox") # Common for running in containers/CI
    options.add_argument("--disable-dev-shm-usage") # Common for running in containers/CI
    options.add_argument("--disable-gpu") # Often needed for headless
    options.add_argument("--window-size=1920,1080") # Initial window size for capture
    # Hide scrollbars to prevent them from being part of the screenshot
    options.add_argument("--hide-scrollbars")


    # Path to ChromeDriver, ensure it's executable and compatible with installed Chrome
    # This might need to be configured externally if not in a standard path.
    service = ChromeService(executable_path=CHROME_DRIVER_PATH, log_path=WEBDRIVER_LOG_PATH)
    driver = None

    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(timeout)
        
        logging.info(f"Navigating to {url}")
        driver.get(url)

        # Wait for page to be somewhat loaded.
        # A simple time.sleep or waiting for document.readyState can be used.
        # More complex waits (e.g. for specific elements) could be added if needed.
        time.sleep(5) # Give some time for JS rendering, adjust as needed
        
        # Check document.readyState
        for _ in range(timeout // 2):
            if driver.execute_script("return document.readyState") == "complete":
                break
            time.sleep(2)
        else:
            logging.warning(f"Page {url} might not have fully loaded (readyState != complete) after {timeout}s.")

        # It's good practice to ensure the body has some height, otherwise screenshot might be tiny
        body_height = driver.execute_script("return document.body.scrollHeight")
        if body_height < 100: # Arbitrary small number
            logging.warning(f"Page {url} has very small body height ({body_height}px). Screenshot might be small.")
        # driver.set_window_size(1920, body_height if body_height > 1080 else 1080) # Adjust height if needed


        logging.info(f"Capturing screenshot for {url}")
        screenshot_data = driver.get_screenshot_as_png()
        
        raw_image = Image.open(BytesIO(screenshot_data)).convert('RGB')
        
        # Intermediate processing: Crop to 1920x1080 if capture was set to that window size
        # This ensures a consistent aspect ratio for the "raw" capture before final scaling.
        # If the content is shorter than 1080px, it will be pasted onto a white background.
        intermediate_width, intermediate_height = 1920, 1080
        intermediate_image = Image.new('RGB', (intermediate_width, intermediate_height), (255, 255, 255))
        
        # Crop the raw image to fit into intermediate_width x intermediate_height
        # This assumes raw_image is at least intermediate_width wide due to window size setting.
        # If raw_image is shorter than intermediate_height, it pastes what's available.
        crop_h = min(raw_image.height, intermediate_height)
        intermediate_image.paste(raw_image.crop((0, 0, intermediate_width, crop_h)), (0,0))
        logging.info(f"Processed raw screenshot of {raw_image.width}x{raw_image.height} to {intermediate_width}x{intermediate_height} intermediate.")

        # Final Scaling and Centering (similar to image slides)
        img_width, img_height = intermediate_image.size
        img_aspect_ratio = img_width / img_height
        screen_aspect_ratio = target_screen_width / target_screen_height

        if img_aspect_ratio > screen_aspect_ratio:
            scale_factor = target_screen_width / img_width
        else:
            scale_factor = target_screen_height / img_height
        
        new_width = int(img_width * scale_factor)
        new_height = int(img_height * scale_factor)

        if new_width <= 0 or new_height <= 0:
            logging.error(f"Invalid new dimensions ({new_width}x{new_height}) for website image {url}. Skipping.")
            return None

        scaled_image = intermediate_image.resize((new_width, new_height), Image.LANCZOS)
        
        final_canvas = Image.new('RGB', (target_screen_width, target_screen_height), (0, 0, 0)) # Black background
        x = (target_screen_width - new_width) // 2
        y = (target_screen_height - new_height) // 2
        final_canvas.paste(scaled_image, (x, y))
        
        logging.info(f"Successfully captured and processed website {url} to {target_screen_width}x{target_screen_height} canvas.")
        return final_canvas
    
    except SeleniumTimeoutException as e:
        logging.error(f"Selenium timeout capturing website {url}: {e}", exc_info=True)
    except WebDriverException as e: # More general Selenium exception
        logging.error(f"Selenium WebDriverException capturing website {url}: {e}", exc_info=True)
    except IOError as e: # Pillow related errors
        logging.error(f"Pillow error processing website screenshot for {url}: {e}", exc_info=True)
    except Exception as e: # Catch any other unexpected errors
        logging.critical(f"Unexpected error in capture_website for {url}: {e}", exc_info=True)
        return None # Moved directly after the logging statement within this except block
    finally:
        if driver:
            try:
                driver.quit()
                logging.info(f"WebDriver quit for {url}")
            except Exception as e:
                logging.error(f"Error quitting WebDriver for {url}: {e}", exc_info=True)


def fetch_and_process_website_slide(slide_doc, screen_width, screen_height, couchdb_slideshows_db_url, tv_uuid):
    """
    Fetches/captures a website screenshot, processes it, applies text overlays, and uploads to CouchDB.
    Uses a cache for website screenshots.
    """
    slide_name = slide_doc.get('name', 'Unnamed Website Slide')
    url = slide_doc.get('url')

    if not url:
        logging.error(f"Website slide '{slide_name}' is missing 'url'. Skipping.")
        return None

    # Cache Check
    cache_key = hashlib.md5(url.encode('utf-8')).hexdigest() # Use hash of URL for cleaner key
    if cache_key in website_screenshot_cache:
        cached_item = website_screenshot_cache[cache_key]
        if time.time() - cached_item['timestamp'] < WEBSITE_CACHE_EXPIRY_SECONDS:
            logging.info(f"Using cached screenshot for {url} (Slide: '{slide_name}')")
            # Ensure the cached image is copied and has the correct screen dimensions if they changed
            # For simplicity, we assume screen dimensions are constant for cached images,
            # or that they are re-scaled if needed (though current cache stores final canvas)
            # If screen dimensions can change dynamically, the cache might need to store intermediate images
            # or re-process. For now, let's assume the cached image is display-ready.
            slide_doc['processed_image'] = cached_item['image'].copy() 
            # Apply text overlays (as they are not part of the website screenshot cache)
            # This reuses the text overlay logic from fetch_and_process_image_slide structure
            slide_doc['scrolling_texts'] = []
            text_overlays = slide_doc.get('text_overlays', [])
            if not isinstance(text_overlays, list): text_overlays = []
            for text_params in text_overlays:
                # (Text processing logic copied and adapted from fetch_and_process_image_slide)
                text_content = text_params.get('text')
                if not text_content: continue
                text_render_surface, text_original_width = get_cached_text_surface(text_params, screen_width)
                if not text_render_surface: continue
                if text_params.get('scroll', False):
                    slide_doc['scrolling_texts'].append({'surface': text_render_surface, 'original_width': text_original_width, 'params': text_params})
                else:
                    pos_x, pos_y = 0,0; margin = text_params.get('margin',10); text_align = text_params.get('align','bottom_center'); surf_w, surf_h = text_render_surface.size
                    if text_align == 'top_left': pos_x,pos_y=margin,margin
                    elif text_align == 'top_center': pos_x,pos_y=(screen_width-surf_w)//2,margin
                    # ... (include all other alignment options as in fetch_and_process_image_slide)
                    elif text_align == 'bottom_center': pos_x,pos_y=(screen_width-surf_w)//2, screen_height-surf_h-margin
                    else: pos_x,pos_y=(screen_width-surf_w)//2, screen_height-surf_h-margin # Default to bottom_center
                    
                    if slide_doc['processed_image'].mode != 'RGBA': slide_doc['processed_image'] = slide_doc['processed_image'].convert('RGBA')
                    temp_layer = Image.new('RGBA', slide_doc['processed_image'].size, (0,0,0,0))
                    temp_layer.paste(text_render_surface, (pos_x, pos_y))
                    slide_doc['processed_image'] = Image.alpha_composite(slide_doc['processed_image'], temp_layer).convert('RGB')
            return slide_doc
        else:
            logging.info(f"Cached screenshot for {url} expired. Re-capturing.")

    # Capture Website
    captured_image_canvas = capture_website(url, screen_width, screen_height)

    if captured_image_canvas:
        slide_doc['processed_image'] = captured_image_canvas
        
        # Cache Update
        if len(website_screenshot_cache) >= MAX_WEBSITE_CACHE_ENTRIES:
            # Simple FIFO eviction by finding the oldest entry
            oldest_key = min(website_screenshot_cache, key=lambda k: website_screenshot_cache[k]['timestamp'])
            logging.info(f"Website cache full. Removing oldest entry for URL hash: {oldest_key}")
            del website_screenshot_cache[oldest_key]
        
        website_screenshot_cache[cache_key] = {'image': captured_image_canvas.copy(), 'timestamp': time.time()}
        logging.info(f"Cached new screenshot for {url}")

        # Apply text overlays (same logic as above for cached version)
        slide_doc['scrolling_texts'] = []
        text_overlays = slide_doc.get('text_overlays', [])
        if not isinstance(text_overlays, list): text_overlays = []
        for text_params in text_overlays:
            text_content = text_params.get('text')
            if not text_content: continue
            text_render_surface, text_original_width = get_cached_text_surface(text_params, screen_width)
            if not text_render_surface: continue
            if text_params.get('scroll', False):
                 slide_doc['scrolling_texts'].append({'surface': text_render_surface, 'original_width': text_original_width, 'params': text_params})
            else:
                pos_x, pos_y = 0,0; margin = text_params.get('margin',10); text_align = text_params.get('align','bottom_center'); surf_w, surf_h = text_render_surface.size
                if text_align == 'top_left': pos_x,pos_y=margin,margin
                elif text_align == 'top_center': pos_x,pos_y=(screen_width-surf_w)//2,margin
                # ... (include all other alignment options as in fetch_and_process_image_slide)
                elif text_align == 'bottom_center': pos_x,pos_y=(screen_width-surf_w)//2, screen_height-surf_h-margin
                else: pos_x,pos_y=(screen_width-surf_w)//2, screen_height-surf_h-margin # Default to bottom_center

                if slide_doc['processed_image'].mode != 'RGBA': slide_doc['processed_image'] = slide_doc['processed_image'].convert('RGBA')
                temp_layer = Image.new('RGBA', slide_doc['processed_image'].size, (0,0,0,0))
                temp_layer.paste(text_render_surface, (pos_x, pos_y))
                slide_doc['processed_image'] = Image.alpha_composite(slide_doc['processed_image'], temp_layer).convert('RGB')

        # MOVED UPLOAD LOGIC AND DEBUG LOGS HERE
        logging.info(f"WSS_UPLOAD_DEBUG: About to check processed_image for upload. Type: {type(slide_doc.get('processed_image'))}, Is None: {slide_doc.get('processed_image') is None}, Image Mode (if not None): {slide_doc.get('processed_image').mode if slide_doc.get('processed_image') else 'N/A'}")
        if slide_doc.get('processed_image'):
            logging.info("WSS_UPLOAD_DEBUG: Condition `slide_doc.get('processed_image')` is true. Entering upload logic block.")
            try:
                image_to_upload = slide_doc['processed_image']

                # Convert Pillow Image to PNG bytes
                png_buffer = io.BytesIO()
                image_to_upload.save(png_buffer, format="PNG")
                png_bytes = png_buffer.getvalue()
                png_buffer.close()

                # Generate a unique attachment name
                base_name = slide_doc.get('name', 'website_capture')
                attachment_name = f"{base_name}_{int(time.time())}.png"

                logging.info(f"Preparing to upload captured website image as '{attachment_name}' for slide '{slide_doc.get('name', 'N/A')}'.")

                # Fetch current document revision to allow upload
                doc_for_rev = fetch_document(couchdb_slideshows_db_url, tv_uuid)

                if doc_for_rev and doc_for_rev.get('_rev'):
                    current_rev = doc_for_rev.get('_rev')
                    logging.info(f"Fetched current doc revision '{current_rev}' for '{tv_uuid}' before attachment upload.")

                    new_rev_after_upload = upload_attachment_to_couchdb(
                        couchdb_slideshows_db_url,
                        tv_uuid,
                        current_rev,
                        attachment_name,
                        png_bytes,
                        "image/png"
                    )

                    if new_rev_after_upload:
                        logging.info(f"Successfully uploaded website screenshot '{attachment_name}'. New doc rev: {new_rev_after_upload}.")
                    else:
                        logging.error(f"Failed to upload website screenshot '{attachment_name}' for slide '{slide_doc.get('name', 'N/A')}'.")
                else:
                    logging.error(f"Could not fetch document or revision for '{tv_uuid}' to upload attachment for slide '{slide_doc.get('name', 'N/A')}'.")

            except Exception as e:
                logging.error(f"Error during website screenshot to CouchDB attachment conversion or upload for slide '{slide_doc.get('name', 'N/A')}': {e}", exc_info=True)

        return slide_doc # End of successful capture path
    else: # Capture failed
        logging.error(f"Failed to capture and process website slide: {slide_name} ({url})")
        return None # End of failed capture path
    # The final 'return slide_doc' that was here is now removed as all paths should have explicit returns.


def process_slides_from_doc(doc, couchdb_url, tv_uuid, screen_width, screen_height, app_config):
    """
    Extracts and processes slide definitions from the document.
    """
    if not doc or not isinstance(doc, dict):
        logging.warning("process_slides_from_doc: Received invalid or empty document.")
        return []

    raw_slides = doc.get('slides', [])
    if not isinstance(raw_slides, list):
        logging.warning(f"process_slides_from_doc: 'slides' field is not a list. Found: {type(raw_slides)}")
        return []

    logging.info(f"Processing {len(raw_slides)} slides from the document.")
    processed_slides = []
    for i, slide_def_orig in enumerate(raw_slides):
        slide_def = slide_def_orig.copy() # Work with a copy
        slide_def['transition_time_ms'] = int(slide_def_orig.get('transition_time_ms', DEFAULT_TRANSITION_TIME_MS))
        
        slide_type = slide_def.get('type', 'image') 
        slide_name = slide_def.get('name', f'Unnamed Slide {i+1}')
        logging.info(f"Processing Slide {i+1}: Name='{slide_name}', Type='{slide_type}', Transition: {slide_def['transition_time_ms']}ms")

        processed_slide_content = None
        if slide_type == 'image':
            if screen_width and screen_height: 
                processed_slide_content = fetch_and_process_image_slide(
                    slide_def, couchdb_url, tv_uuid, screen_width, screen_height
                )
            else:
                logging.warning(f"Skipping image slide '{slide_name}' due to missing screen dimensions.")
        
        elif slide_type == 'website':
            if screen_width and screen_height:
                processed_slide_content = fetch_and_process_website_slide(
                    slide_def, screen_width, screen_height, couchdb_url, tv_uuid # Pass couchdb_url and tv_uuid directly
                )
            else:
                logging.warning(f"Skipping website slide '{slide_name}' due to missing screen dimensions.")
        
        elif slide_type == 'video':
            # Video slides don't strictly need screen_width/height for initial prep,
            # but they will for display. The main loop will handle frame scaling.
            processed_slide_content = fetch_and_prepare_video_slide(
                slide_def, couchdb_url, tv_uuid, app_config
            )
            # Video slides don't have a 'processed_image' at this stage, they have 'video_temp_path'
            if processed_slide_content and processed_slide_content.get('video_temp_path'):
                 processed_slides.append(processed_slide_content)
                 # Set content_type if not already set by fetch_and_prepare_video_slide
                 if 'content_type' not in processed_slide_content: # Should be set by the func
                      processed_slide_content['content_type'] = 'video'
                 continue # Skip the common check for 'processed_image' for video slides

        else:
            logging.info(f"Slide '{slide_name}' is type '{slide_type}'. Not yet supported for full processing.")

        if processed_slide_content and processed_slide_content.get('processed_image'): # For image/website
            processed_slides.append(processed_slide_content)
        elif not processed_slide_content and slide_type not in ['video']: # If it failed and wasn't video
             logging.warning(f"Failed to process slide: {slide_name} (Type: {slide_type}). It will not be displayed.")
        elif slide_type in ['video'] and not processed_slide_content: # Video failed processing
             logging.warning(f"Failed to process video slide: {slide_name}. It will not be displayed.")


    logging.info(f"Successfully prepared {len(processed_slides)} slides out of {len(raw_slides)} for potential display.")
    global processed_slides_global_for_cleanup
    processed_slides_global_for_cleanup = list(processed_slides) # Store a copy for atexit cleanup
    return processed_slides

def load_config(config_path):
    """
    Loads configuration from the specified path.
    """
    config = configparser.ConfigParser()
    if not os.path.exists(config_path):
        logging.error(f"Configuration file not found: {config_path}")
        # In a real application, you might exit or raise an exception
        # For this script, we'll log and return None or an empty dict
        # to allow the script to potentially continue in a limited mode or fail later.
        # However, the requirement is to exit.
        exit(1) # Exiting as per requirement

    try:
        config.read(config_path)
    except configparser.Error as e:
        logging.error(f"Error parsing configuration file {config_path}: {e}")
        exit(1) # Exiting as per requirement

    if 'settings' not in config:
        logging.error(f"Missing [settings] section in configuration file: {config_path}")
        exit(1) # Exiting as per requirement

    settings = config['settings']
    couchdb_url = settings.get('couchdb_url')
    tv_uuid = settings.get('tv_uuid')
    manager_url = settings.get('manager_url')

    if not all([couchdb_url, tv_uuid, manager_url]):
        missing_keys = []
        if not couchdb_url: missing_keys.append('couchdb_url')
        if not tv_uuid: missing_keys.append('tv_uuid')
        if not manager_url: missing_keys.append('manager_url')
        logging.error(f"Missing essential keys in [settings] section: {', '.join(missing_keys)} in {config_path}")
        exit(1) # Exiting as per requirement
    
    loaded_settings = {
        'couchdb_url': couchdb_url,
        'tv_uuid': tv_uuid,
        'manager_url': manager_url
    }
    logging.info(f"Successfully loaded configuration from {config_path}")
    return loaded_settings

def get_framebuffer_info(fb_path):
    """
    Placeholder function to get framebuffer information.
    Theoretically uses fcntl.ioctl to get screen fixed and variable screen info.
    Returns mock data for now.
    """
    logging.info(f"Attempting to get framebuffer info for {fb_path}")
    # Mock data: width, height, bits_per_pixel
    # These would typically be obtained using ioctl calls like FBIOGET_VSCREENINFO
    # and FBIOGET_FSCREENINFO
    # struct fb_var_screeninfo {
    #     __u32 xres;
    #     __u32 yres;
    #     __u32 xres_virtual;
    #     __u32 yres_virtual;
    #     __u32 xoffset;
    #     __u32 yoffset;
    #     __u32 bits_per_pixel;
    #     ...
    # };
    # struct fb_fix_screeninfo { // From <linux/fb.h>
    #    char id[16];            /* identification string eg "TT Builtin" */
    #    unsigned long smem_start;   /* Start of frame buffer mem */
    #    __u32 smem_len;         /* Length of frame buffer mem */
    #    __u32 type;             /* see FB_TYPE_*		*/
    #    __u32 type_aux;         /* Interleave for interleaved Planes */
    #    __u32 visual;           /* see FB_VISUAL_*		*/
    #    __u16 xpanstep;         /* zero if no hardware panning  */
    #    __u16 ypanstep;         /* zero if no hardware panning  */
    #    __u16 ywrapstep;        /* zero if no hardware ywrap    */
    #    __u32 line_length;      /* length of a line in bytes    */
    #    unsigned long mmio_start;   /* Start of Memory Mapped I/O   */
    #    __u32 mmio_len;         /* Length of Memory Mapped I/O  */
    #    __u32 accel;            /* Type of acceleration available */
    #    __u16 reserved[3];      /* Reserved for future use  */
    # };
    # In a real implementation, this function would use fcntl.ioctl with FBIOGET_VSCREENINFO
    # and FBIOGET_FSCREENINFO to get actual screen properties. It might also open the
    # framebuffer device (e.g., os.open(fb_path, os.O_RDWR)) and potentially mmap it.
    # The file object or mmap object would then be stored in fb_info['fb_obj'].
    # This fb_obj would be closed on application exit (e.g., using atexit).
    fb_info = {
        'width': 1920,
        'height': 1080,
        'bpp': 32,
        'fb_obj': None, 
        'img_mode': 'RGB' 
    }
    # Example pixel format determination based on bpp (highly simplified):
    if fb_info['bpp'] == 32:
        fb_info['img_mode'] = 'RGBA' # Or 'RGB' if alpha is not used/supported by fb
    elif fb_info['bpp'] == 24:
        fb_info['img_mode'] = 'RGB'
    elif fb_info['bpp'] == 16:
        fb_info['img_mode'] = 'RGB;16' # Example, might be specific (e.g. RGB565)
    
    logging.info(f"Mock framebuffer info: {fb_info['width']}x{fb_info['height']}, {fb_info['bpp']}bpp, Pillow mode: {fb_info['img_mode']}. FB object is currently: {fb_info['fb_obj']}.")
    return fb_info


def write_to_framebuffer(fb_obj_unused, image_data, screen_width, screen_height, bpp_unused, img_mode_unused): # fb_obj would be used if real
    """
    Placeholder function to write image data (Pillow Image) to the framebuffer.
    fb_obj would be the opened framebuffer file or mmap object.
    image_data is expected to be a Pillow Image object.
    """
    # In a real implementation:
    # 1. Convert image_data (Pillow Image) to raw bytes in the correct pixel format
    #    (e.g., RGBA, BGRA, RGB565) based on actual framebuffer properties (fb_var_screeninfo).
    #    The `img_mode_unused` would be replaced by `fb_info['actual_pixel_format_for_pillow']`.
    #    E.g., if fb needs BGRA:
    #      image_data_converted = image_data.convert('RGBA') # Ensure it has alpha if needed
    #      r, g, b, a = image_data_converted.split()
    #      final_image_data = Image.merge("RGBA", (b, g, r, a)) # Swap R and B
    #      raw_bytes = final_image_data.tobytes()
    #    Or for RGB565:
    #      raw_bytes = image_data.convert('RGB').tobytes('raw', 'RGB;16')
    # 2. Ensure `raw_bytes` matches the expected `line_length` (stride) from `fb_fix_screeninfo`.
    #    This might involve padding each line if Pillow's output stride differs.
    # 3. Write/blit `raw_bytes` to `fb_obj_real` (the opened framebuffer device or mmap object).
    #    - Using `os.write(fb_obj_real_fd, raw_bytes)` after `os.lseek(fb_obj_real_fd, 0, os.SEEK_SET)`.
    #    - Or `mmap_obj.seek(0); mmap_obj.write(raw_bytes)`.

    # Current placeholder logging:
    img_byte_size = 0
    if image_data:
        try:
            # Simulate getting bytes for logging purposes
            img_byte_size = len(image_data.tobytes())
        except Exception: # Can fail if mode is weird for tobytes
            pass

    logging.debug(f"Simulating write to framebuffer: {screen_width}x{screen_height}, {img_byte_size} bytes (from Pillow Image).")
    # time.sleep(0.001) # Simulate a small delay of writing to framebuffer


def apply_text_and_scroll(base_canvas, slide_text_overlays, scroll_positions, screen_width, screen_height, delta_time):
    """
    Applies static and scrolling text overlays to a copy of the base_canvas.
    For video, `base_canvas` is a single frame.
    Static text definitions are also passed and applied if not pre-rendered.

    Args:
        base_canvas (Image.Image): The base image to draw upon.
        slide_text_definitions (list): List of text definitions from the slide 
                                     (e.g., from 'scrolling_texts' or 'static_text_surfaces').
                                     Each item is a dict like {'surface': Image, 'params': dict, 'original_width': int}.
                                     For static text on video, 'original_width' might not be present.
        scroll_positions (dict): A dictionary mapping `idx` (from `slide_text_definitions`) to current X scroll position.
                                 This is updated in-place for scrolling text.
        screen_width (int): Width of the target screen.
        screen_height (int): Height of the target screen.
        delta_time (float): Time elapsed since the last frame, for scroll speed calculation.
        is_video_frame (bool): If True, implies static text also needs to be rendered from definitions.

    Returns:
        Image.Image: The new canvas with all text applied.
    """
    canvas_with_text = base_canvas.copy()
    if canvas_with_text.mode != 'RGBA': # Ensure RGBA for alpha compositing
        canvas_with_text = canvas_with_text.convert('RGBA')

    for idx, text_info in enumerate(slide_text_overlays):
        text_surface = text_info['surface']
        params = text_info['params']
        
        if text_surface is None:
            logging.warning(f"Text surface is None for overlay idx {idx}. Skipping.")
            continue

        is_scrolling_text = params.get('scroll', False)
        
        if is_scrolling_text:
            original_text_width = text_info.get('original_width', text_surface.width) # Fallback to surface width
            # Calculate new scroll position for scrolling text
            if idx not in scroll_positions: # Initialize if not present
                 scroll_positions[idx] = screen_width
            
            scroll_positions[idx] -= SCROLL_SPEED_PPS * delta_time
            current_x = int(scroll_positions[idx])
            
            if current_x + original_text_width < 0: # Reset if scrolled off screen
                scroll_positions[idx] = screen_width
                current_x = screen_width
            
            # Default Y position for scrolling text (e.g., bottom of screen)
            default_y = screen_height - text_surface.height - params.get('margin', 10) 
            text_y_position = params.get('y_position', default_y)
            if isinstance(text_y_position, str): # e.g. "90%"
                try: text_y_position = int(screen_height * (int(text_y_position.rstrip('%')) / 100.0) - text_surface.height / 2)
                except ValueError: text_y_position = default_y
        
        elif is_video_frame: # Static text being applied to a video frame
            # Determine position for static text (similar to fetch_and_process_image_slide)
            text_align = params.get('align', 'bottom_center')
            margin = params.get('margin', 10)
            surf_width, surf_height = text_surface.size
            if text_align == 'top_left': current_x, text_y_position = margin, margin
            elif text_align == 'top_center': current_x, text_y_position = (screen_width - surf_width) // 2, margin
            elif text_align == 'top_right': current_x, text_y_position = screen_width - surf_width - margin, margin
            elif text_align == 'center_left': current_x, text_y_position = margin, (screen_height - surf_height) // 2
            elif text_align == 'center': current_x, text_y_position = (screen_width - surf_width) // 2, (screen_height - surf_height) // 2
            elif text_align == 'center_right': current_x, text_y_position = screen_width - surf_width - margin, (screen_height - surf_height) // 2
            elif text_align == 'bottom_left': current_x, text_y_position = margin, screen_height - surf_height - margin
            elif text_align == 'bottom_center': current_x, text_y_position = (screen_width - surf_width) // 2, screen_height - surf_height - margin
            elif text_align == 'bottom_right': current_x, text_y_position = screen_width - surf_width - margin, screen_height - surf_height - margin
            else: # Default if align is unknown
                current_x, text_y_position = (screen_width - surf_width) // 2, screen_height - surf_height - margin
        else: # Static text already on base_canvas for image/website, skip here
            continue 

        # Composite the text
        temp_text_layer = Image.new('RGBA', canvas_with_text.size, (0,0,0,0))
        temp_text_layer.paste(text_surface, (current_x, text_y_position))
        canvas_with_text = Image.alpha_composite(canvas_with_text, temp_text_layer)

    return canvas_with_text.convert('RGB')


def main():
    logging.info("Starting Pillow Slideshow script...")
    global processed_slides_global_for_cleanup # Allow main to update this for atexit

    # Load configuration
    app_config = load_config(CONFIG_FILE_PATH)
    logging.info(f"Loaded configuration: {app_config}")
    
    raw_couchdb_url_from_config = app_config['couchdb_url']
    parsed_config_url = urlparse(raw_couchdb_url_from_config)
    slideshows_db_path = "/slideshows" # Hardcode the database name as per user preference
    couchdb_slideshows_db_url = urlunparse((
        parsed_config_url.scheme,
        parsed_config_url.netloc,
        slideshows_db_path,
        '', '', '' # No params, query, or fragment
    ))
    logging.info(f"All CouchDB operations will target the slideshows database determined as: {couchdb_slideshows_db_url}")
    tv_uuid = app_config['tv_uuid']

    fb_info = get_framebuffer_info(FB_DEVICE) # fb_info includes width, height, bpp, img_mode, fb_obj (None for now)
    screen_width, screen_height, bpp, img_mode = fb_info['width'], fb_info['height'], fb_info['bpp'], fb_info['img_mode']


    # changes_thread = threading.Thread(
    #     target=watch_changes, args=(couchdb_slideshows_db_url, tv_uuid, need_refetch), daemon=True
    # )
    # changes_thread.start()
    # logging.info("CouchDB changes watcher thread started.") # This log might be misleading now, but keeping for consistency unless asked to remove

    current_slides = []
    initial_doc = fetch_document(couchdb_slideshows_db_url, tv_uuid)
    if initial_doc:
        current_slides = process_slides_from_doc(initial_doc, couchdb_slideshows_db_url, tv_uuid, screen_width, screen_height, app_config)
    else:
        logging.warning("Could not fetch initial slideshow document. Starting with empty slide list.")

    current_slide_index = 0
    outgoing_slide_canvas = None # Pillow Image of the last displayed frame
    is_running = True
    
    # Initialize scroll positions for all slides (first time)
    # This dict will store {slide_index: {text_overlay_index: scroll_x_position}}
    slide_scroll_positions = {} 

    try:
        while is_running:
            if need_refetch.is_set():
                need_refetch.clear()
                logging.info("Change detected, refetching and reprocessing slides.")
                # Cleanup old temp files from the previous list of slides
                cleanup_all_temp_files() # Uses global processed_slides_global_for_cleanup
                
                new_doc = fetch_document(couchdb_slideshows_db_url, tv_uuid)
                if new_doc:
                    current_slides = process_slides_from_doc(new_doc, couchdb_slideshows_db_url, tv_uuid, screen_width, screen_height, app_config)
                    # processed_slides_global_for_cleanup is updated by process_slides_from_doc
                else:
                    current_slides = []
                current_slide_index = 0
                outgoing_slide_canvas = None # Reset transition state
                slide_scroll_positions = {} # Reset scroll positions for new slides
                
                if not current_slides:
                    logging.info("No slides after refetch. Displaying placeholder.")
                    placeholder_img = Image.new('RGB', (screen_width, screen_height), "black")
                    draw = ImageDraw.Draw(placeholder_img)
                    draw.text((10,10), "No slides loaded. Waiting...", fill="white")
                    write_to_framebuffer(fb_info.get('fb_obj'), placeholder_img, screen_width, screen_height, bpp, img_mode)
                    time.sleep(5)
                    continue

            if not current_slides:
                logging.info("No slides to display. Waiting for content.")
                placeholder_img = Image.new('RGB', (screen_width, screen_height), "black")
                draw = ImageDraw.Draw(placeholder_img)
                draw.text((10,10), "No slides. Check configuration.", fill="white")
                write_to_framebuffer(fb_info.get('fb_obj'), placeholder_img, screen_width, screen_height, bpp, img_mode)
                time.sleep(10) # Wait longer if no slides at all
                need_refetch.set() # Trigger a refetch attempt
                continue

            if current_slide_index >= len(current_slides):
                current_slide_index = 0 # Loop slideshow

            slide = current_slides[current_slide_index]
            slide_duration_s = float(slide.get('duration', DEFAULT_SLIDE_DURATION_S))
            transition_time_ms = int(slide.get('transition_time_ms', DEFAULT_TRANSITION_TIME_MS))
            slide_name = slide.get('name', f"Unnamed Slide {current_slide_index + 1}")
            slide_type = slide.get('content_type', slide.get('type', 'unknown'))

            logging.info(f"Preparing slide {current_slide_index + 1}/{len(current_slides)}: '{slide_name}' (Type: {slide_type}), Duration: {slide_duration_s}s, Transition: {transition_time_ms}ms")

            # Prepare incoming_slide_canvas for transition (first frame or static image)
            incoming_canvas_for_transition = None
            if slide_type == 'video':
                # For video, transition is typically from/to black or previous slide's last frame to first video frame.
                # Here, we'll fade from black for simplicity if no outgoing_slide_canvas.
                # The actual first frame of video will be rendered after transition.
                incoming_canvas_for_transition = Image.new('RGB', (screen_width, screen_height), (0,0,0))
            elif slide.get('processed_image'):
                incoming_canvas_for_transition = slide['processed_image'] # Already has static text
            
            if incoming_canvas_for_transition is None: # Fallback
                 logging.error(f"Slide '{slide_name}' has no content to display. Showing error placeholder.")
                 incoming_canvas_for_transition = Image.new('RGB', (screen_width, screen_height), (128,0,128)) # Magenta
                 draw = ImageDraw.Draw(incoming_canvas_for_transition)
                 draw.text((10,10), f"Error: Slide '{slide_name}' content missing.", fill="white")


            # Perform transition
            if outgoing_slide_canvas and transition_time_ms > 0:
                perform_fade_transition(FB_DEVICE, screen_width, screen_height, bpp, img_mode, outgoing_slide_canvas, incoming_canvas_for_transition, transition_time_ms)
            else: # No transition (first slide or zero duration)
                # Apply text and scroll for the first frame before writing if it's not a video slide
                # Video slides handle text per frame.
                if slide_type != 'video':
                    # For static/website, 'processed_image' has static text.
                    # Scrolling text needs to be applied frame by frame.
                    # Initialize scroll positions for the current slide if not already done.
                    if current_slide_index not in slide_scroll_positions:
                         slide_scroll_positions[current_slide_index] = {
                            idx: screen_width for idx, _ in enumerate(slide.get('scrolling_texts', []))
                        }                       
                    
                    # Apply text and scroll for the first frame. Delta_time is 0 for the initial frame.
                    final_display_image = apply_text_and_scroll(
                        incoming_canvas_for_transition, 
                        slide.get('scrolling_texts', []), # Pass scrolling text definitions
                        slide_scroll_positions[current_slide_index], 
                        screen_width,
                        screen_height, # Add screen_height here
                        0 
                    )
                    write_to_framebuffer(fb_info.get('fb_obj'), final_display_image, screen_width, screen_height, bpp, img_mode)
                    outgoing_slide_canvas = final_display_image.copy() 
                else: # Video slide, initial canvas (likely black) is written.
                    write_to_framebuffer(fb_info.get('fb_obj'), incoming_canvas_for_transition, screen_width, screen_height, bpp, img_mode)
                    outgoing_slide_canvas = incoming_canvas_for_transition.copy()

            current_slide_name_for_status = slide.get('name', f"Unnamed Slide {current_slide_index + 1}")
            # tv_uuid is available from app_config['tv_uuid'] or already in a variable
            # couchdb_slideshows_db_url is also available from earlier in main()
            status_doc_id = f"status_{tv_uuid}"

            logging.info(f"Updating TV status for displayed slide: '{current_slide_name_for_status}' to doc ID '{status_doc_id}'.")
            update_tv_status_document(couchdb_slideshows_db_url, status_doc_id, tv_uuid, current_slide_name_for_status)

            # Main display phase for the slide
            slide_start_time = time.time()
            frame_target_duration = 1.0 / SCROLL_FPS 

            if slide_type == 'video':
                video_path = slide.get('video_temp_path')
                video_fps = slide.get('video_fps', SCROLL_FPS) # Use video's FPS if available
                video_frame_duration = 1.0 / video_fps if video_fps > 0 else frame_target_duration
                
                if video_path and os.path.exists(video_path):
                    video_cap = None
                    try:
                        video_cap = cv2.VideoCapture(video_path)
                        if not video_cap.isOpened():
                            logging.error(f"Could not open video {video_path} for slide '{slide_name}'. Displaying error.")
                            error_img = Image.new('RGB', (screen_width, screen_height), "red")
                            ImageDraw.Draw(error_img).text((10,10), f"Error: Video {slide_name} not found.", fill="white")
                            write_to_framebuffer(fb_info.get('fb_obj'), error_img, screen_width, screen_height, bpp, img_mode)
                            outgoing_slide_canvas = error_img.copy()
                            time.sleep(slide_duration_s) # Show error for slide duration
                        else:
                            # Initialize scroll positions for this video slide's text overlays
                            if current_slide_index not in slide_scroll_positions:
                                slide_scroll_positions[current_slide_index] = {
                                    idx: screen_width for idx, _ in enumerate(slide.get('static_text_surfaces', []) + slide.get('scrolling_texts', []))
                                }
                            
                            combined_text_defs = slide.get('static_text_surfaces', []) + slide.get('scrolling_texts', [])

                            while (time.time() - slide_start_time) < slide_duration_s:
                                loop_frame_start_time = time.time()
                                ret, frame = video_cap.read()
                                if not ret: # End of video
                                    video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0) # Loop video
                                    ret, frame = video_cap.read()
                                    if not ret: 
                                        logging.warning(f"Failed to loop video {slide_name}, or video is empty.")
                                        break 

                                if frame is not None:
                                    pillow_frame = cv2_to_pillow(frame)
                                    if pillow_frame:
                                        # Scale and center video frame
                                        video_w, video_h = pillow_frame.size
                                        aspect_ratio = video_w / video_h if video_h > 0 else 1.0
                                        
                                        if screen_width / screen_height > aspect_ratio:
                                            new_h = screen_height; new_w = int(aspect_ratio * new_h)
                                        else:
                                            new_w = screen_width; new_h = int(new_w / aspect_ratio) if aspect_ratio > 0 else screen_height
                                        
                                        scaled_video_frame = pillow_frame.resize((new_w, new_h), Image.LANCZOS)
                                        frame_canvas = Image.new('RGB', (screen_width, screen_height), (0,0,0))
                                        frame_canvas.paste(scaled_video_frame, ((screen_width - new_w)//2, (screen_height - new_h)//2))
                                        
                                        # Apply all text (static and scrolling) to the current video frame
                                        final_frame_for_fb = apply_text_and_scroll(
                                            frame_canvas, 
                                            combined_text_defs, # Pass combined static and scrolling text defs
                                            slide_scroll_positions[current_slide_index], 
                                            screen_width,
                                            screen_height, # Add screen_height here
                                            video_frame_duration # delta_time for this frame
                                        )
                                        write_to_framebuffer(fb_info.get('fb_obj'), final_frame_for_fb, screen_width, screen_height, bpp, img_mode)
                                        outgoing_slide_canvas = final_frame_for_fb.copy()
                                
                                elapsed_frame_time = time.time() - loop_frame_start_time
                                time.sleep(max(0, video_frame_duration - elapsed_frame_time))
                    except Exception as e:
                        logging.error(f"Error during video playback for slide '{slide_name}': {e}", exc_info=True)
                        # Show an error image for the remainder of the slide duration
                        error_img = Image.new('RGB', (screen_width, screen_height), "darkred")
                        ImageDraw.Draw(error_img).text((10,10), f"Playback Error: {slide_name}", fill="white")
                        write_to_framebuffer(fb_info.get('fb_obj'), error_img, screen_width, screen_height, bpp, img_mode)
                        outgoing_slide_canvas = error_img.copy()
                        remaining_time = slide_duration_s - (time.time() - slide_start_time)
                        if remaining_time > 0: time.sleep(remaining_time)
                    finally:
                        if video_cap and video_cap.isOpened():
                            video_cap.release()
                else: 
                    logging.error(f"Video path missing or invalid for slide '{slide_name}'. Skipping video playback.")
                    placeholder_img = Image.new('RGB', (screen_width, screen_height), "grey")
                    ImageDraw.Draw(placeholder_img).text((10,10), f"Video Error: {slide_name}", fill="black")
                    write_to_framebuffer(fb_info.get('fb_obj'), placeholder_img, screen_width, screen_height, bpp, img_mode)
                    outgoing_slide_canvas = placeholder_img.copy()
                    time.sleep(slide_duration_s)


            elif slide.get('processed_image'): # Image or Website slide (already has static text)
                base_image = slide['processed_image'] 

                if current_slide_index not in slide_scroll_positions: # Initialize if needed
                    slide_scroll_positions[current_slide_index] = {
                         idx: screen_width for idx, _ in enumerate(slide.get('scrolling_texts', []))
                    }

                if slide.get('scrolling_texts'):
                    while (time.time() - slide_start_time) < slide_duration_s:
                        loop_frame_start_time = time.time()
                        
                        current_display_canvas = apply_text_and_scroll(
                            base_image, 
                            slide.get('scrolling_texts', []), # Only pass scrolling texts here
                            slide_scroll_positions[current_slide_index], 
                            screen_width,
                            screen_height, # Add screen_height here
                            frame_target_duration 
                        )
                        write_to_framebuffer(fb_info.get('fb_obj'), current_display_canvas, screen_width, screen_height, bpp, img_mode)
                        outgoing_slide_canvas = current_display_canvas.copy()
                        
                        elapsed_frame_time = time.time() - loop_frame_start_time
                        time.sleep(max(0, frame_target_duration - elapsed_frame_time))
                else: # Static image/website with no scrolling text
                    outgoing_slide_canvas = base_image.copy() 
                    # Image was already displayed by transition or initial write_to_framebuffer call
                    # So just sleep for the remaining duration
                    remaining_duration = slide_duration_s - (time.time() - slide_start_time)
                    if remaining_duration > 0: time.sleep(remaining_duration)
            
            else: # Fallback for unknown type or if processed_image is missing
                logging.warning(f"Slide '{slide_name}' (Type: {slide_type}) has no displayable content. Showing error placeholder.")
                placeholder_img = Image.new('RGB', (screen_width, screen_height), (20,20,20)) 
                draw = ImageDraw.Draw(placeholder_img)
                draw.text((10,10), f"Error: Slide '{slide_name}'\n (Type: {slide_type}) \nNo content.", fill="red")
                write_to_framebuffer(fb_info.get('fb_obj'), placeholder_img, screen_width, screen_height, bpp, img_mode)
                outgoing_slide_canvas = placeholder_img.copy()
                time.sleep(slide_duration_s)


            current_slide_index +=1
            if current_slide_index >= len(current_slides):
                current_slide_index = 0 # Loop

    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received. Shutting down.")
    except KeyboardInterrupt:
        logging.info("Shutdown requested by user (KeyboardInterrupt).")
        is_running = False # Ensure loop terminates
    except Exception as e:
        logging.critical(f"Unhandled critical error in main loop: {e}", exc_info=True)
        is_running = False # Ensure loop terminates
    finally:
        logging.info("Exiting main loop. Final cleanup will be handled by atexit.")
        # is_running = False # This is already set in except blocks, or loop condition handles natural exit
        if fb_info and fb_info.get('fb_obj'): # Conceptual: close framebuffer if it was opened and stored
            try:
                # fb_info['fb_obj'].close() # Example if it were a file object
                logging.info("Closed framebuffer object (conceptual).")
            except Exception as e:
                logging.error(f"Error closing framebuffer object (conceptual): {e}", exc_info=True)


# Global list to track slides that need cleanup, managed by process_slides_from_doc
def cleanup_all_temp_files():
    """Iterates through the global list of processed slides and calls their cleanup functions."""
    logging.info(f"Executing atexit cleanup for {len(processed_slides_global_for_cleanup)} slides from global list.")
    # Create a copy for iteration, as the original list might be modified elsewhere (though not typical for atexit)
    slides_to_clean = list(processed_slides_global_for_cleanup)
    for slide in slides_to_clean:
        if callable(slide.get('cleanup_func')):
            try:
                logging.debug(f"Calling cleanup for slide: {slide.get('name', 'N/A')}")
                slide['cleanup_func']()
            except Exception as e:
                logging.error(f"Error during cleanup for slide {slide.get('name', 'N/A')}: {e}", exc_info=True)
    processed_slides_global_for_cleanup.clear() # Clear the global list after cleanup attempt


if __name__ == "__main__":
    import atexit
    # Ensure cleanup_all_temp_files is registered to be called when the program exits,
    # regardless of whether it's a normal exit or due to an unhandled exception (excluding os._exit).
    atexit.register(cleanup_all_temp_files)
    main()

[end of files/pillow_slideshow.py]
