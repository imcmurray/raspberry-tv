import pygame
import requests
import json
import time
import threading
import configparser
import logging
from io import BytesIO
from datetime import datetime, timezone
import cv2
import numpy as np
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import hashlib
import os
import tempfile

import sys # For sys.exit

# Define Configuration Path
CONFIG_FILE_PATH = '/etc/slideshow.conf'

# Function to load configuration
def load_config(config_path):
    config = configparser.ConfigParser()
    if not config.read(config_path):
        logging.error(f"Critical: Configuration file {config_path} not found or is empty.")
        print(f"Critical: Configuration file {config_path} not found or is empty.", file=sys.stderr)
        sys.exit(1)
        
    if not config.has_section('settings'):
        logging.error(f"Critical: Configuration file {config_path} is missing the [settings] section.")
        print(f"Critical: Configuration file {config_path} is missing the [settings] section.", file=sys.stderr)
        sys.exit(1)
    return config

# Load configuration
config = load_config(CONFIG_FILE_PATH)

# Read essential settings
try:
    couchdb_url = config.get('settings', 'couchdb_url')
    tv_uuid = config.get('settings', 'tv_uuid')
    manager_url = config.get('settings', 'manager_url')
except configparser.NoOptionError as e:
    logging.error(f"Critical: Missing essential configuration in {CONFIG_FILE_PATH}: {e}")
    print(f"Critical: Missing essential configuration in {CONFIG_FILE_PATH}: {e}", file=sys.stderr)
    sys.exit(1)

# Read optional settings
office_start_time_str = config.get('settings', 'office_start_time', fallback=None)
office_end_time_str = config.get('settings', 'office_end_time', fallback=None)

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('/var/log/slideshow.log')])
logger = logging.getLogger()

# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
screen_width, screen_height = screen.get_size()

# State variables
state = "connecting"
slides = []
need_refetch = threading.Event()
website_cache = {}  # Cache for website screenshots
capture_queue = []  # Queue for upcoming website captures
capture_lock = threading.Lock()

# Function to capture website screenshot
def capture_website(url, timeout=10):
    """Capture website screenshot and return pygame surface"""
    try:
        # Setup headless Chrome
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--force-device-scale-factor=1')
        chrome_options.add_argument('--high-dpi-support=1')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--allow-running-insecure-content')
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(timeout)
        
        # Navigate to URL
        driver.get(url)
        
        # Wait for page to load
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        
        # Set viewport to full HD
        driver.set_window_size(1920, 1080)
        
        # Take screenshot
        screenshot_data = driver.get_screenshot_as_png()
        driver.quit()
        
        # Convert to pygame surface
        image_data = BytesIO(screenshot_data)
        image = pygame.image.load(image_data)
        
        # Ensure image is exactly 1920x1080 for full HD
        if image.get_size() != (1920, 1080):
            scaled_image = pygame.transform.smoothscale(image, (1920, 1080))
        else:
            scaled_image = image
        
        # Now scale to fit screen if needed
        img_width, img_height = scaled_image.get_size()
        width_ratio = screen_width / img_width
        height_ratio = screen_height / img_height
        scale_ratio = min(width_ratio, height_ratio)
        
        new_width = int(img_width * scale_ratio)
        new_height = int(img_height * scale_ratio)
        
        scaled_image = pygame.transform.smoothscale(scaled_image, (new_width, new_height))
        
        return scaled_image, screenshot_data
        
    except Exception as e:
        logger.error(f"Error capturing website {url}: {e}")
        return None, None

# Function to upload website screenshot to CouchDB
def upload_website_screenshot(url, screenshot_data):
    """Upload website screenshot to CouchDB and return attachment name"""
    try:
        # Create unique filename based on URL and timestamp
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        timestamp = int(time.time())
        filename = f"website_{timestamp}_{url_hash}.png"
        
        # Upload to CouchDB
        upload_url = f"{couchdb_url}/slideshows/{tv_uuid}/{filename}"
        
        # Get current document revision
        doc_response = requests.get(f"{couchdb_url}/slideshows/{tv_uuid}", timeout=5)
        if doc_response.status_code == 200:
            current_rev = doc_response.json().get('_rev')
            upload_url += f"?rev={current_rev}"
            
            response = requests.put(upload_url, 
                                  data=screenshot_data,
                                  headers={'Content-Type': 'image/png'},
                                  timeout=10)
            
            if response.status_code in [200, 201]:
                logger.info(f"Successfully uploaded website screenshot: {filename}")
                return filename
            else:
                logger.error(f"Failed to upload website screenshot: {response.status_code}")
                return None
        else:
            logger.error(f"Failed to get document revision for website upload")
            return None
            
    except Exception as e:
        logger.error(f"Error uploading website screenshot: {e}")
        return None

# Function to handle video content
def process_video(video_name):
    """Process video file and return video capture object"""
    try:
        # Download video from CouchDB to temporary file
        url = f"{couchdb_url}/slideshows/{tv_uuid}/{video_name}"
        headers = {'Cache-Control': 'no-store'}
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            # Save to temporary file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
            temp_file.write(response.content)
            temp_file.close()
            
            # Open with OpenCV
            cap = cv2.VideoCapture(temp_file.name)
            
            if cap.isOpened():
                return cap, temp_file.name
            else:
                logger.error(f"Failed to open video: {video_name}")
                os.unlink(temp_file.name)
                return None, None
        else:
            logger.error(f"HTTP error {response.status_code} fetching video {video_name}")
            return None, None
            
    except Exception as e:
        logger.error(f"Error processing video {video_name}: {e}")
        return None, None

# Function to convert OpenCV frame to pygame surface
def cv2_to_pygame(cv2_frame):
    """Convert OpenCV frame to pygame surface"""
    try:
        # Convert BGR to RGB
        rgb_frame = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
        # Rotate 90 degrees and flip for correct orientation
        rgb_frame = np.rot90(rgb_frame)
        rgb_frame = np.flipud(rgb_frame)
        # Create pygame surface
        surface = pygame.surfarray.make_surface(rgb_frame)
        return surface
    except Exception as e:
        logger.error(f"Error converting cv2 frame to pygame: {e}")
        return None

# Background thread for website capture
def website_capture_worker():
    """Background worker to capture website screenshots"""
    while True:
        try:
            with capture_lock:
                if capture_queue:
                    slide_data = capture_queue.pop(0)
                else:
                    slide_data = None
            
            if slide_data:
                url = slide_data.get('url')
                if url:
                    # Check if we need to refresh this URL (not in cache or stale)
                    needs_refresh = True
                    if url in website_cache:
                        cached = website_cache[url]
                        if time.time() - cached['timestamp'] < 60:  # Less than 1 minute old
                            needs_refresh = False
                    
                    if needs_refresh:
                        logger.info(f"Pre-capturing website: {url}")
                        surface, screenshot_data = capture_website(url)
                        if surface and screenshot_data:
                            # Upload to CouchDB
                            filename = upload_website_screenshot(url, screenshot_data)
                            if filename:
                                website_cache[url] = {
                                    'surface': surface,
                                    'filename': filename,
                                    'timestamp': time.time()
                                }
            
            time.sleep(1)  # Check queue every second
            
        except Exception as e:
            logger.error(f"Error in website capture worker: {e}")
            time.sleep(5)

# Start website capture worker thread
threading.Thread(target=website_capture_worker, daemon=True).start()

# Function to queue website for pre-capture
def queue_website_capture(slides_list, current_index):
    """Queue upcoming website slides for pre-capture"""
    try:
        with capture_lock:
            # Look ahead 1-2 slides
            for i in range(1, min(3, len(slides_list) - current_index)):
                next_index = (current_index + i) % len(slides_list)
                next_slide = slides_list[next_index]
                
                if next_slide.get('type') == 'website':
                    url = next_slide.get('url')
                    if url and url not in website_cache:
                        # Check if already queued
                        if not any(item.get('url') == url for item in capture_queue):
                            capture_queue.append({'url': url})
                            logger.info(f"Queued website for capture: {url}")
    except Exception as e:
        logger.error(f"Error queuing website capture: {e}")

# Function to fetch the slideshow document from CouchDB
def fetch_document():
    try:
        url = f"{couchdb_url}/slideshows/{tv_uuid}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            logger.info("Successfully fetched document")
            return response.json()
        elif response.status_code == 404:
            logger.warning("Document not found")
            return None
        else:
            raise Exception(f"HTTP error {response.status_code}")
    except requests.RequestException as e:
        logger.error(f"Error fetching document: {e}")
        return None

# Function to fetch and process content from CouchDB attachments
def fetch_content(slide_doc, text_params=None):
    """Fetch and process content (image/video/website) with text overlay"""
    content_type = slide_doc.get('type', 'image')
    content_name = slide_doc.get('name')
    
    if content_type == 'website':
        url = slide_doc.get('url')
        if not url:
            logger.error(f"Website slide missing URL: {slide_doc}")
            return None, None, None, None
            
        # Check cache first
        if url in website_cache:
            cached = website_cache[url]
            # Use cached version if less than 1 minute old for more frequent updates
            if time.time() - cached['timestamp'] < 60:
                image = cached['surface']
                # Process text overlay if needed
                if text_params and text_params.get('text'):
                    text_surface, text_rect = process_text_overlay(image, text_params)
                    return image, text_surface, text_rect, cached['filename']
                return image, None, None, cached['filename']
            else:
                # Remove stale cache entry
                logger.info(f"Removing stale cache entry for: {url}")
                del website_cache[url]
        
        # Capture fresh screenshot
        logger.info(f"Capturing fresh website screenshot: {url}")
        surface, screenshot_data = capture_website(url)
        if surface and screenshot_data:
            filename = upload_website_screenshot(url, screenshot_data)
            if filename:
                website_cache[url] = {
                    'surface': surface,
                    'filename': filename,
                    'timestamp': time.time()
                }
                image = surface
                content_name = filename
            else:
                # Use surface without uploading
                image = surface
                content_name = f"website_{int(time.time())}.png"
        else:
            # Try to use cached version as fallback
            if url in website_cache:
                cached = website_cache[url]
                logger.warning(f"Using cached website screenshot as fallback: {url}")
                image = cached['surface']
                content_name = cached['filename']
            else:
                logger.error(f"Failed to capture website and no cache available: {url}")
                return None, None, None, None
                
    elif content_type == 'video':
        logger.error(f"Video content should be handled separately: {content_name}")
        return None, None, None, None
        
    else:  # Default to image
        try:
            url = f"{couchdb_url}/slideshows/{tv_uuid}/{content_name}"
            headers = {'Cache-Control': 'no-store'}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                image_data = BytesIO(response.content)
                image = pygame.image.load(image_data)
                
                img_width, img_height = image.get_size()
                
                width_ratio = screen_width / img_width
                height_ratio = screen_height / img_height
                scale_ratio = min(width_ratio, height_ratio)
                
                new_width = int(img_width * scale_ratio)
                new_height = int(img_height * scale_ratio)
                
                image = pygame.transform.smoothscale(image, (new_width, new_height))
            else:
                logger.error(f"HTTP error {response.status_code} fetching content {content_name}")
                return None, None, None, None
        except Exception as e:
            logger.error(f"Error fetching content {content_name}: {e}")
            return None, None, None, None

    # Process text overlay
    if text_params and text_params.get('text'):
        text_surface, text_rect = process_text_overlay(image, text_params)
        return image, text_surface, text_rect, content_name
    
    return image, None, None, content_name

# Function to process text overlay
def process_text_overlay(image, text_params):
    """Process text overlay for any content type"""
    try:
        text_content = text_params['text']
        if '{datetime}' in text_content:
            text_content = text_content.replace('{datetime}', datetime.now().strftime("%Y-%m-%d %H:%M"))

        font_size_map = {"small": 24, "medium": 36, "large": 48}
        actual_font_size = font_size_map.get(text_params.get('text_size', 'medium'), 36)
        
        try:
            font = pygame.font.Font("freesansbold.ttf", actual_font_size)
        except IOError:
            font = pygame.font.Font(None, actual_font_size)
        
        text_color_hex = text_params.get('text_color', '#FFFFFF')
        text_color_rgb = pygame.Color(text_color_hex)
        
        text_surface = font.render(text_content, True, text_color_rgb)
        
        text_pos_key = text_params.get('text_position', 'bottom-center')
        text_rect = text_surface.get_rect()

        # Calculate position relative to image
        img_width, img_height = image.get_size()
        padding = 10

        if text_pos_key == 'top-left':
            text_rect.topleft = (padding, padding)
        elif text_pos_key == 'top-center':
            text_rect.midtop = (img_width // 2, padding)
        elif text_pos_key == 'top-right':
            text_rect.topright = (img_width - padding, padding)
        elif text_pos_key == 'center-left':
            text_rect.midleft = (padding, img_height // 2)
        elif text_pos_key == 'center':
            text_rect.center = (img_width // 2, img_height // 2)
        elif text_pos_key == 'center-right':
            text_rect.midright = (img_width - padding, img_height // 2)
        elif text_pos_key == 'bottom-left':
            text_rect.bottomleft = (padding, img_height - padding)
        elif text_pos_key == 'bottom-center':
            text_rect.midbottom = (img_width // 2, img_height - padding)
        elif text_pos_key == 'bottom-right':
            text_rect.bottomright = (img_width - padding, img_height - padding)
        else:
            text_rect.midbottom = (img_width // 2, img_height - padding)
        
        # Handle background color
        text_bg_color_hex = text_params.get('text_background_color')
        surface_to_return = text_surface
        
        if text_bg_color_hex and text_bg_color_hex.strip():
            try:
                text_bg_color_rgb = pygame.Color(text_bg_color_hex)
                bg_padding = 5
                
                surface_with_background = pygame.Surface(
                    (text_surface.get_width() + 2 * bg_padding, text_surface.get_height() + 2 * bg_padding),
                    pygame.SRCALPHA
                )
                surface_with_background.fill(text_bg_color_rgb)
                surface_with_background.blit(text_surface, (bg_padding, bg_padding))
                surface_to_return = surface_with_background
                
                # Adjust position for background
                text_rect.x -= bg_padding
                text_rect.y -= bg_padding
            except ValueError as ve:
                logger.error(f"Invalid text_background_color: {text_bg_color_hex} - {ve}")
        
        return surface_to_return, text_rect
        
    except Exception as e:
        logger.error(f"Error processing text overlay: {e}")
        return None, None

# Function to update TV status document in CouchDB
def update_tv_status(couchdb_base_url, tv_doc_uuid, current_slide_info):
    status_doc_id = f"status_{tv_doc_uuid}"
    status_doc_url = f"{couchdb_base_url}/slideshows/{status_doc_id}"

    current_rev = None
    try:
        response = requests.get(status_doc_url, timeout=5)
        if response.status_code == 200:
            current_rev = response.json().get('_rev')
        elif response.status_code != 404:
            logger.warning(f"Error fetching status doc {status_doc_id}: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching status doc {status_doc_id}: {e}")

    status_data = {
        "type": "tv_status",
        "tv_uuid": tv_doc_uuid,
        "current_slide_id": current_slide_info['id'],
        "current_slide_filename": current_slide_info['filename'],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    if current_rev:
        status_data['_rev'] = current_rev

    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.put(status_doc_url, json=status_data, headers=headers, timeout=5)
        if response.status_code not in [200, 201]:
            logger.error(f"Error updating status doc {status_doc_id}: {response.status_code} - {response.text}")
        else:
            logger.info(f"Successfully updated status for {tv_doc_uuid} to slide {current_slide_info['filename']}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error updating status doc {status_doc_id}: {e}")

# Background thread to watch for changes in CouchDB
def watch_changes():
    while True:
        try:
            url = f"{couchdb_url}/slideshows/_changes"
            params = {
                "feed": "continuous",
                "heartbeat": 10000,
                "doc_ids": json.dumps([tv_uuid])
            }
            response = requests.get(url, params=params, stream=True, timeout=10)
            for line in response.iter_lines():
                if line:
                    change = json.loads(line)
                    if 'id' in change and change['id'] == tv_uuid:
                        logger.info("Change detected, setting need_refetch")
                        need_refetch.set()
        except requests.RequestException as e:
            logger.error(f"Error in changes feed: {e}")
            time.sleep(30)

# Start the background thread to watch for changes
threading.Thread(target=watch_changes, daemon=True).start()

FADE_STEPS = 30

# Function to process slides from document
def process_slides_from_doc(doc):
    """Process slides from document and return processed slide list"""
    processed_slides = []
    
    for slide_doc in doc.get('slides', []):
        content_type = slide_doc.get('type', 'image')
        
        if content_type == 'video':
            # Handle video slides
            video_cap, temp_file = process_video(slide_doc['name'])
            if video_cap:
                processed_slides.append({
                    'type': 'video',
                    'video_cap': video_cap,
                    'temp_file': temp_file,
                    'duration': slide_doc.get('duration', 10),
                    'id': slide_doc['name'],
                    'filename': slide_doc['name'],
                    'text_params': {
                        'text': slide_doc.get('text'),
                        'text_color': slide_doc.get('text_color'),
                        'text_size': slide_doc.get('text_size'),
                        'text_position': slide_doc.get('text_position'),
                        'text_background_color': slide_doc.get('text_background_color', None)
                    },
                    'transition_time': slide_doc.get('transition_time', 0),
                    'scroll_text': slide_doc.get('scroll_text', False)
                })
        else:
            # Handle image and website slides
            text_params = {
                'text': slide_doc.get('text'),
                'text_color': slide_doc.get('text_color'),
                'text_size': slide_doc.get('text_size'),
                'text_position': slide_doc.get('text_position'),
                'text_background_color': slide_doc.get('text_background_color', None)
            }
            
            image_surface, text_surface, text_rect, content_name = fetch_content(slide_doc, text_params)
            
            if image_surface:
                processed_slides.append({
                    'type': content_type,
                    'image': image_surface,
                    'text_surface': text_surface,
                    'text_rect': text_rect,
                    'duration': slide_doc.get('duration', 10),
                    'id': slide_doc['name'],
                    'filename': content_name or slide_doc['name'],
                    'text_params': text_params,
                    'transition_time': slide_doc.get('transition_time', 0),
                    'scroll_text': slide_doc.get('scroll_text', False),
                    'url': slide_doc.get('url')  # For website slides
                })
    
    return processed_slides

# Main loop
while True:
    if state == "connecting":
        screen.fill((0, 0, 0))
        font = pygame.font.SysFont(None, 24)
        text = font.render("Connecting to server...", True, (255, 255, 255))
        text_rect = text.get_rect(center=(screen_width / 2, screen_height / 2))
        screen.blit(text, text_rect)
        pygame.display.flip()
        doc = fetch_document()
        if doc is not None:
            if doc:
                slides = process_slides_from_doc(doc)
                if slides:
                    state = "slideshow"
                    first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                    update_tv_status(couchdb_url, tv_uuid, first_slide_info)
                else:
                    state = "default"
            else:
                state = "default"
        else:
            time.sleep(30)
    elif state == "default":
        message = f"This TV is not configured. Please add it in the Slideshow Manager at {manager_url} with UUID: {tv_uuid}."
        screen.fill((0, 0, 0))
        font = pygame.font.SysFont(None, 24)
        text = font.render(message, True, (255, 255, 255))
        text_rect = text.get_rect(center=(screen_width / 2, screen_height / 2))
        screen.blit(text, text_rect)
        pygame.display.flip()
        if need_refetch.is_set():
            need_refetch.clear()
            doc = fetch_document()
            if doc:
                slides = process_slides_from_doc(doc)
                if slides:
                    state = "slideshow"
                    first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                    update_tv_status(couchdb_url, tv_uuid, first_slide_info)
            time.sleep(1)
    elif state == "slideshow":
        for slide_index, slide_data in enumerate(slides):
            # Queue upcoming websites for pre-capture
            queue_website_capture(slides, slide_index)
            
            current_display_slide_info = {'id': slide_data['id'], 'filename': slide_data['filename']}
            update_tv_status(couchdb_url, tv_uuid, current_display_slide_info)

            if slide_data['type'] == 'video':
                # Handle video playback
                video_cap = slide_data['video_cap']
                start_time = time.time()
                slide_duration = slide_data.get('duration', 10)
                
                while time.time() - start_time < slide_duration:
                    if need_refetch.is_set():
                        break
                        
                    ret, frame = video_cap.read()
                    if not ret:
                        # Loop video
                        video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = video_cap.read()
                        if not ret:
                            break
                    
                    # Convert frame to pygame surface
                    surface = cv2_to_pygame(frame)
                    if surface:
                        # Scale to fit screen
                        frame_width, frame_height = surface.get_size()
                        width_ratio = screen_width / frame_width
                        height_ratio = screen_height / frame_height
                        scale_ratio = min(width_ratio, height_ratio)
                        
                        new_width = int(frame_width * scale_ratio)
                        new_height = int(frame_height * scale_ratio)
                        
                        scaled_surface = pygame.transform.smoothscale(surface, (new_width, new_height))
                        
                        # Center on screen
                        center_x = (screen_width - new_width) // 2
                        center_y = (screen_height - new_height) // 2
                        
                        screen.fill((0, 0, 0))
                        screen.blit(scaled_surface, (center_x, center_y))
                        
                        # Add text overlay if needed
                        if slide_data.get('text_surface') and slide_data.get('text_rect'):
                            text_surface = slide_data['text_surface']
                            text_rect = slide_data['text_rect']
                            screen.blit(text_surface, (center_x + text_rect.left, center_y + text_rect.top))
                        
                        pygame.display.flip()
                    
                    # Check for events
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            pygame.quit()
                            sys.exit()
                        if event.type == pygame.KEYDOWN:
                            if event.key == pygame.K_ESCAPE:
                                pygame.quit()
                                sys.exit()
                    
                    time.sleep(0.03)  # ~30 FPS
                
                # Clean up video
                video_cap.release()
                if slide_data.get('temp_file'):
                    try:
                        os.unlink(slide_data['temp_file'])
                    except:
                        pass
                        
            else:
                # Handle image/website slides (existing logic)
                img_width, img_height = slide_data['image'].get_size()
                center_x = (screen_width - img_width) // 2
                center_y = (screen_height - img_height) // 2

                scroll_x = screen_width
                
                # Fade-In Logic
                incoming_transition_duration_ms = slide_data.get('transition_time', 0)
                if incoming_transition_duration_ms > 0:
                    delay_per_step = (incoming_transition_duration_ms / FADE_STEPS) / 1000.0
                    
                    slide_render_surface = pygame.Surface((img_width, img_height), pygame.SRCALPHA)
                    slide_render_surface.blit(slide_data['image'], (0,0))
                    if slide_data.get('text_surface') and not slide_data.get('scroll_text'):
                        slide_render_surface.blit(slide_data['text_surface'], slide_data['text_rect'])

                    for alpha_step in range(FADE_STEPS + 1):
                        if need_refetch.is_set(): break
                        
                        alpha_value = int((alpha_step / FADE_STEPS) * 255)
                        slide_render_surface.set_alpha(alpha_value)
                        
                        screen.fill((0,0,0))
                        screen.blit(slide_render_surface, (center_x, center_y))
                        pygame.display.flip()
                        time.sleep(delay_per_step)
                    if need_refetch.is_set(): continue

                start_time = time.time()
                slide_duration = slide_data.get('duration', 10)
                if not isinstance(slide_duration, (int, float)) or slide_duration <= 0:
                    logger.warning(f"Invalid duration for slide {slide_data.get('filename', 'Unknown')}: '{slide_duration}'. Defaulting to 10s.")
                    slide_duration = 10

                # Main display loop for the slide
                while time.time() - start_time < slide_duration:
                    if need_refetch.is_set():
                        need_refetch.clear()
                        doc = fetch_document()
                        if doc:
                            new_slides = process_slides_from_doc(doc)
                            slides = new_slides
                            if not slides:
                                state = "default"
                            else:
                                first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                                update_tv_status(couchdb_url, tv_uuid, first_slide_info)
                                scroll_x = screen_width
                                slide_data = slides[0]
                                start_time = time.time()
                                slide_duration = slide_data.get('duration', 10)
                                if not isinstance(slide_duration, (int, float)) or slide_duration <= 0:
                                    slide_duration = 10
                        else:
                            state = "default"
                        
                        if state == "default":
                            break
                    
                    if state == "default":
                        break

                    screen.fill((0, 0, 0))
                    screen.blit(slide_data['image'], (center_x, center_y))

                    # Render text
                    if slide_data.get('text_surface') and slide_data.get('text_rect'):
                        text_surface = slide_data['text_surface']
                        original_text_rect = slide_data['text_rect']
                        
                        if slide_data.get('scroll_text'):
                            screen.blit(text_surface, (scroll_x, center_y + original_text_rect.top))
                            scroll_x -= 2
                            if scroll_x < -text_surface.get_width():
                                scroll_x = screen_width
                        else:
                            screen.blit(text_surface, (center_x + original_text_rect.left, center_y + original_text_rect.top))
                    
                    pygame.display.flip()
                    
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            pygame.quit()
                            sys.exit()
                        if event.type == pygame.KEYDOWN:
                            if event.key == pygame.K_ESCAPE:
                                pygame.quit()
                                sys.exit()
                    
                    time.sleep(0.03)

                if need_refetch.is_set(): break

                # Fade-Out Logic
                outgoing_transition_duration_ms = slide_data.get('transition_time', 0)
                if outgoing_transition_duration_ms > 0 and slides and not need_refetch.is_set():
                    current_screen_snapshot = screen.copy()
                    delay_per_step = (outgoing_transition_duration_ms / FADE_STEPS) / 1000.0
                    
                    for alpha_step in range(FADE_STEPS, -1, -1):
                        if need_refetch.is_set(): break
                        
                        alpha_value = int((alpha_step / FADE_STEPS) * 255)
                        current_screen_snapshot.set_alpha(alpha_value)
                        
                        screen.fill((0,0,0))
                        screen.blit(current_screen_snapshot, (0,0))
                        pygame.display.flip()
                        time.sleep(delay_per_step)
                    
                    if not need_refetch.is_set():
                        screen.fill((0,0,0))
                        pygame.display.flip()

            if state == "default" or need_refetch.is_set():
                break