#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive a VMM chip-configuration scan: one RUN per config, unattended.

WHY A SCRIPT AND NOT A RUN PLAN. Every other scan we do varies HV, which lives
in the sub-run schedule, so one run covers the whole scan. The chip config
cannot work that way: chip.apply() refuses while a capture is running, and a
successful apply must be followed by a warm reset to re-arm the run gate. So a
config scan is a SEQUENCE OF RUNS, and something has to sit above the DAQ and
drive it. That is this.

Per config:
    select -> apply (rc 0) -> warm reset (0 failed hybrids) -> start combined
    run -> wait for it to finish -> next

Refusals are deliberate. It will not start a config it cannot finish before
--until, it aborts the whole scan if a warm reset will not reach 0 failed
hybrids, and it never starts a run on top of one that has not exited or while
Dream is still tearing down (Dream answers a trigger with 409 until it is idle).

Interaction with capture_guard: leave the guard RUNNING — its detection and
stop are what catch a dead readout within ~90 s. But disable its auto-recovery
(config/no_auto_recovery) for the duration, because the guard restarting a run
on its own would collide with this script's sequencing. This script re-applies
the config and warm resets before every run anyway, which is the same recovery;
a config whose run dies early is simply retried here.

Usage:
    config_scan.py --until 21:00 --configs a.txt,b.txt [--minutes 55] [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

VMM_FLASK = 'http://localhost:5002'
DREAM_FLASK = 'http://128.141.21.144:5001'
REPO = '/local/p2/DAQ_Control_VMM_Beam'
LOG = '/local/p2/config_scan.log'
NO_AUTO_RECOVERY = os.path.join(REPO, 'config', 'no_auto_recovery')

STEP_TIMEOUT_S = 300
WARM_RESET_TRIES = 3
DREAM_TEARDOWN_TIMEOUT_S = 900
POLL_S = 10
# Wall-clock cost of everything around the data: apply, warm reset, HV ramp from
# 0, teardown and the crate ramping back down. Measured ~8-9 min on 2026-07-30.
OVERHEAD_MIN = 9


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} | {msg}'
    print(line, flush=True)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def req(path, payload=None, base=VMM_FLASK, timeout=20):
    try:
        url = f'{base}{path}'
        if payload is None:
            r = urllib.request.Request(url)
        else:
            r = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {'success': False, 'message': f'request failed: {e}'}


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=20).stdout
    except Exception:
        return ''


def daq_running():
    # [/] both avoids self-matching this very check and excludes
    # vmm_daq_control.py, the persistent server that runs for weeks.
    return sh('pgrep -f "[/]daq_control[.]py" || true').strip() != ''


def dream_idle():
    d = req('/status', base=DREAM_FLASK)
    if not isinstance(d, list):
        return False
    for s in d:
        if s.get('name') == 'daq_control':
            return s.get('status') in ('WAITING', 'Run Complete', 'ERROR')
    return False


def chip_status():
    return req('/chip_config/status')


def active_run_plan():
    """RUN_PLAN as run_config_beam.py will actually use it.

    This script does not choose the schedule — /run_config_py regenerates the
    config from that file at every start. If RUN_PLAN were left on an HV-scan
    plan, each 'config scan' run would silently take that plan's points at that
    plan's voltages, and the whole night would be the wrong measurement.
    """
    for line in open(os.path.join(REPO, 'run_config_beam.py')):
        s = line.strip()
        if s.startswith('RUN_PLAN') and '=' in s and not s.startswith('#'):
            return s.split('=', 1)[1].split('#')[0].strip().strip('\'"')
    return None


def verify_started_run(cfg):
    """The run that just started must be a config_scan run for THIS config.

    Checked against the config actually written for the run, so a stale
    RUN_PLAN, an apply that did not take, or a selected-vs-applied mismatch
    cannot quietly mislabel a whole night of data.
    """
    p = os.path.join(REPO, 'config', 'json_run_configs', 'run_config_beam.json')
    try:
        d = json.load(open(p))
    except Exception as e:
        return False, f'cannot read generated run config: {e}'
    if d.get('run_plan') != 'config_scan':
        return False, f'run_plan is {d.get("run_plan")!r}, expected config_scan'
    if d.get('chip_config') != cfg:
        return False, (f'run says chip_config={d.get("chip_config")!r} but this '
                       f'step is {cfg!r}')
    if len(d.get('sub_runs', [])) != 1:
        return False, f'expected 1 sub-run, got {len(d.get("sub_runs", []))}'
    return True, (f'{d["sub_runs"][0]["sub_run_name"]} '
                  f'{d["sub_runs"][0]["run_time"]} min')


def wait_until(pred, timeout_s, what):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(3)
    log(f'   TIMEOUT after {timeout_s}s waiting for {what}')
    return False


def select_and_apply(cfg):
    r = req('/chip_config/select', {'file': cfg})
    if not r.get('success'):
        log(f'   select failed: {r.get("message")}')
        return False
    req('/chip_config/apply', {})
    if not wait_until(lambda: chip_status().get('running') is False,
                      STEP_TIMEOUT_S, 'chip apply'):
        return False
    last = chip_status().get('last') or {}
    log(f'   applied {last.get("file")} rc={last.get("rc")}')
    return last.get('rc') == 0 and last.get('file') == cfg


def warm_reset_until_ready():
    for attempt in range(1, WARM_RESET_TRIES + 1):
        req('/chip_config/warm_reset', {})
        wait_until(lambda: chip_status().get('warm_reset', {}).get('running') is False,
                   STEP_TIMEOUT_S, 'warm reset')
        w = chip_status().get('warm_reset', {})
        failed = (w.get('last') or {}).get('failed')
        log(f'   warm reset {attempt}/{WARM_RESET_TRIES}: failed={failed} '
            f'armed={w.get("armed")}')
        if failed == 0 and w.get('armed'):
            return True
    return False


def run_one(cfg, minutes, dry_run):
    log(f'=== {cfg} ({minutes} min) ===')
    if dry_run:
        log('   DRY RUN — not touching the DAQ')
        return True

    if daq_running():
        log('   a run is already active; refusing to start another')
        return False
    if not wait_until(dream_idle, DREAM_TEARDOWN_TIMEOUT_S, 'Dream to be idle'):
        log('   Dream never went idle; it would answer the trigger with 409')
        return False
    if not select_and_apply(cfg):
        log('   apply failed — skipping this config')
        return False
    if not warm_reset_until_ready():
        log('   warm reset never reached 0 failed hybrids — ABORTING THE SCAN, '
            'this needs a human')
        return None                      # None = fatal, stop everything

    req('/update_run_config_py', {})
    time.sleep(2)
    r = req('/run_config_py', {'dream': True})
    if not r.get('success'):
        log(f'   start failed: {r.get("message")}')
        return False
    run_name = r.get('run_name')
    log(f'   started {run_name}')

    ok, detail = verify_started_run(cfg)
    if not ok:
        log(f'   WRONG RUN STARTED: {detail} — stopping it rather than recording '
            f'mislabelled data')
        req('/stop_run', {})
        return None                      # fatal: the setup is not what we think
    log(f'   verified: {detail}')

    # Wait for it to finish. Generous: minutes of data plus ramp and teardown.
    deadline = time.time() + (minutes + 3 * OVERHEAD_MIN) * 60
    while time.time() < deadline:
        if not daq_running():
            log(f'   {run_name} finished')
            return True
        time.sleep(POLL_S)
    log(f'   {run_name} overran its window — leaving it and stopping the scan')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--until', required=True,
                    help='HH:MM wall clock; no config is STARTED unless it can '
                         'finish by then')
    ap.add_argument('--configs', required=True,
                    help='comma-separated config_ext filenames, in order')
    ap.add_argument('--minutes', type=int, default=None,
                    help='data minutes per config; default: fit them all in the '
                         'window with equal livetime')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    configs = [c.strip() for c in a.configs.split(',') if c.strip()]
    hh, mm = (int(x) for x in a.until.split(':'))
    now = datetime.now()
    until = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if until <= now:
        until += timedelta(days=1)
    avail = (until - now).total_seconds() / 60

    minutes = a.minutes
    if minutes is None:
        # Equal livetime for every config — a config comparison at unequal
        # statistics is not a comparison.
        minutes = int(avail / len(configs)) - OVERHEAD_MIN
    if minutes < 5:
        log(f'Only {avail:.0f} min available for {len(configs)} configs — '
            f'that is {minutes} min each. Refusing; give fewer configs or more time.')
        return 2

    plan = active_run_plan()
    if plan != 'config_scan' and not a.dry_run:
        log(f'REFUSING: run_config_beam.py has RUN_PLAN={plan!r}, not '
            f'\'config_scan\'. Every run would take that plan schedule and '
            f'voltages instead of a fixed-HV config point. Set it and re-run.')
        return 2

    log(f'CONFIG SCAN START: {len(configs)} configs x {minutes} min, '
        f'until {until:%H:%M} ({avail:.0f} min available, ~{OVERHEAD_MIN} min '
        f'overhead per config)')
    for c in configs:
        log(f'   queued: {c}')

    if not a.dry_run:
        with open(NO_AUTO_RECOVERY, 'w') as f:
            f.write('config_scan.py is sequencing runs; its own retry logic '
                    'replaces capture_guard auto-recovery. Detection and the '
                    'automatic STOP stay active.\n')
        log(f'   disabled capture_guard auto-recovery via {NO_AUTO_RECOVERY}')

    done, failed = [], []
    try:
        for cfg in configs:
            left = (until - datetime.now()).total_seconds() / 60
            need = minutes + OVERHEAD_MIN
            if left < need:
                log(f'SKIP {cfg}: {left:.0f} min left, needs {need}. '
                    f'Not starting what cannot finish.')
                failed.append((cfg, 'no time left'))
                continue
            res = run_one(cfg, minutes, a.dry_run)
            if res is None:
                log('FATAL — stopping the scan')
                failed.append((cfg, 'fatal'))
                break
            (done if res else failed).append(
                cfg if res else (cfg, 'failed'))
    finally:
        if not a.dry_run:
            try:
                os.remove(NO_AUTO_RECOVERY)
                log('   re-enabled capture_guard auto-recovery')
            except FileNotFoundError:
                pass

    log(f'CONFIG SCAN END: {len(done)} done, {len(failed)} not')
    for c in done:
        log(f'   OK   {c}')
    for c in failed:
        log(f'   MISS {c}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
