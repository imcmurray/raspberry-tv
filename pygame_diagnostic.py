#!/usr/bin/env python3
"""
Pygame Display Diagnostic Script for Raspberry Pi OS 64-bit
Helps debug display driver issues and framebuffer configuration
"""

import os
import sys
import subprocess
import logging
import time

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

def check_system_info():
    """Check basic system information"""
    logger.info("=== SYSTEM INFORMATION ===")
    
    # OS Info
    try:
        with open('/etc/os-release', 'r') as f:
            os_info = f.read()
            logger.info(f"OS Info:\n{os_info}")
    except:
        logger.error("Could not read OS info")
    
    # Check for Raspberry Pi OS specifically
    try:
        if os.path.exists('/etc/rpi-issue'):
            with open('/etc/rpi-issue', 'r') as f:
                rpi_info = f.read().strip()
                logger.info(f"Raspberry Pi OS: {rpi_info}")
        else:
            logger.warning("This does not appear to be Raspberry Pi OS")
    except:
        logger.error("Could not read Raspberry Pi info")
    
    # User groups
    try:
        result = subprocess.run(['groups'], capture_output=True, text=True)
        logger.info(f"User groups: {result.stdout.strip()}")
        groups = result.stdout.strip().split()
        if 'video' in groups:
            logger.info("✓ User is in 'video' group (required for framebuffer access)")
        else:
            logger.warning("✗ User is NOT in 'video' group (may cause framebuffer access issues)")
        if 'render' in groups:
            logger.info("✓ User is in 'render' group (good for GPU access)")
        else:
            logger.info("ℹ User is NOT in 'render' group (optional for GPU access)")
    except:
        logger.error("Could not get user groups")
    
def check_framebuffer_devices():
    """Check framebuffer device configuration"""
    logger.info("=== FRAMEBUFFER DEVICES ===")
    
    # Check specific framebuffer devices for dual HDMI
    fb_configs = {
        '/dev/fb0': 'HDMI0 (Primary/Console)',
        '/dev/fb1': 'HDMI1 (Secondary/Slideshow)'
    }
    
    for fb_path, description in fb_configs.items():
        if os.path.exists(fb_path):
            readable = os.access(fb_path, os.R_OK)
            writable = os.access(fb_path, os.W_OK)
            
            # Get device info
            try:
                stat_info = os.stat(fb_path)
                major, minor = os.major(stat_info.st_rdev), os.minor(stat_info.st_rdev)
                logger.info(f"✓ {fb_path} ({description}): exists=True, readable={readable}, writable={writable}")
                logger.info(f"  Device: major={major}, minor={minor}")
                
                # Try to get framebuffer info
                try:
                    result = subprocess.run(['fbset', '-fb', fb_path], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        # Parse resolution from fbset output
                        lines = result.stdout.strip().split('\n')
                        for line in lines:
                            if 'geometry' in line:
                                logger.info(f"  Resolution info: {line.strip()}")
                                break
                    else:
                        logger.info(f"  fbset failed: {result.stderr.strip()}")
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    logger.info(f"  fbset not available or timed out")
                    
            except Exception as e:
                logger.warning(f"  Error getting device info: {e}")
        else:
            logger.warning(f"✗ {fb_path} ({description}): does not exist")
    
    # Check for additional framebuffer devices
    for i in range(2, 5):
        fb_path = f"/dev/fb{i}"
        if os.path.exists(fb_path):
            logger.info(f"Additional framebuffer found: {fb_path}")

def check_drm_devices():
    """Check DRM/KMS devices"""
    logger.info("=== DRM/KMS DEVICES ===")
    
    drm_path = "/dev/dri"
    if os.path.exists(drm_path):
        drm_devices = os.listdir(drm_path)
        logger.info(f"DRM devices found: {drm_devices}")
        
        for device in sorted(drm_devices):
            device_path = os.path.join(drm_path, device)
            readable = os.access(device_path, os.R_OK)
            writable = os.access(device_path, os.W_OK)
            
            if device.startswith('card'):
                # Primary DRM card devices
                logger.info(f"✓ {device_path}: readable={readable}, writable={writable}")
                if device == 'card0':
                    logger.info(f"  card0 typically maps to HDMI0 (fb0)")
                elif device == 'card1':
                    logger.info(f"  card1 typically maps to HDMI1 (fb1)")
            else:
                logger.info(f"  {device_path}: readable={readable}, writable={writable}")
    else:
        logger.warning("✗ No /dev/dri directory found (KMS/DRM not available)")

def check_sdl2_info():
    """Check SDL2 installation and capabilities"""
    logger.info("=== SDL2 INFORMATION ===")
    
    # Check SDL2 packages for Raspberry Pi OS
    packages_to_check = [
        'libsdl2-2.0-0',
        'libsdl2-dev', 
        'libdrm2',
        'libgbm1'
    ]
    
    for package in packages_to_check:
        try:
            result = subprocess.run(['dpkg', '-l', package], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                # Extract version from dpkg output
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if line.startswith('ii'):
                        parts = line.split()
                        if len(parts) >= 3:
                            logger.info(f"✓ Package {package}: {parts[2]} (installed)")
                        break
            else:
                logger.warning(f"✗ Package {package}: not installed")
        except:
            logger.error(f"Could not check package {package}")

def check_boot_config():
    """Check Raspberry Pi boot configuration"""
    logger.info("=== BOOT CONFIGURATION ===")
    
    # Check config.txt locations
    config_paths = ['/boot/firmware/config.txt', '/boot/config.txt']
    config_path = None
    
    for path in config_paths:
        if os.path.exists(path):
            config_path = path
            logger.info(f"✓ Found config.txt at: {path}")
            break
    
    if not config_path:
        logger.error("✗ Could not find config.txt")
        return
    
    try:
        with open(config_path, 'r') as f:
            config_content = f.read()
            
        # Check for dual HDMI configuration
        hdmi_configs = [
            'hdmi_group:0=',
            'hdmi_group:1=',
            'hdmi_force_hotplug:0=',
            'hdmi_force_hotplug:1=',
            'dtoverlay=vc4-fkms-v3d'
        ]
        
        for config_line in hdmi_configs:
            if config_line in config_content:
                # Find the actual line
                for line in config_content.split('\n'):
                    if config_line in line and not line.strip().startswith('#'):
                        logger.info(f"✓ {line.strip()}")
                        break
            else:
                logger.warning(f"✗ Missing: {config_line}")
                
        # Check for problematic KMS overlay
        if 'dtoverlay=vc4-kms-v3d' in config_content and not config_content.count('#dtoverlay=vc4-kms-v3d'):
            logger.warning("⚠ vc4-kms-v3d overlay is enabled (may prevent separate framebuffers)")
            
    except Exception as e:
        logger.error(f"Error reading config.txt: {e}")
    
    # Check cmdline.txt for framebuffer console configuration
    cmdline_paths = ['/boot/firmware/cmdline.txt', '/boot/cmdline.txt']
    cmdline_path = None
    
    for path in cmdline_paths:
        if os.path.exists(path):
            cmdline_path = path
            logger.info(f"✓ Found cmdline.txt at: {path}")
            break
    
    if cmdline_path:
        try:
            with open(cmdline_path, 'r') as f:
                cmdline_content = f.read().strip()
                logger.info(f"Kernel command line: {cmdline_content}")
                
                if 'consoleblank=0' in cmdline_content:
                    logger.info("✓ consoleblank=0 found (screen blanking disabled)")
                else:
                    logger.warning("✗ consoleblank=0 not found (screen may blank)")
                    
                if 'fbcon=map:0' in cmdline_content:
                    logger.info("✓ fbcon=map:0 found (console on fb0)")
                else:
                    logger.warning("✗ fbcon=map:0 not found (console mapping not specified)")
                    
        except Exception as e:
            logger.error(f"Error reading cmdline.txt: {e}")
    else:
        logger.error("✗ Could not find cmdline.txt")

def test_pygame_drivers():
    """Test pygame driver capabilities for Raspberry Pi OS"""
    logger.info("=== PYGAME DRIVER TESTS ===")
    
    try:
        import pygame
        logger.info(f"✓ Pygame version: {pygame.version.ver}")
        logger.info(f"✓ SDL version: {pygame.version.SDL}")
    except ImportError as e:
        logger.error(f"✗ Could not import pygame: {e}")
        logger.error("Install pygame with: pip install pygame")
        return
    
    # Test drivers suitable for Raspberry Pi OS (no X11/Wayland)
    drivers_to_test = [
        {'name': 'fbcon', 'fbdev': '/dev/fb1', 'description': 'Framebuffer Console (HDMI1/Slideshow)'},
        {'name': 'fbcon', 'fbdev': '/dev/fb0', 'description': 'Framebuffer Console (HDMI0/Console)'},
        {'name': 'kmsdrm', 'fbdev': None, 'description': 'Kernel Mode Setting Direct Rendering'}
    ]
    
    successful_drivers = []
    
    for config in drivers_to_test:
        driver = config['name']
        fbdev = config['fbdev']
        description = config['description']
        
        logger.info(f"Testing: {driver} - {description}")
        
        # Set environment for this test
        os.environ['SDL_VIDEODRIVER'] = driver
        if fbdev:
            os.environ['SDL_FBDEV'] = fbdev
            os.environ['SDL_NOMOUSE'] = '1'
        elif 'SDL_FBDEV' in os.environ:
            del os.environ['SDL_FBDEV']
        
        if driver == 'kmsdrm':
            if os.path.exists('/dev/dri'):
                drm_devices = [f for f in os.listdir('/dev/dri') if f.startswith('card')]
                if drm_devices:
                    # Prefer card1 for slideshow display
                    preferred_card = 'card1' if 'card1' in drm_devices else drm_devices[0]
                    os.environ['SDL_DRM_DEVICE'] = f'/dev/dri/{preferred_card}'
                    logger.info(f"  Using DRM device: /dev/dri/{preferred_card}")
        
        try:
            # Initialize pygame
            pygame.quit()
            pygame.init()
            
            # Check if this driver is actually being used
            actual_driver = pygame.display.get_driver()
            logger.info(f"  Pygame initialized with driver: {actual_driver}")
            
            # Try to create a test display
            try:
                # Use small size for testing to avoid taking over full screen
                screen = pygame.display.set_mode((320, 240))
                screen.fill((0, 255, 0))  # Green for success
                pygame.display.flip()
                time.sleep(0.5)  # Brief pause to see the test
                screen.fill((0, 0, 0))   # Black
                pygame.display.flip()
                
                logger.info(f"  ✓ SUCCESS: Created display with {driver} ({description})")
                successful_drivers.append({
                    'driver': driver,
                    'fbdev': fbdev,
                    'description': description,
                    'actual_driver': actual_driver
                })
                
                pygame.display.quit()
                
            except Exception as e:
                logger.warning(f"  ✗ FAILED to create display with {driver}: {e}")
                
        except Exception as e:
            logger.warning(f"  ✗ FAILED to initialize pygame with {driver}: {e}")
    
    # Summary of successful drivers
    if successful_drivers:
        logger.info("=== WORKING DRIVERS SUMMARY ===")
        for i, driver_info in enumerate(successful_drivers, 1):
            logger.info(f"{i}. {driver_info['driver']} - {driver_info['description']}")
            if driver_info['fbdev']:
                logger.info(f"   Framebuffer: {driver_info['fbdev']}")
            logger.info(f"   Actual SDL driver: {driver_info['actual_driver']}")
    else:
        logger.error("✗ No working display drivers found!")
    
    # Clean up
    try:
        pygame.quit()
    except:
        pass

def test_environment_vars():
    """Test current environment variable settings"""
    logger.info("=== ENVIRONMENT VARIABLES ===")
    
    # Current environment state
    env_vars = {
        'SDL_VIDEODRIVER': os.environ.get('SDL_VIDEODRIVER', 'not set'),
        'SDL_FBDEV': os.environ.get('SDL_FBDEV', 'not set'),
        'SDL_DRM_DEVICE': os.environ.get('SDL_DRM_DEVICE', 'not set'),
        'SDL_NOMOUSE': os.environ.get('SDL_NOMOUSE', 'not set'),
        'DISPLAY': os.environ.get('DISPLAY', 'not set'),
    }
    
    for var, value in env_vars.items():
        if var == 'DISPLAY' and value != 'not set':
            logger.warning(f"⚠ {var}: {value} (X11 environment detected - may interfere with framebuffer)")
        elif var.startswith('SDL_') and value != 'not set':
            logger.info(f"✓ {var}: {value}")
        else:
            logger.info(f"  {var}: {value}")

def check_kernel_modules():
    """Check loaded kernel modules"""
    logger.info("=== KERNEL MODULES ===")
    
    modules_to_check = ['drm', 'vc4', 'drm_kms_helper']
    
    try:
        result = subprocess.run(['lsmod'], capture_output=True, text=True)
        lsmod_output = result.stdout
        
        for module in modules_to_check:
            if module in lsmod_output:
                # Extract module info
                for line in lsmod_output.split('\n'):
                    if line.startswith(module):
                        logger.info(f"✓ {line}")
                        break
            else:
                logger.warning(f"✗ Module {module}: not loaded")
                
    except Exception as e:
        logger.error(f"Could not check kernel modules: {e}")

def check_gpu_memory():
    """Check GPU memory allocation"""
    logger.info("=== GPU MEMORY ===")
    
    try:
        # Check GPU memory split
        result = subprocess.run(['vcgencmd', 'get_mem', 'gpu'], capture_output=True, text=True)
        if result.returncode == 0:
            gpu_mem = result.stdout.strip()
            logger.info(f"GPU memory: {gpu_mem}")
            
            # Extract numeric value
            if '=' in gpu_mem:
                mem_value = int(gpu_mem.split('=')[1].replace('M', ''))
                if mem_value < 64:
                    logger.warning(f"⚠ GPU memory ({mem_value}M) may be insufficient for hardware acceleration")
                else:
                    logger.info(f"✓ GPU memory ({mem_value}M) should be sufficient")
        else:
            logger.warning("Could not check GPU memory (vcgencmd not available)")
            
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        logger.warning("Could not check GPU memory allocation")

def run_slideshow_simulation():
    """Simulate slideshow environment for testing"""
    logger.info("=== SLIDESHOW SIMULATION ===")
    
    try:
        import pygame
        
        # Set up environment like slideshow.py does
        os.environ['SDL_FBDEV'] = '/dev/fb1'
        os.environ['SDL_VIDEODRIVER'] = 'fbcon'
        os.environ['SDL_NOMOUSE'] = '1'
        
        logger.info("Setting up slideshow environment (fb1, fbcon, no mouse)")
        
        pygame.quit()
        pygame.init()
        
        actual_driver = pygame.display.get_driver()
        logger.info(f"Slideshow simulation driver: {actual_driver}")
        
        # Test fullscreen mode like the actual slideshow
        try:
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            width, height = screen.get_size()
            logger.info(f"✓ Fullscreen mode successful: {width}x{height}")
            
            # Test basic drawing
            screen.fill((0, 0, 255))  # Blue
            pygame.display.flip()
            time.sleep(1)
            
            screen.fill((0, 0, 0))    # Black
            pygame.display.flip()
            time.sleep(0.5)
            
            logger.info("✓ Basic drawing operations successful")
            
        except Exception as e:
            logger.error(f"✗ Fullscreen simulation failed: {e}")
        finally:
            try:
                pygame.display.quit()
                pygame.quit()
            except:
                pass
                
    except ImportError:
        logger.error("✗ Cannot run simulation - pygame not available")
    except Exception as e:
        logger.error(f"✗ Simulation failed: {e}")

def main():
    """Run all diagnostic tests"""
    logger.info("Starting Pygame Display Diagnostic for Raspberry Pi OS 64-bit")
    logger.info("=" * 60)
    
    check_system_info()
    check_framebuffer_devices()
    check_drm_devices()
    check_boot_config()
    check_sdl2_info()
    check_kernel_modules()
    check_gpu_memory()
    test_environment_vars()
    test_pygame_drivers()
    run_slideshow_simulation()
    
    logger.info("=" * 60)
    logger.info("Diagnostic complete")
    logger.info("If issues persist, check:")
    logger.info("1. User is in 'video' group: sudo usermod -a -G video $USER")
    logger.info("2. Boot config has dual HDMI setup")
    logger.info("3. Framebuffer devices exist and are accessible")
    logger.info("4. pygame is installed: pip install pygame")

if __name__ == "__main__":
    main()