"""firmware/tools/nbe_node.py against a real (local, http) server: login, register, configure."""
import importlib.util
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

REPO = Path(__file__).resolve().parents[1]
PW = "correct horse battery"


def _tool():
    spec = importlib.util.spec_from_file_location("nbe_node", REPO / "firmware" / "tools" / "nbe_node.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def live_server(tmp_path):
    from sglab.auth import AuthStore
    from sglab.pki import PKI
    from sglab.settings import load_settings
    from sglab.web.app import create_app

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'''
[auth]
enabled = true
[pki]
dir = "{tmp_path / 'pki'}"
[provisioning]
broker_uri = "mqtts://mqtt.example.org:8883"
''')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True)
    PKI(s.pki_dir).init_ca()
    AuthStore(s.db_path).add_user("ada", PW, "admin")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(s), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


def test_tool_login_register_configure(live_server, tmp_path, monkeypatch, capsys):
    t = _tool()
    monkeypatch.setattr(t, "CONF_DIR", tmp_path / "cfg")
    monkeypatch.setattr(t, "CONF", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(t.getpass, "getpass", lambda prompt="": PW if "password:" == prompt.strip()[-9:] else "wifipass123")
    assert t.main(["login", live_server, "--user", "ada", "--allow-http"]) == 0
    assert (tmp_path / "cfg" / "config.json").stat().st_mode & 0o777 == 0o600
    assert t.main(["register", "FNG07", "--site", "glasses"]) == 0
    assert t.main(["list"]) == 0
    assert "FNG07" in capsys.readouterr().out

    # a project copy with an existing sdkconfig that still says USB + another node ID
    proj = tmp_path / "proj"
    shutil.copytree(REPO / "firmware" / "esp-idf" / "ppg_stream", proj, ignore=shutil.ignore_patterns("build", "certs"))
    (proj / "sdkconfig").write_text('CONFIG_PPG_DEVICE_ID="FNG01"\nCONFIG_PPG_LINK_USB_SERIAL_JTAG=y\n'
                                    "# CONFIG_PPG_LINK_UART is not set\n# CONFIG_PPG_LINK_MQTT is not set\n")
    assert t.main(["configure", "fng07", "--project", str(proj), "--wifi-ssid", "UCSD-DEVICE"]) == 0
    assert "BEGIN CERTIFICATE" in (proj / "main" / "certs" / "client.crt").read_text()
    assert (proj / "main" / "certs" / "client.key").stat().st_mode & 0o777 == 0o600
    sdk = (proj / "sdkconfig").read_text()
    assert 'CONFIG_PPG_DEVICE_ID="FNG07"' in sdk and "CONFIG_PPG_LINK_MQTT=y" in sdk
    assert "# CONFIG_PPG_LINK_USB_SERIAL_JTAG is not set" in sdk and 'CONFIG_PPG_SITE="glasses"' in sdk
    assert 'CONFIG_PPG_WIFI_PASSWORD="wifipass123"' in sdk
    assert 'CONFIG_PPG_MQTT_BROKER_URI="mqtts://mqtt.example.org:8883"' in (proj / "sdkconfig.nbe").read_text()
    # every symbol the tool sets exists in the firmware's Kconfig (or is an ESP-IDF one)
    kconfig = (proj / "main" / "Kconfig.projbuild").read_text()
    for eap_user in ("", "jdoe"):
        for k in t.node_values({"node_id": "X", "broker_uri": "", "topic_prefix": "", "broker_ca_mode": "bundle"},
                               "", "", eap_user):
            assert k.startswith("ESP_") or f"config {k}\n" in kconfig, k

    # WPA2-Enterprise (UCSD-PROTECTED): password from the environment, outer identity = username
    monkeypatch.setenv("NBE_WIFI_PASSWORD", "eap-secret")
    assert t.main(["configure", "FNG07", "--project", str(proj), "--wifi-ssid", "UCSD-PROTECTED",
                   "--wifi-eap-user", "jdoe"]) == 0
    sdk = (proj / "sdkconfig").read_text()
    assert "CONFIG_PPG_WIFI_AUTH_EAP=y" in sdk and "# CONFIG_PPG_WIFI_AUTH_PSK is not set" in sdk
    assert 'CONFIG_PPG_WIFI_EAP_USERNAME="jdoe"' in sdk and 'CONFIG_PPG_WIFI_EAP_IDENTITY="jdoe"' in sdk
    assert 'CONFIG_PPG_WIFI_PASSWORD="eap-secret"' in sdk
    assert 'CONFIG_PPG_WIFI_SSID2=""' in sdk and 'CONFIG_PPG_WIFI_SSID3=""' in sdk

    # fallback networks: #2 password from the environment, #3 asked; kept on the next run, "" removes one
    monkeypatch.setenv("NBE_WIFI2_PASSWORD", "hotspot-pw")
    assert t.main(["configure", "FNG07", "--project", str(proj), "--wifi-ssid", "UCSD-PROTECTED",
                   "--wifi-eap-user", "jdoe", "--wifi2-ssid", "Lab Hotspot", "--wifi3-ssid", "home"]) == 0
    sdk = (proj / "sdkconfig").read_text()
    assert 'CONFIG_PPG_WIFI_SSID2="Lab Hotspot"' in sdk and 'CONFIG_PPG_WIFI_PASSWORD2="hotspot-pw"' in sdk
    assert 'CONFIG_PPG_WIFI_SSID3="home"' in sdk and 'CONFIG_PPG_WIFI_PASSWORD3="wifipass123"' in sdk
    monkeypatch.delenv("NBE_WIFI2_PASSWORD")
    assert t.main(["configure", "FNG07", "--project", str(proj), "--wifi-ssid", "UCSD-PROTECTED",
                   "--wifi-eap-user", "jdoe", "--wifi3-ssid", ""]) == 0
    sdk = (proj / "sdkconfig").read_text()
    assert 'CONFIG_PPG_WIFI_SSID2="Lab Hotspot"' in sdk and 'CONFIG_PPG_WIFI_PASSWORD2="hotspot-pw"' in sdk
    assert 'CONFIG_PPG_WIFI_SSID3=""' in sdk and 'CONFIG_PPG_WIFI_PASSWORD3=""' in sdk
    monkeypatch.delenv("NBE_WIFI_PASSWORD")

    # unknown node without --register -> clear error
    assert t.main(["configure", "NOPE1", "--project", str(proj), "--wifi-ssid", "x", "--wifi-password", ""]) == 1
    assert "404" in capsys.readouterr().err

    # firmware upload: a raw image goes up; this one isn't FNG07's build, so the server says why
    junk = tmp_path / "ppg_stream.bin"
    junk.write_bytes(b"\xe9" + bytes(4000))
    assert t.main(["upload", "FNG07", str(junk)]) == 1
    assert "not an ESP-IDF app image" in capsys.readouterr().err
    assert t.main(["ota", "FNG07"]) == 1  # no MQTT on this test server
