#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot NXCALS backfill for the H4 barrier (T2 TAX) logs.

The live watcher only looks SCALAR_LOOKBACK_S (15 min) into the past, so any
period when it was down — or before the TAX poll existed at all — is simply
absent from h4_tax_*.csv. NXCALS keeps the history regardless, so it can be
recovered after the fact. That is what this does.

Counterpart to the banco fork's sps_monitor/backfill_nxcals.py, which backfills
beam / spill / H4-counter data but NOT the TAX: that fork has no TAX code, its
H4 story is built from XBH4.BEND currents. The barrier is XTAX_022_023, which
has no XBH4 prefix and so never appeared in that survey. See
docs/H4_ACCESS_INFERENCE.md.

MUST run on a host that can reach NXCALS (Technical Network). ntof-x17-daq is
TN-trusted and lxplus can reach it; **banco cannot** — banco holds a copy of this
file for parity and for running from lxplus, but its own H4 barrier panel reads
mx17 through the /sps/tax_history proxy rather than keeping local CSVs.

    # on ntof-x17-daq (writes straight into the live log dir)
    /home/mx17/venvs/nxcals/bin/python sps_monitor/backfill_tax_nxcals.py \
        --start 2026-07-14 --end 2026-07-19

    # on lxplus, publishing to the EOS dir the bridge reads
    SPS_SPILL_LOG_DIR=/eos/user/a/akallits/beam_monitor \
    /eos/user/a/akallits/nxcals_venv/bin/python \
        sps_monitor/backfill_tax_nxcals.py --start 2026-07-14 --end 2026-07-19

Safe to re-run: each day is rewritten from the union of what was on disk and
what NXCALS returned, deduplicated on unix_ts. It refuses to touch the CURRENT
day by default, because the live watcher is appending to that file and a
read-modify-write would race it (--include-today overrides, for use while the
watcher is stopped).

Backfilling PAST days is safe with the watcher running: the watcher seeds
_last_tax_ts from the newest row across all files, and older days cannot move
that maximum.
"""

import os
import sys
import csv
import argparse
from datetime import datetime, timedelta

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_DIR)

from sps_monitor.sps_spill_controller import SPS_LOG_DIR

try:
    # x17 repo: the TAX constants and classifier live in the controller.
    from sps_monitor.sps_spill_controller import (H4_TAX_VAR, H4_TAX_OPEN_MAX,
                                                  H4_TAX_BLOCK_MIN, tax_state)
except ImportError:
    # banco / lxplus fork: that controller has NO TAX code — its H4 story is
    # built from XBH4.BEND currents, and the barrier has no XBH4 prefix so it
    # never appeared in that survey. Define them here so this one file is
    # runnable from any TN-capable host whichever fork it is sitting in.
    # Keep these values in sync with the x17 controller.
    H4_TAX_VAR = "XTAX_022_023:POSITION_MEAS"
    H4_TAX_OPEN_MAX = -100.0     # below this: parked out, beam can reach H4
    H4_TAX_BLOCK_MIN = 100.0     # above this: parked in, H4 blocked

    def tax_state(pos):
        """Classify one H4 TAX position: open / blocked / moving (mid-stroke)."""
        if pos is None:
            return None
        if pos <= H4_TAX_OPEN_MAX:
            return "open"
        if pos >= H4_TAX_BLOCK_MIN:
            return "blocked"
        return "moving"

TAX_CSV_FIELDS = ["timestamp", "unix_ts", "position_mm", "state"]


def log(msg):
    print(f"[tax-backfill {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _read_existing(path):
    """{unix_ts: row} for a day already on disk, so a re-run adds to the file
    rather than replacing it."""
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path) as f:
            for r in csv.DictReader(f):
                try:
                    out[round(float(r["unix_ts"]), 3)] = r
                except (TypeError, ValueError, KeyError):
                    continue
    except Exception as e:
        log(f"  WARN could not read {os.path.basename(path)}: {e}")
    return out


def _write_day(path, rows_by_ts):
    """Atomic-ish rewrite: temp file then replace, so an interrupted run cannot
    leave a half-written day behind."""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TAX_CSV_FIELDS)
        w.writeheader()
        for ts in sorted(rows_by_ts):
            w.writerow(rows_by_ts[ts])
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="last day, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--out", default=SPS_LOG_DIR,
                    help=f"output directory (default {SPS_LOG_DIR})")
    ap.add_argument("--include-today", action="store_true",
                    help="also rewrite today's file — ONLY with the watcher stopped")
    ap.add_argument("--driver-port", default="5041",
                    help="Spark driver port; keep away from 5011 (live watcher), "
                         "5001 (Flask) and 5031 (the scalars backfill)")
    args = ap.parse_args()

    today = datetime.now().date()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else today - timedelta(days=1))
    if end >= today and not args.include_today:
        end = today - timedelta(days=1)
        log(f"clamped end to {end} (today is live — use --include-today to override)")
    if start > end:
        log("nothing to do: start is after end")
        return 0

    log(f"target {start} .. {end}   var={H4_TAX_VAR}")
    log(f"out    -> {args.out}")

    import pytimber
    log("starting NXCALS session (Spark spin-up, ~1 min)...")
    db = pytimber.LoggingDB(source="nxcals", sparkprops={
        "spark.driver.port": args.driver_port, "spark.ui.enabled": "false"})
    log("NXCALS session up")

    total_new = 0
    day = start
    while day <= end:
        t0 = datetime.combine(day, datetime.min.time())
        t1 = t0 + timedelta(days=1)
        path = os.path.join(args.out, f"h4_tax_{day:%Y-%m-%d}.csv")
        try:
            res = db.get([H4_TAX_VAR], t0, t1)
            ts, vals = res.get(H4_TAX_VAR, ([], []))
        except Exception as e:
            log(f"  {day}  QUERY FAILED: {type(e).__name__}: {e}")
            day += timedelta(days=1)
            continue

        existing = _read_existing(path)
        before = len(existing)
        for t, v in zip(ts, vals):
            # NXCALS hands these back as numpy scalars, which are not python
            # int/float — a bare float() in a try is the only reliable coercion.
            try:
                t = round(float(t), 3)
                v = float(v)
            except (TypeError, ValueError):
                continue
            if t in existing:
                continue
            existing[t] = {
                "timestamp": datetime.fromtimestamp(t).isoformat(timespec="milliseconds"),
                "unix_ts": t,
                "position_mm": round(v, 3),
                "state": tax_state(v),
            }
        new = len(existing) - before
        total_new += new
        if new or not os.path.exists(path):
            _write_day(path, existing)
        log(f"  {day}  {new:6d} new rows ({before} -> {len(existing)})")
        day += timedelta(days=1)

    log(f"done — {total_new} new rows total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
