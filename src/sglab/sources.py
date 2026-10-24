"""Line sources: a real serial port, or a simulated ESP32 speaking the same protocol."""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from .protocol import encode


class SerialSource:
    """Wraps pyserial. readline() returns a str (possibly '') within `timeout` s."""

    def __init__(self, port: str, baud: int = 921600, timeout: float = 0.2):
        import serial  # pyserial

        self.port = port
        if "://" in port:  # rfc2217://host:port, socket://host:port
            self.ser = serial.serial_for_url(port, baudrate=baud, timeout=timeout)
        else:
            try:  # exclusive: a second program (or a second sglab) can't read the same port
                self.ser = serial.Serial(port, baud, timeout=timeout, exclusive=True)
            except (ValueError, TypeError):  # platforms without the exclusive option
                self.ser = serial.Serial(port, baud, timeout=timeout)
        self.ser.reset_input_buffer()

    def write(self, cmd: str) -> None:
        self.ser.write((cmd.strip() + "\n").encode())
        self.ser.flush()

    def readline(self) -> str:
        return self.ser.readline().decode("ascii", "replace")

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class SimSource:
    """A fake ESP32 + MAX30102 streaming protocol lines in real time.

    Uses ppg.synthetic so the signal has realistic pulses, plus optional link
    faults (dropped and corrupted lines) to exercise the recorder.
    """

    def __init__(self, dev: str = "SIM01", fs: float = 100.0, hr_bpm: float = 72.0,
                 drop_prob: float = 0.0, corrupt_prob: float = 0.0, seed: int = 0,
                 realtime: bool = True, dia_amp: tuple = (0.6, 0.3), duration_s: float = 600.0,
                 dc: float = 120_000, perfusion: float = 0.02, noise_frac: float = 0.03,
                 motion: tuple = ()):
        from ppg.synthetic import make_recording

        self.port = "sim://" + dev
        self.dev, self.fs, self.realtime = dev, fs, realtime
        self.rng = np.random.default_rng(seed)
        self.drop_prob, self.corrupt_prob = drop_prob, corrupt_prob
        df, _ = make_recording(duration_s=duration_s, fs=fs, hr_bpm=hr_bpm, motion=[tuple(m) for m in motion],
                               dropouts=[], jitter_ms=0, seed=seed, dia_amp=tuple(dia_amp),
                               channels={"IR": (dc, perfusion), "RED": (0.75 * dc, 0.5 * perfusion)},
                               noise_frac=noise_frac)
        self._ir = df["IR"].to_numpy()
        self._red = df["RED"].to_numpy()
        self._out: deque[str] = deque()
        self._lock = threading.Lock()
        self._streaming = False
        self._seq = 0
        self._t0 = time.monotonic()
        self._t_start = None
        self._boot_us = int(self.rng.integers(1e6, 5e6))
        self._push(encode(f"E,{self._now_us()},BOOT,"))

    def _now_us(self) -> int:
        return self._boot_us + int((time.monotonic() - self._t0) * 1e6)

    def _header(self) -> str:
        return encode(f"H,proto=1,dev={self.dev},fw=sim-0.1,sensor=SIM,fs={self.fs:g},ch=ir|red,"
                      f"adc=18,led_ma=6.4|6.4,pw_us=411,avg=1,site=sim")

    def _push(self, line: str) -> None:
        with self._lock:
            self._out.append(line)

    def write(self, cmd: str) -> None:
        c = cmd.strip().upper()
        if c == "INFO":
            self._push(self._header())
        elif c == "START":
            self._seq = 0
            self._push(self._header())
            self._push(encode(f"E,{self._now_us()},START,"))
            self._t_start = time.monotonic()
            self._streaming = True
        elif c == "STOP":
            self._streaming = False
            self._push(encode(f"E,{self._now_us()},STOP,"))

    def _generate(self) -> None:
        if not self._streaming:
            return
        if self.realtime:
            due = int((time.monotonic() - self._t_start) * self.fs) + 1
        else:
            due = self._seq + 50
        due = min(due, self._ir.size)
        if self.realtime:  # once-per-second status line, like the real firmware
            now = time.monotonic()
            if now - getattr(self, "_last_status", self._t_start) >= 1.0:
                self._last_status = now
                self._push(encode(f"S,{self._now_us()},fs_meas={self.fs:.1f},ovf=0,alc_ovf=0,i2c_err=0,tx_drop=0,temp_c=31.25"))
        t_start_us = self._boot_us + int((self._t_start - self._t0) * 1e6)
        while self._seq < due:
            i = self._seq
            t_us = t_start_us + int(i * 1e6 / self.fs)
            line = encode(f"D,{i},{t_us},{self._ir[i]},{self._red[i]}")
            self._seq += 1
            r = self.rng.random()
            if r < self.drop_prob:
                continue
            if r < self.drop_prob + self.corrupt_prob:
                line = line[:10] + "9" + line[11:]
            self._push(line)

    def readline(self) -> str:
        self._generate()
        with self._lock:
            if self._out:
                return self._out.popleft()
        time.sleep(0.005 if self.realtime else 0)
        return ""

    def close(self) -> None:
        self._streaming = False


def open_source(port: str, baud: int, **sim_kw):
    """sim://ID            simulated device in this process
    mqtt://ID           node on the MQTT broker (needs the web app's MQTT hub)
    rfc2217://host:port network serial port (e.g. ser2net on a bench Raspberry Pi)
    socket://host:port  raw TCP serial bridge
    anything else       local serial port (COM5, /dev/ttyACM0, ...)"""
    if port.startswith("sim://"):
        dev = port[len("sim://"):] or "SIM01"
        return SimSource(dev=dev, **sim_kw)
    if port.startswith("mqtt://"):
        from .mqtt_hub import MqttSource, get_hub

        hub = get_hub()
        if hub is None:
            raise RuntimeError("MQTT is not configured on this server ([mqtt] in config/server.toml)")
        return MqttSource(hub, port[len("mqtt://"):])
    return SerialSource(port, baud)


def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:  # pragma: no cover
        return []
    return [{"port": p.device, "description": p.description, "hwid": p.hwid} for p in lp.comports()]
