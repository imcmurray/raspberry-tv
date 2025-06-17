# Raspberry Pi TV Slideshow

This project runs a digital slideshow application on a Raspberry Pi (or other Linux-based systems), designed to display images, videos, and websites. It is managed by Ansible for deployment and configuration, and features a Rust-based application for rendering the slideshow content.

## Features

- **Multiple Slide Types**: Supports images, videos, and full-page website displays.
- **Configuration via CouchDB**: Slideshow content and configuration are primarily managed via a CouchDB database.
- **Text Overlays**: Display custom text (including date/time) over slides with configurable font size, color, and position. Scrolling text is supported for longer messages.
- **Headless Operation**: Designed to run on a Raspberry Pi or similar device connected to a TV, typically in a headless setup.
- **Dynamic Updates**: Monitors CouchDB for changes to the slideshow document and automatically refreshes content.
- **Status Reporting**: Updates a status document in CouchDB with information about the currently displayed slide.
- **Attachment Management**: Includes logic to clean up unused image/video attachments from CouchDB to save space.

## Core Components

- **`slideshow_rs` (Rust Application)**: The main application responsible for fetching slideshow configurations, rendering slides (images, videos using FFmpeg, websites using headless Chrome), and managing the display. It uses `egui` for the UI framework.
- **Ansible Playbook (`playbook.yml`)**: Automates the setup of the target device, including installation of system dependencies (Rust, FFmpeg, Chromium, etc.), deployment of the `slideshow_rs` application, and configuration of the systemd service to run the slideshow.
- **CouchDB**: Used as a backend to store slideshow definitions, TV configurations, and status updates.

## Deployment

Deployment is handled via Ansible. Ensure you have Ansible installed on your control machine.

1.  **Prepare Inventory**:
    Update your Ansible inventory file (e.g., `hosts.ini` or pass directly via command line) with the IP address or hostname of your target Raspberry Pi device. Example for command line: `192.168.X.X,` (the comma is important).

2.  **Configure Variables**:
    Review and customize variables in `vars/main.yml` and potentially within the roles if needed (e.g., `service_user`, CouchDB URLs if they differ from defaults).

3.  **Run Ansible Playbook**:
    Execute the playbook using a command similar to:
    ```bash
    ansible-playbook playbook.yml -u YOUR_SSH_USER -kK -i YOUR_PI_IP_OR_HOSTNAME, -e ansible_python_interpreter=/usr/bin/python3
    ```
    - Replace `YOUR_SSH_USER` with the SSH username for your Pi (e.g., `ubuntu`, `pi`).
    - Replace `YOUR_PI_IP_OR_HOSTNAME` with the actual IP address or hostname.
    - `-kK`: Prompts for SSH password and sudo password (become password). If using SSH keys, you might not need `-k`.

## Application Build Process

The `slideshow_rs` Rust application is built directly on the target device by Ansible.
- The `roles/app/tasks/main.yml` playbook includes a task that copies the `slideshow_rs` source code to `/opt/slideshow_rs` on the target.
- It then runs `cargo build --release` within that directory to compile the application.
- The compiled binary (e.g., `/opt/slideshow_rs/target/release/slideshow_rs`) is then managed by a systemd service (`slideshow_rs.service`).

## System Requirements (Target Device)

- A Debian-based Linux distribution (e.g., Raspberry Pi OS, Ubuntu).
- Rust and Cargo (installed by Ansible).
- FFmpeg libraries (development headers for build, runtime libraries for execution - installed by Ansible).
- Chromium browser (installed by Ansible, for website slides).
- Standard build tools (`build-essential`, `pkg-config` - installed by Ansible).
- X11 and relevant development libraries (`libgtk-3-dev`, `libxcb-render0-dev`, `libxcb-shape0-dev`, `libxcb-xfixes0-dev`, `libxkbcommon-dev`, `libgl1-mesa-dev`, `libegl1-mesa-dev` etc.) for the `egui` graphical interface. Ansible attempts to install common ones.

## Configuration File

The application uses a configuration file located at `/etc/slideshow.conf` on the target device. This file is provisioned by Ansible (from `templates/slideshow.conf.j2`) and contains settings like:

```ini
[settings]
couchdb_url = http://your_couchdb_server:5984
tv_uuid = your_tv_unique_identifier
manager_url = http://your_slideshow_manager_url
```

Ensure these values are correctly set for your environment, typically by modifying `vars/main.yml` which populates the template.

---

This README provides a general overview. For more detailed information on specific components or troubleshooting, refer to the respective Ansible roles and the `slideshow_rs` application source code.