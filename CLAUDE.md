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

## Important Notes

- System assumes CouchDB is accessible without authentication
- Reboot is triggered automatically if HDMI settings change
- Pi uses DHCP unless network role is modified for static IP
- All image content is resized to HD resolution before storage
- Web interfaces require CouchDB CORS configuration for cross-origin requests