# Raspberry Pi TV

*COMING SOON*

---

#### Future Docs
SSH to the raspberry pi. This establishes a known connection. Then:

`ansible-playbook -i "raspberry_pi," playbook.yml`

Replace `"raspberry_pi"` with the Pi's IP or hostname.

- Assumes CouchDB is accessible without authentication
- A reboot is triggered if HDMI or blanking settings change
- The Pi uses DHCP for networking unless you modify the network role with a static IP.