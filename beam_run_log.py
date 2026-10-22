#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-sub-run beam log — the exposure record an HV scan needs to be comparable.

WHY THIS EXISTS (2026-08-02). A scan point is only comparable with another point
if you know how much beam each one actually saw. Three separate gaps left us
unable to say that:

  * The mesh/drift plans write no BEAM_QUALITY.json. Only config_scan.py does,
    so every HV scan point ever taken here carries no beam record at all.
  * The NXCALS intensity feed (lxplus -> EOS -> beam_bridge) has been dead since
    2026-08-01 17:29, so /beam/status has published nothing for a day.
  * Vistar, the one live source, appends to logs/beam_history.csv only on an
    ON/OFF TRANSITION. A point taken during steady beam produces no rows at all,
    and a slow intensity drift between two points is completely invisible.

The cost was immediate: on run_61 the 450 V point yielded LESS per live second
than the 440 V point below it, and we could not tell gain suppression near the
discharge limit from the beam simply being weaker during that half hour.

So this samples every source on a fixed cadence and writes one row per sample
into the run directory, where it travels with the data to EOS. It computes
nothing and decides nothing — analysis normalises from the rows afterwards.

Deliberately a separate process, not a hook inside daq_control: it must never be
able to delay, block or crash a run. Every source is best-effort; anything
unreachable is recorded as empty and the loop continues.

Usage:  beam_run_log.py [--interval S]
Stop:   kill the process (rows are flushed every sample).
"""

import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
RUN_STATE = os.path.join(REPO, 'config', 'current_run_state.json')
LOCAL_FLASK = 'http://localhost:5002'
DREAM_FLASK = 'http://128.141.21.144:5001'
LOG_NAME = 'beam_run_log.csv'

COLUMNS = [
    'timestamp', 'unix_ts', 'run', 'sub_run', 'daq_status',
    # Vistar: the SPS target intensity page. Independent of NXCALS and of
    # lxplus; this is what kept working on 2026-08-02.
    'vistar_state', 'vistar_target', 'vistar_intensity_e11',
    # NXCALS via the bridge. age_s and last_pulse_time are NOT optional extras:
    # when the lxplus watcher dies the bridge keeps serving the last payload it
    # ever saw, so last_pulse_e10 stays a perfectly plausible number (1231.45
    # on 2026-08-02, from 18:51 the PREVIOUS DAY). Without the age beside it
    # there is no way to tell live beam from a 20-hour-old freeze, and the whole
    # point of this file is to make exposure unambiguous.
    'nxcals_stale', 'nxcals_age_s', 'nxcals_last_pulse_time',
    'nxcals_last_pulse_e10', 'nxcals_protons_10min_e10', 'nxcals_pulses_10min',
    # Dream's cumulative trigger count. The most direct integrated-exposure
    # proxy we have: differencing it across a sub-run gives triggers taken.
    'dream_run_events',
    # Bytes captured so far in this sub-run, for a cross-check that does not
    # depend on any beam instrumentation at all.
    'subrun_bytes',
]


def _get_json(url, timeout=8):
    """Best-effort GET. Returns {} on any failure — never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return {}


def _run_active():
    """Is a run actually collecting right now?

    Dream is NOT a usable signal for this: after a run ends it keeps reporting
    the last sub-run name indefinitely (status goes to 'Run Complete' but the
    Subrun field stays), so gating on 'Dream named a sub-run' logs idle rows
    forever and pads a finished run's exposure with time it never took beam.
    The daq_control process is the honest test, and it is the same idiom
    stop_run_at.sh and config_scan.py use.
    """
    try:
        return subprocess.run(['pgrep', '-f', '[/]daq_control[.]py'],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _active_run():
    """(run_name, run_dir) from the run-state file, or (None, None)."""
    try:
        with open(RUN_STATE) as f:
            run = json.load(f).get('run_name')
    except Exception:
        return None, None
    if not run:
        return None, None
    try:
        sys.path.insert(0, REPO)
        import run_config_beam as rc
        return run, os.path.join(rc.BASE_DATA_DIR, 'runs', run)
    except Exception:
        return run, None


def _dream_fields():
    """(sub_run, daq_status, run_events) from Dream's component status."""
    sub_run = daq_status = run_events = ''
    for comp in _get_json(f'{DREAM_FLASK}/status') or []:
        if not isinstance(comp, dict):
            continue
        if comp.get('name') == 'daq_control':
            daq_status = comp.get('status', '')
            for f in comp.get('fields', []):
                if f.get('label') == 'Subrun':
                    sub_run = f.get('value', '')
        elif comp.get('name') == 'dream_daq':
            run_events = comp.get('run_events', '')
    return sub_run, daq_status, run_events


def _subrun_bytes(run_dir, sub_run):
    """Total pcapng bytes in this sub-run so far. 0 if not resolvable."""
    if not run_dir or not sub_run:
        return ''
    d = os.path.join(run_dir, sub_run)
    total = 0
    for root, _dirs, files in os.walk(d):
        for fn in files:
            if fn.endswith('.pcapng'):
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    return total


def sample():
    """One row of every source, as a dict. Never raises."""
    run, run_dir = _active_run()
    sub_run, daq_status, run_events = _dream_fields()
    vis = _get_json(f'{LOCAL_FLASK}/beam_state')
    nx = _get_json(f'{DREAM_FLASK}/beam/status')
    now = datetime.datetime.now()
    return run, run_dir, {
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'unix_ts': f'{now.timestamp():.0f}',
        'run': run or '',
        'sub_run': sub_run,
        'daq_status': daq_status,
        'vistar_state': vis.get('state', ''),
        'vistar_target': vis.get('target', ''),
        'vistar_intensity_e11': vis.get('intensity_e11', ''),
        # stale=True is the honest value when the lxplus watcher is dead: the
        # bridge keeps answering, but with a frozen payload. Trust age_s over
        # stale — the bridge briefly serves a freshly-written 'waiting' state
        # with stale=false and no pulse fields at all while it is deciding.
        'nxcals_stale': nx.get('stale', ''),
        'nxcals_age_s': (f"{nx['age_s']:.0f}" if isinstance(nx.get('age_s'), (int, float)) else ''),
        'nxcals_last_pulse_time': nx.get('last_pulse_time', ''),
        'nxcals_last_pulse_e10': nx.get('last_pulse_e10', ''),
        'nxcals_protons_10min_e10': nx.get('protons_10min_e10', ''),
        'nxcals_pulses_10min': nx.get('pulses_10min', ''),
        'dream_run_events': run_events,
        'subrun_bytes': _subrun_bytes(run_dir, sub_run),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=float, default=20.0,
                    help='seconds between samples (default 20)')
    args = ap.parse_args()

    print(f'[beam_run_log] sampling every {args.interval:.0f}s', flush=True)
    last_run = None
    while True:
        try:
            run, run_dir, row = sample()
            # Only log while a run is actually collecting. Idle time between
            # runs is not exposure and would pad the file with rows that mean
            # nothing; the sub_run field is what ties a row to a scan point.
            if run_dir and row['sub_run'] and _run_active():
                path = os.path.join(run_dir, LOG_NAME)
                new = not os.path.exists(path)
                os.makedirs(run_dir, exist_ok=True)
                with open(path, 'a', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=COLUMNS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
                if run != last_run:
                    print(f'[beam_run_log] logging {run} -> {path}', flush=True)
                    last_run = run
        except Exception as e:                      # never die on a bad sample
            print(f'[beam_run_log] sample failed: {e}', flush=True)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
