#!/usr/bin/env python3
"""
Pygame Display Diagnostic Script
Helps debug display driver issues on Raspberry Pi
"""

import os
import sys
import subprocess
import logging

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
    
    # User groups
    try:
        result = subprocess.run(['groups'], capture_output=True, text=True)
        logger.info(f"User groups: {result.stdout.strip()}")
    except:
        logger.error("Could not get user groups")
    
    # Check devices
    logger.info("=== DEVICE STATUS ===")
    
    # DRM devices
    drm_path = "/dev/dri"
    if os.path.exists(drm_path):
        drm_devices = os.listdir(drm_path)
        logger.info(f"DRM devices: {drm_devices}")
        for device in drm_devices:
            device_path = os.path.join(drm_path, device)
            readable = os.access(device_path, os.R_OK)
            writable = os.access(device_path, os.W_OK)
            logger.info(f"  {device_path}: readable={readable}, writable={writable}")
    else:
        logger.warning("No /dev/dri directory found")
    
    # Framebuffer devices
    fb_devices = [f"/dev/fb{i}" for i in range(3)]
    for fb in fb_devices:
        if os.path.exists(fb):
            readable = os.access(fb, os.R_OK)
            writable = os.access(fb, os.W_OK)
            logger.info(f"Framebuffer {fb}: exists=True, readable={readable}, writable={writable}")
        else:
            logger.info(f"Framebuffer {fb}: exists=False")

def check_sdl2_info():
    """Check SDL2 installation and capabilities"""
    logger.info("=== SDL2 INFORMATION ===")
    
    # Check SDL2 packages
    packages_to_check = [
        'libsdl2-2.0-0',
        'libsdl2-dev', 
        'libdrm2',
        'libgbm1',
        'mesa-utils'
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
                            logger.info(f"Package {package}: {parts[2]} (installed)")
                        break
            else:
                logger.warning(f"Package {package}: not installed")
        except:
            logger.error(f"Could not check package {package}")

def test_pygame_drivers():
    """Test pygame driver capabilities"""
    logger.info("=== PYGAME DRIVER TESTS ===")
    
    try:
        import pygame
        logger.info(f"Pygame version: {pygame.version.ver}")
        logger.info(f"SDL version: {pygame.version.SDL}")
    except ImportError as e:
        logger.error(f"Could not import pygame: {e}")
        return
    
    # Test driver availability without initializing display
    drivers_to_test = ['kmsdrm', 'fbcon', 'directfb', 'wayland', 'x11']
    
    for driver in drivers_to_test:
        logger.info(f"Testing driver: {driver}")
        
        # Set environment
        os.environ['SDL_VIDEODRIVER'] = driver
        if driver == 'fbcon':
            os.environ['SDL_FBDEV'] = '/dev/fb1'
            os.environ['SDL_NOMOUSE'] = '1'
        elif driver == 'kmsdrm':
            if os.path.exists('/dev/dri'):
                drm_devices = [f for f in os.listdir('/dev/dri') if f.startswith('card')]
                if drm_devices:
                    os.environ['SDL_DRM_DEVICE'] = f'/dev/dri/{drm_devices[0]}'
        
        try:
            # Initialize pygame
            pygame.quit()
            pygame.init()
            
            # Check if this driver is actually being used
            actual_driver = pygame.display.get_driver()
            logger.info(f"  Pygame initialized with driver: {actual_driver}")
            
            # Try to create a small display (not fullscreen to avoid issues)
            try:
                screen = pygame.display.set_mode((100, 100))
                screen.fill((255, 0, 0))  # Red
                pygame.display.flip()
                logger.info(f"  SUCCESS: Created display with {driver}")
                pygame.display.quit()
            except Exception as e:
                logger.warning(f"  FAILED to create display with {driver}: {e}")
                
        except Exception as e:
            logger.warning(f"  FAILED to initialize pygame with {driver}: {e}")
    
    # Clean up
    try:
        pygame.quit()
    except:
        pass

def test_environment_vars():
    """Test various environment variable combinations"""
    logger.info("=== ENVIRONMENT VARIABLE TESTS ===")
    
    env_vars = {
        'SDL_VIDEODRIVER': os.environ.get('SDL_VIDEODRIVER', 'not set'),
        'SDL_FBDEV': os.environ.get('SDL_FBDEV', 'not set'),
        'SDL_DRM_DEVICE': os.environ.get('SDL_DRM_DEVICE', 'not set'),
        'DISPLAY': os.environ.get('DISPLAY', 'not set'),
        'WAYLAND_DISPLAY': os.environ.get('WAYLAND_DISPLAY', 'not set'),
    }
    
    for var, value in env_vars.items():
        logger.info(f"{var}: {value}")

def check_kernel_modules():
    """Check loaded kernel modules"""
    logger.info("=== KERNEL MODULES ===")
    
    modules_to_check = ['drm', 'vc4', 'drm_kms_helper', 'drm_display_helper']
    
    try:
        result = subprocess.run(['lsmod'], capture_output=True, text=True)
        lsmod_output = result.stdout
        
        for module in modules_to_check:
            if module in lsmod_output:
                # Extract module info
                for line in lsmod_output.split('\n'):
                    if line.startswith(module):
                        logger.info(f"Module {module}: {line}")
                        break
            else:
                logger.warning(f"Module {module}: not loaded")
                
    except Exception as e:
        logger.error(f"Could not check kernel modules: {e}")

def main():
    """Run all diagnostic tests"""
    logger.info("Starting Pygame Display Diagnostic")
    logger.info("=" * 50)
    
    check_system_info()
    check_sdl2_info()
    check_kernel_modules()
    test_environment_vars()
    test_pygame_drivers()
    
    logger.info("=" * 50)
    logger.info("Diagnostic complete")

if __name__ == "__main__":
    main()