# firmware/

Everything needed to build and flash NBE sensor nodes. This folder is self-contained:
share it with anyone who flashes boards (they don't need the rest of the repo).

| Path | What |
|---|---|
| `PROTOCOL.md` | PPG protocol v1: the lines every board sends (USB or MQTT) |
| `esp-idf/ppg_stream/` | **Main firmware**: ESP-IDF v6.x project for XIAO ESP32-S3 + MAX30102, USB or Wi-Fi/MQTT link, configured in `idf.py menuconfig` |
| `tools/nbe_node.py` | **Node provisioning tool** (Python 3.9+, no packages): registers a node on the NBE server or fetches its credentials, writes them into the ESP-IDF project, optionally builds + flashes |
| `arduino/ppg_stream/` | Older Arduino reference sketch (SparkFun library), kept for comparison |

## Flashing a Wi-Fi node (admin account on the NBE server needed)

```bash
# once per computer
python tools/nbe_node.py login https://ppg.example.org

# per board (ESP-IDF terminal)
python tools/nbe_node.py mac -p COM5                  # UCSD-DEVICE: register this MAC first
python tools/nbe_node.py register FNG02 --site finger # skip if the node already exists
python tools/nbe_node.py configure FNG02              # asks for Wi-Fi SSID + password
cd esp-idf/ppg_stream
idf.py build flash monitor                            # watch it connect; Ctrl-] quits
```

`configure` writes `main/certs/client.crt` + `client.key` (the board's identity – never commit
or share), `sdkconfig.nbe`, and updates `sdkconfig`. Re-flashing a board later: run `configure`
again (same ID) and `idf.py build flash`. `configure FNG02 --flash -p COM5` does both steps.

The board then shows up on the server's **Devices** page within ~15 s, and anyone with the
operator role can record from it in the browser.

## USB board for a lab PC

Leave the link at *USB Serial/JTAG* in menuconfig (the default) and use `sglab record` /
`sglab serve` on the PC it's plugged into – no server or credentials involved.

New boards or sensors only need to speak `PROTOCOL.md`; nothing on the server side changes.
