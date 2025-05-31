# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **centrally-managed digital signage system** for Raspberry Pi devices. Multiple TVs can be deployed and managed through a central CouchDB server for content distribution and monitoring.

## Core Architecture

### System Components
- **slideshow.py** - Main Python application using pygame for fullscreen image display
- **hdmi_sleep.py** - Power management script for office hours (9 AM - 5 PM)  
- **dashboard.html** - Real-time monitoring interface for TV status
- **manager.html** - Web interface for managing slideshow content and TV configuration
- **Ansible playbook** - Automated deployment and configuration system

### Central Dependencies
- **CouchDB Server** (`couchdb.utb.circ10.dcn:5984`) - Stores slideshow content, configurations, and TV status
- **Manager Server** (`couchdb.utb.circ10.dcn:8000`) - Hosts the management web interface
- **systemd services** - `slideshow.service` and `hdmi_sleep.service` for automatic startup

### Configuration Files
- `/etc/slideshow.conf` - Main configuration (CouchDB URL, TV UUID, office hours, timezone)
- `vars/main.yml` - Ansible variables for deployment
- Service templates in `templates/` directory

## Common Development Tasks

### Deployment
```bash
# Deploy to Raspberry Pi (replace with actual IP/hostname)
ansible-playbook -i "raspberry_pi," playbook.yml
```

### Testing Changes
- **CouchDB connectivity**: Verify URLs in `vars/main.yml` are accessible
- **Image processing**: Test pygame image loading and scaling in slideshow.py
- **Service status**: Check systemd service logs on Pi after deployment
- **Web interface**: Test dashboard.html and manager.html with local CouchDB instance

### Ansible Roles Structure
- **common** - Package updates, Python dependencies
- **network** - Network configuration (currently DHCP)
- **hdmi** - Display settings, console blanking, triggers reboot
- **time** - Timezone configuration  
- **app** - Deploys scripts, creates services, sets up web dashboard

## Key System Behavior

### TV State Management
- TVs fetch configuration from CouchDB using their UUID
- Real-time updates via CouchDB `_changes` feed
- Status reporting back to `status_<uuid>` documents
- Automatic failover to default message if CouchDB unavailable

### Content Features
- Image scaling to fit 1920x1080 display
- Text overlays with positioning, colors, transitions
- Scrolling text support
- Fade in/out transitions between slides
- Dynamic content updates without restart

### Power Management
- HDMI output automatically controlled by office hours
- `vcgencmd display_power` commands for Pi-specific power control

## Issue Tracking & Known Solutions

### Issue #8: Display Driver Initialization Failures (IN PROGRESS)

**Problem**: Pygame fails to initialize any display drivers on Ubuntu Raspberry Pi systems, with all drivers (kmsdrm, fbcon, directfb) reporting "not available".

**Symptoms**:
- All pygame display drivers fail with "not available" errors
- DRM devices and framebuffers exist but are not accessible to pygame
- System shows proper hardware detection but pygame cannot use drivers

**Root Cause**: 
- Pygame compiled without proper SDL2 video driver support
- Missing SDL2 development packages for hardware-specific drivers
- Potential permissions or runtime library issues

**Solution Implemented**:
1. **Enhanced Package Installation** (Ansible):
   - Added missing SDL2 development packages: `libkms1`, `libdrm-dev`, `libgbm-dev`, `libegl1-mesa-dev`, `libgles2-mesa-dev`
   - Added keyboard support package: `kbd`

2. **Pygame Rebuild with Native Compilation**:
   - Force rebuild pygame from source with `--no-binary=pygame`
   - Set proper environment variables for SDL2 library detection
   - Clear pip cache to ensure clean rebuild

3. **Diagnostic and Fallback System** in `slideshow.py`:
   - Created comprehensive diagnostic script (`pygame_diagnostic.py`)
   - Added dummy driver fallback for logging/debugging when hardware fails
   - Implemented safe display update function for graceful dummy mode handling
   - Enhanced error reporting with automatic diagnostic execution

4. **Testing Infrastructure**: 
   - Deployed diagnostic script via Ansible for on-device troubleshooting
   - Added comprehensive system, package, and driver capability checking

**Status**: 🔄 **IN PROGRESS** - Enhanced driver support and diagnostics implemented, awaiting deployment testing

**Testing**: Deploy updated Ansible playbook and run diagnostic script on affected Pi to verify fixes.

### Issue #2: Chrome Driver Session Management (RESOLVED)

**Problem**: Chrome driver session management issues when capturing website screenshots, particularly evident during testing on Arch Linux systems.

**Symptoms**:
- Chrome driver session leaks and "Target window already closed" errors
- Missing dependencies on fresh Arch installations
- Website screenshot failures with poor error handling

**Root Cause**: 
- Inadequate error handling and resource management in Selenium WebDriver
- Missing Chrome/Chromium, chromedriver, and selenium packages on Arch Linux
- Arch-specific path differences for Chromium binary location

**Solution Implemented**:
1. **Dependency Installation** (Arch Linux):
   ```bash
   sudo pacman -S chromium chromedriver python-selenium
   ```

2. **Code Fixes** in `slideshow.py`:
   - Proper driver cleanup with try/finally blocks
   - Retry logic (3 attempts) for failed captures
   - Session validation before screenshots
   - Arch-specific Chrome flags for stability
   - Auto-detection of Chromium binary at `/usr/bin/chromium`
   - Enhanced error messages with Arch installation commands

3. **Testing Infrastructure**: 
   - Created `test_chrome.py` for verifying Chrome/Selenium setup
   - Validates chromedriver, chromium, and basic screenshot functionality

**Status**: ✅ **RESOLVED** - Fixed in commits `ac940a0` and later Arch-specific improvements

**Testing**: Verified working on Arch Linux after dependency installation and code updates.

## Important Notes

- System assumes CouchDB is accessible without authentication
- Reboot is triggered automatically if HDMI settings change
- Pi uses DHCP unless network role is modified for static IP
- All image content is resized to HD resolution before storage
- Web interfaces require CouchDB CORS configuration for cross-origin requests
- **Arch Linux users**: Ensure Chromium, chromedriver, and python-selenium are installed before running slideshow

## Memories

- X11 should never be picked as an option for this project