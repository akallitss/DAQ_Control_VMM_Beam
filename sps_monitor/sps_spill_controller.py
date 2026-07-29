#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPS slow-extraction spill monitor — the Beam2 tab's data source.

Ported 2026-07-27 from nTof_x17_DAQ/sps_monitor/sps_spill_controller.py, which
was written on 2026-07-23 to answer "is the SPS pause / spill / pause structure
visible in NXCALS?". It is. Here it is not a test tab: H4 IS a slow-extracted
North Area line, so this intra-cycle view is the real description of the beam we
take data with, and the existing Beam tab's one-scalar-per-spill
(SPSQC:MEAN_SPILL_INTENSITY) is a summary of it.

Unlike n_TOF (one point per PS cycle = one proton pulse), the SPS delivers a
SLOW EXTRACTION: a multi-second spill in which the stored beam is bled out of
the ring continuously. So there is no "pulse intensity" to plot — the useful
signal is the intra-cycle ring-intensity ramp, whose NEGATIVE DERIVATIVE is the
instantaneous extraction rate, i.e. the flux hitting the target (and hence our
detectors) as a function of time.

Data source:
  * SPS.BCTDC24.51454:Acquisition:{measStamp,totalIntensity} — per-cycle ARRAYS
    giving ring intensity vs time-in-cycle. measStamp is ms from cycle start,
    5 ms/sample; totalIntensity is in 1e10 protons (unitExponent = 10, the same
    unit the beam monitor uses). This is the device SPSQC itself quotes in
    SPSQC:BCT_NAME, so it is the canonical extraction BCT.
  * SPSQC:* — one scalar per SPS cycle: destination, extracted intensity,
    effective spill length, spill duty factor, extraction/beam-out times.
  * H4_VARS (see below) — our own zone's line status and counters, so the log
    says not just "the SPS extracted" but "and it reached H4".

DEPLOYMENT — this is the important difference from the n_TOF original.

NXCALS is only reachable from the CERN network, so this does NOT run on banco.
It runs inside the beam watcher process ON LXPLUS, publishes to EOS, and
beam_bridge.py pulls the results down to banco:

    lxplus  ── NXCALS ──►  /eos/.../beam_monitor/{sps_state.json, sps_spill_*.csv}
    banco   ── xrdcp ────►  <repo>/config/sps_state.json  +  SPS_LOG_DIR/*.csv

Hence every path here is env-overridable, exactly like
beam_monitor/beam_intensity_controller.py: the lxplus watcher points
SPS_SPILL_STATE / SPS_SPILL_LOG_DIR at the EOS mount, and banco uses the
repo-local defaults that the bridge writes into.

This module does NOT own a Spark session. pytimber/Spark is a ~1.3 GB JVM and
the watcher already runs exactly one, so this monitor BORROWS that handle (see
BeamIntensityMonitor.run_blocking). Everything here is wrapped so that an SPS
failure can never disturb the primary beam logging.
"""

import os
import csv
import gzip
import json
from datetime import datetime, timedelta

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_MODULE_DIR)

# Env-overridable so the same module works on both ends of the bridge — see the
# DEPLOYMENT note above. Defaults are the banco-side (bridge consumer) paths.
SPS_LOG_DIR = os.environ.get(
    "SPS_SPILL_LOG_DIR", os.path.join(_REPO_DIR, "beam_monitor", "logs"))
SPS_STATE_PATH = os.environ.get(
    "SPS_SPILL_STATE", os.path.join(_REPO_DIR, "config", "sps_state.json"))

# --- NXCALS variables -------------------------------------------------------
# Intra-cycle ring intensity vs time-in-cycle. Both are per-cycle arrays and MUST
# be read as a pair: measStamp is the x-axis (ms since cycle start) for the
# totalIntensity y-axis. Do NOT assume the array spans the whole cycle — on the
# 2026-07-23 FTARGET cycle the acquisition is 1815 samples = 9070 ms of a
# 10800 ms cycle (it stops shortly after beam-out at 9040 ms).
SPS_STAMP_VAR = os.environ.get("SPS_STAMP_VAR",
                               "SPS.BCTDC24.51454:Acquisition:measStamp")
SPS_INT_VAR = os.environ.get("SPS_INT_VAR",
                             "SPS.BCTDC24.51454:Acquisition:totalIntensity")

# Per-cycle scalars (one point per SPS cycle).
SPSQC_VARS = {
    "destination": "SPSQC:DESTINATION",
    "extracted_e10": "SPSQC:EXTRACTED_INTENSITY",
    "spill_len_ms": "SPSQC:EFF_SPILL_LENGHT",       # sic — misspelled in NXCALS
    "duty_factor": "SPSQC:SPILL_DUTY_FACTOR",
    "extraction_time_ms": "SPSQC:EXTRACTION_TIME",
    "beam_out_time_ms": "SPSQC:BEAM_OUT_TIME",
    "cycle_len_ms": "SPSQC:DURATION_IN_MS",
}

# --- H4 line ----------------------------------------------------------------
# From a live NXCALS variable survey on 2026-07-27. Findings, because they
# constrain what "line open" can honestly mean here:
#
#  * There is NO North Area beam-stopper or line-status variable in NXCALS. The
#    %STOPPER% / %XSTP% searches return Linac4 interlocks and nothing else, and
#    XBH4 publishes only four families: BeamInfo, BEAMTRIGGER, BEND (magnet
#    currents) and EXPT (experiment-position scalers).
#  * XBH4.BEAMTRIGGER:COUNTS is flat zero over 8 h of good beam — not usable.
#  * Of every experiment position on H4 (HNA142, HNA162, HNA348, HNA445,
#    HNA487, HNA903, HNA910, NP04, GIF), only these have non-zero data:
#        XBH4.EXPT.GIF.001..004:COUNTS   (looks like a 4-fold telescope)
#        XBH4.EXPT.HNA162.005:COUNTS     (single counter, the largest rates)
#    Everything else, including all of HNA348, reads exactly zero.
#  * The counters are NOT interchangeable. HNA162.005 never drops below ~155000
#    and GIF.001 never below ~250 — those sit on a large ambient floor and are
#    useless as a beam-present signal. But GIF.003 and GIF.004 (higher
#    coincidence fold, so they need real beam particles) go to EXACTLY ZERO when
#    beam stops reaching the zone, and they are the signal that matters.
#  * The H4 BEND magnet currents (280.0 / 478.0 / 216.6 A) are stable to
#    +/-0.2 A and did not move once in 48 h — INCLUDING through every access.
#    They say the line is powered, not that beam is arriving, so they are a
#    veto and not the verdict.
#
# THE DISCRIMINATOR IS "SPS DELIVERING **AND** OUR COUNTERS DEAD". This is the
# thing that makes H4 different from n_TOF and it is worth being explicit about:
# at n_TOF an access stops the beam itself, so the intensity plot shows it. Here
# the SPS keeps extracting to the North Area target throughout — the T-target is
# far upstream of us — and only our branch closes. So an access is invisible in
# every SPS-side variable and only shows up as "beam is being delivered, and we
# are seeing none of it". Verified against 5.5 days of logged data: GIF.004 goes
# to zero in 5-20 min blocks clustered around 09:00-10:00, 14:00-16:00 and
# 21:00-22:00 on most days, which is exactly the shape of access periods.
#
# WHICH OF THESE IS OUR ZONE IS NOT YET CONFIRMED — the line configuration loaded
# is H4A.DRD1.00x, comment "GIOVANNI PIONS (5 mm beam spot at 505 m) @ DRD1,
# MUONS @GIF", i.e. two users share the line and both sets of counters are live.
# All of them are therefore logged: they are one extra scalar each, the columns
# cost nothing, and picking the wrong one now would silently mislabel the whole
# archive. Narrow H4_COUNT_VARS once the zone is confirmed; the CSV keeps the
# others so nothing has to be re-backfilled.
H4_COUNT_VARS = {
    "h4_gif_001": "XBH4.EXPT.GIF.001:COUNTS",
    "h4_gif_002": "XBH4.EXPT.GIF.002:COUNTS",
    "h4_gif_003": "XBH4.EXPT.GIF.003:COUNTS",
    "h4_gif_004": "XBH4.EXPT.GIF.004:COUNTS",
    "h4_hna162_005": "XBH4.EXPT.HNA162.005:COUNTS",
}
# The subset that actually answers "is beam arriving". These are the high-fold
# counters: they need real beam particles, so they read exactly zero during an
# access, where the low-fold ones keep counting ambient background. Verified over
# 5.5 days of logged cycles — see the discriminator note above.
H4_BEAM_COUNTERS = ("h4_gif_004", "h4_gif_003")
# H4 bend magnets — the line-open signal. Three well-separated bends rather than
# one, so a single dead logger cannot flip the verdict. XBH4.BEND.022.492 is
# deliberately absent: it returns NO DATA and would poison an all-of test.
H4_BEND_VARS = {
    "h4_bend_027_a": "XBH4.BEND.022.027:I_MEAS",
    "h4_bend_309_a": "XBH4.BEND.022.309:I_MEAS",
    "h4_bend_706_a": "XBH4.BEND.022.706:I_MEAS",
}
# Above this (amps) a bend counts as energised. Nominal is 216-478 A and the
# noise is +/-0.2 A, so anything in between separates "on" from "off" cleanly.
H4_BEND_ON_A = float(os.environ.get("H4_BEND_ON_A", "10"))
# Reserved for a genuine boolean line-state variable if one is ever identified
# in NXCALS. Empty is handled: h4_open then comes from the bend currents.
H4_STATUS_VARS = {}
# Which line configuration is loaded, and for whom. Not per-cycle — it changes
# only when the beam physicist loads a new file — but it is the thing that
# explains "the beam vanished and nothing is broken": someone re-tuned H4 for
# another user. Published in the state, not the per-cycle CSV.
H4_INFO_VARS = {
    "line_config": "XBH4.BeamInfo:lastFileLoadedName",
    "line_comment": "XBH4.BeamInfo:comment",
    "line_energy_gev": "XBH4.BeamInfo:INITIAL_ENERGY",
}
# Everything the per-cycle CSV carries about H4, in one place.
H4_CYCLE_VARS = {}
H4_CYCLE_VARS.update(H4_COUNT_VARS)
H4_CYCLE_VARS.update(H4_BEND_VARS)

SPS_UNIT = "1e10 protons"
# Destination of the slow-extracted fixed-target beam (the North Area, which is
# what feeds H4). Other destinations in the same supercycle (e.g. SPS_DUMP)
# carry no spill. Env-overridable because it is the one constant here that is a
# claim about OUR beam line rather than about the SPS.
EXTRACTED_DEST = os.environ.get("SPS_EXTRACTED_DEST", "FTARGET")
# SPSQC:EXTRACTED_INTENSITY is in PROTONS (raw, ~1.3e13), not 1e10 units like
# the BCT arrays — divide to put both on the beam monitor's 1e10 scale.
EXTRACTED_SCALE_E10 = 1e-10
# Below this a "FTARGET" cycle carried no real beam.
SPILL_THRESHOLD_E10 = float(os.environ.get("SPS_SPILL_THRESHOLD", "50"))

POLL_S = 30.0             # matches the beam watcher's cadence (it drives us)
SCALAR_LOOKBACK_S = 900.0  # window for the per-cycle scalars / CSV
PROFILE_LOOKBACK_S = 330.0  # window for the (heavy) intra-cycle arrays
SPILL_OFF_GAP_S = 120.0    # no extracted cycle for this long -> SPS spill OFF
# Downsample the stitched timeline to this step. 50 ms is >> fine enough to
# resolve a ~4.5 s spill and keeps the published JSON around 100 kB.
TIMELINE_STEP_MS = 50.0
# Downsample the single-cycle profile to at most this many points.
PROFILE_MAX_POINTS = 500

# --- fine-precision profile archive -----------------------------------------
# The state file's timeline is downsampled to 50 ms and only ever covers the last
# ~5 minutes, so it is a display, not a record. This archive is the record: every
# spill's intensity and extraction rate at the BCT's own 5 ms sampling, which is
# what an offline correlation between detector rate and beam structure needs.
#
# Three things keep it from swamping the EOS bridge:
#  * only EXTRACTING cycles are kept. The rest of the supercycle carries no beam
#    to us, and archiving it would roughly triple the volume for nothing.
#  * gzip. These are long runs of similar floats and compress ~8x. Appending
#    successive gzip members is legal and `zcat`/gzip.open read them back as one
#    stream, so this stays a plain append with no rewrite.
#  * HOURLY files, not daily. The bridge re-copies whole files (xrdcp has no
#    partial transfer), so a day-long file would mean re-pulling an ever-growing
#    archive every poll. An hourly file caps that at ~1 MB.
# Measured shape: ~1800 samples/cycle, one extracted spill every ~40 s.
PROFILE_ARCHIVE = os.environ.get("SPS_PROFILE_ARCHIVE", "1") not in ("0", "", "no")
# "extracted" (default) archives only cycles going to EXTRACTED_DEST; "all"
# archives every cycle the BCT published, for a supercycle-structure study.
PROFILE_ARCHIVE_SCOPE = os.environ.get("SPS_PROFILE_SCOPE", "extracted")


def _to_list(a):
    """numpy array / scalar -> plain python list of floats (json-safe)."""
    try:
        return [float(x) for x in a]
    except TypeError:
        return [float(a)]


def _nearest(times, values, t, tol=1.0):
    """Value of a per-cycle scalar series nearest to time t (None past tol).

    The tolerance MUST stay well under the shortest cycle (3.6 s here): several
    SPSQC variables (EFF_SPILL_LENGHT, SPILL_DUTY_FACTOR) are published only for
    extracting cycles, and a loose tolerance silently attributes the neighbouring
    FTARGET cycle's spill length to the SPS_DUMP cycle next to it.
    """
    best, best_d = None, tol
    for tt, vv in zip(times, values):
        d = abs(tt - t)
        if d < best_d:
            best, best_d = vv, d
    return best


def _truthy(v):
    """Interpret an NXCALS line-status value as open(True)/closed(False).

    NXCALS returns these as numbers or as strings depending on the device, and
    the string spellings are not consistent between systems, so both are handled
    and anything unrecognised returns None (= unknown) rather than guessing.
    """
    if v is None:
        return None
    if isinstance(v, (bool,)):
        return bool(v)
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().upper()
    if s in ("TRUE", "OPEN", "ON", "1", "OUT", "EXTRACTED", "ENABLED"):
        return True
    if s in ("FALSE", "CLOSED", "OFF", "0", "IN", "DISABLED", "VETO"):
        return False
    try:
        return bool(float(s))
    except ValueError:
        return None


def _derive_rate(stamp_ms, intensity_e10, t_start_ms=None, t_end_ms=None):
    """Extraction rate (1e10 protons/s) vs time from the ring-intensity ramp.

    The ring loses beam only through extraction (plus small losses), so
    -dI/dt IS the spill. Uses a centred difference over a +/-`half` window to
    beat down BCT sample noise; the window is in samples, so it scales with
    whatever sampling the acquisition used.

    `t_start_ms`/`t_end_ms` are SPSQC's EXTRACTION_TIME and BEAM_OUT_TIME, and
    the rate is forced to zero outside them. That is not cosmetic: at beam-out
    the residual stored beam (~7% of the cycle here) is DUMPED INTERNALLY in a
    few ms, which is a genuine -dI/dt spike ~5x the spill plateau but is NOT
    beam on the target. Left in, it dominates the peak and the colour scale
    while representing no flux at all downstream.
    """
    n = min(len(stamp_ms), len(intensity_e10))
    if n < 8:
        return []
    half = 4
    rate = [0.0] * n
    for i in range(n):
        ms = stamp_ms[i]
        if t_start_ms is not None and t_start_ms >= 0 and ms < t_start_ms:
            continue
        if t_end_ms is not None and t_end_ms >= 0 and ms > t_end_ms:
            continue
        a, b = max(0, i - half), min(n - 1, i + half)
        dt_s = (stamp_ms[b] - stamp_ms[a]) / 1000.0
        if dt_s <= 0:
            continue
        # negative slope -> positive extraction rate; clamp the noise floor so
        # the flat top and the inter-spill pause read as a clean zero.
        r = -(intensity_e10[b] - intensity_e10[a]) / dt_s
        rate[i] = r if r > 0 else 0.0
    return rate


class SpsSpillMonitor:
    """Polls the SPS spill variables using a pytimber handle owned by someone
    else (the beam watcher). Publishes SPS_STATE_PATH + per-cycle CSV."""

    def __init__(self, state_path=SPS_STATE_PATH, log_dir=SPS_LOG_DIR, logger=None):
        self.state_path = state_path
        self.log_dir = log_dir
        self._log = logger or (lambda m: None)
        self._last_logged_ts = self._newest_logged_ts()
        self._last_profile_ts = 0.0

    def log(self, msg):
        self._log(f"[sps] {msg}")

    # ---------------- poll ----------------

    def poll(self, db):
        """One NXCALS pass. Returns the state dict it published."""
        now = datetime.now()

        # 1) per-cycle scalars over the long window (cheap; drives the CSV)
        scal_vars = dict(SPSQC_VARS)
        scal_vars.update(H4_STATUS_VARS)
        scal_vars.update(H4_CYCLE_VARS)
        scal_res = db.get(list(scal_vars.values()),
                          now - timedelta(seconds=SCALAR_LOOKBACK_S), now)
        scal = {}
        for key, var in scal_vars.items():
            ts, vals = scal_res.get(var, ([], []))
            scal[key] = ([float(t) for t in ts], list(vals))

        dest_t, dest_v = scal["destination"]
        cycles = []
        for t, dest in zip(dest_t, dest_v):
            row = {"unix_ts": t, "destination": str(dest)}
            for key in SPSQC_VARS:
                if key == "destination":
                    continue
                v = _nearest(*scal[key], t)
                row[key] = float(v) if v is not None else None
            if row.get("extracted_e10") is not None:
                row["extracted_e10"] *= EXTRACTED_SCALE_E10
            # H4: line state as open/closed/unknown, counters and bend currents
            # as raw numbers so the archive keeps the measurement, not a verdict.
            for key in H4_STATUS_VARS:
                row[key] = _truthy(_nearest(*scal[key], t))
            for key in H4_CYCLE_VARS:
                v = _nearest(*scal[key], t)
                row[key] = float(v) if v is not None else None
            cycles.append(row)
        cycles.sort(key=lambda r: r["unix_ts"])

        self._log_rows([c for c in cycles if c["unix_ts"] > self._last_logged_ts])

        # 2) intra-cycle arrays over the short window (heavy: ~1800 floats/cycle)
        prof_res = db.get([SPS_STAMP_VAR, SPS_INT_VAR],
                          now - timedelta(seconds=PROFILE_LOOKBACK_S), now)
        st_t, st_v = prof_res.get(SPS_STAMP_VAR, ([], []))
        in_t, in_v = prof_res.get(SPS_INT_VAR, ([], []))
        stamps = {float(t): _to_list(v) for t, v in zip(st_t, st_v)}

        profiles = []   # (cycle_start_unix, dest, stamp_ms[], intensity[], rate[])
        for t, iv in zip(in_t, in_v):
            t = float(t)
            stamp = stamps.get(t)
            if stamp is None:      # pair them by identical cycle stamp
                continue
            inten = _to_list(iv)
            n = min(len(stamp), len(inten))
            if n < 8:
                continue
            stamp, inten = stamp[:n], inten[:n]
            dest = _nearest(dest_t, dest_v, t)
            # Gate the rate to the machine's own declared spill window so the
            # end-of-cycle internal dump is not mistaken for extracted flux.
            t_start = _nearest(*scal["extraction_time_ms"], t)
            t_end = _nearest(*scal["beam_out_time_ms"], t)
            # The array is logged when the acquisition completes, so the cycle
            # started ~(last measStamp) earlier. Constant offset -> only shifts
            # the timeline, never distorts the spill shape.
            start = t - stamp[-1] / 1000.0
            profiles.append((start, str(dest) if dest else None, stamp, inten,
                             _derive_rate(stamp, inten, t_start, t_end)))
        profiles.sort(key=lambda p: p[0])

        if PROFILE_ARCHIVE:
            self._archive_profiles(profiles)

        # 3) stitch every cycle's extraction rate onto absolute wall-clock time.
        #    THIS is the pause / spill / pause trace.
        tl_t, tl_r = [], []
        for start, _dest, stamp, _inten, rate in profiles:
            if not rate:
                continue
            last_kept = None
            for ms, r in zip(stamp, rate):
                if last_kept is not None and ms - last_kept < TIMELINE_STEP_MS:
                    continue
                last_kept = ms
                tl_t.append(round(start + ms / 1000.0, 3))
                tl_r.append(round(r, 3))

        # 4) newest cycle that actually extracted -> the featured spill profile
        featured = None
        for start, dest, stamp, inten, rate in reversed(profiles):
            if dest != EXTRACTED_DEST or not rate or max(rate) <= 0:
                continue
            step = max(1, len(stamp) // PROFILE_MAX_POINTS)
            featured = {
                "cycle_start": datetime.fromtimestamp(start).isoformat(timespec="milliseconds"),
                "cycle_start_unix": round(start, 3),
                "destination": dest,
                "t_ms": [round(x, 1) for x in stamp[::step]],
                "intensity_e10": [round(x, 2) for x in inten[::step]],
                "rate_e10_per_s": [round(x, 2) for x in rate[::step]],
                "peak_rate_e10_per_s": round(max(rate), 2),
                # Mean over the spilling samples only. This, not the peak, is the
                # number to quote: the peak is a short transient at extraction
                # start and runs ~3x the plateau the detector actually sees.
                "mean_rate_e10_per_s": round(
                    sum(r for r in rate if r > 0) / max(1, sum(1 for r in rate if r > 0)), 2),
            }
            break

        # 5) summary
        extracted = [c for c in cycles
                     if c["destination"] == EXTRACTED_DEST
                     and (c.get("extracted_e10") or 0) >= SPILL_THRESHOLD_E10]
        last = extracted[-1] if extracted else None
        since = (now.timestamp() - last["unix_ts"]) if last else None
        recent = [c for c in cycles if c["unix_ts"] >= now.timestamp() - 600]
        recent_ex = [c for c in extracted if c["unix_ts"] >= now.timestamp() - 600]
        # Supercycle period: median gap between successive extracted cycles.
        gaps = sorted(b["unix_ts"] - a["unix_ts"]
                      for a, b in zip(extracted, extracted[1:]))
        period = round(gaps[len(gaps) // 2], 2) if gaps else None

        state = {
            "connected": True,
            "timestamp": now.isoformat(timespec="seconds"),
            "unit": SPS_UNIT,
            "intensity_var": SPS_INT_VAR,
            "spill_on": since is not None and since <= SPILL_OFF_GAP_S,
            "last_spill_time": (datetime.fromtimestamp(last["unix_ts"])
                                .isoformat(timespec="seconds") if last else None),
            "seconds_since_spill": round(since, 1) if since is not None else None,
            "last_extracted_e10": round(last["extracted_e10"], 1) if last else None,
            "last_spill_len_ms": (round(last["spill_len_ms"], 0)
                                  if last and last["spill_len_ms"] else None),
            "last_duty_factor": (round(last["duty_factor"], 3)
                                 if last and last["duty_factor"] else None),
            "last_cycle_len_ms": (round(last["cycle_len_ms"], 0)
                                  if last and last["cycle_len_ms"] else None),
            "supercycle_period_s": period,
            "spills_10min": len(recent_ex),
            "protons_10min_e10": round(sum(c["extracted_e10"] for c in recent_ex), 1),
            "destinations_10min": sorted({c["destination"] for c in recent}),
            "spill_off_gap_s": SPILL_OFF_GAP_S,
            "timeline": {"t_unix": tl_t, "rate_e10_per_s": tl_r,
                         "span_s": PROFILE_LOOKBACK_S},
            "profile": featured,
            "csv_path": self._csv_path(),
            "last_error": None,
        }
        state.update(self._h4_summary(cycles, scal, now))
        state.update(self._h4_info(db, now))
        self._write_state(state)
        return state

    # ---------------- H4 line ----------------

    def _h4_summary(self, cycles, scal, now):
        """H4 line state for the GUI: open/closed plus the zone counters.

        `h4_open` is deliberately three-valued. None means "we could not tell",
        which is NOT the same as closed — telling the shift crew the line is shut
        when we simply have no data would send them chasing a fault that is not
        there. It is only False when the counters answered and said zero.
        """
        out = {"h4_count_vars": dict(H4_COUNT_VARS),
               "h4_bend_vars": dict(H4_BEND_VARS),
               "h4_status_vars": dict(H4_STATUS_VARS)}
        if not H4_STATUS_VARS and not H4_CYCLE_VARS:
            out.update({"h4_open": None, "h4_counts": {}, "h4_counts_10min": {},
                        "h4_note": "no H4 variables configured"})
            return out

        # A real status flag, if one is ever configured, wins over the derived
        # answer: the machine's own assertion beats our inference from rates.
        states = {}
        for key in H4_STATUS_VARS:
            _ts, vals = scal.get(key, ([], []))
            states[key] = _truthy(vals[-1]) if len(vals) else None
        known = [v for v in states.values() if v is not None]
        out["h4_status"] = states

        counts = {}
        for key in H4_COUNT_VARS:
            _ts, vals = scal.get(key, ([], []))
            counts[key] = float(vals[-1]) if len(vals) else None
        out["h4_counts"] = counts

        # Counts over the last 10 min: the empirical "beam is actually arriving
        # here". Summed per counter over the cycles in the window.
        recent = [c for c in cycles if c["unix_ts"] >= now.timestamp() - 600]
        totals = {key: round(sum(c[key] for c in recent if c.get(key) is not None), 1)
                  for key in H4_COUNT_VARS}
        out["h4_counts_10min"] = totals

        bends = {}
        for key in H4_BEND_VARS:
            _ts, vals = scal.get(key, ([], []))
            bends[key] = float(vals[-1]) if len(vals) else None
        out["h4_bend_currents_a"] = bends
        answered = [v for v in bends.values() if v is not None]
        bends_off = bool(answered) and not all(v > H4_BEND_ON_A for v in answered)

        # Was the SPS actually delivering in the window? Without that, our
        # counters reading zero says nothing about H4 — no beam anywhere is not
        # an access. This is the whole basis of the verdict.
        delivering = [c for c in recent
                      if c["destination"] == EXTRACTED_DEST
                      and (c.get("extracted_e10") or 0) >= SPILL_THRESHOLD_E10]
        seen = sum(totals.get(k, 0) or 0 for k in H4_BEAM_COUNTERS)
        out["h4_spills_delivered_10min"] = len(delivering)
        out["h4_beam_counts_10min"] = seen

        if known:
            out["h4_open"] = all(known)
            out["h4_open_from"] = "status variable"
        elif bends_off:
            # A de-energised bend is decisive on its own: nothing gets down the
            # line, whatever the counters happen to read.
            out["h4_open"] = False
            out["h4_open_from"] = "bend magnets not energised"
        elif not delivering:
            # SPS not extracting to us: we cannot distinguish "line closed" from
            # "nothing to send", so we do not try.
            out["h4_open"] = None
            out["h4_open_from"] = "SPS not delivering — cannot tell"
        elif not any(totals.get(k) is not None for k in H4_BEAM_COUNTERS):
            out["h4_open"] = None
            out["h4_open_from"] = "zone counters not reporting"
        else:
            out["h4_open"] = seen > 0
            out["h4_open_from"] = (
                f"zone counters ({len(delivering)} spill(s) delivered, "
                f"{seen:.0f} counts seen)")
        return out

    def _h4_info(self, db, now):
        """The loaded H4 line configuration — which beam, for which user.

        Queried over a week because these change only when the line is re-tuned,
        so a short window usually returns nothing at all.
        """
        if not H4_INFO_VARS:
            return {}
        try:
            res = db.get(list(H4_INFO_VARS.values()),
                         now - timedelta(days=7), now)
        except Exception as e:
            self.log(f"H4 line info query failed: {e}")
            return {}
        out = {}
        for key, var in H4_INFO_VARS.items():
            ts, vals = res.get(var, ([], []))
            if len(ts) == 0:
                out[key] = None
                continue
            newest = max(zip(ts, vals), key=lambda p: float(p[0]))
            out[key] = str(newest[1]) if not isinstance(newest[1], (int, float)) \
                else float(newest[1])
            out[key + "_since"] = datetime.fromtimestamp(
                float(newest[0])).isoformat(timespec="seconds")
        return out

    # ---------------- CSV ----------------

    @property
    def _csv_fields(self):
        return (["timestamp", "unix_ts", "destination", "extracted_e10",
                 "spill_len_ms", "duty_factor", "extraction_time_ms",
                 "beam_out_time_ms", "cycle_len_ms"]
                + sorted(H4_STATUS_VARS) + sorted(H4_CYCLE_VARS))

    def _csv_path(self, day=None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"sps_spill_{day}.csv")

    def _newest_logged_ts(self):
        """Largest unix_ts already logged, so a restart does not re-log the
        lookback window (same trick as the beam watcher)."""
        try:
            files = sorted(f for f in os.listdir(self.log_dir)
                           if f.startswith("sps_spill_") and f.endswith(".csv"))
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

    def _log_rows(self, rows):
        """Append new per-cycle rows — every SPS cycle, dump cycles included, so
        the supercycle structure is reconstructable from the CSV alone."""
        if not rows:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            fields = self._csv_fields
            by_day = {}
            for r in rows:
                dt = datetime.fromtimestamp(r["unix_ts"])
                by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append((dt, r))
            for day, day_rows in by_day.items():
                path = self._csv_path(day)
                new_file = not os.path.exists(path)
                with open(path, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    if new_file:
                        w.writeheader()
                    for dt, r in day_rows:
                        out = {"timestamp": dt.isoformat(timespec="milliseconds"),
                               "unix_ts": round(r["unix_ts"], 3)}
                        for k in fields[2:]:
                            v = r.get(k)
                            # Full precision, deliberately: this CSV is the
                            # archive the offline analysis reads, and rounding
                            # here cannot be undone later.
                            out[k] = repr(v) if isinstance(v, float) else v
                        w.writerow(out)
            self._last_logged_ts = max(r["unix_ts"] for r in rows)
        except Exception as e:
            self.log(f"CSV log failed: {e}")

    def _archive_profiles(self, profiles):
        """Append every new spill's full intra-cycle profile to an hourly file.

        One JSON object per line (gzipped JSONL), because the rows are ragged —
        the acquisition length varies with the cycle — and a rectangular CSV
        would either truncate or pad them. Only cycles newer than the last
        archived one are written, so the overlapping poll windows cannot
        duplicate a spill. See the PROFILE_ARCHIVE notes above for why this is
        extracted-only, gzipped and hourly.
        """
        new = [p for p in profiles if p[0] > self._last_profile_ts]
        if PROFILE_ARCHIVE_SCOPE != "all":
            new = [p for p in new if p[1] == EXTRACTED_DEST]
        if not new:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            by_hour = {}
            for p in new:
                hour = datetime.fromtimestamp(p[0]).strftime("%Y-%m-%d_%H")
                by_hour.setdefault(hour, []).append(p)
            for hour, rows in by_hour.items():
                path = os.path.join(self.log_dir, f"sps_profile_{hour}.jsonl.gz")
                # Append mode writes a new gzip member; concatenated members are
                # a valid gzip stream, so readers still see one flat file.
                with gzip.open(path, "at") as f:
                    for start, dest, stamp, inten, rate in rows:
                        f.write(json.dumps({
                            "cycle_start_unix": round(start, 3),
                            "cycle_start": datetime.fromtimestamp(start)
                                           .isoformat(timespec="milliseconds"),
                            "destination": dest,
                            "sample_ms": (round((stamp[-1] - stamp[0]) / (len(stamp) - 1), 3)
                                          if len(stamp) > 1 else None),
                            "t_ms": [round(x, 1) for x in stamp],
                            # 1e-3 in units of 1e10 protons = 1e7 protons, which
                            # is far below the BCT's own resolution, so this
                            # rounding discards noise and not signal.
                            "intensity_e10": [round(x, 3) for x in inten],
                            "rate_e10_per_s": [round(x, 3) for x in rate],
                        }) + "\n")
            # Advance past everything considered this pass, not just what was
            # written: otherwise a skipped (non-extracting) cycle is re-examined
            # on every poll for as long as it stays in the lookback window.
            self._last_profile_ts = max(p[0] for p in profiles)
        except Exception as e:
            self.log(f"profile archive failed: {e}")

    # ---------------- state file ----------------

    def write_error(self, msg):
        self._write_state({
            "connected": False,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "unit": SPS_UNIT,
            "spill_on": None,
            "h4_open": None,
            "last_error": str(msg),
        })

    def _write_state(self, state):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_path)   # atomic
        except Exception as e:
            self.log(f"state write failed: {e}")
