#!/usr/bin/env python3
"""
SPS beam state from the CERN Vistar SPS Page 1 image.

Vistar publishes SPS Page 1 only as a rendered PNG (the numbers are not
available as data without NXCALS/CESAR credentials), so the target intensity
table (T2/T4/T6/T10, I/E11 column) is read straight out of the image. The
Vistar font is an un-antialiased fixed bitmap font, so glyphs are matched
EXACTLY against templates in beam_glyphs.json — a misread is impossible; an
unrecognized glyph (layout change, new character) fails the parse loudly
instead of returning a wrong number.

BeamStateTracker polls the image, decides BEAM ON/OFF for one target line
(intensity >= threshold, debounced over 2 samples), tracks how long the beam
has been off and appends every transition to logs/beam_history.csv. State
survives restarts via config/beam_state.json, so an off-period spanning a
Flask restart keeps its original start time.
"""

import csv
import json
import os
import threading
import time
from datetime import datetime

import numpy as np
import requests
from PIL import Image
from io import BytesIO

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
GLYPHS_PATH = os.path.join(_HERE, "beam_glyphs.json")
BEAM_CONFIG_PATH = os.path.join(_BASE, "config", "beam_config.json")
BEAM_PERSIST_PATH = os.path.join(_BASE, "config", "beam_state.json")
BEAM_HISTORY_CSV = os.path.join(_BASE, "logs", "beam_history.csv")

# --- Vistar image fetch (shared with the GUI's /beam_image proxy) ---
# Public PNGs behind op-webtools.web.cern.ch/vistar; short cache so any number
# of open GUIs plus the state poller cause one upstream fetch per TTL.
BEAM_VISTARS = {
    "SPS1":       {"name": "SPS Page 1",   "img": "https://vistar-capture.s3.cern.ch/sps1.png"},
    "SPSFastBCT": {"name": "SPS Fast BCT", "img": "https://vistar-capture.s3.cern.ch/spsbctf.png"},
    "SPSBSRT":    {"name": "SPS BSRT",     "img": "https://vistar-capture.s3.cern.ch/spsbsrt.png"},
    "SPSBT":      {"name": "SPS BT",       "img": "https://vistar-capture.s3.cern.ch/spsbt.png"},
}
BEAM_CACHE_TTL = 5.0  # s — CERN asks external viewers to poll SPS1 at ~7 s
_beam_cache = {}      # usr -> {"t": fetch time, "png": bytes|None, "error": str|None}
_beam_cache_lock = threading.Lock()


def fetch_beam_png(usr):
    """Cached fetch of one Vistar PNG. Returns the cache entry for usr; on
    upstream failure the previous image is kept (with 'error' set) so the GUI
    shows a stale frame instead of a broken one."""
    now = time.time()
    with _beam_cache_lock:
        entry = _beam_cache.get(usr)
        if entry and now - entry["t"] < BEAM_CACHE_TTL:
            return entry
    try:
        r = requests.get(BEAM_VISTARS[usr]["img"], timeout=10)
        r.raise_for_status()
        png, error = r.content, None
    except Exception as e:
        png, error = None, str(e)
    with _beam_cache_lock:
        prev = _beam_cache.get(usr, {})
        if png is None:
            png = prev.get("png")  # keep last good frame on failure
        entry = {"t": now, "png": png, "error": error}
        _beam_cache[usr] = entry
    return entry


# --- SPS Page 1 target-table parser ---
# On the 800x600 page the target label + I/E11 value live in x<300, but the
# table's VERTICAL position moves (the graph above it changes height between
# supercycles), so scan the whole column: a line is a target row iff its
# first token exactly matches a T2/T4/T6/T10 label bitmap — graph traces,
# headers and footer text simply never match. Glyphs in a number are <8 px
# apart, tokens (label / value) 100+ px, so token split at gap >= 8 px.
_TABLE_X = (0, 300)
_TABLE_Y = (60, 590)
_INK_THRESH = 100
_TOKEN_GAP = 8


def _load_glyphs():
    with open(GLYPHS_PATH) as f:
        g = json.load(f)
    to_key = lambda bitmap: tuple(bitmap)
    chars = {to_key(v): k for k, v in g["chars"].items()}
    labels = {to_key(v): k for k, v in g["labels"].items()}
    return chars, labels


_GLYPH_CHARS, _GLYPH_LABELS = _load_glyphs()


def _bitmap_key(img, y0, y1, xa, xb):
    """Glyph cropped to its ink bounding box as a tuple of row strings."""
    g = img[y0:y1, xa:xb]
    ys = np.where(g.any(axis=1))[0]
    g = g[ys[0]:ys[-1] + 1]
    return tuple("".join(map(str, row.astype(int))) for row in g)


def _runs(mask_1d, offset=0):
    """(start, end) runs of True."""
    runs, start = [], None
    for i, v in enumerate(mask_1d):
        if v and start is None:
            start = i
        if not v and start is not None:
            runs.append((start + offset, i + offset))
            start = None
    if start is not None:
        runs.append((start + offset, len(mask_1d) + offset))
    return runs


def parse_sps1_targets(png_bytes):
    """{'T2': 32.6, 'T4': None, ...} from an SPS Page 1 PNG. A target maps to
    None when its value contains an unrecognized glyph; targets absent from
    the page are absent from the dict. Raises nothing — returns {} if the
    table region is empty/unrecognizable."""
    img = np.array(Image.open(BytesIO(png_bytes)).convert("L")) > _INK_THRESH
    x0, x1 = _TABLE_X
    y0, y1 = _TABLE_Y
    targets = {}
    for (ly0, ly1) in _runs(img[y0:y1, x0:x1].any(axis=1), y0):
        glyphs = _runs(img[ly0:ly1, x0:x1].any(axis=0), x0)
        if not glyphs:
            continue
        # split into tokens at gaps >= _TOKEN_GAP
        tokens, cur = [], [glyphs[0]]
        for r in glyphs[1:]:
            if r[0] - cur[-1][1] >= _TOKEN_GAP:
                tokens.append(cur)
                cur = []
            cur.append(r)
        tokens.append(cur)
        # token 0: whole-token label match (T2/T4/T6/T10 glyphs touch)
        label = _GLYPH_LABELS.get(_bitmap_key(img, ly0, ly1, tokens[0][0][0], tokens[0][-1][1]))
        if label is None or len(tokens) < 2:
            continue  # header line, separator or unknown label
        # token 1: the I/E11 value, glyph by glyph
        value = ""
        for (xa, xb) in tokens[1]:
            ch = _GLYPH_CHARS.get(_bitmap_key(img, ly0, ly1, xa, xb))
            if ch is None:
                value = None
                break
            value += ch
        try:
            targets[label] = float(value) if value else None
        except ValueError:
            targets[label] = None
    return targets


# --- Beam ON/OFF state tracking ---

class BeamStateTracker:
    """Polls SPS Page 1, tracks BEAM ON/OFF for one target and records
    transitions. States: 'ON', 'OFF', 'UNKNOWN' (no successful parse yet, or
    none within unknown_after_s)."""

    POLL_S = 7            # CERN-requested external poll rate
    DEBOUNCE_N = 2        # consecutive samples to flip ON/OFF (rejects glitches)
    UNKNOWN_AFTER_S = 90  # no good parse for this long -> UNKNOWN

    def __init__(self):
        self.config = self._load_json(BEAM_CONFIG_PATH) or {}
        self.config.setdefault("target", "T2")
        self.config.setdefault("threshold_e11", 1.0)

        self.state = "UNKNOWN"
        self.since = datetime.now()   # when current state began
        self.intensity = None         # last parsed I/E11 for the target
        self.targets = {}             # all parsed target intensities
        self.last_ok = None           # last successful parse (datetime)
        self.last_error = None
        self._pending = []            # recent raw on/off samples for debounce
        self._lock = threading.Lock()

        # Resume the previous state so an off-period spanning a restart keeps
        # its start time (only if the persisted state is for the same target).
        p = self._load_json(BEAM_PERSIST_PATH)
        if p and p.get("target") == self.config["target"]:
            try:
                self.state = p["state"]
                self.since = datetime.fromisoformat(p["since"])
            except (KeyError, ValueError):
                pass

        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="beam-state")
        self._thread.start()

    @staticmethod
    def _load_json(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def _save_config(self):
        os.makedirs(os.path.dirname(BEAM_CONFIG_PATH), exist_ok=True)
        with open(BEAM_CONFIG_PATH, "w") as f:
            json.dump(self.config, f, indent=2)

    def _persist_state(self):
        try:
            with open(BEAM_PERSIST_PATH, "w") as f:
                json.dump({"state": self.state, "since": self.since.isoformat(),
                           "target": self.config["target"]}, f)
        except Exception as e:
            print(f"[beam] Failed to persist state: {e}")

    def _record(self, event, off_duration_s=None):
        """Append one transition to the beam history CSV."""
        try:
            os.makedirs(os.path.dirname(BEAM_HISTORY_CSV), exist_ok=True)
            new = not os.path.exists(BEAM_HISTORY_CSV)
            with open(BEAM_HISTORY_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "event", "target", "intensity_e11",
                                "threshold_e11", "off_duration_s"])
                w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event,
                            self.config["target"], self.intensity,
                            self.config["threshold_e11"],
                            round(off_duration_s) if off_duration_s is not None else ""])
        except Exception as e:
            print(f"[beam] Failed to write history: {e}")

    def _set_state(self, new):
        if new == self.state:
            return
        off_duration = None
        if new == "ON" and self.state == "OFF":
            off_duration = (datetime.now() - self.since).total_seconds()
        prev = self.state
        self.state = new
        self.since = datetime.now()
        self._persist_state()
        self._record(f"BEAM_{new}", off_duration_s=off_duration)
        print(f"[beam] {prev} -> {new}"
              + (f" (off for {off_duration:.0f}s)" if off_duration else ""))

    def _loop(self):
        while True:
            try:
                self._sample()
            except Exception as e:
                print(f"[beam] Unhandled error in poll loop: {e}")
            time.sleep(self.POLL_S)

    def _sample(self):
        entry = fetch_beam_png("SPS1")
        with self._lock:
            if entry["png"] is None or entry["error"]:
                self.last_error = entry["error"]
            else:
                targets = parse_sps1_targets(entry["png"])
                target = self.config["target"]
                if targets.get(target) is not None:
                    self.targets = targets
                    self.intensity = targets[target]
                    self.last_ok = datetime.now()
                    self.last_error = None
                    self._pending.append(self.intensity >= self.config["threshold_e11"])
                    self._pending = self._pending[-self.DEBOUNCE_N:]
                    if len(self._pending) == self.DEBOUNCE_N and len(set(self._pending)) == 1:
                        self._set_state("ON" if self._pending[0] else "OFF")
                else:
                    self.last_error = (f"target {target} not parsed "
                                       f"(page shows: {sorted(targets) or 'no table'})")
            # no good parse for too long -> state genuinely unknown
            if self.state != "UNKNOWN" and (
                    self.last_ok is None
                    or (datetime.now() - self.last_ok).total_seconds() > self.UNKNOWN_AFTER_S):
                self._pending = []
                self._set_state("UNKNOWN")

    # --- API for the Flask routes ---

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "target": self.config["target"],
                "threshold_e11": self.config["threshold_e11"],
                "intensity_e11": self.intensity,
                "targets": self.targets,
                "since": self.since.isoformat(),
                "duration_s": round((datetime.now() - self.since).total_seconds()),
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "error": self.last_error,
            }

    def set_target(self, target):
        with self._lock:
            if target == self.config["target"]:
                return
            self.config["target"] = target
            self._save_config()
            # Different signal -> current state no longer meaningful.
            self._pending = []
            self.intensity = None
            self._set_state("UNKNOWN")

    def set_threshold(self, threshold_e11):
        with self._lock:
            self.config["threshold_e11"] = float(threshold_e11)
            self._save_config()
            self._pending = []


# Single tracker shared by app.py routes and monitor.py rules. Created on
# first import (module-level, like DaqMonitor in app.py).
tracker = BeamStateTracker()
