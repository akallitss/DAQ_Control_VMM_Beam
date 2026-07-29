#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot NXCALS backfill for the beam / spill / H4 logs.

The live watcher only ever looks a few minutes into the past, so anything that
happened while it was down is simply absent. It was down for most of 2026-07-23
to 2026-07-26 (see the gaps in beam_monitor/logs), and the SPS spill and H4 logs
did not exist at all before 2026-07-27. NXCALS keeps the history regardless, so
it can all be recovered after the fact — that is what this does.

Run ON LXPLUS under the NXCALS venv, with the output pointed at the EOS
beam_monitor directory the bridge reads:

    SPS_BEAM_LOG_DIR=/eos/user/a/akallits/beam_monitor \
    SPS_SPILL_LOG_DIR=/eos/user/a/akallits/beam_monitor \
    /eos/user/a/akallits/nxcals_venv/bin/python sps_monitor/backfill_nxcals.py \
        --start 2026-07-22 --what scalars

ORDERING MATTERS. Run this BEFORE restarting the watcher with the spill monitor
enabled: the watcher seeds its "newest row already logged" from the CSVs on
disk, so a backfill that lands first is picked up seamlessly and the watcher
simply continues from the end of it. Run it after, and the two write the same
rows twice.

It is safe to re-run: every file is rewritten from the union of what was already
there and what NXCALS returned, deduplicated on the cycle timestamp. It refuses
to touch the CURRENT day's files by default, because the live watcher is
appending to those and a read-modify-write would race it (--include-today
overrides, for use while the watcher is stopped).

--what scalars   per-spill intensity, per-cycle SPSQC scalars, H4 counters. Fast
                 (a few minutes for a week) and the high-value part.
--what profiles  the 5 ms intra-cycle profiles. SLOW and large — these are
                 per-cycle arrays of ~1800 floats; expect ~1-2 h and ~100 MB
                 gzipped for a week. Chunked by hour and resumable: an hour whose
                 output file already exists is skipped.
--what all       both.
"""

import os
import sys
import csv
import gzip
import json
import argparse
from datetime import datetime, timedelta

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_DIR)

from beam_monitor.beam_intensity_controller import (
    BEAM_LOG_DIR, BEAM_VARIABLE, PULSE_THRESHOLD_E10)
from sps_monitor.sps_spill_controller import (
    SPS_LOG_DIR, SPSQC_VARS, SPS_STAMP_VAR, SPS_INT_VAR, H4_STATUS_VARS,
    H4_CYCLE_VARS, EXTRACTED_DEST, EXTRACTED_SCALE_E10, PROFILE_ARCHIVE_SCOPE,
    _nearest, _to_list, _truthy, _derive_rate)


def log(msg):
    print(f"[backfill {datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# CSV merge helpers
# --------------------------------------------------------------------------

def _read_existing(path, key="unix_ts"):
    """Existing rows by timestamp, so a re-run adds to a file instead of
    replacing it (and so a partially-covered day keeps the part it had)."""
    rows = {}
    if not os.path.exists(path):
        return rows
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    rows[round(float(row[key]), 3)] = row
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError as e:
        log(f"WARNING: could not read {path}: {e}")
    return rows


def _write_merged(path, fields, rows_by_ts):
    """Atomic rewrite: build the whole file next to it, then rename over. A
    half-written CSV on EOS would be read by the bridge as truth."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for ts in sorted(rows_by_ts):
            w.writerow(rows_by_ts[ts])
    os.replace(tmp, path)


def _day_span(day):
    t0 = datetime.combine(day, datetime.min.time())
    return t0, t0 + timedelta(days=1)


# --------------------------------------------------------------------------
# 1) per-spill beam intensity  (the Beam tab's CSV)
# --------------------------------------------------------------------------

BEAM_FIELDS = ["timestamp", "unix_ts", "intensity_e10"]


def backfill_beam(db, day):
    t0, t1 = _day_span(day)
    path = os.path.join(BEAM_LOG_DIR, f"beam_intensity_{day.isoformat()}.csv")
    existing = _read_existing(path)
    before = len(existing)

    ts, vals = db.get(BEAM_VARIABLE, t0, t1).get(BEAM_VARIABLE, ([], []))
    for t, v in zip(ts, vals):
        t = round(float(t), 3)
        if t in existing:
            continue
        existing[t] = {
            "timestamp": datetime.fromtimestamp(t).isoformat(timespec="milliseconds"),
            "unix_ts": t,
            # Same 3 dp as the watcher writes, so backfilled and live rows are
            # indistinguishable to anything reading the file.
            "intensity_e10": round(float(v), 3),
        }
    _write_merged(path, BEAM_FIELDS, existing)
    log(f"beam    {day}: {len(existing) - before:5d} new rows "
        f"({before} -> {len(existing)})  {path}")
    return len(existing) - before


# --------------------------------------------------------------------------
# 2) per-cycle SPS spill scalars + H4  (the Beam2 tab's CSV)
# --------------------------------------------------------------------------

def _spill_fields():
    return (["timestamp", "unix_ts", "destination", "extracted_e10",
             "spill_len_ms", "duty_factor", "extraction_time_ms",
             "beam_out_time_ms", "cycle_len_ms"]
            + sorted(H4_STATUS_VARS) + sorted(H4_CYCLE_VARS))


def backfill_spill(db, day):
    t0, t1 = _day_span(day)
    path = os.path.join(SPS_LOG_DIR, f"sps_spill_{day.isoformat()}.csv")
    existing = _read_existing(path)
    before = len(existing)

    allvars = dict(SPSQC_VARS)
    allvars.update(H4_STATUS_VARS)
    allvars.update(H4_CYCLE_VARS)
    res = db.get(list(allvars.values()), t0, t1)
    scal = {}
    for key, var in allvars.items():
        vts, vv = res.get(var, ([], []))
        scal[key] = ([float(x) for x in vts], list(vv))

    dest_t, dest_v = scal["destination"]
    for t, dest in zip(dest_t, dest_v):
        t = round(float(t), 3)
        if t in existing:
            continue
        row = {"timestamp": datetime.fromtimestamp(t).isoformat(timespec="milliseconds"),
               "unix_ts": t, "destination": str(dest)}
        for key in SPSQC_VARS:
            if key == "destination":
                continue
            v = _nearest(*scal[key], t)
            row[key] = repr(float(v)) if v is not None else None
        if row.get("extracted_e10") is not None:
            row["extracted_e10"] = repr(float(row["extracted_e10"]) * EXTRACTED_SCALE_E10)
        for key in H4_STATUS_VARS:
            row[key] = _truthy(_nearest(*scal[key], t))
        for key in H4_CYCLE_VARS:
            v = _nearest(*scal[key], t)
            row[key] = repr(float(v)) if v is not None else None
        existing[t] = row

    _write_merged(path, _spill_fields(), existing)
    log(f"spill   {day}: {len(existing) - before:5d} new cycles "
        f"({before} -> {len(existing)})  {path}")
    return len(existing) - before


# --------------------------------------------------------------------------
# 3) fine-precision intra-cycle profiles
# --------------------------------------------------------------------------

def backfill_profiles_hour(db, hour_start):
    """One hour of 5 ms profiles -> one gzipped JSONL file.

    Hour granularity is not cosmetic: these are the heaviest queries NXCALS will
    be asked for here, and an hour is small enough that a failure costs one
    retry rather than a whole day. An existing output file means that hour is
    already done, which is what makes the whole run resumable.
    """
    hour_end = hour_start + timedelta(hours=1)
    path = os.path.join(SPS_LOG_DIR,
                        f"sps_profile_{hour_start:%Y-%m-%d_%H}.jsonl.gz")
    if os.path.exists(path):
        log(f"profile {hour_start:%m-%d %H}: exists, skipping")
        return 0

    res = db.get([SPS_STAMP_VAR, SPS_INT_VAR], hour_start, hour_end)
    st_t, st_v = res.get(SPS_STAMP_VAR, ([], []))
    in_t, in_v = res.get(SPS_INT_VAR, ([], []))
    if len(in_t) == 0:
        log(f"profile {hour_start:%m-%d %H}: no data")
        return 0
    stamps = {float(t): _to_list(v) for t, v in zip(st_t, st_v)}

    # The gating scalars, fetched with a margin so a cycle at either edge of the
    # hour still finds its own EXTRACTION_TIME / BEAM_OUT_TIME.
    gate_vars = {k: SPSQC_VARS[k] for k in
                 ("destination", "extraction_time_ms", "beam_out_time_ms")}
    gres = db.get(list(gate_vars.values()),
                  hour_start - timedelta(minutes=1), hour_end + timedelta(minutes=1))
    gate = {}
    for key, var in gate_vars.items():
        gts, gv = gres.get(var, ([], []))
        gate[key] = ([float(x) for x in gts], list(gv))

    written = 0
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt") as f:
        for t, iv in sorted(zip(in_t, in_v), key=lambda p: float(p[0])):
            t = float(t)
            stamp = stamps.get(t)
            if stamp is None:
                continue
            inten = _to_list(iv)
            n = min(len(stamp), len(inten))
            if n < 8:
                continue
            stamp, inten = stamp[:n], inten[:n]
            dest = _nearest(*gate["destination"], t)
            dest = str(dest) if dest else None
            if PROFILE_ARCHIVE_SCOPE != "all" and dest != EXTRACTED_DEST:
                continue
            rate = _derive_rate(stamp, inten,
                                _nearest(*gate["extraction_time_ms"], t),
                                _nearest(*gate["beam_out_time_ms"], t))
            start = t - stamp[-1] / 1000.0
            f.write(json.dumps({
                "cycle_start_unix": round(start, 3),
                "cycle_start": datetime.fromtimestamp(start)
                               .isoformat(timespec="milliseconds"),
                "destination": dest,
                "sample_ms": (round((stamp[-1] - stamp[0]) / (len(stamp) - 1), 3)
                              if len(stamp) > 1 else None),
                "t_ms": [round(x, 1) for x in stamp],
                "intensity_e10": [round(x, 3) for x in inten],
                "rate_e10_per_s": [round(x, 3) for x in rate],
            }) + "\n")
            written += 1

    if written:
        os.replace(tmp, path)
        log(f"profile {hour_start:%m-%d %H}: {written:4d} spills -> "
            f"{os.path.getsize(path) / 1e6:.1f} MB")
    else:
        os.remove(tmp)
        log(f"profile {hour_start:%m-%d %H}: no extracting cycles")
    return written


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--end", default=None,
                    help="last day (inclusive), YYYY-MM-DD; default = yesterday")
    ap.add_argument("--what", default="scalars",
                    choices=["scalars", "profiles", "all"])
    ap.add_argument("--include-today", action="store_true",
                    help="also cover today (needed for the spill/H4 logs, which "
                         "have no history before the spill monitor was deployed)")
    ap.add_argument("--force-beam-today", action="store_true",
                    help="also rewrite TODAY's beam intensity CSV — only with the "
                         "watcher stopped, since it appends to that file live")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    today = datetime.now().date()
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else today - timedelta(days=1))
    if end >= today and not args.include_today:
        log(f"end {end} is today or later; clipping to {today - timedelta(days=1)} "
            f"(pass --include-today to override, with the watcher stopped)")
        end = today - timedelta(days=1)
    if end < start:
        log("nothing to do")
        return

    log(f"target {start} .. {end}   what={args.what}")
    log(f"beam  -> {BEAM_LOG_DIR}")
    log(f"spill -> {SPS_LOG_DIR}")
    if not H4_STATUS_VARS and not H4_CYCLE_VARS:
        log("NOTE: no H4 variables configured — spill CSVs will have no H4 columns")

    import pytimber
    log("starting NXCALS session (Spark spin-up, ~1 min)...")
    # Away from 5011 (the live watcher's driver port) and 5001 (the Flask GUI).
    db = pytimber.LoggingDB(source="nxcals", sparkprops={
        "spark.driver.port": "5031", "spark.ui.enabled": "false"})
    log("NXCALS session up")

    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)

    if args.what in ("scalars", "all"):
        for day in days:
            # The beam watcher is normally RUNNING while this backfill fills in
            # the spill history, and it appends to today's beam CSV every 30 s.
            # Rewriting that file from a snapshot would drop whatever it appended
            # in between, so today's beam CSV is left alone unless explicitly
            # forced (i.e. the watcher is known to be stopped). Today's SPILL CSV
            # has no such owner until the spill monitor is deployed, so it is
            # always safe to write.
            if day == today and not args.force_beam_today:
                log(f"beam    {day}: skipped — the live watcher owns today's file "
                    f"(--force-beam-today to override, watcher stopped)")
            else:
                try:
                    backfill_beam(db, day)
                except Exception as e:
                    log(f"beam  {day}: FAILED {e}")
            try:
                backfill_spill(db, day)
            except Exception as e:
                log(f"spill {day}: FAILED {e}")

    if args.what in ("profiles", "all"):
        total = 0
        for day in days:
            for h in range(24):
                hour = datetime.combine(day, datetime.min.time()) + timedelta(hours=h)
                if hour > datetime.now():
                    break
                try:
                    total += backfill_profiles_hour(db, hour)
                except Exception as e:
                    log(f"profile {hour:%m-%d %H}: FAILED {e}")
        log(f"profiles: {total} spills archived")

    log("done")


if __name__ == "__main__":
    main()
