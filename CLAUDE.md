# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **centrally-managed digital signage system** for Raspberry Pi devices. Multiple TVs can be deployed and managed through a central CouchDB server for content distribution and monitoring.

## Core Architecture

### System Components
- **`slideshow_rs`** - Main Rust application using `egui` (via `eframe`) for fullscreen display of images, videos (via `ffmpeg-next`), and websites (via `headless_chrome`).
- **`hdmi_sleep.py`** - Python-based power management script for office hours (9 AM - 5 PM). (Assumed to be kept, uses Python)
- **`dashboard.html`** - Real-time monitoring interface for TV status (interacts with CouchDB).
- **`manager.html`** - Web interface for managing slideshow content and TV configuration (interacts with CouchDB).
- **Ansible playbook** - Automated deployment and configuration system for the Rust application and its dependencies.

### Central Dependencies
- **CouchDB Server** (e.g., `couchdb.utb.circ10.dcn:5984`) - Stores slideshow content, configurations, and TV status.
- **Manager Server** (e.g., `couchdb.utb.circ10.dcn:8000`) - Hosts the management web interface.
- **systemd services** - `slideshow_rs.service` (for the Rust app) and `hdmi_sleep.service`.

### Configuration Files
- `/etc/slideshow.conf` - Main configuration (CouchDB URL, TV UUID, manager URL). Read by `slideshow_rs`.
- `vars/main.yml` - Ansible variables for deployment.
- Service template for Rust app: `templates/slideshow_rs.service.j2`.

## Common Development Tasks

### Deployment
```bash
# Deploy to target device (e.g., Raspberry Pi)
ansible-playbook playbook.yml -u YOUR_SSH_USER -kK -i YOUR_PI_IP_OR_HOSTNAME, -e ansible_python_interpreter=/usr/bin/python3
```
(Replace placeholders as needed)

### Building and Running `slideshow_rs`
- **Building on target (done by Ansible)**: `cd /opt/slideshow_rs && cargo build --release`
- **Building locally for development**: `cd slideshow_rs && cargo build`
- **Running locally (requires X11 and appropriate environment for egui, might need specific setup)**:
  ```bash
  # Example: Ensure DISPLAY is set, you might need to be in a desktop environment
  # or use a virtual framebuffer like Xvfb for true headless testing of the UI part.
  # The application itself is designed for fullscreen on a device with a display server.
  cd slideshow_rs && cargo run
  ```
  *Note: For local runs, `/etc/slideshow.conf` would need to exist or be mocked, and CouchDB/Chromium accessible.*

### Testing Changes
- **CouchDB connectivity**: Verify URLs in `vars/main.yml` (used to populate `/etc/slideshow.conf`) are accessible.
- **Media Rendering**: Test image, video, and website slide rendering by `slideshow_rs`.
- **Service status**: Check `systemd` service logs for `slideshow_rs.service` on the target device after deployment (e.g., `journalctl -u slideshow_rs.service -f`).
- **Web interface**: Test `dashboard.html` and `manager.html` with your CouchDB instance.

### Ansible Roles Structure
- **common**: Package updates, Rust installation, FFmpeg dev libraries, Chromium, X11/GTK dev libs (for `eframe`), other common dependencies.
- **network**: Network configuration (currently DHCP).
- **hdmi**: Display settings, console blanking. (May trigger reboot if settings change)
- **time**: Timezone configuration.
- **app**: Deploys `slideshow_rs` source, builds it, configures and starts `slideshow_rs.service`. Also deploys `hdmi_sleep.py` and `dashboard.html`.

## Key System Behavior

### TV State Management
- `slideshow_rs` fetches configuration from CouchDB using its UUID specified in `/etc/slideshow.conf`.
- Real-time updates via CouchDB `_changes` feed, triggering `slideshow_rs` to refetch its document.
- Status reporting (current slide, timestamp) back to `status_<uuid>` documents in CouchDB by `slideshow_rs`.
- Displays a default message if the CouchDB document is not found or improperly configured.

### Content Features
- **Image Display**: Scaled to fit display (e.g., 1920x1080) by `egui`.
- **Video Playback**: Uses `ffmpeg-next` for decoding, rendered via `egui`.
- **Website Display**: Uses `headless_chrome` to capture screenshots, displayed as images.
- **Text Overlays**: Rendered by `egui` with configurable properties (position, color, size, background).
- **Scrolling Text**: Supported for text overlays that exceed available width.
- **Dynamic Content Updates**: `slideshow_rs` refreshes content upon detecting changes in its CouchDB document.
- **Attachment Cleanup**: `slideshow_rs` periodically cleans up unreferenced attachments from its CouchDB document.

### Power Management
- `hdmi_sleep.py` script controls HDMI output based on office hours (assumed to use `vcgencmd display_power` or similar). `slideshow_rs` itself does not directly manage HDMI power but should continue running.

## System Dependencies for `slideshow_rs` (Managed by Ansible)
- **Rust/Cargo**
- **Build tools**: `build-essential`, `pkg-config`
- **FFmpeg**: `libavformat-dev`, `libavcodec-dev`, `libavutil-dev`, `libswscale-dev`
- **Chromium**: `chromium-browser` (for headless website capture)
- **eframe/egui GUI toolkit**:
    - `libgtk-3-dev` (or `libatk1.0-dev`, `libcairo2-dev`, `libgdk-pixbuf2.0-dev`, `libpango1.0-dev`)
    - X11 dev libraries: `libxcb-render0-dev`, `libxcb-shape0-dev`, `libxcb-xfixes0-dev`, `libxkbcommon-dev`
    - Fontconfig: `libfontconfig1-dev`
    - OpenGL: `libgl1-mesa-dev`, `libegl1-mesa-dev` (or similar depending on target GPU/drivers)

## Issue Tracking & Known Solutions

*This section should be updated based on experiences with the Rust implementation.*

### (Example) Issue: Headless Chrome fails on minimal systems
**Problem**: `headless_chrome` cannot find Chrome or fails to launch.
**Symptoms**: Errors related to browser not found or sandbox issues.
**Solution**: Ensure `chromium-browser` is correctly installed by Ansible. On some systems, Chrome might need specific launch options like `--no-sandbox` (use with caution, implies security risks) if running in a very restricted environment. Check permissions for the `service_user`.

### (Example) Issue: FFmpeg build or runtime errors
**Problem**: `ffmpeg-next` crate fails to build, or `slideshow_rs` fails at runtime with FFmpeg errors.
**Symptoms**: Build errors related to finding FFmpeg libraries, or runtime errors about codec/format issues.
**Solution**: Verify all FFmpeg `*-dev` packages are installed by Ansible (`libavformat-dev`, `libavcodec-dev`, `libavutil-dev`, `libswscale-dev`, `pkg-config`). Ensure `pkg-config` can find them. For runtime, ensure FFmpeg shared libraries are accessible.

*(Previous Python-specific issues like Pygame display driver problems or Python Selenium issues are now obsolete and have been removed.)*

## Important Notes

- System assumes CouchDB is accessible (check `/etc/slideshow.conf` for URL).
- Reboot might be triggered by Ansible if HDMI or console blanking settings change (via `hdmi` role).
- Pi typically uses DHCP unless network role is modified.
- Web interfaces (`dashboard.html`, `manager.html`) require CouchDB CORS configuration for cross-origin requests.

## Memories

- (This section can be kept or removed; the X11 note is less relevant now as `egui` is X11/Wayland based for its native backend.)