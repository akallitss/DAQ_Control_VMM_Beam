#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on July 10 2026
Created in PyCharm
Created as nTof_x17_DAQ/beam_monitor/beam_intensity_controller.py

@author: Dylan Neff, dylan

Live SPS beam-intensity monitor for the P2 SPS VMM beam test, fed from CERN
NXCALS (the database behind Timber). A standalone process (the beam_watcher
tmux session) owns the NXCALS/Spark session, polls the proton intensity
slow-extracted towards the North Area, appends every logged pulse to a per-day
CSV and publishes a summary to BEAM_STATE_PATH for the Flask app.

Runs ALONGSIDE the Vistar ON/OFF beam monitor (flask_app/beam_state.py): this
one is the NXCALS proton-intensity trend, that one parses SPS Page 1 for a
coarse ON/OFF chip. They publish to separate state files.

The variable is F16.BCT372.TOF:INTENSITY — the beam-current transformer in the
FTN transfer line (last BCT before the n_TOF target), filtered to cycles whose
destination is TOF. Units are 1e10 protons per pulse (a dedicated pulse is
~800, i.e. 8e12 protons; parasitic pulses ~400). The PS writes one point per
TOF cycle, including ~0 points when a TOF cycle plays with no beam, so a fresh
point with low intensity means "beam off", while no points at all means TOF is
out of the supercycle (or the NXCALS pipeline is down) — the state reports
both timestamps so the GUI can tell the difference.

NXCALS practicalities (why this is its own process + venv):
  * pytimber >=4 drags in a full local Spark session — ~1 GB of JVM + PySpark
    that must NOT live in the DAQ venv. It is installed in NXCALS_PYTHON
    (built from acc-py-repo.cern.ch, which this machine can reach directly).
  * Authentication is the user's Kerberos ticket (same /tmp/krb5cc_1000 the
    EOS backup uses). The watcher renews it with `kinit -R` while it is
    renewable; once past the renewable life (~5 days) a manual
    `kinit akallits@CERN.CH` reseed is needed — the state file exposes the
    expiry so the GUI can warn before that happens.
  * The first query after startup takes ~30-60 s (Spark spin-up); after that
    each poll is ~1-2 s. NXCALS data latency is a few tens of seconds, which
    the beam-off gap threshold must (and does) exceed.
"""

import os
import csv
import json
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta

# Shared file paths for the watcher/Flask split (resolved relative to the repo so
# watcher + Flask agree). The beam_watcher process is the sole owner of the NXCALS
# session; Flask only reads BEAM_STATE_PATH and the CSVs.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_MODULE_DIR)
# Per-day intensity CSVs live with the other slow-control logs on the data disk,
# not in the repo.
BEAM_LOG_DIR = os.path.expanduser("~/p2_sps_vmm/slow_control/beam_intensity")
# NOTE: distinct from the Vistar beam monitor's config/beam_state.json — this
# NXCALS intensity watcher runs ALONGSIDE that ON/OFF tracker, so it publishes to
# its own file to avoid clobbering it.
BEAM_STATE_PATH = os.path.join(_REPO_DIR, "config", "beam_intensity_state.json")

# Interpreter the watcher must run under (Flask's venv does not have pytimber).
NXCALS_PYTHON = os.path.expanduser("~/venvs/nxcals/bin/python")

# NXCALS variable + interpretation — SPS North Area (P2 SPS beam test).
# TODO-SPS: confirm the exact variable in Timber once the beam line (H2/H4/...)
# is assigned. Candidates, most- to least-specific for our detector:
#   * the XBPF/scintillator counters of the assigned beam line (best: particles
#     actually through our zone),
#   * the T2/T4/T6 target BSI intensity for our target (e.g. T2 for H2/H4),
#   * SPS.BCTDC.51454:SFTPRO_INT — protons slow-extracted from the SPS towards
#     the North Area (upstream of target sharing; good beam on/off signal,
#     wrong for absolute normalisation). Used as the placeholder default.
BEAM_VARIABLE = "SPS.BCTDC.51454:SFTPRO_INT"
BEAM_UNIT = "1e10 protons"
# Points below this are empty cycles, not real spills. TODO-SPS: retune against
# real spill values of the chosen variable.
PULSE_THRESHOLD_E10 = 50.0

POLL_S = 30.0            # NXCALS query cadence
LOOKBACK_S = 600.0       # stats window (spills / protons in the last 10 min)
BEAM_OFF_GAP_S = 300.0   # no spill for this long -> beam considered OFF. SPS
                         # supercycles space SFTPRO spills up to a few minutes
                         # apart, so this must exceed the longest normal gap.
KRB_RENEW_S = 4 * 3600.0  # try `kinit -R` this often to keep the ticket alive


class BeamIntensityMonitor:
    """Owns the pytimber/NXCALS session plus the poll/log loop."""

    def __init__(self, poll_s=POLL_S, state_path=BEAM_STATE_PATH, log_dir=BEAM_LOG_DIR):
        self.poll_s = poll_s
        self.state_path = state_path
        self.log_dir = log_dir
        self.db = None
        self.connected = False
        self.last_error = None
        self._stop = False
        # Unix ts of the newest CSV row — dedups across polls AND across watcher
        # restarts (each poll re-queries the whole lookback window, so without
        # seeding from the CSV a restart would re-log up to LOOKBACK_S of rows).
        self._last_logged_ts = self._newest_logged_ts()
        self._last_point = None      # (unix_ts, value) newest point of any size
        self._last_pulse = None      # (unix_ts, value) newest point >= threshold
        self._last_krb_renew = 0.0

    # ---------------- NXCALS session ----------------

    def _connect(self):
        """Create the pytimber session (slow: local Spark spin-up)."""
        try:
            import pytimber
            self.log("starting NXCALS session (Spark spin-up, ~1 min)...")
            # The NXCALS bundle's spark-defaults.conf pins spark.driver.port to 5001
            # — the Flask GUI's port. If this watcher starts while Flask is down,
            # Spark steals 5001 and Flask can never come back. Pin the driver away
            # from it (and drop the unused Spark web UI).
            self.db = pytimber.LoggingDB(source="nxcals", sparkprops={
                "spark.driver.port": "5011",
                "spark.ui.enabled": "false",
            })
            self.connected = True
            self.last_error = None
            self.log("NXCALS session up")
        except Exception as e:
            self.db = None
            self.connected = False
            self.last_error = f"NXCALS connect failed: {e}"
            self.log(self.last_error)
        return self.connected

    # ---------------- Kerberos upkeep ----------------

    @staticmethod
    def _krb_expiry():
        """Expiry datetime of the krbtgt in the default cache, or None."""
        try:
            out = subprocess.run(["klist"], capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception:
            return None
        for line in out.splitlines():
            if "krbtgt/CERN.CH" in line:
                m = re.match(r"\s*\S+\s+\S+\s+(\d+/\d+/\d+)\s+(\d+:\d+:\d+)", line)
                if m:
                    try:
                        return datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                                 "%m/%d/%Y %H:%M:%S")
                    except ValueError:
                        pass
        return None

    def _renew_kerberos(self, force=False):
        """`kinit -R` at most every KRB_RENEW_S (or now, if force). Renewal only
        works within the ticket's renewable life; past that a manual kinit reseed
        is required (same story as the EOS backup)."""
        now = time.time()
        if not force and now - self._last_krb_renew < KRB_RENEW_S:
            return
        self._last_krb_renew = now
        try:
            r = subprocess.run(["kinit", "-R"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                self.log("kerberos ticket renewed (kinit -R)")
            else:
                self.log(f"kinit -R failed: {r.stderr.strip()} — "
                         f"manual `kinit akallits@CERN.CH` needed before expiry")
        except Exception as e:
            self.log(f"kinit -R error: {e}")

    # ---------------- query + state ----------------

    def _poll_once(self):
        """One NXCALS query: fetch new points, log them, rebuild the state dict."""
        t2 = datetime.now()
        t1 = t2 - timedelta(seconds=LOOKBACK_S)
        q0 = time.time()
        res = self.db.get(BEAM_VARIABLE, t1, t2)
        query_s = time.time() - q0

        ts, vals = res.get(BEAM_VARIABLE, ([], []))
        # Cast numpy scalars to native floats here, so every downstream value
        # (state JSON, CSV) is json-serializable without further care.
        points = sorted((float(t), float(v)) for t, v in zip(ts, vals))
        for t, v in points:
            if t > (self._last_point[0] if self._last_point else 0):
                self._last_point = (t, v)
            if v >= PULSE_THRESHOLD_E10 and t > (self._last_pulse[0] if self._last_pulse else 0):
                self._last_pulse = (t, v)
        self._log_rows([(t, v) for t, v in points if t > self._last_logged_ts])

        pulses = [(t, v) for t, v in points if v >= PULSE_THRESHOLD_E10]
        now = time.time()
        since_pulse = now - self._last_pulse[0] if self._last_pulse else None
        since_point = now - self._last_point[0] if self._last_point else None
        krb_exp = self._krb_expiry()

        state = {
            "connected": True,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "variable": BEAM_VARIABLE,
            "unit": BEAM_UNIT,
            "beam_on": since_pulse is not None and since_pulse <= BEAM_OFF_GAP_S,
            "last_pulse_time": (datetime.fromtimestamp(self._last_pulse[0])
                                .isoformat(timespec="seconds") if self._last_pulse else None),
            "last_pulse_e10": round(self._last_pulse[1], 2) if self._last_pulse else None,
            "seconds_since_pulse": round(since_pulse, 1) if since_pulse is not None else None,
            "seconds_since_point": round(since_point, 1) if since_point is not None else None,
            "pulses_10min": len(pulses),
            "protons_10min_e10": round(sum(v for _, v in pulses), 1),
            "avg_pulse_e10": round(sum(v for _, v in pulses) / len(pulses), 1) if pulses else None,
            "pulse_threshold_e10": PULSE_THRESHOLD_E10,
            "beam_off_gap_s": BEAM_OFF_GAP_S,
            "poll_s": self.poll_s,
            "query_s": round(query_s, 2),
            "krb_valid_until": krb_exp.isoformat(timespec="seconds") if krb_exp else None,
            "csv_path": self._csv_path(),
            "last_error": None,
        }
        return state

    # ---------------- CSV logging ----------------

    def _csv_path(self, day=None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"beam_intensity_{day}.csv")

    def _newest_logged_ts(self):
        """Largest unix_ts already in the newest per-day CSV (0.0 if none)."""
        try:
            files = sorted(f for f in os.listdir(self.log_dir)
                           if f.startswith("beam_intensity_") and f.endswith(".csv"))
        except OSError:
            return 0.0
        if not files:
            return 0.0
        newest = 0.0
        try:
            with open(os.path.join(self.log_dir, files[-1]), newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        newest = max(newest, float(row["unix_ts"]))
                    except (KeyError, TypeError, ValueError):
                        pass
        except OSError:
            return 0.0
        return newest

    _CSV_FIELDS = ["timestamp", "unix_ts", "intensity_e10"]

    def _log_rows(self, points):
        """Append new (unix_ts, value) points — every TOF cycle, zeros included, so
        the CSV is the same record Timber would give you. Rows go to the CSV of the
        day they occurred (matters just after midnight)."""
        if not points:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            by_day = {}
            for t, v in points:
                dt = datetime.fromtimestamp(t)
                by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append((dt, t, v))
            for day, rows in by_day.items():
                path = self._csv_path(day)
                new_file = not os.path.exists(path)
                with open(path, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=self._CSV_FIELDS)
                    if new_file:
                        w.writeheader()
                    for dt, t, v in rows:
                        w.writerow({"timestamp": dt.isoformat(timespec="milliseconds"),
                                    "unix_ts": round(t, 3),
                                    "intensity_e10": round(float(v), 3)})
            self._last_logged_ts = max(t for t, _ in points)
        except Exception as e:
            self.log(f"CSV log failed: {e}")

    # ---------------- watcher IPC (state file) ----------------

    def log(self, msg):
        """Timestamped, prefixed line for the beam_watcher tmux pane."""
        print(f"{datetime.now().strftime('%H:%M:%S')} [beam_watcher] {msg}", flush=True)

    def _write_state(self, state):
        """Atomically publish the current state for the Flask app to read."""
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_path)   # atomic: readers never see a partial file
        except Exception as e:
            self.log(f"state write failed: {e}")

    def _write_error_state(self):
        self._write_state({
            "connected": False,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "variable": BEAM_VARIABLE,
            "unit": BEAM_UNIT,
            "beam_on": None,
            "last_error": self.last_error,
        })

    # ---------------- poll loop ----------------

    def run_blocking(self):
        """Poll/log loop in the current thread until SIGINT/SIGTERM. Used by
        beam_watcher.py."""
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "_stop", True))
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "_stop", True))
        self.log(f"beam watcher starting ({BEAM_VARIABLE}, poll {self.poll_s}s, "
                 f"beam-off gap {BEAM_OFF_GAP_S}s)")
        while not self._stop:
            if not self.connected and not self._connect():
                self._write_error_state()
                self._sleep(60.0)
                continue
            try:
                self._renew_kerberos()
                state = self._poll_once()
                self._write_state(state)
            except Exception as e:
                # Query died (kerberos expiry, network blip, Spark session loss).
                # Renew the ticket now and rebuild the session on the next pass.
                self.last_error = f"query failed: {e}"
                self.log(self.last_error)
                self._write_error_state()
                self._renew_kerberos(force=True)
                self.connected = False
                self.db = None
            self._sleep(self.poll_s)
        self.log("beam watcher stopped")

    def _sleep(self, seconds):
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(0.5)
