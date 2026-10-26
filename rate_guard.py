#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Data-rate guard for unattended lab runs: stop the run if the capture floods.

The VMM 4/7/9 bursts (docs/LAB_COSMICS_OBSERVATIONS.md items 13-16, 36-46) jump to 1-9 GB/min in some
configurations and the hot channel moves with each configuration, so a channel mask cannot be complete. This is the safety net:
every --interval seconds it sums the size of the pcapng files of the run in progress; if the growth over the last --window
seconds exceeds --max-mb-min on --strikes consecutive checks, it

  1. writes config/rate_guard_tripped.json (time, run, rate, the chips and channels carrying the data in the head of the
     newest capture) and a logbook note,
  2. asks the flask server to stop the run (POST /stop_run).

It does nothing when no run is active, and never touches HV or the chip configuration. Start it next to a long run:
    rate_guard.py --max-mb-min 400
(tmux session vmm_rate_guard). --dry-run only logs what it would do.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from run_config_beam import BASE_DATA_DIR  # noqa: E402

LOG = os.path.join(REPO, 'logs', 'rate_guard.log')
TRIP = os.path.join(REPO, 'config', 'rate_guard_tripped.json')
STATE = os.path.join(REPO, 'config', 'current_run_state.json')


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} | {msg}'
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def daq_running():
    return subprocess.run('pgrep -f "[/]daq_control[.]py"', shell=True, capture_output=True).stdout.strip() != b''


def current_run():
    try:
        return json.load(open(STATE)).get('run_name')
    except Exception:
        return None


def run_files(run):
    return glob.glob(os.path.join(BASE_DATA_DIR, 'runs', run, '*', 'raw_daq_data', '*.pcapng'))


def total_bytes(run):
    t = 0
    for p in run_files(run):
        try:
            t += os.path.getsize(p)
        except OSError:
            pass
    return t


def culprits(run, max_packets=3000):
    """Chips / channels carrying the data in the first packets of the newest capture (best effort).

    Uses the DAQ's own decoder (vmm_qa/vmm_stations.parse_pcapng): the fast one it used before moved out to
    P2_basket_analysis/cosmic_vmm_analysis on 2026-10-25, which is not deployed on the DAQ machine."""
    try:
        sys.path.insert(0, os.path.join(REPO, 'vmm_qa'))
        from vmm_stations import parse_pcapng
        import numpy as np
        files = sorted(run_files(run), key=os.path.getmtime)
        hits, _meta = parse_pcapng(files[-1], max_packets=max_packets)
        hits = hits[hits['over_threshold'] == 1]
        tot = len(hits) or 1
        out = []
        for vv, n in hits['vmm'].value_counts().head(3).items():
            a = np.bincount(hits.loc[hits['vmm'] == vv, 'ch'].to_numpy(dtype=np.int64), minlength=64)
            top = np.argsort(-a)[:4]
            out.append({'vmm': int(vv), 'share_pct': round(100 * n / tot, 1), 'top_channels': [int(x) for x in top if a[x]]})
        return out
    except Exception as e:
        return [f'could not decode: {e}']


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--max-mb-min', type=float, default=400.0)
    ap.add_argument('--interval', type=int, default=15)
    ap.add_argument('--window', type=int, default=45, help='seconds over which the growth rate is measured')
    ap.add_argument('--strikes', type=int, default=3)
    ap.add_argument('--flask', default='http://localhost:5002')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    log(f'RATE GUARD START: limit {a.max_mb_min:g} MB/min over {a.window} s, {a.strikes} strikes in a row, dry_run={a.dry_run}')
    hist, strikes, last_run = [], 0, None
    while True:
        time.sleep(a.interval)
        if not daq_running():
            hist, strikes, last_run = [], 0, None
            continue
        run = current_run()
        if run != last_run:
            hist, strikes, last_run = [], 0, run
        now = time.time()
        hist.append((now, total_bytes(run)))
        hist = [h for h in hist if now - h[0] <= a.window + a.interval]
        if len(hist) < 2 or hist[-1][0] - hist[0][0] < a.window * 0.8:
            continue
        rate = (hist[-1][1] - hist[0][1]) / 1e6 / ((hist[-1][0] - hist[0][0]) / 60)
        strikes = strikes + 1 if rate > a.max_mb_min else 0
        if strikes:
            log(f'{run}: {rate:.0f} MB/min > {a.max_mb_min:g} (strike {strikes}/{a.strikes})')
        if strikes >= a.strikes:
            # Stop first: the culprit decode and the logbook note take seconds and must not delay the stop.
            stop_msg = 'dry run: not stopped'
            if not a.dry_run:
                try:
                    req = urllib.request.Request(a.flask + '/stop_run', data=b'{}', headers={'Content-Type': 'application/json'})
                    stop_msg = 'stop_run -> ' + urllib.request.urlopen(req, timeout=30).read().decode()[:120]
                except Exception as e:
                    stop_msg = f'stop_run FAILED: {e}'
            info = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'run': run, 'rate_mb_min': round(rate), 'limit_mb_min': a.max_mb_min,
                    'culprits_in_newest_capture': culprits(run), 'dry_run': a.dry_run}
            try:
                json.dump(info, open(TRIP, 'w'), indent=1)
            except Exception:
                pass
            log('TRIPPED: ' + json.dumps(info))
            log(stop_msg)
            try:
                subprocess.run([sys.executable, os.path.join(REPO, 'lab_logbook.py'), 'note',
                                f'RATE GUARD tripped on {run}: {rate:.0f} MB/min (limit {a.max_mb_min:g}); culprits {info["culprits_in_newest_capture"]}'],
                               capture_output=True, timeout=30)
            except Exception:
                pass
            hist, strikes = [], 0
            time.sleep(120)       # let the run stop before judging again


if __name__ == '__main__':
    main()
