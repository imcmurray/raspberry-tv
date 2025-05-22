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
# Initialize logger early for load_config issues, but CouchDBHandler needs couchdb_url
# We will re-initialize logging after config is loaded if CouchDBHandler is to be used.
# For now, basic logging before this point will go to console if logger is touched.

class CouchDBHandler(logging.Handler):
    def __init__(self, couchdb_url_param, db_name, tv_uuid_param):
        super().__init__()
        self.couchdb_url = couchdb_url_param
        self.db_name = db_name
        self.tv_uuid = tv_uuid_param
    def __init__(self, couchdb_url_param, db_name, tv_uuid_param): # Renamed params to avoid clash
        super().__init__()
        self.couchdb_url = couchdb_url_param
        self.db_name = db_name
        self.tv_uuid = tv_uuid_param

    def emit(self, record):
        log_entry = self.format(record)
        doc = {
            "tv_uuid": self.tv_uuid, # Uses the instance variable
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": log_entry
        }
        try:
            # Uses the instance variable self.couchdb_url
            response = requests.post(f"{self.couchdb_url}/{self.db_name}", json=doc)
            if response.status_code not in (201, 202):
                # Using logger here might be problematic if this handler itself is failing
                print(f"Failed to log to CouchDB: {response.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"Error logging to CouchDB: {e}", file=sys.stderr)

# Set up logging
# Basic file logger first
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('/var/log/slideshow.log')])
logger = logging.getLogger() # Get root logger

# Now add CouchDBHandler if configured
if couchdb_url and tv_uuid: # Ensure essential vars for CouchDBHandler are present
    couchdb_handler = CouchDBHandler(couchdb_url, "logs", tv_uuid)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s') # Already defined by basicConfig, but explicit for handler
    couchdb_handler.setFormatter(formatter)
    logger.addHandler(couchdb_handler)
else:
    logger.warning("CouchDB URL or TV UUID not configured. CouchDB logging will be disabled.")


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
def fetch_image(image_name, text_params=None):
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
                        
                    scaled_image.blit(text_surface, text_rect)
                except Exception as e:
                    logger.error(f"Error rendering text: {e}")

            return scaled_image
        else:
            raise Exception(f"HTTP error {response.status_code}")
    except requests.RequestException as e:
        logger.error(f"Error fetching image: {e}")
        return None

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
                slides = []
                for slide_doc in doc['slides']:
                    text_params = {
                        'text': slide_doc.get('text'),
                        'text_color': slide_doc.get('text_color'),
                        'text_size': slide_doc.get('text_size'),
                        'text_position': slide_doc.get('text_position')
                    }
                    image = fetch_image(slide_doc['name'], text_params)
                    if image:
                        slides.append({
                            'image': image, # Image now has text rendered on it
                            'duration': slide_doc['duration'],
                            'id': slide_doc['name'], # Filename used as ID for slide item
                            'filename': slide_doc['name']
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
            if doc:
                slides = []
                for slide_doc in doc['slides']: # Renamed to slide_doc to avoid confusion
                    text_params = {
                        'text': slide_doc.get('text'),
                        'text_color': slide_doc.get('text_color'),
                        'text_size': slide_doc.get('text_size'),
                        'text_position': slide_doc.get('text_position')
                    }
                    image = fetch_image(slide_doc['name'], text_params)
                    if image:
                        slides.append({
                            'image': image, # Image now has text rendered on it
                            'duration': slide_doc['duration'],
                            'id': slide_doc['name'], # Filename used as ID for slide item
                            'filename': slide_doc['name']
                        })
                if slides:
                    state = "slideshow"
                    # Report status for the first slide
                    first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                    update_tv_status(couchdb_url, tv_uuid, first_slide_info)
            time.sleep(1)
    elif state == "slideshow":
        for slide_data in slides: # slide_data now contains 'id' and 'filename'
            current_display_slide_info = {'id': slide_data['id'], 'filename': slide_data['filename']}
            update_tv_status(couchdb_url, tv_uuid, current_display_slide_info) # Update status before showing

            start_time = time.time()
            while time.time() - start_time < slide_data['duration']:
                if need_refetch.is_set():
                    need_refetch.clear()
                    doc = fetch_document()
                    if doc:
                        new_slides_temp = [] 
                        for s_doc in doc['slides']: 
                            text_params = {
                                'text': s_doc.get('text'),
                                'text_color': s_doc.get('text_color'),
                                'text_size': s_doc.get('text_size'),
                                'text_position': s_doc.get('text_position')
                            }
                            image = fetch_image(s_doc['name'], text_params)
                            if image:
                                new_slides_temp.append({
                                    'image': image,
                                    'duration': s_doc['duration'],
                                    'id': s_doc['name'], 
                                    'filename': s_doc['name']
                                })
                        slides = new_slides_temp 
                        if not slides:
                            state = "default"
                        else:
                            # Report status for the first slide of the new set
                            first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                            update_tv_status(couchdb_url, tv_uuid, first_slide_info)
                    else: 
                        state = "default"
                    
                    if state == "default": # Break from inner while loop if state changed
                        break 
                
                if state == "default": # Check again if state changed due to refetch
                    break # Break from inner while loop

                screen.blit(slide_data['image'], (0, 0))
                pygame.display.flip()
                time.sleep(1) 
            
            if state == "default": # If state changed, break from for loop
                break
