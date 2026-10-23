#!/usr/bin/env bash
# Runs on the BACKUP machine (not on the server): pulls /srv/nbe from the NBE server into dated,
# hard-linked snapshots on the backup drive. Pull-based on purpose: the server holds no
# credentials for the backup drive, so nothing on the server (or the website) can delete backups.
#
#   /mnt/backup/nbe/2026-10-01/...   one folder per day, unchanged files are hard links (cheap)
#
# Setup (once):
#   1. on the backup host:  ssh-keygen -t ed25519 -f ~/.ssh/nbe_backup -N ""
#   2. on the NBE server, in ~nbe-backup/.ssh/authorized_keys of a read-only account
#      (useradd -m nbe-backup && usermod -aG nbe nbe-backup), one line:
#      command="/usr/bin/rrsync -ro /srv/nbe",restrict ssh-ed25519 AAAA... backup@host
#      (rrsync ships with rsync; Debian: /usr/bin/rrsync)
#   3. the pki folder is mode 700 for nbe only - give the backup account read access with an ACL:
#      setfacl -R -m u:nbe-backup:rX /srv/nbe/pki && setfacl -dR -m u:nbe-backup:rX /srv/nbe/pki
#   4. cron / systemd timer on the backup host, after the server's 02:30 snapshot:
#      15 3 * * *  /usr/local/bin/nbe-pull-backup.sh
#
# The copy includes the device CA private key (pki/ca.key): keep the backup drive encrypted.
set -euo pipefail
SERVER="${NBE_SERVER:-nbe-backup@10.0.0.42}"
KEY="${NBE_KEY:-$HOME/.ssh/nbe_backup}"
DEST="${NBE_DEST:-/mnt/backup/nbe}"
KEEP_DAYS="${NBE_KEEP_DAYS:-90}"

today=$(date +%F)
mkdir -p "$DEST"
prev=$(ls -1d "$DEST"/20??-??-?? 2>/dev/null | grep -v "/$today$" | tail -n 1 || true)
rsync -a --numeric-ids -e "ssh -i $KEY -o BatchMode=yes" \
  --exclude 'ppg-data/catalog.sqlite*' \
  ${prev:+--link-dest="$prev"} "$SERVER:" "$DEST/$today.partial/"
mv -T "$DEST/$today.partial" "$DEST/$today"
# the live catalog is skipped above; the consistent nightly copy is in ppg-data/snapshots/
find "$DEST" -maxdepth 1 -name '20??-??-??' -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "backup $DEST/$today ok ($(du -sh "$DEST/$today" | cut -f1), previous: ${prev:-none})"
