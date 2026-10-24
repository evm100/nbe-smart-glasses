# Deploying the NBE PPG lab server (Edgar's setup)

> **This server (evmserver, nbe.evm100.org):** use `deploy/evmserver/README.md` – the concrete single-box
> setup next to UltraLights (ports, scripts, UCSD-PROTECTED firmware build, Pi flashing).

What runs where, what to copy where, in the order to do it. Placeholders to replace
everywhere: `ppg.example.org` (web), `mqtt.example.org` (broker — can be the same
domain), `10.0.0.10` (Ubuntu web VM, LAN), `10.0.0.42` (NBE core, LAN).

## 0. The pieces

```
 teammates' browsers ──https──▶ nginx ─────────http (LAN)──────────▶ sglab core  ──▶ /srv/nbe/ppg-data
                               (Ubuntu web VM)                       (Debian LXC)      /srv/nbe/pki
 ESP32 nodes ──mqtts:8884, client cert──▶ mosquitto ◀──mqtts, cert CN=nbe-core──┘
 (anywhere with Wi-Fi)                    (Ubuntu web VM)
 flashers' PCs: firmware/tools/nbe_node.py ──https /api/nodes──▶ (via nginx)
 backup box ──ssh pull (read-only, nightly)──▶ /srv/nbe
```

| component | role | lives in repo |
|---|---|---|
| **sglab core** | web UI + API, users, trials, recorder, MQTT client, node registry + device CA, processing | `src/sglab`, `src/ppg` |
| **mosquitto** | broker; mTLS + ACL pin each node to its topics | `deploy/mosquitto/` |
| **nginx** | public HTTPS, rate limits, proxies to core | `deploy/nginx/` |
| **nbe_node.py** | flashers' tool: register node / pull credentials → ESP-IDF project | `firmware/tools/` |
| **firmware** | ESP-IDF project, USB or Wi-Fi/MQTT link | `firmware/esp-idf/ppg_stream` |
| **backup** | pull-based, hard-linked daily snapshots | `deploy/backup/` |

## 1. Decision: split (recommended) or single VM

**Recommended: split.** Keep the Ubuntu web VM as the internet-facing edge (nginx + mosquitto,
which it already runs) and put the sglab core in a **Debian 12 LXC on the stronger Proxmox node**
(osiris). Reasons:

* processing (Gaussian fits over every beat, later model training) is CPU-heavy — it shouldn't
  slow your public sites, and osiris has the cores (and the GPU if you ever pass it through for
  PyTorch; for that a VM or a privileged container is easier than an unprivileged LXC);
* the data and the device CA key never sit on the internet-facing machine;
* the core can be rebooted/upgraded without touching the websites, and Proxmox snapshots of the
  LXC (or a ZFS dataset for `/srv/nbe`) give you cheap rollback.

Cost: the core depends on the LAN link to the web VM (nginx → core, core → broker). On one
Proxmox cluster / one LAN that's a non-issue.

**Single VM** works too: run everything on the Ubuntu web VM, same steps, with
`[web] host = "127.0.0.1"` and the nginx upstream `127.0.0.1:8000`. Moving to the split later
is a copy of `/srv/nbe` + `/etc/nbe` (section 9).

The steps below are for the split; single-VM differences are marked **(single VM)**.

---

## 2. Create the LXC on osiris (Proxmox shell)

```bash
pveam update && pveam download local debian-12-standard_12.7-1_amd64.tar.zst   # name: see `pveam available`
pct create 142 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname nbe-core --unprivileged 1 --features nesting=1 \
  --cores 8 --memory 8192 --swap 2048 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.42/24,gw=10.0.0.1 \
  --onboot 1
# data on its own volume (or a bind mount of a ZFS dataset: mp0: /tank/nbe,mp=/srv/nbe)
pct set 142 --mp0 local-lvm:200,mp=/srv/nbe,backup=1
pct start 142
```

Optional Tailscale inside the LXC (SSH from anywhere): add to `/etc/pve/lxc/142.conf`
```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```
then `pct reboot 142` and install Tailscale in the container as usual.

**(single VM)**: skip; work on the Ubuntu VM.

## 3. Install the core (inside the LXC, as root)

```bash
apt update && apt install -y git
git clone <your repo URL> /root/smart-glasses        # or scp the zip
cd /root/smart-glasses && ./deploy/install.sh
```

`install.sh` creates user `nbe`, `/opt/nbe/{smart-glasses,venv}`, `/etc/nbe/server.toml`
(from `deploy/server.example.toml`), `/srv/nbe/ppg-data` (750) and `/srv/nbe/pki` (700), and the
systemd units. Re-run it after every `git pull` to update.

Convenience alias for the rest of this guide (put it in `/root/.bashrc`):

```bash
alias nbe='sudo -u nbe SGLAB_ROOT=/opt/nbe/smart-glasses SGLAB_SERVER_CONFIG=/etc/nbe/server.toml /opt/nbe/venv/bin/sglab'
```

### 3a. Edit `/etc/nbe/server.toml`

| key | split | single VM |
|---|---|---|
| `[web] host` | `"0.0.0.0"` (firewalled to the web VM, below) | `"127.0.0.1"` |
| `[web] public_url` | `https://ppg.example.org` | same |
| `[web] trusted_proxies` | `"10.0.0.10"` | `"127.0.0.1"` |
| `[mqtt] host` / `port` | `mqtt.example.org` / `8884` | same |
| `[provisioning] broker_uri` | `mqtts://mqtt.example.org:8884` | same |

The core must reach the broker **by the name on its certificate**. If `mqtt.example.org`
resolves to your public IP and your router doesn't hairpin, pin it to the LAN address
(AdGuard Home DNS rewrite, or in the LXC):

```bash
echo "10.0.0.10 mqtt.example.org" >> /etc/hosts
```

### 3b. Certificates and the first admin

```bash
nbe pki init                 # NBE Device CA -> /srv/nbe/pki/ca.crt + ca.key (back this up!)
nbe pki core-cert            # /srv/nbe/pki/core.crt + core.key  (CN = nbe-core, the server's MQTT identity)
nbe user add edgar --role admin --display "Edgar"
```

### 3c. Firewall (split): only nginx may reach port 8000

```bash
apt install -y nftables
cat >/etc/nftables.conf <<'EOF'
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    tcp dport 22 accept
    ip saddr 10.0.0.10 tcp dport 8000 accept      # nginx on the web VM
    iifname "tailscale0" accept
    icmp type echo-request accept
  }
}
EOF
systemctl enable --now nftables
```

The app trusts `X-Real-IP` only because nothing but nginx can reach it — keep it that way.

Hold off on starting the service until the broker is set up (it works without, but `doctor`
is more useful then).

## 4. Broker: mosquitto on the Ubuntu web VM

Copy two files from the core / repo to the web VM:

| from | to (web VM) | mode |
|---|---|---|
| core: `/srv/nbe/pki/ca.crt` (public cert only, **not** ca.key) | `/etc/mosquitto/certs/nbe-device-ca.crt` | 644 |
| repo: `deploy/mosquitto/nbe.conf` | `/etc/mosquitto/conf.d/nbe.conf` | 644 |
| repo: `deploy/mosquitto/nbe-acl` | merged into your ACL (step B3) | 640 root:mosquitto |

```bash
scp root@10.0.0.42:/srv/nbe/pki/ca.crt /tmp/ && sudo install -m 644 /tmp/ca.crt /etc/mosquitto/certs/nbe-device-ca.crt
```

**B1. Edit `nbe.conf`**: `certfile`/`keyfile` = the Let's Encrypt cert for `mqtt.example.org`
that mosquitto can already read (the same copies / deploy hook your lighting listener uses).

**B2. Port.** 8884 keeps NBE separate from your lighting listener (different trusted CA, can't
mix up clients). Open it: `sudo ufw allow 8884/tcp` and forward 8884 on the router.
If a network blocks 8884, see "Port blocked" in section 10.

**B3. The ACL — check how your broker is set up first:**

```bash
grep -rn "per_listener_settings\|acl_file\|allow_anonymous\|password_file" /etc/mosquitto/
```

* **You already have an `acl_file`** (likely, for the lighting system) and `per_listener_settings`
  is false/absent: append the contents of `deploy/mosquitto/nbe-acl` to that file. The `pattern`
  lines only match `nbe/...` topics, so lighting clients are unaffected.
* **No `acl_file` yet**: adding one would deny everything to your lighting clients. Either add
  their rules too (`user <name>` + `topic readwrite <their topics>/#`), or set
  `per_listener_settings true` and give each listener its own auth lines — that moves
  `allow_anonymous`/`password_file`/`acl_file` under each `listener`, so read
  `man mosquitto.conf` and test the lighting system after.

With `per_listener_settings true`, put `acl_file /etc/mosquitto/nbe-acl` (+
`allow_anonymous false`) under the 8884 listener in `nbe.conf` instead.

```bash
sudo mosquitto -c /etc/mosquitto/mosquitto.conf -v     # foreground once: config errors show here; Ctrl-C
sudo systemctl restart mosquitto && sudo journalctl -u mosquitto -n 20
```

Certificate renewal: when certbot renews the LE cert, mosquitto needs the new copy + a restart
(your lighting deploy hook already does this; make sure it covers the cert `nbe.conf` uses).
The device CA lasts 10 years, node certs 3 years (`sglab pki show`).

## 5. nginx on the Ubuntu web VM

| from repo | to (web VM) |
|---|---|
| `deploy/nginx/nbe-ratelimit.conf` | `/etc/nginx/conf.d/nbe-ratelimit.conf` |
| `deploy/nginx/nbe-lab.conf` | `/etc/nginx/sites-available/nbe-lab.conf` |

Edit `server_name` and the `upstream` address (`10.0.0.42:8000`; **single VM** `127.0.0.1:8000`).

```bash
sudo ln -s ../sites-available/nbe-lab.conf /etc/nginx/sites-enabled/
sudo certbot certonly --nginx -d ppg.example.org     # or reuse a wildcard cert and edit the paths
sudo nginx -t && sudo systemctl reload nginx
```

## 6. Start and check (back on the core)

```bash
systemctl enable --now nbe-lab nbe-snapshot.timer
nbe doctor                # data dir writable, admin exists, CA present, broker reachable as nbe-core
journalctl -u nbe-lab -f
```

Open `https://ppg.example.org`, log in as edgar. The header shows **● broker** green when
the core is connected to mosquitto.

End-to-end test without hardware — register a test node and run a simulated ESP32 that uses its
real certificate through the real broker:

```bash
nbe node register SIM01 --site finger
nbe sim-node SIM01 --duration 120 &
```

`Devices` shows SIM01 online within a few seconds; start a trial with it under `New trial`,
stop, save, and the session appears under `Sessions` with "recorded by edgar". Disable SIM01
afterwards (`Nodes` page or `nbe node disable SIM01`).

## 7. People

**Operators / viewers (browser only, nothing to install):** create accounts on
`Users` (admin) — `operator` runs trials, `viewer` only browses. Send the initial password
privately; they change it under their name (top right). Accounts are disabled, never deleted.

**Flashers (admin role, have ESP-IDF):** share the `firmware/` folder (it is self-contained:
firmware, protocol, tool). They need an admin account. Once:

```bash
python firmware/tools/nbe_node.py login https://ppg.example.org
```

Per board:

```bash
python firmware/tools/nbe_node.py mac -p COM5            # UCSD-DEVICE: register this MAC first
python firmware/tools/nbe_node.py register FNG02 --site finger
python firmware/tools/nbe_node.py configure FNG02        # asks for Wi-Fi SSID/password
cd firmware/esp-idf/ppg_stream && idf.py build flash monitor
```

(`configure FNG02 --flash -p COM5` does the build + flash too, from the ESP-IDF terminal.)
The same can be done without the tool: `Nodes` page → register → "zip" → unzip into the
project → set Wi-Fi in menuconfig.

## 8. Backups

* **Nightly on the core**: `nbe-snapshot.timer` (02:30) writes a consistent copy of the catalog
  database to `/srv/nbe/ppg-data/snapshots/` (users, nodes, audit and the session index live in it).
* **On the backup machine**: `deploy/backup/nbe-pull-backup.sh` (setup steps in its header)
  pulls `/srv/nbe` over SSH with a read-only, `rrsync`-restricted key into dated hard-linked
  folders. The server can't delete backups; nothing on the website can delete data at all.
* Restore test once: copy a snapshot folder somewhere, point a scratch `server.toml` at it, run
  `sglab serve` and browse.
* The copy contains `pki/ca.key`. Keep the backup drive encrypted.

Deleting a recording (your manual backend path): stop nothing, just
`mv /srv/nbe/ppg-data/raw/<session> /srv/nbe/ppg-data/_trash/` (+ its `processed/` folder),
then `nbe reindex`. It drops out of the catalog; the backup still has it until retention expires.

## 9. Updating, moving

```bash
cd /root/smart-glasses && git pull && ./deploy/install.sh && systemctl restart nbe-lab
```

Restarting stops running trials (files are flushed; `nbe recover` saves them) — do it when no
one is recording (the banner on every page shows active trials).

Moving single VM → LXC later: stop the service, `rsync -a /srv/nbe/ newhost:/srv/nbe/`,
copy `/etc/nbe/server.toml`, run `install.sh` on the new host, change the nginx upstream.
Node certificates and the CA move with `/srv/nbe/pki`; boards don't need re-flashing.

## 10. Troubleshooting

| symptom | check |
|---|---|
| ● broker red | `nbe doctor`; core → `mqtt.example.org:8884` reachable? hosts entry? `journalctl -u mosquitto` shows `nbe-core` connecting? |
| node never online | `idf.py monitor` on the board: Wi-Fi reason codes (UCSD-DEVICE MAC registered? 2.4 GHz?), "transport error" (URI / port blocked / CA), "connection refused" (cert not from the device CA / disabled at broker) |
| node online, trial gets no data | ACL not loaded (`Denied PUBLISH` in mosquitto log), topic prefix mismatch, node disabled on `Nodes` |
| "stale" | node stopped publishing status but the broker hasn't timed it out yet (≤ 45 s) — power/Wi-Fi |
| port 8884 blocked on a network | use 8883 if free, or terminate MQTT on 443 with nginx `stream { ssl_preread }` SNI routing (separate hostname) |
| login says too many attempts | 5 failures per username / 20 per IP in 15 min; wait, or restart the service |

## 11. Security summary

* Browsers: HTTPS only (HSTS), HttpOnly + Secure + SameSite=Lax session cookie, Origin check on
  every state change, scrypt password hashes, login rate limits (nginx + app), roles, audit log.
* Scripts / tool: per-user API tokens (stored hashed, revocable on `Account` / `Users`).
* Nodes: per-node client certificate (EC P-256) from your own CA, mTLS at the broker, ACL by CN,
  server ignores unregistered/disabled IDs. Leaked key → disable the node + `rotate` + re-flash.
* No delete endpoints for recordings, users or nodes. `/files/` only serves `raw/` and `processed/`.
* The core never faces the internet directly; the device CA key never leaves the core host
  (and the encrypted backup).

## 12. Before recording people

Human-subject data needs the lab's IRB approval and data-management rules: who may access it,
where it may be stored (a personal server may or may not be allowed — ask the PI / IRB
coordinator), retention, and de-identification. The system only stores subject codes
(no names) — keep it that way, and keep the code ↔ person key on paper or in the lab's approved
storage, not here.
