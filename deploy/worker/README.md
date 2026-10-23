# Processing + firmware builds on a separate machine (Debian 13 LXC)

evmserver stays the home of everything that has to be **stored or served**: recordings, the
catalog, processing results, the device CA, OTA images, the web UI and the broker. The heavy
**compute** runs in an LXC on the stronger machine:

```
                       evmserver (nbe.evm100.org)                        nbe-proc LXC (Debian 13)
browsers ──https──▶ nginx ─▶ sglab ─▶ /srv/nbe/ppg-data   ◀──https──  sglab worker ×N   (Layers 1-2)
                              │  jobs table (queue)         claim, raw CSV down, outputs up
                              └─ /srv/nbe/pki/firmware    ◀──https──  build-firmware.sh (ESP-IDF in Docker)
                                    (OTA images)            nbe_node.py configure / upload
Raspberry Pi: flash.sh ◀── scp ── nbe-proc:~/nbe-firmware/out/   (first flash only)
```

* **Only outbound HTTPS** from the LXC to `https://nbe.evm100.org`. Nothing on evmserver has
  to be opened, and the LXC holds no data: each job's CSVs are downloaded into a temp folder and
  deleted after the outputs are uploaded.
* The worker logs in with a **`worker`-role** token. That role can do nothing but the
  `/api/worker/*` calls: it can't browse sessions, run trials or touch nodes.
* "Process" (session page, or "process after saving" when finishing a trial) **queues** a job.
  If the LXC is off, jobs wait and the session page says *queued (no processing worker online)*.
  They run as soon as it's back. A job whose worker dies mid-run returns to the queue after 30 min.
* Results land in `/srv/nbe/ppg-data/processed/<session>/<stream>/` exactly as before, and every run
  records which worker made it (`summary.worker`).

## 1. evmserver (once, as root)

```bash
sudo deploy/evmserver/enable-remote-processing.sh
```

It updates the installed code, sets `[processing] mode = "remote"` in `/etc/nbe/server.toml`,
installs the nginx `location /api/worker/` (512 MB uploads), creates user `nbe-worker` (role
`worker`), restarts sglab (it asks first, since a restart stops running trials) and **prints a
token** (`nbe_…`). Keep the token for step 2.

## 2. The LXC

**Create it** on the Proxmox host. The script picks the newest `debian-13-standard` template.
The container is unprivileged with `nesting=1,keyctl=1`, so Docker works inside:

```bash
# copy deploy/worker/create-lxc.sh to the Proxmox host, then:
CTID=150 IP=dhcp ./create-lxc.sh                         # or IP=192.168.1.230/24 GW=192.168.1.1
TAILSCALE=1 CTID=150 ./create-lxc.sh                     # also pass /dev/net/tun (Tailscale inside)
```

Defaults: half the host's cores, 16 GB RAM, 64 GB disk (ESP-IDF image ~5 GB, ~1 GB build tree per node).
It asks for the container's root password.

**Set it up** from a copy of the repo:

```bash
# from evmserver:
rsync -a --exclude .venv/ --exclude data/ --exclude '__pycache__/' ~/NBE/smart-glasses/ root@<lxc>:/root/smart-glasses/
# in the LXC, as root:
NBE_WORKER_TOKEN=nbe_... /root/smart-glasses/deploy/worker/setup-worker.sh
```

It installs Python + Docker, puts the code in `/opt/nbe` (base package only: numpy/scipy/pandas/
matplotlib, no web or MQTT stack) and checks that the server and the token work. It then starts
`nbe-worker@1..N` (`WORKERS=4` for four jobs at once; default 2). It also adds `BUILD_USER`
(default `evm100`) to the `docker` group and pulls `espressif/idf:v6.1`. Re-run it after copying in
new code. Keep evmserver and the LXC on the same code, since the run records the worker's
`ppg` version.

If it can't reach `nbe.evm100.org` from the LAN (no NAT hairpin on the router), it says how
to pin the name to `192.168.1.220` in the LXC's `/etc/hosts`.

Watch it:

```bash
journalctl -fu 'nbe-worker@*'                 # in the LXC
nbe jobs                                      # on evmserver: queue, states, which worker
```

Test: open any session → **Re-process** (admin). The page shows *running on nbe-proc-1*, and then
the results.

## 3. Firmware builds in the LXC (as `BUILD_USER`, no root)

Same script, same outputs. Only the machine changes. `build-firmware.sh` already talks to the
server over HTTPS: it pulls node credentials with `nbe_node.py configure` and uploads OTA images
with `nbe_node.py upload`.

```bash
# as evm100 in the LXC (log in again after setup so the docker group applies)
rsync -a --exclude .venv/ --exclude data/ evm100@evmserver:NBE/smart-glasses/ ~/smart-glasses/
python3 ~/smart-glasses/firmware/tools/nbe_node.py login https://nbe.evm100.org        # admin login
install -D -m 600 /dev/null ~/.config/nbe-node/wifi.env
scp evm100@evmserver:.config/nbe-node/wifi.env ~/.config/nbe-node/wifi.env            # or write it again
~/smart-glasses/deploy/evmserver/build-firmware.sh          # GLS01 HED01 FNG01 -> ~/nbe-firmware/out + OTA upload
```

OTA updates work as before: **Update** on https://nbe.evm100.org/admin/nodes. The images stay on
evmserver. For a USB flash, the Pi now copies from the LXC:
`scp -r evm100@<lxc>:nbe-firmware/out ~/nbe-fw && ~/nbe-fw/flash.sh GLS01`.

When the LXC build works, evmserver no longer needs its build leftovers.
`~/nbe-firmware/` holds images with the Wi-Fi password, so delete it, along with
`~/.config/nbe-node/wifi.env` there. `docker image rm espressif/idf:v6.1` frees ~5 GB. Leave
Docker itself alone, because UltraLights' mosquitto runs in it.

## Going back

Set `mode = "local"` under `[processing]` in `/etc/nbe/server.toml` and run
`systemctl restart nbe-lab`. Jobs still queued then won't run by themselves. Press Process again,
or run `nbe process --all`.
