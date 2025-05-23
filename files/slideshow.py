import pygame
import requests
import json
import time
import threading
import configparser
import logging
from io import BytesIO
from datetime import datetime, timezone # Added timezone

import sys # For sys.exit

# Define Configuration Path
CONFIG_FILE_PATH = '/etc/slideshow.conf'

# Function to load configuration
def load_config(config_path):
    config = configparser.ConfigParser()
    if not config.read(config_path):
        # This specific check might be tricky if read returns an empty list on success with empty file
        # A better check is usually for specific sections/options after attempting to read.
        # However, if the file is critical and not found, config.read returns an empty list.
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
    manager_url = config.get('settings', 'manager_url') # Used in default message
except configparser.NoOptionError as e:
    logging.error(f"Critical: Missing essential configuration in {CONFIG_FILE_PATH}: {e}")
    print(f"Critical: Missing essential configuration in {CONFIG_FILE_PATH}: {e}", file=sys.stderr)
    sys.exit(1)

# Read optional settings (example for office hours, not currently used in logic but shows pattern)
office_start_time_str = config.get('settings', 'office_start_time', fallback=None)
office_end_time_str = config.get('settings', 'office_end_time', fallback=None)


# Custom logging handler to send logs to CouchDB
# Set up logging
# Basic file logger first
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('/var/log/slideshow.log')])
logger = logging.getLogger() # Get root logger


# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
screen_width, screen_height = screen.get_size()

# State variables
state = "connecting"
slides = []
need_refetch = threading.Event()

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

# Function to fetch and process an image from CouchDB attachments
def fetch_image(image_name, text_params=None): # scroll_text_flag removed
    try:
        url = f"{couchdb_url}/slideshows/{tv_uuid}/{image_name}"
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
            
            scaled_image = pygame.transform.smoothscale(image, (new_width, new_height))

            if text_params and text_params.get('text'):
                try:
                    text_content = text_params['text']
                    if '{datetime}' in text_content: # Handle datetime placeholder
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

                    # Calculate (x,y) for text_surface on scaled_image (new_width, new_height)
                    padding = 10 # General padding from edges

                    if text_pos_key == 'top-left':
                        text_rect.topleft = (padding, padding)
                    elif text_pos_key == 'top-center':
                        text_rect.midtop = (new_width // 2, padding)
                    elif text_pos_key == 'top-right':
                        text_rect.topright = (new_width - padding, padding)
                    elif text_pos_key == 'center-left':
                        text_rect.midleft = (padding, new_height // 2)
                    elif text_pos_key == 'center':
                        text_rect.center = (new_width // 2, new_height // 2)
                    elif text_pos_key == 'center-right':
                        text_rect.midright = (new_width - padding, new_height // 2)
                    elif text_pos_key == 'bottom-left':
                        text_rect.bottomleft = (padding, new_height - padding)
                    elif text_pos_key == 'bottom-center':
                        text_rect.midbottom = (new_width // 2, new_height - padding)
                    elif text_pos_key == 'bottom-right':
                        text_rect.bottomright = (new_width - padding, new_height - padding)
                    else: # Default to bottom-center
                        text_rect.midbottom = (new_width // 2, new_height - padding)
                    
                    text_bg_color_hex = text_params.get('text_background_color')
                    surface_to_blit_or_return = text_surface # Default to just the text
                    blit_pos_for_static = text_rect.topleft # Default blit pos for non-backgrounded text
                    
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
                            surface_to_blit_or_return = surface_with_background
                            
                            # Adjusted blit position for the backgrounded surface
                            blit_pos_for_static = (text_rect.left - bg_padding, text_rect.top - bg_padding)
                        except ValueError as ve:
                            logger.error(f"Invalid text_background_color: {text_bg_color_hex} - {ve}")
                            # Fallback to text_surface without background, blit_pos_for_static remains text_rect.topleft
                    
                    # Function now returns the scaled_image and text_surface/text_rect separately.
                    # No blitting of text onto scaled_image happens here.
                    return scaled_image, surface_to_blit_or_return, text_rect
                        
                except Exception as e: # Catches errors in text rendering part
                    logger.error(f"Error rendering text: {e}")
                    # Fallback: return base image, no text components
                    return scaled_image, None, None

            # No text defined in text_params (text_params was None or text_params.get('text') was falsey)
            return scaled_image, None, None # Return base image, no text components
        else: # HTTP error when fetching image
            logger.error(f"HTTP error {response.status_code} fetching image {image_name}")
            return None, None, None # Indicate image fetch failure and no text components
    except requests.RequestException as e: # Network error when fetching image
        logger.error(f"Network error fetching image {image_name}: {e}")
        return None, None, None # Indicate image fetch failure and no text components
    except Exception as e: # Other unexpected errors (e.g. Pygame image load, font not found)
        logger.error(f"Unexpected error fetching/processing image {image_name}: {e}")
        return None, None, None # Indicate image fetch failure and no text components

# Function to update TV status document in CouchDB
def update_tv_status(couchdb_base_url, tv_doc_uuid, current_slide_info):
    status_doc_id = f"status_{tv_doc_uuid}"
    status_doc_url = f"{couchdb_base_url}/slideshows/{status_doc_id}" # Assumes 'slideshows' is the DB name

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
        "current_slide_id": current_slide_info['id'], # Filename used as ID for slide item
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

FADE_STEPS = 30 # Number of steps for fade transitions

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
            if doc: # Document exists and was fetched
                slides = []
                for slide_doc in doc.get('slides', []): # Ensure 'slides' key exists
                    text_params = {
                        'text': slide_doc.get('text'),
                        'text_color': slide_doc.get('text_color'),
                        'text_size': slide_doc.get('text_size'),
                        'text_position': slide_doc.get('text_position'),
                        'text_background_color': slide_doc.get('text_background_color', None)
                    }
                    # scroll_text_flag removed from fetch_image call
                    image_surface, text_surface_for_slide, text_rect_for_slide = fetch_image(
                        slide_doc['name'],
                        text_params
                    )
                    if image_surface: # Only proceed if image itself was loaded
                        slides.append({
                            'image': image_surface, # Pure image
                            'text_surface': text_surface_for_slide, # Text surface (or None)
                            'text_rect': text_rect_for_slide,       # Text rect (or None)
                            'duration': slide_doc.get('duration', 10),
                            'id': slide_doc['name'],
                            'filename': slide_doc['name'],
                            'text_params': text_params, # Keep for reference
                            'transition_time': slide_doc.get('transition_time', 0),
                            'scroll_text': slide_doc.get('scroll_text', False) # Keep for main loop logic
                        })
                if slides:
                    state = "slideshow"
                    # Report status for the first slide
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
            if doc: # Document exists and was fetched
                slides = []
                for slide_doc in doc.get('slides', []): # Ensure 'slides' key exists
                    text_params = {
                        'text': slide_doc.get('text'),
                        'text_color': slide_doc.get('text_color'),
                        'text_size': slide_doc.get('text_size'),
                        'text_position': slide_doc.get('text_position'),
                        'text_background_color': slide_doc.get('text_background_color', None)
                    }
                    # scroll_text_flag removed from fetch_image call
                    image_surface, text_surface_for_slide, text_rect_for_slide = fetch_image(
                        slide_doc['name'],
                        text_params
                    )
                    if image_surface: # Only proceed if image itself was loaded
                        slides.append({
                            'image': image_surface, # Pure image
                            'text_surface': text_surface_for_slide, # Text surface (or None)
                            'text_rect': text_rect_for_slide,       # Text rect (or None)
                            'duration': slide_doc.get('duration', 10),
                            'id': slide_doc['name'],
                            'filename': slide_doc['name'],
                            'text_params': text_params, # Keep for reference
                            'transition_time': slide_doc.get('transition_time', 0),
                            'scroll_text': slide_doc.get('scroll_text', False) # Keep for main loop logic
                        })
                if slides:
                    state = "slideshow"
                    # Report status for the first slide
                    first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                    update_tv_status(couchdb_url, tv_uuid, first_slide_info)
            time.sleep(1)
    elif state == "slideshow":
        for slide_index, slide_data in enumerate(slides):
            current_display_slide_info = {'id': slide_data['id'], 'filename': slide_data['filename']}
            update_tv_status(couchdb_url, tv_uuid, current_display_slide_info)

            # Calculate centered position for the base image
            img_width, img_height = slide_data['image'].get_size()
            center_x = (screen_width - img_width) // 2
            center_y = (screen_height - img_height) // 2

            scroll_x = screen_width # Still screen-wide for scrolling text initiation
            
            # Fade-In Logic for the current slide
            incoming_transition_duration_ms = slide_data.get('transition_time', 0)
            if incoming_transition_duration_ms > 0:
                delay_per_step = (incoming_transition_duration_ms / FADE_STEPS) / 1000.0
                
                # Prepare the surface to fade in (base image + static text)
                # This surface is the size of the image itself.
                slide_render_surface = pygame.Surface((img_width, img_height), pygame.SRCALPHA)
                slide_render_surface.blit(slide_data['image'], (0,0)) # Blit image at (0,0) on this smaller surface
                if slide_data.get('text_surface') and not slide_data.get('scroll_text'):
                    # text_rect is relative to the image, so blit directly onto slide_render_surface
                    slide_render_surface.blit(slide_data['text_surface'], slide_data['text_rect'])

                for alpha_step in range(FADE_STEPS + 1):
                    if need_refetch.is_set(): break
                    
                    alpha_value = int((alpha_step / FADE_STEPS) * 255)
                    slide_render_surface.set_alpha(alpha_value)
                    
                    screen.fill((0,0,0))
                    # Blit the prepared surface (image + static text) at the centered position
                    screen.blit(slide_render_surface, (center_x, center_y))
                    pygame.display.flip()
                    time.sleep(delay_per_step)
                if need_refetch.is_set(): continue # Skip to next iteration of outer loop if refetch occurred

            start_time = time.time()
            slide_duration = slide_data.get('duration', 10)
            if not isinstance(slide_duration, (int, float)) or slide_duration <= 0:
                logger.warning(f"Invalid or missing duration for slide {slide_data.get('filename', 'Unknown')}: '{slide_duration}'. Defaulting to 10s.")
                slide_duration = 10

            # Main display loop for the slide
            while time.time() - start_time < slide_duration:
                if need_refetch.is_set():
                    need_refetch.clear()
                    doc = fetch_document()
                    if doc: # Document exists and was fetched
                        new_slides_temp = []
                        for s_doc in doc.get('slides', []): # Ensure 'slides' key exists
                            text_params = {
                                'text': s_doc.get('text'),
                                'text_color': s_doc.get('text_color'),
                                'text_size': s_doc.get('text_size'),
                                'text_position': s_doc.get('text_position'),
                                'text_background_color': s_doc.get('text_background_color', None)
                            }
                            # scroll_text_flag removed from fetch_image call
                            image_surface, text_surface_for_slide, text_rect_for_slide = fetch_image(
                                s_doc['name'],
                                text_params
                            )
                            if image_surface: # Only proceed if image itself was loaded
                                new_slides_temp.append({
                                    'image': image_surface, # Pure image
                                    'text_surface': text_surface_for_slide, # Text surface (or None)
                                    'text_rect': text_rect_for_slide,       # Text rect (or None)
                                    'duration': s_doc.get('duration', 10),
                                    'id': s_doc['name'],
                                    'filename': s_doc['name'],
                                    'text_params': text_params, # Keep for reference
                                    'transition_time': s_doc.get('transition_time', 0),
                                    'scroll_text': s_doc.get('scroll_text', False) # Keep for main loop logic
                                })
                        slides = new_slides_temp # Update slides list
                        if not slides:
                            state = "default" # No slides, go to default state
                        else:
                            # Report status for the first slide of the new set if slideshow continues
                            first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                            update_tv_status(couchdb_url, tv_uuid, first_slide_info)
                            # Important: Reset scroll_x for the new first slide
                            scroll_x = screen_width 
                            # Also reset the timer and current slide_data to the new first slide
                            slide_data = slides[0] # This might be problematic if the outer loop isn't broken correctly
                                                   # The outer loop should break and restart to handle this properly.
                                                   # For now, this will make the current iteration use the new slide 0.
                            start_time = time.time()
                            slide_duration = slide_data.get('duration', 10)
                            if not isinstance(slide_duration, (int, float)) or slide_duration <= 0:
                                logger.warning(f"Invalid or missing duration for new slide {slide_data.get('filename', 'Unknown')}: '{slide_duration}'. Defaulting to 10s.")
                                slide_duration = 10

                    else: # doc fetch failed
                        state = "default" # Go to default state if doc fetch fails
                    
                    if state == "default": # Break from inner while loop if state changed
                        break 
                
                if state == "default": # Check again if state changed due to refetch
                    break # Break from inner while loop (this slide's duration loop)

                screen.fill((0, 0, 0)) # Ensure black background
                # Blit the pure base image at the centered position
                screen.blit(slide_data['image'], (center_x, center_y))

                # Render text (static or scrolling), adjusting for centered image
                if slide_data.get('text_surface') and slide_data.get('text_rect'):
                    text_surface_to_render = slide_data['text_surface']
                    original_text_rect = slide_data['text_rect'] # This is relative to image (0,0)
                    
                    if slide_data.get('scroll_text'):
                        # Scrolling text: X is scroll_x (screen-wide), Y is relative to centered image top
                        screen.blit(text_surface_to_render, (scroll_x, center_y + original_text_rect.top))
                        scroll_x -= 2 # Adjust scroll speed as needed
                        if scroll_x < -text_surface_to_render.get_width():
                            scroll_x = screen_width
                    else: # Static text
                        # Static text: Position is relative to centered image's top-left
                        screen.blit(text_surface_to_render, (center_x + original_text_rect.left, center_y + original_text_rect.top))
                
                pygame.display.flip()
                # Check for Pygame events (like quit)
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        pygame.quit()
                        sys.exit()
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_ESCAPE: # Allow exiting with ESC
                           pygame.quit()
                           sys.exit()
                
                time.sleep(0.03) # Shorter sleep for smoother scrolling

            if need_refetch.is_set(): break # Break from main slide loop if refetch needed during duration

            # Fade-Out Logic for the current slide
            outgoing_transition_duration_ms = slide_data.get('transition_time', 0)
            if outgoing_transition_duration_ms > 0 and slides and not need_refetch.is_set(): # ensure slides not empty
                current_screen_snapshot = screen.copy() # Capture the final state of the current slide
                delay_per_step = (outgoing_transition_duration_ms / FADE_STEPS) / 1000.0
                
                for alpha_step in range(FADE_STEPS, -1, -1):
                    if need_refetch.is_set(): break
                    
                    alpha_value = int((alpha_step / FADE_STEPS) * 255)
                    current_screen_snapshot.set_alpha(alpha_value)
                    
                    screen.fill((0,0,0)) # Fill with black before blitting semi-transparent surface
                    screen.blit(current_screen_snapshot, (0,0))
                    pygame.display.flip()
                    time.sleep(delay_per_step)
                
                if not need_refetch.is_set(): # Ensure screen is black if fade completed fully
                    screen.fill((0,0,0))
                    pygame.display.flip()

            if state == "default" or need_refetch.is_set(): # If state changed or refetch needed, break from outer for loop
                break
