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
import sys
import signal
from PIL import Image
from queue import Queue
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from contextlib import contextmanager
import subprocess
import atexit

# Set up basic logging early for display setup debugging
import logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
early_logger = logging.getLogger('display_setup')

# Handle systemd SIGHUP signal
def signal_handler(signum, frame):
    pass

signal.signal(signal.SIGHUP, signal_handler)

# Raspberry Pi OS display setup
def setup_raspberry_pi_display():
    """Set up display environment for Raspberry Pi OS"""
    early_logger.info("Setting up display for Raspberry Pi OS")
    # Use fb1 (HDMI1/secondary port) for slideshow, leaving fb0 (HDMI0/primary) for console
    os.environ['SDL_FBDEV'] = '/dev/fb1'
    os.environ['SDL_VIDEODRIVER'] = 'fbcon'
    os.environ['SDL_NOMOUSE'] = '1'
    early_logger.info("Raspberry Pi OS display setup completed")

# Function to check if framebuffer exists and is accessible
def check_framebuffer(fb_path):
    """Check if framebuffer device exists and is accessible"""
    try:
        exists = os.path.exists(fb_path)
        accessible = os.access(fb_path, os.R_OK | os.W_OK) if exists else False
        early_logger.info(f"Framebuffer {fb_path}: exists={exists}, accessible={accessible}")
        return exists and accessible
    except Exception as e:
        early_logger.error(f"Error checking framebuffer {fb_path}: {e}")
        return False


# Setup display environment for Raspberry Pi OS
setup_raspberry_pi_display()

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
    print(f"Critical: Configuration file {CONFIG_FILE_PATH}: {e}", file=sys.stderr)
    sys.exit(1)

# Read optional settings
office_start_time_str = config.get('settings', 'office_start_time', fallback=None)
office_end_time_str = config.get('settings', 'office_end_time', fallback=None)

# Set up logging (update the early logger configuration)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('/var/log/slideshow.log')],
                    force=True)  # Force reconfiguration 
logger = logging.getLogger()

# Initialize Pygame with debugging
early_logger.info("Initializing pygame...")
pygame.init()

# Debug pygame video driver info
try:
    driver_name = pygame.display.get_driver()
    early_logger.info(f"Pygame using video driver: {driver_name}")
except:
    early_logger.warning("Could not get pygame video driver name")

# Debug available video drivers
try:
    drivers = pygame.display.list_drivers()
    early_logger.info(f"Available pygame video drivers: {drivers}")
except:
    early_logger.warning("Could not list pygame video drivers")

# Debug environment variables
early_logger.info(f"SDL_VIDEODRIVER: {os.environ.get('SDL_VIDEODRIVER', 'not set')}")
early_logger.info(f"SDL_FBDEV: {os.environ.get('SDL_FBDEV', 'not set')}")
early_logger.info(f"DISPLAY: {os.environ.get('DISPLAY', 'not set')}")

# Additional debugging - check if our display setup ran
early_logger.info("Running on Raspberry Pi OS")
early_logger.info(f"Current working directory: {os.getcwd()}")
early_logger.info(f"Process environment dump: {dict(os.environ)}")

# If environment variables are not set, something went wrong - try to fix it
if os.environ.get('SDL_VIDEODRIVER') is None:
    early_logger.error("SDL_VIDEODRIVER is not set! Display setup may have failed.")
    early_logger.info("Attempting emergency display setup...")
    
    early_logger.info("Emergency: Setting up Raspberry Pi display environment")
    setup_raspberry_pi_display()
    
    # Show what we set
    early_logger.info(f"Emergency setup - SDL_VIDEODRIVER: {os.environ.get('SDL_VIDEODRIVER')}")
    early_logger.info(f"Emergency setup - SDL_FBDEV: {os.environ.get('SDL_FBDEV')}")
    early_logger.info(f"Emergency setup - DISPLAY: {os.environ.get('DISPLAY')}")

# Try multiple display drivers for Raspberry Pi OS
display_drivers_to_try = [
    {'driver': 'fbcon', 'fbdev': '/dev/fb1'},  # Preferred: HDMI1 port
    {'driver': 'fbcon', 'fbdev': '/dev/fb0'},  # Fallback: HDMI0 port
    {'driver': 'kmsdrm', 'fbdev': None},       # Fallback: Direct rendering
]

screen = None
screen_width, screen_height = 1920, 1080
successful_driver = None

for attempt, config in enumerate(display_drivers_to_try):
    driver = config['driver']
    fbdev = config['fbdev']
    
    early_logger.info(f"Attempt {attempt + 1}: Trying {driver} driver" + (f" with {fbdev}" if fbdev else ""))
    
    # Set environment for this attempt
    os.environ['SDL_VIDEODRIVER'] = driver
    if fbdev:
        os.environ['SDL_FBDEV'] = fbdev
    elif 'SDL_FBDEV' in os.environ:
        del os.environ['SDL_FBDEV']
    
    # Special setup for different drivers
    if driver == 'kmsdrm':
        # For KMS/DRM, ensure we're in the video group and check for DRM devices
        early_logger.info("Setting up KMS/DRM driver...")
        try:
            # Check for DRM devices
            drm_devices = [f for f in os.listdir('/dev/dri') if f.startswith('card')]
            early_logger.info(f"Available DRM devices: {drm_devices}")
            if not drm_devices:
                early_logger.warning("No DRM devices found in /dev/dri")
            else:
                # Try to use the primary card (usually card0 for HDMI0, card1 for HDMI1)
                # For dual HDMI setup, use card1 for slideshow display
                preferred_card = 'card1' if 'card1' in drm_devices else drm_devices[0]
                os.environ['SDL_DRM_DEVICE'] = f'/dev/dri/{preferred_card}'
                early_logger.info(f"Using DRM device: /dev/dri/{preferred_card}")
                
                # Additional DRM/KMS environment variables
                os.environ['SDL_VIDEODRIVER'] = 'kmsdrm'
                os.environ['SDL_KMSDRM_DEVICE_INDEX'] = '0'  # Use first available device
                
        except Exception as e:
            early_logger.warning(f"Could not check DRM devices: {e}")
    
    elif driver == 'directfb':
        # DirectFB setup if needed
        early_logger.info("Setting up DirectFB driver...")
        pass
    
    try:
        # Reinitialize pygame with new driver
        pygame.quit()
        pygame.init()
        
        early_logger.info(f"Attempting to create display with {driver}...")
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        screen_width, screen_height = screen.get_size()
        early_logger.info(f"SUCCESS! Created display: {screen_width}x{screen_height} using {driver}")
        successful_driver = driver
        
        # Test display briefly
        early_logger.info("Testing display output...")
        screen.fill((0, 255, 0))  # Green for success
        safe_display_flip()
        time.sleep(1)
        screen.fill((0, 0, 0))    # Black
        safe_display_flip()
        early_logger.info(f"Display test completed successfully with {driver}")
        
        break  # Success! Exit the retry loop
        
    except Exception as e:
        early_logger.warning(f"Failed to create display with {driver}: {e}")
        
        # Continue to next driver
        continue

if screen is None:
    early_logger.error("CRITICAL: Failed to create display with any driver!")
    early_logger.error("All display methods failed. Running pygame diagnostic...")
    
    # Run the diagnostic script to help troubleshoot
    try:
        import subprocess
        # Use the same Python environment as the slideshow
        result = subprocess.run([sys.executable, '/usr/local/bin/pygame_diagnostic.py'], 
                              capture_output=True, text=True, timeout=60)
        early_logger.info("Pygame diagnostic output:")
        early_logger.info(result.stdout)
        if result.stderr:
            early_logger.error("Pygame diagnostic errors:")
            early_logger.error(result.stderr)
    except Exception as e:
        early_logger.warning(f"Could not run pygame diagnostic: {e}")
    
    # Try one final fallback with dummy driver for logging/debugging
    early_logger.info("Attempting final fallback with dummy driver for debugging...")
    try:
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
        pygame.quit()
        pygame.init()
        screen = pygame.display.set_mode((1920, 1080))
        screen_width, screen_height = 1920, 1080
        early_logger.warning("Running in DUMMY MODE - no display output will be visible!")
        early_logger.warning("Check system packages and permissions using: /usr/local/bin/pygame_diagnostic.py")
        successful_driver = 'dummy'
    except Exception as e:
        early_logger.error(f"Even dummy driver failed: {e}")
        early_logger.error("Slideshow cannot start. Please check pygame installation and drivers.")
        sys.exit(1)
else:
    early_logger.info(f"Final display configuration: {successful_driver} driver, {screen_width}x{screen_height}")
    early_logger.info(f"Final SDL_VIDEODRIVER: {os.environ.get('SDL_VIDEODRIVER')}")
    early_logger.info(f"Final SDL_FBDEV: {os.environ.get('SDL_FBDEV', 'not set')}")
    early_logger.info(f"Final DISPLAY: {os.environ.get('DISPLAY', 'not set')}")

# Helper function for safe display updates
def safe_display_flip():
    """Safely update display, handling dummy mode gracefully"""
    try:
        if successful_driver != 'dummy':
            pygame.display.flip()
        else:
            # In dummy mode, just sleep briefly to simulate display update
            time.sleep(0.01)
    except Exception as e:
        logger.warning(f"Display flip failed: {e}")

# State variables
state = "connecting"
slides = []
need_refetch = threading.Event()
website_cache = {}  # Cache for website screenshots
capture_queue = Queue(maxsize=1)  # Queue for single webpage capture
capture_lock = threading.Lock()
capture_in_progress = False  # Flag to track ongoing capture
current_slide_index = 0  # Track current slide index
cleanup_lock = threading.Lock()  # Lock for attachment cleanup

# Text rendering cache for performance optimization
text_cache = {}  # Cache for rendered text surfaces
last_datetime_minute = None  # Track last rendered datetime minute

# Scrolling performance variables
scroll_speed_pixels_per_second = 100  # Configurable scroll speed
last_frame_time = 0  # Track frame timing for smooth scrolling
scroll_pause_duration = 1.0  # Pause duration between scroll cycles (seconds)

# Function to capture website screenshot
def capture_website(url, timeout=20):
    """Capture website screenshot and return pygame surface"""
    driver = None
    try:
        # Setup headless Chrome
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--kiosk')
        chrome_options.add_argument('--force-device-scale-factor=1')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--hide-scrollbars')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-features=TranslateUI,VizDisplayCompositor')
        chrome_options.add_argument('--no-first-run')
        chrome_options.add_argument('--no-default-browser-check')

        # Initialize driver with service
        from selenium.webdriver.chrome.service import Service
        service = Service()
        
        try:
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("Using Chrome browser")
        except Exception as chrome_error:
            logger.error(f"Failed to initialize Chrome driver: {chrome_error}")
            logger.error("Ensure Chromium/Chrome and ChromeDriver are installed:")
            logger.error("  Arch Linux: sudo pacman -S chromium chromedriver python-selenium")
            logger.error("  Ubuntu/Debian: sudo apt install chromium-browser chromium-chromedriver python3-selenium")
            logger.error("  RHEL/CentOS: sudo dnf install chromium chromedriver python3-selenium")
            logger.error("  Or install via pip: pip install selenium")
            return None, None

        # Set timeouts
        driver.set_page_load_timeout(timeout)
        driver.implicitly_wait(10)
        
        # Set window size
        driver.set_window_size(1920, 1080)
        time.sleep(2.0)

        # Navigate to URL
        try:
            driver.get(url)
        except Exception as nav_error:
            logger.error(f"Navigation failed for {url}: {nav_error}")
            return None, None
        
        # Wait for page to load
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception as wait_error:
            logger.warning(f"Page load timeout for {url}, proceeding anyway: {wait_error}")
        
        # Set viewport styles
        try:
            driver.execute_script("""
                document.body.style.width = '1920px';
                document.body.style.minHeight = '1080px';
                document.body.style.overflow = 'hidden';
                document.body.style.margin = '0';
                document.body.style.padding = '0';
                document.documentElement.style.width = '1920px';
                document.documentElement.style.minHeight = '1080px';
                document.documentElement.style.overflow = 'hidden';
                document.documentElement.style.margin = '0';
                document.documentElement.style.padding = '0';
                var meta = document.createElement('meta');
                meta.name = 'viewport';
                meta.content = 'width=1920, height=1080, initial-scale=1, shrink-to-fit=no';
                document.head.appendChild(meta);
            """)
            # Log viewport size
            viewport_size = driver.execute_script("""
                return {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    outerWidth: window.outerWidth,
                    outerHeight: window.outerHeight,
                    documentHeight: document.documentElement.clientHeight,
                    bodyHeight: document.body.clientHeight
                };
            """)
            logger.info(f"Viewport size for {url}: {viewport_size}")
        except Exception as js_error:
            logger.error(f"JavaScript execution failed for {url}: {js_error}")
        
        time.sleep(2.0)
        
        # Take full screenshot
        try:
            # Get page dimensions
            total_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, document.body.offsetHeight, document.documentElement.offsetHeight, document.body.clientHeight, document.documentElement.clientHeight);")
            total_width = 1920
            
            # Set window height to capture full page
            driver.set_window_size(1920, total_height)
            screenshot_data = driver.get_screenshot_as_png()
            logger.debug(f"Screenshot captured successfully: ({len(screenshot_data)} bytes)")
        except Exception as screenshot_error:
            logger.error(f"Failed to take screenshot for {url}: {screenshot_error}")
            return None, None
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception as cleanup_error:
                    logger.warning(f"Driver cleanup failed: {cleanup_error}")

        if not screenshot_data:
            logger.error(f"No screenshot data captured for {url}")
            return None, None

        # Process screenshot to 1920x1080
        image = Image.open(BytesIO(screenshot_data))
        img_width, img_height = image.size
        logger.info(f"Raw screenshot size for {url}: {img_width}x{img_height}")
        
        if img_width != 1920 or img_height < 1080:
            # Create 1920x1080 image with white background
            new_image = Image.new('RGB', (1920, 1080), (255, 255, 255))
            # Paste original image at top
            new_image.paste(image, (0, 0))
            image = new_image
        elif img_height > 1080:
            # Crop to 1920x1080 from top
            image = image.crop((0, 0, 1920, 1080))
        
        # Save to BytesIO for pygame
        output = BytesIO()
        image.save(output, format='PNG')
        screenshot_data = output.getvalue()
        output.close()

        # Convert to pygame surface
        image_data = BytesIO(screenshot_data)
        image_surface = pygame.image.load(image_data)

        # Verify resolution
        img_width, img_height = image_surface.get_size()
        if (img_width, img_height) != (1920, 1080):
            logger.error(f"Screenshot processed to {img_width}x{img_height}, expected 1920x1080")
            return None, None

        # Scale to fit screen
        width_ratio = screen_width / img_width
        height_ratio = screen_height / img_height
        scale_ratio = min(width_ratio, height_ratio)
        new_width = int(img_width * scale_ratio)
        new_height = int(img_height * scale_ratio)
        scaled_image = pygame.transform.smoothscale(image_surface, (new_width, new_height))

        return scaled_image, screenshot_data

    except Exception as e:
        logger.error(f"Error capturing website {url}: {e}")
        if driver:
            try:
                driver.quit()
            except Exception as cleanup_error:
                logger.warning(f"Driver cleanup failed during exception handling: {cleanup_error}")
        return None, None

# Function to upload website screenshot to CouchDB
def upload_website_screenshot(url, screenshot_data):
    """Upload website screenshot to CouchDB and return attachment name"""
    try:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        timestamp = int(time.time())
        filename = f"website_{timestamp}_{url_hash}.png"
        upload_url = f"{couchdb_url}/slideshows/{tv_uuid}/{filename}"
        
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
    temp_file_path = None
    try:
        url = f"{couchdb_url}/slideshows/{tv_uuid}/{video_name}"
        headers = {'Cache-Control': 'no-store'}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
            try:
                temp_file.write(response.content)
                temp_file_path = temp_file.name
            finally:
                temp_file.close()
            
            cap = cv2.VideoCapture(temp_file_path)
            if cap.isOpened():
                return cap, temp_file_path
            else:
                logger.error(f"Failed to open video: {video_name}")
                try:
                    os.unlink(temp_file_path)
                except OSError as cleanup_error:
                    logger.warning(f"Failed to cleanup temp file {temp_file_path}: {cleanup_error}")
                return None, None
        else:
            logger.error(f"HTTP error {response.status_code} fetching video {video_name}")
            return None, None
    except Exception as e:
        logger.error(f"Error processing video {video_name}: {e}")
        # Cleanup temp file if it was created
        if temp_file_path:
            try:
                os.unlink(temp_file_path)
            except OSError as cleanup_error:
                logger.warning(f"Failed to cleanup temp file after error {temp_file_path}: {cleanup_error}")
        return None, None

# Context manager for video resources
@contextmanager
def video_resource_manager(video_cap, temp_file_path):
    """Context manager to ensure proper cleanup of video resources"""
    try:
        yield video_cap, temp_file_path
    finally:
        # Always cleanup video capture
        if video_cap:
            try:
                video_cap.release()
                logger.debug("Video capture released successfully")
            except Exception as cap_error:
                logger.warning(f"Failed to release video capture: {cap_error}")
        
        # Always cleanup temp file
        if temp_file_path:
            try:
                os.unlink(temp_file_path)
                logger.debug(f"Successfully cleaned up temp video file: {temp_file_path}")
            except OSError as file_error:
                logger.warning(f"Failed to cleanup temp video file {temp_file_path}: {file_error}")

# Function to convert OpenCV frame to pygame surface
def cv2_to_pygame(cv2_frame):
    """Convert OpenCV frame to pygame surface"""
    try:
        rgb_frame = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
        rgb_frame = np.rot90(rgb_frame)
        rgb_frame = np.flipud(rgb_frame)
        surface = pygame.surfarray.make_surface(rgb_frame)
        return surface
    except Exception as e:
        logger.error(f"Error converting cv2 frame to pygame: {e}")
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
        # Check cache for pre-captured screenshot
        if url in website_cache:
            cached = website_cache[url]
            image = cached['surface']
            content_name = cached['filename']
            logger.info(f"Using pre-captured website screenshot: {url}")
            if text_params and text_params.get('text'):
                text_surface, text_rect = process_text_overlay(image, text_params)
                return image, text_surface, text_rect, content_name
            return image, None, None, content_name
        # Attempt fresh capture once
        logger.info(f"Capturing fresh website screenshot: {url}")
        surface, screenshot_data = capture_website(url, timeout=20)
        if surface and screenshot_data:
            filename = upload_website_screenshot(url, screenshot_data)
            website_cache[url] = {
                'surface': surface,
                'filename': filename or f"website_{int(time.time())}.png",
                'timestamp': time.time()
            }
            image = surface
            content_name = filename or f"website_{int(time.time())}.png"
        else:
            # Fall back to previous cached image if available
            if url in website_cache:
                cached = website_cache[url]
                image = cached['surface']
                content_name = cached['filename']
                logger.warning(f"Using previous cached screenshot due to capture failure: {url}")
            else:
                logger.error(f"Failed to capture website and no cache available: {url}")
                return None, None, None, None
        if text_params and text_params.get('text'):
            text_surface, text_rect = process_text_overlay(image, text_params)
            return image, text_surface, text_rect, content_name
        return image, None, None, content_name
    elif content_type == 'video':
        logger.error(f"Video content should be handled separately: {content_name}")
        return None, None, None, None
    else:
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
    if text_params and text_params.get('text'):
        text_surface, text_rect = process_text_overlay(image, text_params)
        return image, text_surface, text_rect, content_name
    return image, None, None, content_name

# Function to validate and sanitize slide duration
def validate_slide_duration(duration, slide_name="Unknown", default_duration=10):
    """Validate slide duration and return safe value"""
    try:
        if duration is None:
            return default_duration
        
        # Convert to float if possible
        if isinstance(duration, str):
            duration = float(duration)
        
        if not isinstance(duration, (int, float)):
            logger.warning(f"Invalid duration type for slide '{slide_name}': {type(duration).__name__}. Using default {default_duration}s.")
            return default_duration
        
        if duration <= 0:
            logger.warning(f"Invalid duration value for slide '{slide_name}': {duration}. Using default {default_duration}s.")
            return default_duration
        
        if duration > 300:  # Prevent extremely long durations (5 minutes max)
            logger.warning(f"Duration too long for slide '{slide_name}': {duration}s. Capping at 300s.")
            return 300
        
        return duration
        
    except (ValueError, TypeError) as e:
        logger.error(f"Error validating duration for slide '{slide_name}': {e}. Using default {default_duration}s.")
        return default_duration

# Function to calculate optimal scroll speed based on content
def calculate_scroll_speed(text_width, screen_width, base_speed=100):
    """Calculate optimal scroll speed based on text length and screen size"""
    # Adjust speed based on text length relative to screen width
    ratio = text_width / screen_width
    if ratio > 3:  # Very long text
        return base_speed * 1.5
    elif ratio > 2:  # Long text
        return base_speed * 1.2
    elif ratio < 0.5:  # Short text
        return base_speed * 0.8
    return base_speed

# Function to get cached or render text surface
def get_cached_text_surface(image, text_params, force_refresh=False):
    """Get cached text surface or render new one if needed"""
    global last_datetime_minute, text_cache
    
    try:
        text_content = text_params['text']
        has_datetime = '{datetime}' in text_content
        
        # Generate cache key based on text parameters
        cache_key = (
            text_content,
            text_params.get('text_size', 'medium'),
            text_params.get('text_color', '#FFFFFF'),
            text_params.get('text_position', 'bottom-center'),
            text_params.get('text_background_color', ''),
            image.get_size()  # Include image size for positioning
        )
        
        # Check if we need to refresh datetime content
        current_minute = None
        if has_datetime:
            current_time = datetime.now()
            current_minute = (current_time.year, current_time.month, current_time.day, 
                            current_time.hour, current_time.minute)
            
            # Force refresh if minute has changed
            if last_datetime_minute != current_minute:
                force_refresh = True
                last_datetime_minute = current_minute
        
        # Return cached surface if available and no refresh needed
        if not force_refresh and cache_key in text_cache:
            cached_surface, cached_rect = text_cache[cache_key]
            return cached_surface, cached_rect
        
        # Render new text surface
        if has_datetime:
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
        
        # Calculate text position
        text_pos_key = text_params.get('text_position', 'bottom-center')
        text_rect = text_surface.get_rect()
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
        
        # Apply background if specified
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
                text_rect.x -= bg_padding
                text_rect.y -= bg_padding
            except ValueError as ve:
                logger.error(f"Invalid text_background_color: {text_bg_color_hex} - {ve}")
        
        # Cache the result (limit cache size to prevent memory issues)
        if len(text_cache) > 50:  # Clear cache if it gets too large
            text_cache.clear()
        text_cache[cache_key] = (surface_to_return, text_rect)
        
        return surface_to_return, text_rect
        
    except Exception as e:
        logger.error(f"Error processing cached text overlay: {e}")
        return None, None

# Function to process text overlay (legacy support)
def process_text_overlay(image, text_params):
    """Process text overlay for any content type (legacy wrapper)"""
    return get_cached_text_surface(image, text_params, force_refresh=True)

# Function to get referenced attachments from document
def get_referenced_attachments(doc):
    """Extract set of attachment names that are currently referenced"""
    referenced_attachments = set()
    for slide in doc.get('slides', []):
        if slide.get('type') != 'website':
            attachment_name = slide.get('name')
            if attachment_name:
                referenced_attachments.add(attachment_name)
        else:
            # For website slides, use the cached filename
            url = slide.get('url')
            if url in website_cache:
                attachment_name = website_cache[url].get('filename')
                if attachment_name:
                    referenced_attachments.add(attachment_name)
    return referenced_attachments

# Function to perform immediate cleanup (called when slides change)
def cleanup_unused_attachments_immediate():
    """Immediate cleanup of unused attachments when slides are updated"""
    try:
        with cleanup_lock:
            logger.info("Starting immediate attachment cleanup...")
            
            # Fetch current document
            doc_response = requests.get(f"{couchdb_url}/slideshows/{tv_uuid}", timeout=10)
            if doc_response.status_code != 200:
                logger.warning(f"Failed to fetch document for immediate cleanup: {doc_response.status_code}")
                return

            doc = doc_response.json()
            referenced_attachments = get_referenced_attachments(doc)
            
            # Get all attachments
            attachments = doc.get('_attachments', {})
            attachment_names = set(attachments.keys())
            
            # Find unused attachments
            unused_attachments = list(attachment_names - referenced_attachments)
            
            if not unused_attachments:
                logger.debug("No unused attachments found during immediate cleanup")
                return
            
            logger.info(f"Found {len(unused_attachments)} unused attachments for immediate cleanup")
            
            # Delete unused attachments in batch (up to 5 at once to avoid overwhelming)
            batch_size = min(5, len(unused_attachments))
            for i in range(0, len(unused_attachments), batch_size):
                batch = unused_attachments[i:i + batch_size]
                _delete_attachment_batch(batch)
                
    except Exception as e:
        logger.error(f"Error during immediate attachment cleanup: {e}")

# Function to delete a batch of attachments efficiently
def _delete_attachment_batch(attachment_names):
    """Delete a batch of attachments efficiently"""
    if not attachment_names:
        return
    
    try:
        # Get fresh document revision for batch
        doc_response = requests.get(f"{couchdb_url}/slideshows/{tv_uuid}", timeout=10)
        if doc_response.status_code != 200:
            logger.error(f"Failed to get document for batch deletion: {doc_response.status_code}")
            return
        
        doc = doc_response.json()
        current_rev = doc.get('_rev')
        
        # Delete attachments in this batch
        deleted_count = 0
        for attachment_name in attachment_names:
            try:
                delete_url = f"{couchdb_url}/slideshows/{tv_uuid}/{attachment_name}?rev={current_rev}"
                delete_response = requests.delete(delete_url, timeout=10)
                
                if delete_response.status_code in [200, 202]:
                    logger.info(f"Successfully deleted unused attachment: {attachment_name}")
                    deleted_count += 1
                    # Update revision for next deletion in batch
                    current_rev = delete_response.json().get('rev', current_rev)
                else:
                    logger.warning(f"Failed to delete attachment {attachment_name}: {delete_response.status_code}")
                    
            except Exception as e:
                logger.error(f"Error deleting attachment {attachment_name}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Batch deletion completed: {deleted_count}/{len(attachment_names)} attachments deleted")
            
    except Exception as e:
        logger.error(f"Error during batch attachment deletion: {e}")

# Function to clean up unused attachments (periodic background task)
def cleanup_unused_attachments():
    """Periodic cleanup of all unused attachments"""
    while True:
        try:
            with cleanup_lock:
                logger.info("Starting periodic attachment cleanup...")
                
                # Fetch the current slideshow document
                doc_response = requests.get(f"{couchdb_url}/slideshows/{tv_uuid}", timeout=10)
                if doc_response.status_code != 200:
                    logger.error(f"Failed to fetch slideshow document for cleanup: {doc_response.status_code}")
                    time.sleep(900)  # Retry after 15 minutes
                    continue

                doc = doc_response.json()
                referenced_attachments = get_referenced_attachments(doc)
                
                # Get all attachments in the document
                attachments = doc.get('_attachments', {})
                attachment_names = set(attachments.keys())

                # Identify unused attachments
                unused_attachments = list(attachment_names - referenced_attachments)
                
                if not unused_attachments:
                    logger.debug("No unused attachments found during periodic cleanup")
                else:
                    logger.info(f"Found {len(unused_attachments)} unused attachments for periodic cleanup")
                    
                    # Sort by size (delete larger files first to free more space)
                    def get_attachment_size(name):
                        return attachments.get(name, {}).get('length', 0)
                    
                    unused_attachments.sort(key=get_attachment_size, reverse=True)
                    
                    # Delete all unused attachments in batches
                    batch_size = 10  # Larger batches for periodic cleanup
                    total_deleted = 0
                    
                    for i in range(0, len(unused_attachments), batch_size):
                        batch = unused_attachments[i:i + batch_size]
                        batch_start_count = total_deleted
                        _delete_attachment_batch(batch)
                        # Assume successful deletion for counting (logged in batch function)
                        total_deleted += len(batch)
                        
                        # Brief pause between batches to avoid overwhelming CouchDB
                        if i + batch_size < len(unused_attachments):
                            time.sleep(1)
                    
                    logger.info(f"Periodic cleanup completed: processed {len(unused_attachments)} unused attachments")

        except Exception as e:
            logger.error(f"Error during periodic attachment cleanup: {e}")
        
        # Run every 15 minutes instead of 1 hour
        time.sleep(900)  # 15 minutes = 900 seconds

# Start attachment cleanup thread
threading.Thread(target=cleanup_unused_attachments, daemon=True).start()

# Perform initial cleanup on startup (after a brief delay to let system stabilize)
def startup_cleanup():
    """Perform cleanup on system startup"""
    time.sleep(30)  # Wait 30 seconds for system to stabilize
    logger.info("Performing startup attachment cleanup...")
    cleanup_unused_attachments_immediate()

threading.Thread(target=startup_cleanup, daemon=True).start()

# Background thread for website capture
def website_capture_worker():
    """Background worker to capture website screenshots"""
    global capture_in_progress
    while True:
        try:
            # Get next URL from queue (blocks until available)
            slide_data = capture_queue.get()
            url = slide_data.get('url')
            if url:
                with capture_lock:
                    if capture_in_progress:
                        logger.debug(f"Skipping capture for {url}, another capture in progress")
                        capture_queue.task_done()
                        continue
                    capture_in_progress = True
                
                try:
                    logger.info(f"Pre-capturing website: {url}")
                    surface, screenshot_data = capture_website(url, timeout=15)
                    if surface and screenshot_data:
                        filename = upload_website_screenshot(url, screenshot_data)
                        website_cache[url] = {
                            'surface': surface,
                            'filename': filename or f"website_{int(time.time())}.png",
                            'timestamp': time.time()
                        }
                        logger.info(f"Successfully pre-captured website: {url}")
                    else:
                        logger.warning(f"Pre-capture failed for website: {url}")
                finally:
                    with capture_lock:
                        capture_in_progress = False
                    capture_queue.task_done()
        except Exception as e:
            logger.error(f"Error in website capture worker: {e}")
            with capture_lock:
                capture_in_progress = False
            capture_queue.task_done()
        time.sleep(1)

# Start website capture worker thread
threading.Thread(target=website_capture_worker, daemon=True).start()

# Function to queue website for pre-capture
def queue_website_capture(slides_list, current_index):
    """Queue upcoming website slide for pre-capture, 2 slides ahead"""
    try:
        with capture_lock:
            # Only queue if no capture is in progress and queue is empty
            if not capture_in_progress and capture_queue.empty():
                look_ahead = 2
                next_index = (current_index + look_ahead) % len(slides_list)
                # Modulo operation already ensures valid index, no additional check needed
                next_slide = slides_list[next_index]
                if next_slide.get('type') == 'website':
                    url = next_slide.get('url')
                    if url:
                        # Queue website for capture
                        capture_queue.put({'url': url})
                        logger.info(f"Queued website for capture (slide {next_index}): {url}")
    except Exception as e:
        logger.error(f"Error queuing website capture: {e}")

# Function to fetch the slideshow document from CouchDB
def fetch_document():
    try:
        url = f"{couchdb_url}/slideshows/{tv_uuid}"
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        session.mount('http://', HTTPAdapter(max_retries=retries))
        response = session.get(url, timeout=10)
        
        if response.status_code == 200:
            logger.info("Successfully fetched document")
            try:
                return response.json()
            except json.JSONDecodeError as json_error:
                logger.error(f"Invalid JSON in document response: {json_error}")
                return None
        elif response.status_code == 404:
            logger.warning("Document not found")
            return None
        elif response.status_code == 401:
            logger.error("Authentication required for CouchDB access")
            return None
        elif response.status_code == 403:
            logger.error("Access forbidden to CouchDB document")
            return None
        else:
            logger.error(f"HTTP error {response.status_code}: {response.text}")
            return None
            
    except requests.exceptions.ConnectionError as conn_error:
        logger.error(f"Connection error fetching document: {conn_error}")
        return None
    except requests.exceptions.Timeout as timeout_error:
        logger.error(f"Timeout error fetching document: {timeout_error}")
        return None
    except requests.exceptions.RequestException as req_error:
        logger.error(f"Request error fetching document: {req_error}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching document: {e}")
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
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
            session.mount('http://', HTTPAdapter(max_retries=retries))
            response = session.get(url, params=params, stream=True, timeout=30)
            for line in response.iter_lines():
                if line:
                    try:
                        change = json.loads(line.decode('utf-8'))
                        if 'id' in change and change['id'] == tv_uuid:
                            logger.info("Change detected, setting need_refetch")
                            need_refetch.set()
                    except (json.JSONDecodeError, UnicodeDecodeError) as parse_error:
                        logger.warning(f"Failed to parse changes feed line: {parse_error}")
                        continue
        except requests.exceptions.ConnectionError as conn_error:
            logger.error(f"Connection error in changes feed: {conn_error}")
            time.sleep(30)  # Wait before retrying
        except requests.exceptions.Timeout as timeout_error:
            logger.error(f"Timeout error in changes feed: {timeout_error}")
            time.sleep(30)  # Wait before retrying
        except requests.RequestException as req_error:
            logger.error(f"Request error in changes feed: {req_error}")
            time.sleep(30)  # Wait before retrying
        except Exception as e:
            logger.error(f"Unexpected error in changes feed: {e}")
            time.sleep(30)  # Wait before retrying

# Start the background thread to watch for changes
threading.Thread(target=watch_changes, daemon=True).start()

# Function to cleanup old slides
def cleanup_old_slides(old_slides):
    """Cleanup resources from old slides before replacing them"""
    for slide in old_slides:
        if slide.get('type') == 'video' and slide.get('cleanup_func'):
            try:
                slide['cleanup_func']()
            except Exception as cleanup_error:
                logger.warning(f"Error during slide cleanup: {cleanup_error}")

# Function to process slides from document
def process_slides_from_doc(doc):
    """Process slides from document and return processed slide list"""
    processed_slides = []
    for slide_doc in doc.get('slides', []):
        content_type = slide_doc.get('type', 'image')
        if content_type == 'video':
            video_cap, temp_file = process_video(slide_doc['name'])
            if video_cap:
                # Create cleanup function for this video resource
                def cleanup_video_resources(cap=video_cap, file_path=temp_file):
                    if cap:
                        try:
                            cap.release()
                            logger.debug("Video capture released successfully")
                        except Exception as cap_error:
                            logger.warning(f"Failed to release video capture: {cap_error}")
                    if file_path:
                        try:
                            os.unlink(file_path)
                            logger.debug(f"Successfully cleaned up temp video file: {file_path}")
                        except OSError as file_error:
                            logger.warning(f"Failed to cleanup temp video file {file_path}: {file_error}")
                
                processed_slides.append({
                    'type': 'video',
                    'video_cap': video_cap,
                    'temp_file': temp_file,
                    'cleanup_func': cleanup_video_resources,
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
                    'url': slide_doc.get('url')
                })
    return processed_slides

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

FADE_STEPS = 30

# Main loop
while True:
    if state == "connecting":
        screen.fill((0, 0, 0))
        font = pygame.font.SysFont(None, 24)
        text = font.render("Connecting to server...", True, (255, 255, 255))
        text_rect = text.get_rect(center=(screen_width / 2, screen_height / 2))
        screen.blit(text, text_rect)
        safe_display_flip()
        doc = fetch_document()
        if doc is not None:
            if doc:
                slides = process_slides_from_doc(doc)
                if slides:
                    # Trigger immediate cleanup of unused attachments
                    cleanup_unused_attachments_immediate()
                    state = "slideshow"
                    current_slide_index = 0
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
        safe_display_flip()
        if need_refetch.is_set():
            need_refetch.clear()
            doc = fetch_document()
            if doc:
                slides = process_slides_from_doc(doc)
                if slides:
                    # Trigger immediate cleanup of unused attachments
                    cleanup_unused_attachments_immediate()
                    state = "slideshow"
                    current_slide_index = 0
                    first_slide_info = {'id': slides[0]['id'], 'filename': slides[0]['filename']}
                    update_tv_status(couchdb_url, tv_uuid, first_slide_info)
            time.sleep(1)
    elif state == "slideshow":
        slide_index = current_slide_index
        while slide_index < len(slides):
            slide_data = slides[slide_index]
            queue_website_capture(slides, slide_index)
            current_display_slide_info = {'id': slide_data['id'], 'filename': slide_data['filename']}
            update_tv_status(couchdb_url, tv_uuid, current_display_slide_info)
            if slide_data['type'] == 'video':
                video_cap = slide_data['video_cap']
                start_time = time.time()
                slide_duration = validate_slide_duration(slide_data.get('duration'), slide_data.get('filename', 'Unknown'))
                while time.time() - start_time < slide_duration:
                    if need_refetch.is_set():
                        need_refetch.clear()
                        doc = fetch_document()
                        if doc:
                            new_slides = process_slides_from_doc(doc)
                            if new_slides:
                                cleanup_old_slides(slides)
                                slides = new_slides
                                # Trigger immediate cleanup of unused attachments
                                cleanup_unused_attachments_immediate()
                                # Resume from current slide index, or last valid index
                                current_slide_index = min(slide_index, len(slides) - 1)
                                slide_index = current_slide_index
                                slide_data = slides[slide_index]
                                start_time = time.time()
                                slide_duration = validate_slide_duration(slide_data.get('duration'), slide_data.get('filename', 'Unknown'))
                                video_cap = slide_data.get('video_cap')
                                continue
                            else:
                                state = "default"
                                break
                        else:
                            state = "default"
                            break
                    ret, frame = video_cap.read()
                    if not ret:
                        video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = video_cap.read()
                        if not ret:
                            break
                    surface = cv2_to_pygame(frame)
                    if surface:
                        frame_width, frame_height = surface.get_size()
                        width_ratio = screen_width / frame_width
                        height_ratio = screen_height / frame_height
                        scale_ratio = min(width_ratio, height_ratio)
                        new_width = int(frame_width * scale_ratio)
                        new_height = int(frame_height * scale_ratio)
                        scaled_surface = pygame.transform.smoothscale(surface, (new_width, new_height))
                        center_x = (screen_width - new_width) // 2
                        center_y = (screen_height - new_height) // 2
                        screen.fill((0, 0, 0))
                        screen.blit(scaled_surface, (center_x, center_y))
                        if slide_data.get('text_params') and slide_data['text_params'].get('text'):
                            text_params = slide_data['text_params']
                            if '{datetime}' in text_params.get('text', ''):
                                temp_surface = pygame.Surface((scaled_surface.get_width(), scaled_surface.get_height()), pygame.SRCALPHA)
                                current_text_surface, current_text_rect = get_cached_text_surface(temp_surface, text_params)
                                if current_text_surface and current_text_rect:
                                    text_surface = current_text_surface
                                    text_rect = current_text_rect
                                else:
                                    text_surface = slide_data.get('text_surface')
                                    text_rect = slide_data.get('text_rect')
                            else:
                                text_surface = slide_data.get('text_surface')
                                text_rect = slide_data.get('text_rect')
                            if text_surface and text_rect:
                                screen.blit(text_surface, (center_x + text_rect.left, center_y + text_rect.top))
                        safe_display_flip()
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            pygame.quit()
                            sys.exit()
                        if event.type == pygame.KEYDOWN:
                            if event.key == pygame.K_ESCAPE:
                                pygame.quit()
                                sys.exit()
                    # Dynamic frame timing for smooth scrolling
                    current_frame_time = time.time()
                    if last_frame_time > 0:
                        frame_duration = current_frame_time - last_frame_time
                        target_frame_time = 1.0 / 30.0  # Target 30 FPS
                        sleep_time = max(0, target_frame_time - frame_duration)
                    else:
                        sleep_time = 1.0 / 30.0
                    last_frame_time = current_frame_time
                    time.sleep(sleep_time)
                # Cleanup video resources using the cleanup function
                if slide_data.get('cleanup_func'):
                    slide_data['cleanup_func']()
            else:
                img_width, img_height = slide_data['image'].get_size()
                center_x = (screen_width - img_width) // 2
                center_y = (screen_height - img_height) // 2
                scroll_x = screen_width
                scroll_start_time = time.time()  # Track scrolling timing
                scroll_cycle_complete = False  # Track scroll cycle state
                incoming_transition_duration_ms = slide_data.get('transition_time', 0)
                if incoming_transition_duration_ms > 0:
                    delay_per_step = (incoming_transition_duration_ms / FADE_STEPS) / 1000.0
                    slide_render_surface = pygame.Surface((img_width, img_height), pygame.SRCALPHA)
                    slide_render_surface.blit(slide_data['image'], (0,0))
                    if slide_data.get('text_surface') and not slide_data.get('scroll_text'):
                        slide_render_surface.blit(slide_data['text_surface'], slide_data['text_rect'])
                    for alpha_step in range(FADE_STEPS + 1):
                        if need_refetch.is_set():
                            break
                        alpha_value = int((alpha_step / FADE_STEPS) * 255)
                        slide_render_surface.set_alpha(alpha_value)
                        screen.fill((0,0,0))
                        screen.blit(slide_render_surface, (center_x, center_y))
                        safe_display_flip()
                        time.sleep(delay_per_step)
                    if need_refetch.is_set():
                        need_refetch.clear()
                        doc = fetch_document()
                        if doc:
                            new_slides = process_slides_from_doc(doc)
                            if new_slides:
                                cleanup_old_slides(slides)
                                slides = new_slides
                                # Trigger immediate cleanup of unused attachments
                                cleanup_unused_attachments_immediate()
                                current_slide_index = min(slide_index, len(slides) - 1)
                                slide_index = current_slide_index
                                slide_data = slides[slide_index]
                                continue
                            else:
                                state = "default"
                                break
                        else:
                            state = "default"
                            break
                start_time = time.time()
                slide_duration = validate_slide_duration(slide_data.get('duration'), slide_data.get('filename', 'Unknown'))
                while time.time() - start_time < slide_duration:
                    if need_refetch.is_set():
                        need_refetch.clear()
                        doc = fetch_document()
                        if doc:
                            new_slides = process_slides_from_doc(doc)
                            if new_slides:
                                cleanup_old_slides(slides)
                                slides = new_slides
                                # Trigger immediate cleanup of unused attachments
                                cleanup_unused_attachments_immediate()
                                current_slide_index = min(slide_index, len(slides) - 1)
                                slide_index = current_slide_index
                                slide_data = slides[slide_index]
                                start_time = time.time()
                                slide_duration = validate_slide_duration(slide_data.get('duration'), slide_data.get('filename', 'Unknown'))
                                continue
                            else:
                                state = "default"
                                break
                        else:
                            state = "default"
                            break
                    if state == "default":
                        break
                    screen.fill((0, 0, 0))
                    screen.blit(slide_data['image'], (center_x, center_y))
                    if slide_data.get('text_params') and slide_data['text_params'].get('text'):
                        text_params = slide_data['text_params']
                        if '{datetime}' in text_params.get('text', ''):
                            current_text_surface, current_text_rect = get_cached_text_surface(slide_data['image'], text_params)
                            if current_text_surface and current_text_rect:
                                text_surface = current_text_surface
                                original_text_rect = current_text_rect
                            else:
                                text_surface = slide_data.get('text_surface')
                                original_text_rect = slide_data.get('text_rect')
                        else:
                            text_surface = slide_data.get('text_surface')
                            original_text_rect = slide_data.get('text_rect')
                        if text_surface and original_text_rect:
                            if slide_data.get('scroll_text'):
                                # Time-based scrolling for smooth animation
                                current_time = time.time()
                                elapsed_time = current_time - scroll_start_time
                                
                                # Calculate dynamic scroll speed based on text content
                                text_width = text_surface.get_width()
                                dynamic_scroll_speed = calculate_scroll_speed(text_width, screen_width, scroll_speed_pixels_per_second)
                                
                                if not scroll_cycle_complete:
                                    scroll_x = screen_width - (elapsed_time * dynamic_scroll_speed)
                                    
                                    # Check if text has completely exited screen
                                    if scroll_x < -text_width:
                                        scroll_cycle_complete = True
                                        scroll_start_time = current_time  # Start pause timer
                                        scroll_x = -text_width  # Keep text just off screen
                                else:
                                    # Pause period after scroll completion
                                    if elapsed_time >= scroll_pause_duration:
                                        # Reset for next scroll cycle
                                        scroll_cycle_complete = False
                                        scroll_start_time = current_time
                                        scroll_x = screen_width
                                    else:
                                        # Keep text off screen during pause
                                        scroll_x = -text_width
                                
                                screen.blit(text_surface, (int(scroll_x), center_y + original_text_rect.top))
                            else:
                                screen.blit(text_surface, (center_x + original_text_rect.left, center_y + original_text_rect.top))
                    safe_display_flip()
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            pygame.quit()
                            sys.exit()
                        if event.type == pygame.KEYDOWN:
                            if event.key == pygame.K_ESCAPE:
                                pygame.quit()
                                sys.exit()
                    # Dynamic frame timing for smooth scrolling
                    current_frame_time = time.time()
                    if last_frame_time > 0:
                        frame_duration = current_frame_time - last_frame_time
                        target_frame_time = 1.0 / 30.0  # Target 30 FPS
                        sleep_time = max(0, target_frame_time - frame_duration)
                    else:
                        sleep_time = 1.0 / 30.0
                    last_frame_time = current_frame_time
                    time.sleep(sleep_time)
                if need_refetch.is_set():
                    continue
            if state == "default":
                break
            slide_index += 1
            current_slide_index = slide_index % len(slides)
            if slide_index >= len(slides):
                slide_index = 0
