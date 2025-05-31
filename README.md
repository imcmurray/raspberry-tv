# Raspberry Pi TV

*COMING SOON*

---

#### Future Docs
SSH to the raspberry pi. This establishes a known connection. Then:

`ansible-playbook playbook.yml -u ubuntu -kK -i 192.168.IP.HERE, -e ansible_python_interpreter=/usr/bin/python3`

Replace `"192.168.IP.HERE"` with the Pi's IP. The comma after the IP is needed.

- Assumes CouchDB is accessible without authentication
- A reboot is triggered if HDMI or blanking settings change
- The Pi uses DHCP for networking unless you modify the network role with a static IP.