#!/usr/bin/env bash
# Installeert of werkt de slingbag-software bij op de Raspberry Pi 3B.
# Wordt uitgevoerd door install.sh (release) of deploy.sh (vanaf een computer); kan ook handmatig: bash ~/slingbag/deploy/install-pi.sh
set -euo pipefail

APP_DIR="$HOME/slingbag"
DATA_DIR="$HOME/slingbag-data"
SERVICE_USER="$(id -un)"
HOTSPOT_SSID="${HOTSPOT_SSID:-Slingbag}"
HOTSPOT_PSK="${HOTSPOT_PSK:-slingbag-demo}"

step() { printf '\n==> %s\n' "$*"; }

step "Systeempakketten installeren"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-flask python3-waitress python3-gpiozero alsa-utils ffmpeg avahi-daemon curl
# GPIO-backend voor gpiozero; staat in de Raspberry Pi OS-repository.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-lgpio \
  || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-rpi-lgpio \
  || echo "LET OP: geen lgpio gevonden; gpiozero probeert een andere GPIO-backend."

step "Spraakengine Piper en stemmen"
if [ "$(uname -m)" != "aarch64" ]; then
  echo "LET OP: dit is geen 64-bit Raspberry Pi OS; Piper werkt dan niet. Alleen eigen opnames zijn bruikbaar."
else
  bash "$APP_DIR/deploy/download-vendor.sh"
  rm -rf "$APP_DIR/vendor/piper"
  tar -xzf "$APP_DIR/vendor/piper_linux_aarch64.tar.gz" -C "$APP_DIR/vendor"
  echo "Hallo" | "$APP_DIR/vendor/piper/piper" --model "$APP_DIR/vendor/voices/nl_NL-pim-medium.onnx" \
    --output_file /tmp/slingbag-test.wav >/dev/null 2>&1 && echo "Piper werkt." || echo "LET OP: Piper-test mislukt."
fi

step "Rechten voor GPIO en audio"
sudo usermod -aG gpio,audio "$SERVICE_USER"
# De service mag alleen afsluiten en de klok gelijkzetten zonder wachtwoord.
echo "$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/date" | sudo tee /etc/sudoers.d/slingbag >/dev/null
sudo chmod 440 /etc/sudoers.d/slingbag

step "Audio naar de 3,5mm-uitgang"
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
grep -q '^dtparam=audio=on' "$CONFIG_TXT" || echo 'dtparam=audio=on' | sudo tee -a "$CONFIG_TXT" >/dev/null
amixer -q -c Headphones sset PCM 70% unmute 2>/dev/null || echo "Koptelefoonuitgang nog niet zichtbaar; na een herstart opnieuw proberen."

step "Wifi: eigen netwerk '$HOTSPOT_SSID' als terugvaloptie"
sudo raspi-config nonint do_wifi_country NL || true
sudo rfkill unblock wifi || true
if command -v nmcli >/dev/null; then
  if ! nmcli -t -f NAME connection show | grep -qx slingbag-hotspot; then
    sudo nmcli connection add type wifi ifname wlan0 con-name slingbag-hotspot ssid "$HOTSPOT_SSID" \
      802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PSK" \
      connection.autoconnect yes connection.autoconnect-priority -10 >/dev/null
    echo "Hotspot aangemaakt (alleen actief als geen bekend wifinetwerk in de buurt is)."
  fi
fi

step "Service installeren"
mkdir -p "$DATA_DIR"
sudo tee /etc/systemd/system/slingbag.service >/dev/null <<UNIT
[Unit]
Description=Slingbag-activiteitenhulp
After=network.target sound.target

[Service]
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 -m slingbag --data $DATA_DIR --poort 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable slingbag.service >/dev/null
sudo systemctl restart slingbag.service

step "Controle"
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1/api/status >/dev/null; then
    echo "De slingbag draait: open http://$(hostname).local op je telefoon."
    exit 0
  fi
  sleep 1
done
echo "De service reageert nog niet. Bekijk: journalctl -u slingbag -n 50"
exit 1
