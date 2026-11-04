#!/bin/sh
# Move the Pi to another Wi-Fi network over SSH without losing it halfway:
#   ssh -t pi@<current address> ~/ctp500/pi/join-wifi.sh MyWiFi
# It asks for the password while the current connection still works, saves it, then switches in the
# background (this window disconnects). The old network stays as a fallback: if the new one fails, the Pi
# goes back to it.
set -eu
SSID=${1:?usage: join-wifi.sh SSID}

printf 'Wi-Fi password for %s: ' "$SSID"
stty -echo
read -r PW
stty echo
echo

if nmcli -t -f NAME connection show | grep -qx "$SSID"; then
    sudo nmcli connection modify "$SSID" 802-11-wireless.ssid "$SSID" wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$PW" connection.autoconnect yes connection.autoconnect-priority 10
else
    sudo nmcli connection add type wifi ifname wlan0 con-name "$SSID" ssid "$SSID" wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$PW" connection.autoconnect yes connection.autoconnect-priority 10 >/dev/null
fi
unset PW

echo "Saved. Switching to $SSID now; this window will disconnect."
sudo nohup nmcli --wait 60 connection up "$SSID" >/tmp/join-wifi.log 2>&1 &
sleep 2
