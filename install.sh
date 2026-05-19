#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-openwrtbuilder}"
APP_DIR="${APP_DIR:-/opt/openwrt-builder}"
DATA_DIR="${DATA_DIR:-/var/lib/openwrt-builder}"
SERVICE_FILE="/etc/systemd/system/openwrt-builder.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 make gcc g++ gawk gettext git unzip rsync file wget curl ca-certificates \
  tar zstd xz-utils bzip2 patch perl libncurses-dev libssl-dev python3-distutils time

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR" "$DATA_DIR"
cp app.py "$APP_DIR/app.py"
cp openwrt-builder.service "$SERVICE_FILE"
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$APP_DIR#" "$SERVICE_FILE"
sed -i "s#^Environment=OWB_DATA=.*#Environment=OWB_DATA=$DATA_DIR#" "$SERVICE_FILE"
sed -i "s#^User=.*#User=$APP_USER#" "$SERVICE_FILE"

chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 0755 "$APP_DIR/app.py"

systemctl daemon-reload
systemctl enable --now openwrt-builder.service

echo "Installed. Open: http://$(hostname -I | awk '{print $1}'):8088"
echo "Logs: journalctl -u openwrt-builder -f"
