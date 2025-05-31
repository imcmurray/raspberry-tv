import subprocess
import time
import os
from datetime import datetime

# Define active hours (e.g., 9 AM to 5 PM)
active_start = datetime.strptime("09:00", "%H:%M").time()
active_end = datetime.strptime("17:00", "%H:%M").time()

def is_active_time():
    """Check if current time is within active hours."""
    now = datetime.now().time()
    return active_start <= now <= active_end

def is_raspberry_pi_os():
    """Check if running on Raspberry Pi OS."""
    return os.path.exists('/etc/rpi-issue')

def set_hdmi_power(on):
    """Turn slideshow HDMI output (HDMI1) on or off, leave console HDMI (HDMI0) always on."""
    if is_raspberry_pi_os():
        # Control only HDMI1 (slideshow display), leave HDMI0 (console) always on
        cmd = "vcgencmd display_power {} 2".format(1 if on else 0)
        subprocess.run(cmd, shell=True)
    else:
        # On non-Pi systems (like Ubuntu), log the action but don't attempt vcgencmd
        action = "on" if on else "off"
        print(f"Would turn slideshow HDMI {action} (vcgencmd not available on this OS)")

# Main loop
while True:
    if is_active_time():
        set_hdmi_power(True)  # HDMI on during active hours
    else:
        set_hdmi_power(False)  # HDMI off outside active hours
    time.sleep(60)  # Check every minute