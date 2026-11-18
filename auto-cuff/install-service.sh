#!/usr/bin/env bash
# Install the auto-cuff systemd service for the user who owns this checkout.
#   sudo ./install-service.sh          install + enable + start
#   journalctl -u auto-cuff -f         follow the log
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$(stat -c %U "$DIR")}"
[ -x "$DIR/.venv/bin/python" ] || { echo "no venv: python3 -m venv $DIR/.venv && $DIR/.venv/bin/pip install -r $DIR/requirements.txt"; exit 1; }
[ -f "$DIR/node/sdkconfig.nbe" ] || { echo "no credentials: unzip the node's credentials.zip from the server into $DIR/node/"; exit 1; }
# GPIO + Bluetooth access without root
usermod -aG gpio,bluetooth "$USER_NAME" || true
sed -e "s#@DIR@#$DIR#g" -e "s#@USER@#$USER_NAME#g" "$DIR/systemd/auto-cuff.service" > /etc/systemd/system/auto-cuff.service
systemctl daemon-reload
systemctl enable --now auto-cuff.service
systemctl --no-pager status auto-cuff.service | head -n 12
