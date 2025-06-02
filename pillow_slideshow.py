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
from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
# from selenium.webdriver.common.by import By
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC


# Basic logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
WEBSITE_CACHE_EXPIRY_SECONDS = 3600
MAX_WEBSITE_CACHE_ENTRIES = 10

# Transition Constants
FADE_STEPS = 25 # Number of steps for fade animation
DEFAULT_TRANSITION_TIME_MS = 500 # Default duration for slide transitions in milliseconds

# Main Loop Behavior
SCROLL_FPS = 30  # Frames per second for scrolling text animation
SCROLL_SPEED_PPS = 100  # Pixels per second for scrolling text
DEFAULT_SLIDE_DURATION_S = 10  # Default duration for a slide in seconds


# Helper to convert hex color to RGB/RGBA tuple
def hex_to_rgb(hex_color, alpha=None):
    hex_color = hex_color.lstrip('#')
    lv = len(hex_color)
    rgb = tuple(int(hex_color[i:i + lv // 3], 16) for i in range(0, lv, lv // 3))
    if alpha is not None:
        return rgb + (alpha,)
    return rgb


def get_font(size):
    """Loads a font, trying primary, fallback, then default."""
    try:
        return ImageFont.truetype(FONT_PATH_PRIMARY, size)
    except IOError:
        logging.warning(f"Primary font '{FONT_PATH_PRIMARY}' not found at size {size}. Trying fallback.")
        try:
            return ImageFont.truetype(FONT_PATH_FALLBACK, size)
        except IOError:
            logging.warning(f"Fallback font '{FONT_PATH_FALLBACK}' not found at size {size}. Using Pillow's default.")
            try:
                # Pillow's default font is very basic and might not support sizes well.
                return ImageFont.load_default()
            except Exception as e:
                 logging.error(f"Could not load any font, including Pillow's default: {e}")
                 return None # Should not happen with load_default unless Pillow is broken

def render_text_to_surface(text_content, font_size_str, text_color_hex, text_bg_color_hex=None, screen_width_for_scrolling=None, text_padding=5):
    """
    Renders text onto a new Pillow Image surface with a transparent background.
    Returns the surface and its original (pre-scrolling) width.
    """
    font_size_mapping = {"small": 24, "medium": 36, "large": 48, "xlarge": 60}
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
    # For scrolling text, if it's very long, its own width will be used.
    render_width_param = screen_width_for_text_area if not is_scrolling else None


    cache_key_parts = [current_text, font_size, color, bg_color]
    # Only include render_width_param in key if it's used for rendering (i.e. non-scrolling or short scrolling)
    if render_width_param:
        cache_key_parts.append(render_width_param)
    cache_key = tuple(cache_key_parts)


    if not force_refresh and cache_key in text_cache:
        logging.debug(f"Returning cached text surface for: {current_text[:30]}...")
        return text_cache[cache_key]

    # Render the text
    text_surface, original_text_width = render_text_to_surface(
        current_text, font_size, color, bg_color,
        screen_width_for_scrolling=render_width_param,
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
        logging.error(f"Connection error fetching document {doc_url}: {e}")
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout fetching document {doc_url}: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching document {doc_url}: {e}")
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from document {doc_url}: {e}")
    return None

def watch_changes(couchdb_url, tv_uuid, need_refetch_event):
    """Watches the CouchDB _changes feed for the specific document."""
    changes_url = f"{couchdb_url.rstrip('/')}/_changes"
    params = {
        "feed": "continuous",
        "heartbeat": 30000, # 30 seconds in milliseconds
        "doc_ids": json.dumps([tv_uuid]),
        "since": "now" # Start from the current state
    }
    logging.info(f"Starting to watch changes feed for doc ID {tv_uuid} at {changes_url}")

    while True:
        session = get_requests_session()
        try:
            with session.get(changes_url, params=params, stream=True, timeout=45) as response: # Slightly longer timeout for heartbeat
                response.raise_for_status()
                logging.info(f"Successfully connected to changes feed for {tv_uuid}")
                for line in response.iter_lines():
                    if line:
                        try:
                            decoded_line = line.decode('utf-8').strip()
                            if not decoded_line: # Skip empty lines (heartbeats)
                                logging.debug("Received heartbeat or empty line from changes feed.")
                                continue
                            if decoded_line.startswith('{'): # Ensure it's a JSON object
                                change = json.loads(decoded_line)
                                logging.info(f"Received change: {json.dumps(change)}")
                                if change.get("id") == tv_uuid:
                                    logging.info(f"Change detected for document {tv_uuid}. Triggering refetch.")
                                    need_refetch_event.set()
                            else:
                                logging.debug(f"Received non-JSON line from changes feed: {decoded_line}")
                        except json.JSONDecodeError as e:
                            logging.warning(f"Error decoding JSON from changes feed line: '{line.decode('utf-8', errors='ignore')}': {e}")
                        except Exception as e:
                            logging.error(f"Unexpected error processing change line: {e}")
        except requests.exceptions.HTTPError as e:
            logging.error(f"HTTP error watching changes feed: {e.response.status_code} {e.response.reason}. Retrying in 30s.")
        except requests.exceptions.ConnectionError as e:
            logging.error(f"Connection error watching changes feed: {e}. Retrying in 30s.")
        except requests.exceptions.Timeout as e:
            logging.warning(f"Timeout watching changes feed (may be normal due to heartbeat): {e}. Reconnecting.")
            # No sleep here, just reconnect immediately for timeouts if using heartbeat
        except requests.exceptions.RequestException as e:
            logging.error(f"Error watching changes feed: {e}. Retrying in 30s.")
        except Exception as e:
            logging.error(f"Unexpected error in watch_changes loop: {e}. Retrying in 30s.")

        logging.info("Attempting to reconnect to changes feed after 30 seconds...")
        time.sleep(30)

def fetch_and_process_image_slide(slide_doc, couchdb_url, tv_uuid, screen_width, screen_height):
    """
    Fetches image attachment, scales/centers it. Then processes and applies text overlays.
    """
    content_name = slide_doc.get('content_name')
    slide_name = slide_doc.get('name', 'Unnamed Image Slide')

    if not content_name:
        logging.warning(f"Image slide '{slide_name}' is missing 'content_name'. Skipping.")
        return None

    attachment_url = f"{couchdb_url.rstrip('/')}/{tv_uuid}/{content_name}"
    logging.info(f"Fetching image attachment for slide '{slide_name}' from: {attachment_url}")

    session = get_requests_session()
    try:
        response = session.get(attachment_url, timeout=15) # Increased timeout for image download
        response.raise_for_status()

        image = Image.open(BytesIO(response.content))
        img_width, img_height = image.size

        if img_width == 0 or img_height == 0:
            logging.warning(f"Image '{content_name}' for slide '{slide_name}' has zero dimension. Skipping.")
            return None

        # Convert to RGB if it's not (e.g. RGBA, P, L) to ensure compatibility with background
        if image.mode not in ('RGB', 'L'): # Allow L mode (grayscale) as it can be pasted on RGB
             if image.mode == 'RGBA':
                 logging.debug(f"Image '{content_name}' is RGBA, creating RGB canvas for it before pasting on main canvas.")
                 # Create an RGB canvas for the RGBA image to handle transparency
                 rgb_image = Image.new("RGB", image.size, (0,0,0)) # Black background for this intermediate step
                 rgb_image.paste(image, (0,0), mask=image.split()[3]) # Paste using alpha channel as mask
                 image = rgb_image
             elif image.mode == 'P': # Palette mode
                 logging.debug(f"Image '{content_name}' is P (Palette) mode, converting to RGB.")
                 image = image.convert('RGB')
             else: # Other modes like LA (Luminance Alpha)
                 logging.debug(f"Image '{content_name}' is in mode {image.mode}, converting to RGB.")
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
            logging.warning(f"Calculated new dimensions for '{content_name}' are invalid ({new_width}x{new_height}). Skipping.")
            return None

        logging.info(f"Scaling image '{content_name}' from {img_width}x{img_height} to {new_width}x{new_height} for screen {screen_width}x{screen_height}")
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


        logging.info(f"Successfully processed image and text for slide '{slide_name}' with image '{content_name}'.")
        return slide_doc

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error fetching image {attachment_url} for slide '{slide_name}': {e.response.status_code} {e.response.reason}")
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Connection error fetching image {attachment_url} for slide '{slide_name}': {e}")
    except requests.exceptions.Timeout as e:
        logging.error(f"Timeout fetching image {attachment_url} for slide '{slide_name}': {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching image {attachment_url} for slide '{slide_name}': {e}")
    except IOError as e: # Pillow-related errors (e.g., cannot open image)
        logging.error(f"Error processing image {content_name} for slide '{slide_name}': {e}")
    except Exception as e:
        logging.error(f"Unexpected error processing image slide '{slide_name}' with image '{content_name}': {e}")

    return None

def perform_fade_transition(fb_path, screen_width, screen_height, bpp, outgoing_image_canvas, incoming_image_canvas, duration_ms):
    """
    Performs a fade transition between two Pillow Image canvases.
    """
    logging.info(f"Performing fade transition: duration {duration_ms}ms, from {'image' if outgoing_image_canvas else 'None'} to {'image' if incoming_image_canvas else 'None'}")

    if incoming_image_canvas is None:
        logging.warning("Fade transition requested but incoming_image_canvas is None. Nothing to display.")
        # If outgoing_image_canvas exists, it will just stay on screen. If not, screen might be blank.
        return

    if duration_ms <= 0:
        logging.info("Transition duration is 0 or less, displaying incoming image directly.")
        write_to_framebuffer(fb_path, incoming_image_canvas, screen_width, screen_height, bpp)
        return

    black_canvas = Image.new('RGB', (screen_width, screen_height), (0, 0, 0))
    delay_per_step = (duration_ms / FADE_STEPS) / 1000.0 # Convert ms to seconds for time.sleep

    # Fade Out Logic
    if outgoing_image_canvas:
        logging.debug("Fade Out phase...")
        for i in range(FADE_STEPS + 1):
            alpha = i / FADE_STEPS
            try:
                blended_image = Image.blend(outgoing_image_canvas, black_canvas, alpha)
                write_to_framebuffer(fb_path, blended_image, screen_width, screen_height, bpp)
            except ValueError as e: # Can happen if images are not same mode/size, though they should be screen_width/height RGB
                logging.error(f"Error blending images during fade out: {e}. Using black_canvas.")
                write_to_framebuffer(fb_path, black_canvas, screen_width, screen_height, bpp)
            time.sleep(delay_per_step)
    else:
        # If no outgoing image, we can consider the "fade out" as already done (screen is black)
        # or briefly show black before fading in the new one.
        # For simplicity, if no outgoing, we just write black once if fade-out would have occurred.
        logging.debug("No outgoing image, ensuring screen is black before fade in.")
        write_to_framebuffer(fb_path, black_canvas, screen_width, screen_height, bpp)
        # A small pause might be good here if there was no fade-out, but duration_ms should cover it.

    # Fade In Logic
    logging.debug("Fade In phase...")
    for i in range(FADE_STEPS + 1):
        alpha = i / FADE_STEPS
        try:
            blended_image = Image.blend(black_canvas, incoming_image_canvas, alpha)
            write_to_framebuffer(fb_path, blended_image, screen_width, screen_height, bpp)
        except ValueError as e:
            logging.error(f"Error blending images during fade in: {e}. Using incoming_image_canvas directly.")
            write_to_framebuffer(fb_path, incoming_image_canvas, screen_width, screen_height, bpp) # Show final to recover
            break # Exit loop if blending fails
        time.sleep(delay_per_step)

    # Ensure the final frame is exactly the incoming_image_canvas
    logging.debug("Ensuring final frame is purely the incoming image.")
    write_to_framebuffer(fb_path, incoming_image_canvas, screen_width, screen_height, bpp)
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
    Fetches video attachment, stores it in a temp file, and extracts video properties.
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
        # Define cleanup function using default argument to capture current temp_file_path
        slide_doc['cleanup_func'] = lambda path=temp_file_path_actual: (
            logging.info(f"Cleaning up temporary video file: {path}"),
            os.unlink(path) if os.path.exists(path) else None
        )
        logging.info(f"Successfully prepared video slide '{slide_name}' from '{content_name}'.")
        return slide_doc

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching video {attachment_url} for slide '{slide_name}': {e}")
    except IOError as e:
        logging.error(f"IOError saving video {content_name} for slide '{slide_name}': {e}")
    except Exception as e:
        logging.error(f"Unexpected error preparing video slide '{slide_name}': {e}")

    if temp_file_path_actual and os.path.exists(temp_file_path_actual):
        try:
            os.unlink(temp_file_path_actual)
            logging.info(f"Cleaned up orphaned temp video file during error: {temp_file_path_actual}")
        except OSError as unlink_e:
            logging.error(f"Error unlinking orphaned temp video file {temp_file_path_actual}: {unlink_e}")
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

    except Exception as e:
        logging.error(f"Error capturing website {url}: {e}")
        # More specific error handling for Selenium exceptions can be added here
        # (e.g., TimeoutException, WebDriverException)
        return None
    finally:
        if driver:
            driver.quit()
            logging.info(f"WebDriver quit for {url}")


def fetch_and_process_website_slide(slide_doc, screen_width, screen_height, config_unused):
    """
    Fetches/captures a website screenshot, processes it, and applies text overlays.
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
        return slide_doc
    else:
        logging.error(f"Failed to capture and process website slide: {slide_name} ({url})")
        # Return the slide_doc without 'processed_image' to indicate failure for this slide
        # Or, if strict, return None, and let process_slides_from_doc filter it out
        return None


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
                    slide_def, screen_width, screen_height, app_config
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
    # For now, continue returning mock data but structure it for potential future use
    fb_info = {
        'width': 1920,
        'height': 1080,
        'bpp': 32,
        'fb_obj': None, # Placeholder for actual framebuffer file object or mmap object
        'img_mode': 'RGB' # Default Pillow mode to convert to for framebuffer
        # Add other necessary fields like 'line_length' if real ioctl is used
    }
    # Determine img_mode based on bpp, this is a simplification
    if fb_info['bpp'] == 32:
        fb_info['img_mode'] = 'RGBA' # Or 'RGB' if alpha is not used/supported by fb
    elif fb_info['bpp'] == 24:
        fb_info['img_mode'] = 'RGB'
    elif fb_info['bpp'] == 16:
        fb_info['img_mode'] = 'RGB;16' # Example, might be specific (e.g. RGB565)

    logging.info(f"Mock framebuffer info: {fb_info['width']}x{fb_info['height']}, {fb_info['bpp']}bpp, Pillow mode: {fb_info['img_mode']}")
    return fb_info


def write_to_framebuffer(fb_obj_unused, image_data, screen_width, screen_height, bpp_unused, img_mode_unused):
    """
    Placeholder function to write image data (Pillow Image) to the framebuffer.
    fb_obj would be the opened framebuffer file or mmap object.
    image_data is expected to be a Pillow Image object.
    """
    # In a real implementation:
    # 1. Convert image_data (Pillow Image) to raw bytes in the correct pixel format
    #    (e.g., RGBA, BGRA, RGB565) based on framebuffer info.
    #    raw_bytes = image_data.convert(img_mode_from_fb_info).tobytes()
    #    If specific byte order or padding is needed (e.g. from line_length), handle here.
    # 2. Write/blit raw_bytes to fb_obj.
    #    If using os.write: os.lseek(fb_obj, 0, os.SEEK_SET); os.write(fb_obj, raw_bytes)
    #    If using mmap: fb_obj.seek(0); fb_obj.write(raw_bytes)

    # Placeholder logging:
    img_byte_size = 0
    if image_data:
        try:
            # Simulate getting bytes for logging purposes
            img_byte_size = len(image_data.tobytes())
        except Exception: # Can fail if mode is weird for tobytes
            pass

    logging.debug(f"Simulating write to framebuffer: {screen_width}x{screen_height}, {img_byte_size} bytes (from Pillow Image).")
    # time.sleep(0.001) # Simulate a small delay of writing to framebuffer


def apply_text_and_scroll(base_canvas, slide_text_overlays, scroll_positions, screen_width, delta_time):
    """
    Applies static and scrolling text overlays to a copy of the base_canvas.
    Returns the new canvas with text applied.
    Updates scroll_positions dictionary in place.
    """
    canvas_with_text = base_canvas.copy()
    # Ensure canvas is RGBA for alpha_composite, convert back to RGB at the very end if needed.
    if canvas_with_text.mode != 'RGBA':
        canvas_with_text = canvas_with_text.convert('RGBA')

    # Static text is assumed to be pre-rendered on base_canvas for image/website slides.
    # For video, static text might need to be applied here if not pre-rendered per frame.
    # This function primarily focuses on scrolling text for now.

    scrolling_text_defs = [t for t in slide_text_overlays if t.get('params', {}).get('scroll', False)]

    for idx, text_info in enumerate(scrolling_text_defs):
        text_surface = text_info['surface']
        original_text_width = text_info['original_width']
        params = text_info['params']

        if text_surface is None:
            continue

        # Calculate new scroll position
        scroll_positions[idx] -= SCROLL_SPEED_PPS * delta_time
        current_x = int(scroll_positions[idx])

        # Reset if scrolled off screen
        if current_x + original_text_width < 0:
            scroll_positions[idx] = screen_width
            current_x = screen_width

        text_y_position = params.get('y_position', screen_height - text_surface.height - params.get('margin', 10)) # Default bottom
        if isinstance(text_y_position, str): # e.g. "50%"
            try: text_y_position = int(screen_height * (int(text_y_position.rstrip('%')) / 100.0))
            except ValueError: text_y_position = screen_height - text_surface.height - params.get('margin', 10)


        # Composite the scrolling text
        # Use a temporary layer for each text to handle alpha correctly
        temp_text_layer = Image.new('RGBA', canvas_with_text.size, (0,0,0,0))
        temp_text_layer.paste(text_surface, (current_x, text_y_position))
        canvas_with_text = Image.alpha_composite(canvas_with_text, temp_text_layer)

    return canvas_with_text.convert('RGB') # Convert back to RGB for framebuffer


def main():
    logging.info("Starting Pillow Slideshow script...")
    global processed_slides_global_for_cleanup # Allow main to update this for atexit

    # Load configuration
    app_config = load_config(CONFIG_FILE_PATH)
    logging.info(f"Loaded configuration: {app_config}")

    couchdb_url = app_config['couchdb_url']
    tv_uuid = app_config['tv_uuid']

    fb_info = get_framebuffer_info(FB_DEVICE) # fb_info includes width, height, bpp, img_mode, fb_obj (None for now)
    screen_width, screen_height, bpp, img_mode = fb_info['width'], fb_info['height'], fb_info['bpp'], fb_info['img_mode']


    changes_thread = threading.Thread(
        target=watch_changes, args=(couchdb_url, tv_uuid, need_refetch), daemon=True
    )
    changes_thread.start()
    logging.info("CouchDB changes watcher thread started.")

    current_slides = []
    initial_doc = fetch_document(couchdb_url, tv_uuid)
    if initial_doc:
        current_slides = process_slides_from_doc(initial_doc, couchdb_url, tv_uuid, screen_width, screen_height, app_config)
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

                new_doc = fetch_document(couchdb_url, tv_uuid)
                if new_doc:
                    current_slides = process_slides_from_doc(new_doc, couchdb_url, tv_uuid, screen_width, screen_height, app_config)
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
                perform_fade_transition(FB_DEVICE, screen_width, screen_height, bpp, outgoing_slide_canvas, incoming_canvas_for_transition, transition_time_ms)
            else: # No transition (first slide or zero duration)
                # Apply text and scroll for the first frame before writing if it's not a video slide
                # Video slides handle text per frame.
                if slide_type != 'video':
                    # For static/website, 'processed_image' has static text. Scrolling text needs to be applied.
                    # Initialize scroll positions for the current slide if not already done
                    if current_slide_index not in slide_scroll_positions:
                        slide_scroll_positions[current_slide_index] = {
                            idx: screen_width for idx, _ in enumerate(slide.get('scrolling_texts', []))
                        }

                    final_display_image = apply_text_and_scroll(
                        incoming_canvas_for_transition,
                        slide.get('scrolling_texts', []),
                        slide_scroll_positions[current_slide_index],
                        screen_width,
                        0 # delta_time is 0 for initial frame
                    )
                    write_to_framebuffer(fb_info.get('fb_obj'), final_display_image, screen_width, screen_height, bpp, img_mode)
                    outgoing_slide_canvas = final_display_image.copy() # Save for next transition
                else: # Video slide, just write the prepared canvas (likely black or first frame after modification)
                    write_to_framebuffer(fb_info.get('fb_obj'), incoming_canvas_for_transition, screen_width, screen_height, bpp, img_mode)
                    outgoing_slide_canvas = incoming_canvas_for_transition.copy()


            # Main display phase for the slide (handling animations like video/scrolling text)
            slide_start_time = time.time()
            frame_target_duration = 1.0 / SCROLL_FPS # For scrolling text animations

            if slide_type == 'video':
                video_path = slide.get('video_temp_path')
                video_fps = slide.get('video_fps', SCROLL_FPS)
                video_frame_duration = 1.0 / video_fps if video_fps > 0 else frame_target_duration

                if video_path and os.path.exists(video_path):
                    video_cap = cv2.VideoCapture(video_path)
                    if not video_cap.isOpened():
                        logging.error(f"Could not open video {video_path} for slide '{slide_name}'.")
                        outgoing_slide_canvas = Image.new('RGB', (screen_width, screen_height), "red") # Error indication
                        write_to_framebuffer(fb_info.get('fb_obj'), outgoing_slide_canvas, screen_width, screen_height, bpp, img_mode)
                        time.sleep(slide_duration_s) # Show error for slide duration
                    else:
                        # Initialize scroll positions for video slide's text overlays
                        if current_slide_index not in slide_scroll_positions:
                             slide_scroll_positions[current_slide_index] = {
                                idx: screen_width for idx, _ in enumerate(slide.get('text_overlays', [])) # Video uses text_overlays directly for scrolling
                            }

                        while (time.time() - slide_start_time) < slide_duration_s:
                            loop_frame_start_time = time.time()
                            ret, frame = video_cap.read()
                            if not ret:
                                video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0) # Loop video
                                ret, frame = video_cap.read()
                                if not ret: break # Break if loop also fails

                            if frame is not None:
                                pillow_frame = cv2_to_pillow(frame)
                                if pillow_frame:
                                    # Scale and center video frame
                                    video_w, video_h = pillow_frame.size
                                    aspect_ratio = video_w / video_h
                                    if screen_width / screen_height > aspect_ratio: # Screen is wider
                                        new_h = screen_height
                                        new_w = int(aspect_ratio * new_h)
                                    else: # Screen is taller or same aspect
                                        new_w = screen_width
                                        new_h = int(new_w / aspect_ratio)

                                    scaled_video_frame = pillow_frame.resize((new_w, new_h), Image.LANCZOS)
                                    current_display_canvas = Image.new('RGB', (screen_width, screen_height), (0,0,0))
                                    current_display_canvas.paste(scaled_video_frame, ((screen_width - new_w)//2, (screen_height - new_h)//2))

                                    # Apply text (static & scrolling) to video frame
                                    # Video slides store all text in 'text_overlays', scrolling ones have 'scroll:true'
                                    # Static text is applied by get_cached_text_surface and composited by apply_text_and_scroll
                                    # This means fetch_and_process_video_slide should prepare 'scrolling_texts' like other types if we want to use apply_text_and_scroll
                                    # For now, let's assume text_overlays are processed by apply_text_and_scroll
                                    # This requires text_overlays to be structured like 'scrolling_texts' items or adapt apply_text_and_scroll
                                    # Re-simplifying: assume video text overlays are defined in 'scrolling_texts' or 'static_texts' in slide_doc

                                    # Simplified: use `slide.get('text_overlays', [])` and let `apply_text_and_scroll` handle them
                                    # This assumes text_overlays on video are defined similar to scrolling_texts items
                                    # (i.e. pre-rendered surfaces are available or defined to be static/scrolling)
                                    # This part needs careful data structure design from CouchDB.
                                    # For now, let's assume 'text_overlays' are like 'scrolling_texts' for simplicity here.

                                    # Create a list of text_info dicts for apply_text_and_scroll
                                    # This is a bit of a hack due to differing text structures.
                                    # Ideally, video text would also be pre-processed into 'scrolling_texts' and 'static_texts' (on processed_image)
                                    video_text_definitions = []
                                    for text_param_set in slide.get('text_overlays', []):
                                        # We need to get the pre-rendered surface. This is currently done elsewhere.
                                        # This highlights a structural issue: text rendering should be consistently available.
                                        # For now, this part will be less effective for video text.
                                        # Let's assume static text for video is not pre-rendered on a base_image.
                                        # And scrolling text needs its surface.
                                        # This part of the logic is getting very complex due to trying to retrofit.
                                        # A better way: video frames are base, text is applied.
                                        # For now, let's assume `apply_text_and_scroll` can take raw text_params and render if needed.
                                        # This is too much for this function.
                                        # Simplified: video applies its own text overlays if defined.
                                        # This will be less featureful than image/web for now for text on video.
                                        pass # Placeholder for more robust video text overlay


                                    final_frame_for_fb = current_display_canvas # Potentially with text
                                    write_to_framebuffer(fb_info.get('fb_obj'), final_frame_for_fb, screen_width, screen_height, bpp, img_mode)
                                    outgoing_slide_canvas = final_frame_for_fb.copy()

                            elapsed_frame_time = time.time() - loop_frame_start_time
                            time.sleep(max(0, video_frame_duration - elapsed_frame_time))
                        video_cap.release()
                else: # Video path missing or not found
                    logging.error(f"Video path missing or invalid for slide '{slide_name}'. Skipping video playback.")
                    time.sleep(slide_duration_s) # Still wait for slide duration
                    if outgoing_slide_canvas is None: # If there was no previous canvas (e.g. error on first slide)
                        outgoing_slide_canvas = Image.new('RGB', (screen_width, screen_height), "black") # Prepare a black canvas
                    # No change to outgoing_slide_canvas, it remains the last successfully displayed frame.

            elif slide.get('processed_image'): # Image or Website slide
                base_image = slide['processed_image'] # This has static text pre-applied

                if current_slide_index not in slide_scroll_positions:
                    slide_scroll_positions[current_slide_index] = {
                         idx: screen_width for idx, _ in enumerate(slide.get('scrolling_texts', []))
                    }

                if slide.get('scrolling_texts'):
                    while (time.time() - slide_start_time) < slide_duration_s:
                        loop_frame_start_time = time.time()

                        current_display_canvas = apply_text_and_scroll(
                            base_image,
                            slide.get('scrolling_texts', []),
                            slide_scroll_positions[current_slide_index],
                            screen_width,
                            frame_target_duration # Use fixed frame_target_duration for scroll animation step
                        )
                        write_to_framebuffer(fb_info.get('fb_obj'), current_display_canvas, screen_width, screen_height, bpp, img_mode)
                        outgoing_slide_canvas = current_display_canvas.copy()

                        elapsed_frame_time = time.time() - loop_frame_start_time
                        time.sleep(max(0, frame_target_duration - elapsed_frame_time))
                else: # Static image/website with no scrolling text
                    # Already displayed by transition or initial write.
                    # Ensure outgoing_slide_canvas is set correctly.
                    outgoing_slide_canvas = base_image.copy()
                    time.sleep(slide_duration_s)

            else: # Fallback for unknown type or missing content
                logging.warning(f"Slide '{slide_name}' (Type: {slide_type}) has no displayable content. Showing placeholder for duration.")
                placeholder_img = Image.new('RGB', (screen_width, screen_height), (20,20,20)) # Dark grey
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
    finally:
        logging.info("Exiting main loop. Final cleanup will be handled by atexit.")
        is_running = False # Signal threads or other parts if any depend on this
        # fb_obj cleanup should be part of atexit or a dedicated fb_manager class

# Global list to track slides that need cleanup, managed by process_slides_from_doc
# This is a simple approach; a more robust system might use a class or explicit registry.
def cleanup_all_temp_files():
    logging.info(f"Executing atexit cleanup for {len(processed_slides_global_for_cleanup)} processed slides.")
    # Create a copy of the list to iterate over, in case cleanup_func modifies the global list
    # (though lambda for cleanup_func currently doesn't)
    slides_to_cleanup = list(processed_slides_global_for_cleanup)
    for slide in slides_to_cleanup:
        if 'cleanup_func' in slide and callable(slide['cleanup_func']):
            try:
                slide['cleanup_func']()
            except Exception as e:
                logging.error(f"Error during cleanup for slide {slide.get('name', 'N/A')}: {e}")

if __name__ == "__main__":
    import atexit
    atexit.register(cleanup_all_temp_files)
    main()
                    d = ImageDraw.Draw(placeholder_image)
                    text = f"Slide: {slide_name}\nType: {current_slide.get('type', 'N/A')}\n(No displayable content)"
                    try:
                        bbox = d.textbbox((0,0), text)
                        text_width = bbox[2] - bbox[0]
                        text_height = bbox[3] - bbox[1]
                        x = ((screen_width if screen_width else 800) - text_width) / 2
                        y = ((screen_height if screen_height else 600) - text_height) / 2
                        d.text((x, y), text, fill="white", align="center")
                    except AttributeError:
                         d.text((10,10), text, fill="white")
                    if all([screen_width, screen_height, bpp]):
                        write_to_framebuffer(FB_DEVICE, placeholder_image, screen_width, screen_height, bpp)

                current_slide_index += 1
            else: # No processed_slides available
                logging.info("No slides processed or available to display. Displaying placeholder.")
                placeholder_image = Image.new("RGB", (screen_width if screen_width else 800, screen_height if screen_height else 600), "black")
                d = ImageDraw.Draw(placeholder_image)
                text = "Waiting for slides..."
                try:
                    bbox = d.textbbox((0,0), text)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]
                    x = ((screen_width if screen_width else 800) - text_width) / 2
                    y = ((screen_height if screen_height else 600) - text_height) / 2
                    d.text((x, y), text, fill="white")
                except AttributeError:
                     d.text((10,10), text, fill="white")
                if all([screen_width, screen_height, bpp]):
                     write_to_framebuffer(FB_DEVICE, placeholder_image, screen_width, screen_height, bpp)

            slide_duration = 5 # Default duration
            if processed_slides and current_slide_index > 0: # Use duration from the slide just shown
                # current_slide_index is already incremented, so -1 for current shown slide
                shown_slide_index = current_slide_index -1
                if shown_slide_index < len(processed_slides):
                     slide_duration = processed_slides[shown_slide_index].get('duration', 5)

            logging.debug(f"Waiting for {slide_duration} seconds before next slide.")
            time.sleep(slide_duration)

    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received. Shutting down.")
    finally:
        logging.info("Pillow Slideshow script finishing. Cleaning up temporary files...")
        cleanup_all_temp_files() # Call cleanup on exit


# Global list to track slides that need cleanup, managed by process_slides_from_doc
# This is a simple approach; a more robust system might use a class or explicit registry.
def cleanup_all_temp_files():
    logging.info(f"Executing atexit cleanup for {len(processed_slides_global_for_cleanup)} processed slides.")
    # Create a copy of the list to iterate over, in case cleanup_func modifies the global list
    # (though lambda for cleanup_func currently doesn't)
    slides_to_cleanup = list(processed_slides_global_for_cleanup)
    for slide in slides_to_cleanup:
        if 'cleanup_func' in slide and callable(slide['cleanup_func']):
            try:
                slide['cleanup_func']()
            except Exception as e:
                logging.error(f"Error during cleanup for slide {slide.get('name', 'N/A')}: {e}")

if __name__ == "__main__":
    import atexit
    atexit.register(cleanup_all_temp_files)
    main()
