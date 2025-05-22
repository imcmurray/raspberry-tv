import subprocess
import time
from datetime import datetime

# Define active hours (e.g., 9 AM to 5 PM)
active_start = datetime.strptime("09:00", "%H:%M").time()
active_end = datetime.strptime("17:00", "%H:%M").time()

def is_active_time():
    """Check if current time is within active hours."""
    now = datetime.now().time()
    return active_start <= now <= active_end

def set_hdmi_power(on):
    """Turn HDMI output on or off."""
    cmd = "vcgencmd display_power {}".format(1 if on else 0)
    subprocess.run(cmd, shell=True)

# Main loop
while True:
    if is_active_time():
        set_hdmi_power(True)  # HDMI on during active hours
    else:
        set_hdmi_power(False)  # HDMI off outside active hours
    time.sleep(60)  # Check every minute