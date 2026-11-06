"""Eye tracking on the glasses camera: a port of the lab's MATLAB tracker (legacy-camera-code/
NOT_Simplified_eye_V7_win_to_mac.m, "V7") so the same features run live on the server and
offline on recorded .frames files.

Per frame (all in "working" pixels, see SCALE below):

  1. lighting normalisation   the frame's 2..98 % brightness range (slow EMA) is mapped onto
                              60..200 so the tuned cutoffs mean the same in every lighting; then
                              a 3x3 blur (the gain amplifies sensor noise too)
  2. eye presence             no eye = washed out (half the pixels >= 250) or no contrast left
                              (raw range < 15) for 15 frames; back after 5 good frames
  3. motion                   mean |frame difference| above / below the eye's row, normalised;
                              its 3-frame mean is the motion signal for blink counting
  4. eye = darkest round blob scale-normalised Laplacian of Gaussian, sigma 2.5..11 px (pupil
                              under IR .. whole iris in visible light), on the frame / its median
                              (exposure cancels), searched within EYE_WIN px of the eye centre
  5. eye closure %            how much that blob has faded vs the open eye's recent strength
                              (median of the last ~3 s): a closing lid covers pupil/iris
  6. closure state machine    OPEN -> CLOSED when closure > closed_pct; CLOSED -> OPEN when the
                              3-frame median is < open_pct, or under closed_pct for 0.3 s
  7. motion blinks            peak detector with an adaptive floor; a "head move" (motion mostly
                              below the eye's row) suppresses detection for a few frames
  8. gaze                     pupil position while open and steady; a jump >= 3 px is a gaze shift

SCALE: the MATLAB tracker was tuned on an 80x80 crop. Larger frames (the 160x160 zoomed crop
GLS03 sends now) are block-averaged down to ~80 px first, so every tuned constant keeps its
meaning; positions are reported back in the frame's own pixels.

    trk = EyeTracker(w, h)
    out = trk.update(gray_u8_array, t_s)      # dict: overlay + signals for this frame
    trk.summary(t_s)                           # live stats (blinks/min, PERCLOS, ...)
"""
from __future__ import annotations

import math
import threading
from collections import deque

import numpy as np
from scipy import ndimage

WORK_PX = 80  # the MATLAB tracker's crop size: every constant below is in pixels of this size

# tunables (MATLAB V7 values)
NORM_LO, NORM_SPAN, NORM_ALPHA, NORM_MAXGAIN = 60.0, 140.0, 0.05, 4.0
MIN_CONTRAST = 15.0
NO_EYE_LIMIT, EYE_PRESENT_LIMIT = 15, 5
EYE_WIN = 22            # px: blob centres searched within this of the eye centre
EYE_CAL_N = 40          # frames (~1.6 s) to find where the eye is
OPEN_BUF_N = 75         # ~3 s of blob strengths -> open-eye reference (median)
EYE_POS_N = 75          # ~3 s of open-eye positions -> eye centre
CLOSED_PCT, OPEN_PCT = 30.0, 15.0
REOPEN_HOLD_S = 0.3
MAX_CLOSED_S = 30.0
CENT_MIN_GAP_S = 0.25
PEAK_THRESHOLD, MIN_BLINK_GAP_S = 0.18, 0.2
HEAD_UPPER_FRAC, HEAD_MIN_MOTION, HEAD_COOLDOWN_FRAMES = 0.50, 5.0, 8
CENT_ALPHA = 0.8
GAZE_SHIFT_PX = 3.0
SIGMAS = (2.5, 3.5, 5.0, 7.0, 9.0, 11.0)

TRACE_S = 30.0          # per-frame history kept for the live page
STATS_S = 60.0          # window of the live stats (blinks/min, PERCLOS, ...)


def _block_mean(img: np.ndarray, f: int) -> np.ndarray:
    if f <= 1:
        return img
    h, w = (img.shape[0] // f) * f, (img.shape[1] // f) * f
    return img[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def blob_score(img: np.ndarray, win: np.ndarray | None) -> tuple[float, float, float, float]:
    """Strongest dark round blob at any size: (score, x, y, radius), score -inf if none.

    Scale-normalised LoG (sigma^2 * LoG) on the image divided by its median, so exposure and
    gain changes cancel. A dark blob gives a positive response at its centre; radius = sqrt(2) sigma.
    """
    J = img / max(float(np.median(img)), 1.0)
    best = (-math.inf, math.nan, math.nan, math.nan)
    for s in SIGMAS:
        R = (s * s) * ndimage.gaussian_laplace(J, s, mode="nearest")
        R[:4, :] = -np.inf
        R[-4:, :] = -np.inf
        R[:, :4] = -np.inf
        R[:, -4:] = -np.inf
        if win is not None:
            R[~win] = -np.inf
        i = int(np.argmax(R))
        v = float(R.flat[i])
        if v > best[0]:
            r, c = divmod(i, R.shape[1])
            best = (v, float(c), float(r), math.sqrt(2.0) * s)
    return best


class EyeTracker:
    """Live state of one camera stream's eye tracker. update() once per frame, in order."""

    def __init__(self, w: int, h: int):
        self.w, self.h = int(w), int(h)
        self.f = max(1, round(min(self.w, self.h) / WORK_PX))
        self.ww, self.wh = self.w // self.f, self.h // self.f
        self.yy, self.xx = np.mgrid[0:self.wh, 0:self.ww]
        self.lock = threading.Lock()
        self.n_frames = 0
        self.trace: deque = deque()     # per-frame dicts (see update), last TRACE_S
        self.trace_n = 0                # running index = cursor for incremental fetches
        self.blinks: deque = deque()    # (t_close, duration_s or None) of closure blinks
        self.motion_blinks: deque = deque()  # t of motion blinks
        self.gaze_shifts: deque = deque()    # t of gaze shifts
        self.open_time: deque = deque()      # (t, dt, closed) per tracked frame, for PERCLOS
        self.totals = {"closure_blinks": 0, "motion_blinks": 0, "gaze_shifts": 0, "durations": []}
        self.last: dict | None = None
        self._reset_all()

    # ---------------------------------------------------------------- state
    def _reset_all(self) -> None:
        self.norm_lo = math.nan
        self.norm_span = math.nan
        self.no_eye_count = 0
        self.present_count = 0
        self.present = False
        self.prev: np.ndarray | None = None
        self.diff_buf = deque([0.0] * 3, maxlen=3)
        self.upper_frac_buf = deque([0.5] * 3, maxlen=3)
        self._reset_tracking()
        self.inpeak = False
        self.peak_start = 0.0
        self.last_blink_t = -math.inf
        self.motion_floor = 0.05
        self.t_prev: float | None = None

    def _reset_tracking(self) -> None:
        self.cx = self.ww / 2
        self.cy = self.wh / 2
        self.sx = self.ww / 2
        self.sy = self.wh / 2
        self.baseline_ready = False
        self.cal_xy: list[tuple[float, float]] = []
        self.open_buf: deque = deque(maxlen=OPEN_BUF_N)
        self.pos_buf: deque = deque(maxlen=EYE_POS_N)
        self.closure_buf = deque([0.0] * 3, maxlen=3)
        self.gaze_buf = deque(maxlen=3)
        self.closed = False
        self.close_t = 0.0
        self.closed_below_since = math.nan
        self.last_cent_blink = -math.inf
        self.head_cooldown = 0
        self.prev_pupil: tuple[float, float] | None = None
        self.split_row = self.wh // 2

    def recalibrate(self) -> None:
        """Re-learn where the eye is (MATLAB: "Reset green marker" / R key)."""
        with self.lock:
            self._reset_tracking()

    # ---------------------------------------------------------------- per frame
    def update(self, frame: np.ndarray, t: float) -> dict:
        with self.lock:
            out = self._update(np.asarray(frame, dtype=np.float64).reshape(self.h, self.w), float(t))
            self.n_frames += 1
            out["n"] = self.trace_n
            self.trace.append(out)
            self.trace_n += 1
            while self.trace and self.trace[0]["t"] < t - TRACE_S:
                self.trace.popleft()
            for q in (self.blinks, self.motion_blinks, self.gaze_shifts):
                while q and (q[0][0] if isinstance(q[0], tuple) else q[0]) < t - STATS_S:
                    q.popleft()
            while self.open_time and self.open_time[0][0] < t - STATS_S:
                self.open_time.popleft()
            self.last = out
            return out

    def _update(self, full: np.ndarray, t: float) -> dict:
        f = self.f
        raw = _block_mean(full, f)
        dt = 0.04 if self.t_prev is None else min(max(t - self.t_prev, 0.0), 0.5)
        self.t_prev = t
        out = {"t": round(t, 4), "state": "no_eye", "x": None, "y": None, "r": None,
               "cx": None, "cy": None, "win": EYE_WIN * f, "closure": None, "motion": None,
               "thr": None, "closed": False, "head_move": False, "event": None}

        # 1. lighting normalisation
        p_lo, p_hi = np.percentile(raw, [2, 98])
        span = float(p_hi - p_lo)
        washed = float(np.mean(raw >= 250)) >= 0.5
        if math.isnan(self.norm_lo):
            self.norm_lo, self.norm_span = float(p_lo), max(span, 1.0)
        else:
            self.norm_lo += NORM_ALPHA * (float(p_lo) - self.norm_lo)
            self.norm_span += NORM_ALPHA * (max(span, 1.0) - self.norm_span)
        gain = min(NORM_SPAN / self.norm_span, NORM_MAXGAIN)
        eye = np.clip(NORM_LO + (raw - self.norm_lo) * gain, 0, 255)
        eye = ndimage.uniform_filter(eye, 3, mode="nearest")
        out["brightness"] = round(float(raw.mean()), 1)
        out["contrast"] = round(span, 1)

        # 2. eye presence
        if washed or span < MIN_CONTRAST:
            self.no_eye_count += 1
            self.present_count = 0
            if self.no_eye_count >= NO_EYE_LIMIT:
                self.present = False
                self.no_eye_count = NO_EYE_LIMIT
                self.prev = eye
                self.inpeak = False
                self._reset_tracking()
        else:
            self.no_eye_count = 0
            self.present_count += 1
            if self.present_count >= EYE_PRESENT_LIMIT:
                self.present = True
                self.present_count = EYE_PRESENT_LIMIT
        if not self.present:
            out["why"] = "washed out" if washed else ("too dark / no contrast" if span < MIN_CONTRAST else "starting")
            return out

        # 3. motion, split at the eye's own row
        if self.baseline_ready:
            self.split_row = int(min(max(round(self.cy), 15 / 80 * self.wh), self.wh - 15 / 80 * self.wh))
        prev = self.prev if self.prev is not None and self.prev.shape == eye.shape else eye
        d = np.abs(eye - prev)
        up = float(d[:self.split_row].mean()) if self.split_row > 0 else 0.0
        lo = float(d[self.split_row:].mean()) if self.split_row < self.wh else 0.0
        self.prev = eye
        self.diff_buf.append(min((up + lo) / 2 / 50, 1.0))
        motion = float(np.mean(self.diff_buf))

        # 4. eye = strongest dark round blob near the eye centre
        win = (np.hypot(self.xx - self.cx, self.yy - self.cy) <= EYE_WIN) if self.baseline_ready else None
        score, bx, by, br = blob_score(ndimage.gaussian_filter(raw, 1.0, mode="nearest"), win)
        pup_ok = math.isfinite(score)
        if pup_ok:
            self.sx = CENT_ALPHA * bx + (1 - CENT_ALPHA) * self.sx
            self.sy = CENT_ALPHA * by + (1 - CENT_ALPHA) * self.sy
            self.gaze_buf.append((bx, by))
        gx, gy = (np.median(np.array(self.gaze_buf), axis=0) if self.gaze_buf else (math.nan, math.nan))

        # 5. closure % vs the open eye's recent blob strength
        open_ref = float(np.median(self.open_buf)) if self.open_buf else math.nan
        closure = 0.0 if (math.isnan(open_ref) or not pup_ok or open_ref <= 0) else 100.0 * (1 - score / open_ref)
        self.closure_buf.append(closure)

        # head move: motion mostly below the eye's row
        total = up + lo
        self.upper_frac_buf.append(up / (total + 1e-6))
        head_move = (float(np.mean(self.upper_frac_buf)) < HEAD_UPPER_FRAC
                     and total > max(HEAD_MIN_MOTION, 100 * (self.motion_floor + 0.05)))
        if head_move:
            self.head_cooldown = HEAD_COOLDOWN_FRAMES
        elif self.head_cooldown > 0:
            self.head_cooldown -= 1
        suppress = head_move or self.head_cooldown > 0

        out.update(motion=round(motion, 4), head_move=bool(head_move))
        if pup_ok:
            out.update(x=round((bx + 0.5) * f, 1), y=round((by + 0.5) * f, 1), r=round(br * f, 1),
                       gx=round((float(gx) + 0.5) * f, 1), gy=round((float(gy) + 0.5) * f, 1))

        # where is the eye? (first ~1.6 s with a blob)
        if not self.baseline_ready:
            if pup_ok:
                self.cal_xy.append((bx, by))
                self.open_buf.append(score)
            if len(self.cal_xy) >= EYE_CAL_N:
                c = np.median(np.array(self.cal_xy), axis=0)
                self.cx, self.cy = float(c[0]), float(c[1])
                self.pos_buf.clear()
                self.pos_buf.append((self.cx, self.cy))
                self.baseline_ready = True
                self.cal_xy = []
            out["state"] = "calibrating"
            out["cal"] = round(len(self.cal_xy) / EYE_CAL_N, 2) if not self.baseline_ready else 1.0
            return out

        if pup_ok:
            self.open_buf.append(score)
        if pup_ok and not self.closed and not suppress:
            self.pos_buf.append((bx, by))
            c = np.median(np.array(self.pos_buf), axis=0)
            self.cx, self.cy = float(c[0]), float(c[1])

        # 6. closure state machine
        med = float(np.median(self.closure_buf))
        if self.closed and med < CLOSED_PCT:
            if math.isnan(self.closed_below_since):
                self.closed_below_since = t
        else:
            self.closed_below_since = math.nan
        if not suppress and not self.closed and closure > CLOSED_PCT and t - self.last_cent_blink > CENT_MIN_GAP_S:
            self.closed, self.close_t, self.last_cent_blink = True, t, t
            self.totals["closure_blinks"] += 1
            out["event"] = "close"
        elif self.closed and t - self.close_t > MAX_CLOSED_S:
            self._reset_tracking()  # marker assumed stuck: find the eye again
            out["event"] = "lost"
        elif self.closed and (med < OPEN_PCT or (med < CLOSED_PCT and not math.isnan(self.closed_below_since)
                                                 and t - self.closed_below_since >= REOPEN_HOLD_S)):
            end = t if med < OPEN_PCT else self.closed_below_since
            dur = end - self.close_t
            self.closed, self.closed_below_since = False, math.nan
            ok = 0.03 <= dur <= 30.0
            self.blinks.append((self.close_t, dur if ok else None))
            if ok:
                self.totals["durations"].append(dur)
            out["event"] = "open"
            out["dur"] = round(dur, 3) if ok else None

        # 7. motion blinks (adaptive floor)
        eff = max(PEAK_THRESHOLD, self.motion_floor + (PEAK_THRESHOLD - 0.05))
        release = self.motion_floor + 0.45 * (eff - self.motion_floor)
        if not self.inpeak:
            self.motion_floor = 0.98 * self.motion_floor + 0.02 * min(motion, eff)
        elif t - self.peak_start > 1.5:
            self.inpeak = False
            self.motion_floor = 0.9 * self.motion_floor + 0.1 * motion
        completed = False
        if not self.inpeak and motion > eff and t - self.last_blink_t > MIN_BLINK_GAP_S:
            self.inpeak, self.peak_start = True, t
        elif self.inpeak and motion < release:
            self.inpeak, completed = False, True
        if completed and t - self.last_blink_t > MIN_BLINK_GAP_S and (not suppress or self.close_t >= self.peak_start - 0.15):
            self.last_blink_t = t
            self.motion_blinks.append(t)
            self.totals["motion_blinks"] += 1
            if out["event"] is None:
                out["event"] = "motion_blink"

        # 8. gaze (pupil while open, steady, found)
        if pup_ok and not self.closed and not suppress:
            if self.prev_pupil is not None and math.hypot(gx - self.prev_pupil[0], gy - self.prev_pupil[1]) >= GAZE_SHIFT_PX:
                self.gaze_shifts.append(t)
                self.totals["gaze_shifts"] += 1
            self.prev_pupil = (float(gx), float(gy))
            out["gaze_ok"] = True

        self.open_time.append((t, dt, self.closed))
        out.update(state="closed" if self.closed else "open", closed=self.closed, closure=round(closure, 1),
                   thr=round(eff, 4), cx=round((self.cx + 0.5) * f, 1), cy=round((self.cy + 0.5) * f, 1),
                   suppress=bool(suppress))
        return out

    # ---------------------------------------------------------------- live readout
    def summary(self, now: float | None = None) -> dict:
        with self.lock:
            last = self.last or {}
            now = last.get("t", 0.0) if now is None else now
            span = min(STATS_S, max(now - (self.trace[0]["t"] if self.trace else now), 1e-6))
            tracked = sum(dt for _, dt, _ in self.open_time)
            closed = sum(dt for _, dt, c in self.open_time if c)
            durs = [d for _, d in self.blinks if d is not None]
            return {
                "state": last.get("state", "waiting"), "frames": self.n_frames, "w": self.w, "h": self.h,
                "blinks_total": self.totals["closure_blinks"], "motion_blinks_total": self.totals["motion_blinks"],
                "blink_rate_min": round(len(self.blinks) * 60.0 / span, 1) if span > 5 else None,
                "motion_rate_min": round(len(self.motion_blinks) * 60.0 / span, 1) if span > 5 else None,
                "blink_dur_ms": round(1000 * float(np.median(durs)), 0) if durs else None,
                "perclos_pct": round(100 * closed / tracked, 1) if tracked > 5 else None,
                "gaze_shifts_min": round(len(self.gaze_shifts) * 60.0 / max(tracked, 1e-6), 1) if tracked > 5 else None,
                "tracked_frac": round(tracked / span, 2) if span > 5 else None,
                "window_s": round(span, 1),
            }

    def since(self, after: int = -1, max_points: int = 2000) -> list[dict]:
        with self.lock:
            pts = [p for p in self.trace if p["n"] > after]
        return pts[-max_points:]


def track_frames(frames: np.ndarray, times: np.ndarray) -> tuple[list[dict], dict]:
    """Offline: run the tracker over a recorded (n, h, w) uint8 stack. (per-frame rows, summary)."""
    n, h, w = frames.shape
    trk = EyeTracker(w, h)
    rows = []
    for i in range(n):
        rows.append(trk.update(frames[i], float(times[i])))
    s = trk.summary()
    durs = trk.totals["durations"]
    dur_total = float(times[-1] - times[0]) if n > 1 else 0.0
    s.update(blinks_total=trk.totals["closure_blinks"], motion_blinks_total=trk.totals["motion_blinks"],
             gaze_shifts_total=trk.totals["gaze_shifts"], duration_s=round(dur_total, 1),
             blink_rate_min=round(trk.totals["closure_blinks"] * 60 / dur_total, 1) if dur_total > 10 else None,
             blink_dur_ms=round(1000 * float(np.median(durs)), 0) if durs else None)
    return rows, s


class SyntheticEye:
    """A fake eye camera for the simulator and tests: skin-bright frame, dark iris + darker pupil,
    saccades every ~1.5 s, blinks every ~3-6 s (an eyelid sweeping down and back up), sensor noise."""

    def __init__(self, size: int = 160, seed: int = 0, blink_s: float = 0.18):
        self.size, self.blink_s = size, blink_s
        self.rng = np.random.default_rng(seed)
        self.yy, self.xx = np.mgrid[0:size, 0:size].astype(np.float32)
        self.next_blink = 3.0
        self.blink_at = -10.0
        self.next_sacc = 1.0
        self.gaze = (0.0, 0.0)

    def frame(self, t: float) -> np.ndarray:
        s, rng = self.size, self.rng
        if t >= self.next_sacc:
            self.gaze = (float(rng.normal(0, 0.05 * s)), float(rng.normal(0, 0.03 * s)))
            self.next_sacc = t + rng.uniform(0.6, 2.5)
        if t >= self.next_blink:
            self.blink_at = t
            self.next_blink = t + rng.uniform(2.5, 6.0)
        cx, cy = s / 2 + self.gaze[0], s * 0.52 + self.gaze[1]
        img = 165 + 18 * (self.yy / s) + rng.normal(0, 3, (s, s)).astype(np.float32)
        d = np.hypot(self.xx - cx, self.yy - cy)
        img = np.where(d < 0.19 * s, 95 - 20 * (d < 0.15 * s), img)
        img = np.where(d < 0.075 * s, 30, img)
        img = np.where(np.hypot(self.xx - cx - 0.03 * s, self.yy - cy + 0.03 * s) < 0.02 * s, 235, img)  # glint
        ph = (t - self.blink_at) / self.blink_s
        if 0 <= ph <= 1:
            lid = 1 - abs(2 * ph - 1)
            edge = (cy - 0.32 * s) + lid * 0.66 * s
            img = np.where(self.yy < edge, 158 + rng.normal(0, 3, (s, s)), img)
        return np.clip(img, 0, 255).astype(np.uint8)
