# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **centrally-managed digital signage system** for Raspberry Pi devices running **Raspberry Pi OS 64-bit**. Multiple TVs can be deployed and managed through a central CouchDB server for content distribution and monitoring.

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

### Issue #8: Simplified to Raspberry Pi OS Only (RESOLVED)

**Problem**: Complex multi-OS support was causing display driver issues and unnecessary complexity.

**Solution Implemented**:
1. **Removed Ubuntu Support**: Eliminated all Ubuntu detection and conditional logic
2. **Simplified Display Setup**: Focus only on Raspberry Pi OS framebuffer (fbcon) drivers
3. **Streamlined Ansible**: Removed Ubuntu-specific packages and configurations
4. **Simplified pygame Installation**: Use standard pygame installation for Pi OS

**Changes Made**:
- Removed `is_ubuntu()` detection from `slideshow.py`
- Simplified display driver attempts to: fbcon (/dev/fb1), fbcon (/dev/fb0), kmsdrm fallback
- Removed Ubuntu SDL2 compilation complexity from Ansible playbook
- Focus solely on Raspberry Pi OS 64-bit support

**Status**: ✅ **RESOLVED** - System now focused on Pi OS only for better reliability

**Testing**: Deploy simplified playbook on Raspberry Pi OS 64-bit system.

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

- **Raspberry Pi OS 64-bit only**: This system is designed exclusively for Raspberry Pi OS 64-bit
- System assumes CouchDB is accessible without authentication
- Reboot is triggered automatically if HDMI settings change
- Pi uses DHCP unless network role is modified for static IP
- All image content is resized to HD resolution before storage
- Web interfaces require CouchDB CORS configuration for cross-origin requests
- **Framebuffer Display**: Uses /dev/fb1 (HDMI1) by default, falls back to /dev/fb0 (HDMI0)

## Memories

- X11 should never be picked as an option for this project