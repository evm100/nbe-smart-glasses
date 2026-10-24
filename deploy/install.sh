#!/usr/bin/env bash
# Install / update the NBE PPG lab server on Ubuntu 26.04 / Debian 13 (run as root, from the repo).
#
#   sudo deploy/install.sh            first install: user, folders, venv, systemd units
#   sudo deploy/install.sh            again after `git pull` = update (keeps config + data)
#
# Layout it creates:
#   /opt/nbe/smart-glasses   the repo (this checkout is copied there unless it already is there)
#   /opt/nbe/venv            Python environment with sglab
#   /etc/nbe/server.toml     your config (from deploy/server.example.toml, only if missing)
#   /srv/nbe/ppg-data        recordings + catalog          (owner nbe, mode 750)
#   /srv/nbe/pki             device CA + certificates       (owner nbe, mode 700)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/nbe/smart-glasses

apt-get install -y --no-install-recommends python3 python3-venv python3-pip git rsync sqlite3 >/dev/null
id nbe >/dev/null 2>&1 || useradd --system --home /opt/nbe --shell /usr/sbin/nologin nbe
install -d -o root -g root -m 755 /opt/nbe
install -d -o root -g nbe -m 750 /etc/nbe
install -d -o nbe -g nbe -m 750 /srv/nbe /srv/nbe/ppg-data
install -d -o nbe -g nbe -m 700 /srv/nbe/pki

if [[ "$SRC" != "$DEST" ]]; then
  rsync -a --delete --exclude data/ --exclude .venv/ --exclude 'firmware/esp-idf/*/build/' "$SRC/" "$DEST/"
fi
chown -R root:root "$DEST"          # code is read-only for the service

# a release upgrade swaps python3.X, leaving the venv's site-packages orphaned - rebuild it
PYVER=$(python3 -c 'import sys; print("python%d.%d" % sys.version_info[:2])')
[[ -d /opt/nbe/venv/lib/$PYVER ]] || rm -rf /opt/nbe/venv
[[ -d /opt/nbe/venv ]] || python3 -m venv /opt/nbe/venv
/opt/nbe/venv/bin/pip install -q --upgrade pip
/opt/nbe/venv/bin/pip install -q "$DEST[server]"

if [[ ! -f /etc/nbe/server.toml ]]; then
  install -o root -g nbe -m 640 "$DEST/deploy/server.example.toml" /etc/nbe/server.toml
  echo ">> edit /etc/nbe/server.toml (domain names, IPs) before starting the service"
fi
install -m 644 "$DEST"/deploy/systemd/nbe-lab.service "$DEST"/deploy/systemd/nbe-snapshot.* /etc/systemd/system/
systemctl daemon-reload

cat <<MSG
Installed. As the service user you can now run admin commands, e.g.:
  sudo -u nbe SGLAB_ROOT=$DEST SGLAB_SERVER_CONFIG=/etc/nbe/server.toml /opt/nbe/venv/bin/sglab doctor
(tip: alias nbe='sudo -u nbe SGLAB_ROOT=$DEST SGLAB_SERVER_CONFIG=/etc/nbe/server.toml /opt/nbe/venv/bin/sglab')
Then: systemctl enable --now nbe-lab nbe-snapshot.timer      (after an update: systemctl restart nbe-lab)
MSG
