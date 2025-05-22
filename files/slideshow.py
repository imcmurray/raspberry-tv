import pygame
import requests
import json
import time
import threading
import configparser
import logging
from io import BytesIO
from datetime import datetime

# Read configuration from /etc/slideshow.conf
config = configparser.ConfigParser()
config.read('/etc/slideshow.conf')
couchdb_url = config['settings']['couchdb_url']
tv_uuid = config['settings']['tv_uuid']
manager_url = config['settings']['manager_url']

# Custom logging handler to send logs to CouchDB
class CouchDBHandler(logging.Handler):
    def __init__(self, couchdb_url, db_name, tv_uuid):
        super().__init__()
        self.couchdb_url = couchdb_url
        self.db_name = db_name
        self.tv_uuid = tv_uuid

    def emit(self, record):
        log_entry = self.format(record)
        doc = {
            "tv_uuid": self.tv_uuid,
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": log_entry
        }
        try:
            response = requests.post(f"{self.couchdb_url}/{self.db_name}", json=doc)
            if response.status_code not in (201, 202):
                raise Exception(f"Failed to log to CouchDB: {response.status_code}")
        except requests.RequestException as e:
            print(f"Error logging to CouchDB: {e}")

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
couchdb_handler = CouchDBHandler(couchdb_url, "logs", tv_uuid)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
couchdb_handler.setFormatter(formatter)
logger.addHandler(couchdb_handler)
file_handler = logging.FileHandler('/var/log/slideshow.log')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

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

# Function to fetch an image from CouchDB attachments
def fetch_image(image_name):
    try:
        url = f"{couchdb_url}/slideshows/{tv_uuid}/{image_name}"
        headers = {'Cache-Control': 'no-store'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            image_data = BytesIO(response.content)
            image = pygame.image.load(image_data)
            return pygame.transform.scale(image, (screen_width, screen_height))
        else:
            raise Exception(f"HTTP error {response.status_code}")
    except requests.RequestException as e:
        logger.error(f"Error fetching image: {e}")
        return None

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
                for slide in doc['slides']:
                    image = fetch_image(slide['name'])
                    if image:
                        slides.append({
                            'image': image,
                            'text': slide['text'],
                            'text_size': slide['text_size'],
                            'text_color': slide['text_color'],
                            'text_position': slide['text_position'],
                            'duration': slide['duration']
                        })
                if slides:
                    state = "slideshow"
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
                for slide in doc['slides']:
                    image = fetch_image(slide['name'])
                    if image:
                        slides.append({
                            'image': image,
                            'text': slide['text'],
                            'text_size': slide['text_size'],
                            'text_color': slide['text_color'],
                            'text_position': slide['text_position'],
                            'duration': slide['duration']
                        })
                if slides:
                    state = "slideshow"
            time.sleep(1)
    elif state == "slideshow":
        for slide in slides:
            start_time = time.time()
            while time.time() - start_time < slide['duration']:
                if need_refetch.is_set():
                    need_refetch.clear()
                    doc = fetch_document()
                    if doc:
                        slides = []
                        for s in doc['slides']:
                            image = fetch_image(s['name'])
                            if image:
                                slides.append({
                                    'image': image,
                                    'text': s['text'],
                                    'text_size': s['text_size'],
                                    'text_color': s['text_color'],
                                    'text_position': s['text_position'],
                                    'duration': s['duration']
                                })
                        if not slides:
                            state = "default"
                            break
                    else:
                        state = "default"
                        break
                    break
                screen.blit(slide['image'], (0, 0))
                text = slide['text']
                if '{datetime}' in text:
                    text = text.replace('{datetime}', datetime.now().strftime("%Y-%m-%d %H:%M"))
                size_map = {'small': 16, 'medium': 24, 'large': 32}
                font_size = size_map.get(slide['text_size'], 24)
                font = pygame.font.SysFont(None, font_size)
                color = tuple(int(slide['text_color'][i:i+2], 16) for i in (1, 3, 5))
                text_surface = font.render(text, True, color)
                text_rect = text_surface.get_rect()
                positions = {
                    'top-left': (0, 0),
                    'top-center': (screen_width / 2, 0),
                    'top-right': (screen_width, 0),
                    'center-left': (0, screen_height / 2),
                    'center': (screen_width / 2, screen_height / 2),
                    'center-right': (screen_width, screen_height / 2),
                    'bottom-left': (0, screen_height),
                    'bottom-center': (screen_width / 2, screen_height),
                    'bottom-right': (screen_width, screen_height)
                }
                pos_key = slide['text_position']
                if pos_key in positions:
                    if pos_key in ['top-left', 'bottom-left']:
                        text_rect.topleft = positions[pos_key]
                    elif pos_key in ['top-right', 'bottom-right']:
                        text_rect.topright = positions[pos_key]
                    elif pos_key in ['center-left', 'center-right']:
                        text_rect.center = positions[pos_key]
                    elif pos_key == 'center':
                        text_rect.center = positions[pos_key]
                    elif pos_key in ['top-center', 'bottom-center']:
                        text_rect.midtop = positions[pos_key] if pos_key == 'top-center' else (positions[pos_key][0], screen_height)
                else:
                    text_rect.center = (screen_width / 2, screen_height / 2)
                screen.blit(text_surface, text_rect)
                pygame.display.flip()
                time.sleep(1)
            if state != "slideshow":
                break
