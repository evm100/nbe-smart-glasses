#!/usr/bin/env bash
# Hand NBE processing to the worker LXC (deploy/worker). Run once on evmserver; re-runnable.
#
#   sudo deploy/evmserver/enable-remote-processing.sh
#
# Updates the code (install.sh), sets [processing] mode = "remote", adds the /api/worker/ nginx
# location (bigger uploads), creates the worker-role user `nbe-worker` and prints a new API token
# for it - paste that into setup-worker.sh in the LXC. Recordings, results and OTA images stay here.
# Back to in-process processing: set mode = "local" in /etc/nbe/server.toml, restart nbe-lab.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /
REPO="$(cd "$HERE/../.." && pwd)"
DOMAIN=nbe.evm100.org
nbe() { sudo -u nbe SGLAB_ROOT=/opt/nbe/smart-glasses SGLAB_SERVER_CONFIG=/etc/nbe/server.toml MPLCONFIGDIR=/tmp/matplotlib \
          /opt/nbe/venv/bin/sglab "$@"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "code update"
"$REPO/deploy/install.sh" >/dev/null

step "/etc/nbe/server.toml"
if grep -q '^\[processing\]' /etc/nbe/server.toml; then
  sed -i '/^\[processing\]/,/^\[/ s/^mode *=.*/mode = "remote"/' /etc/nbe/server.toml
else
  printf '\n[processing]\nmode = "remote"                        # Layers 1-2 run on the worker LXC (deploy/worker)\n' \
    >> /etc/nbe/server.toml
fi
sed -n '/^\[processing\]/,/^$/p' /etc/nbe/server.toml

step "nginx"
install -m 644 "$HERE/nginx-nbe.conf" /etc/nginx/sites-available/$DOMAIN
nginx -t && systemctl reload nginx

step "worker account"
if nbe user list | grep -qw nbe-worker; then echo "nbe-worker exists"; else
  nbe user add nbe-worker --role worker --display "processing worker" --password "$(head -c 32 /dev/urandom | base64)"
fi

step "restart sglab"
read -rp "restarting stops running trials (sglab recover saves them) - no trial running? [y/N] " ok
[[ "$ok" == [yY]* ]] || { echo "run this again when no trial is running"; exit 1; }
systemctl restart nbe-lab
sleep 4
systemctl -q is-active nbe-lab || { journalctl -u nbe-lab -n 30 --no-pager; exit 1; }
nbe jobs | tail -n 1

step "token for the LXC (shown once; to revoke all: nbe user set nbe-worker --inactive)"
nbe user token nbe-worker --label "worker LXC $(date +%F)"
cat <<MSG

In the LXC (as root, from a copy of the repo):
  NBE_WORKER_TOKEN=<the nbe_... line above> deploy/worker/setup-worker.sh
Until a worker connects, "Process" queues the job (the session page says so).
MSG
