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
import urllib.error
import urllib.request
from datetime import datetime, timedelta

VMM_FLASK = 'http://localhost:5002'
DREAM_FLASK = 'http://128.141.21.144:5001'
REPO = '/local/p2/DAQ_Control_VMM_Beam'
LOG = '/local/p2/config_scan.log'
NO_AUTO_RECOVERY = os.path.join(REPO, 'config', 'no_auto_recovery')
HV_HOLD = os.path.join(REPO, 'config', 'hv_hold')

STEP_TIMEOUT_S = 300
WARM_RESET_TRIES = 3
DREAM_TEARDOWN_TIMEOUT_S = 900
POLL_S = 10
# Wall-clock cost around the data: apply, warm reset, start, gate, teardown.
# With the HV hold in force the crate stays biased between runs, so the two
# expensive parts — ramping down at the end of a run and back up from 0 at the
# start of the next, ~2 min each — only happen at the START of a window and at
# its END. Without the hold this was ~9 min; with it, ~4-5.
OVERHEAD_MIN = 6


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
    except urllib.error.HTTPError as e:
        # Read the BODY. urllib raises on any non-2xx, and these routes put the
        # actual reason in the json — /run_config_py answers 502 with "Dream
        # trigger failed - HTTP 422", and 422 is Dream refusing the HV targets.
        # Reporting only "HTTP Error 502" throws that away and turns a one-line
        # diagnosis into a hunt.
        try:
            body = json.loads(e.read().decode())
            msg = body.get('message') or str(body)
        except Exception:
            msg = f'HTTP {e.code} with no readable body'
        return {'success': False, 'message': f'HTTP {e.code}: {msg}'}
    except Exception as e:
        return {'success': False, 'message': f'request failed: {e}'}


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=20).stdout
    except Exception:
        return ''


try:
    sys.path.insert(0, REPO)
    from run_config_beam import BASE_DATA_DIR
    RUNS_DIR = os.path.join(BASE_DATA_DIR, 'runs')
except Exception:
    RUNS_DIR = '/local/p2/p2data/TB_July26_H4/runs'


def beam_sample():
    b = req('/beam/status', base=DREAM_FLASK, timeout=8)
    return bool(b.get('beam_on')), float(b.get('pulses_10min') or 0)


def dream_events():
    """Dream's event counter = the number of external TCM triggers this run.

    THE metric for 'did this config get beam'. Captured BYTES cannot be used:
    they are what the chip config changes. On 2026-07-30 the first two configs
    gave 25 GB (gain 3.0) and 145 GB (gain 4.5) with the beam on 100% of samples
    in both, and a bytes-based rule duly flagged the perfectly good gain-3.0 run
    as LOW_BEAM and would have had it retaken.

    Dream reads the uRWELLs off the same TCM coincidence and knows nothing about
    the VMM chip config, so its event count measures the beam and only the beam.
    That is the same argument quick_scripts/flag_beam_quality.py makes for using
    event counts across an HV scan; bytes never satisfied it.
    """
    d = req('/status', base=DREAM_FLASK, timeout=8)
    if not isinstance(d, list):
        return None
    for s in d:
        if s.get('name') == 'dream_daq':
            ev = s.get('run_events')
            return int(ev) if isinstance(ev, (int, float)) else None
    return None


def run_capture_bytes(run_name):
    total = files = 0
    for dp, _, fns in os.walk(os.path.join(RUNS_DIR, run_name)):
        for f in fns:
            if f.endswith('.pcapng'):
                total += os.path.getsize(os.path.join(dp, f))
                files += 1
    return total, files


def write_beam_quality(run_name, cfg, stats):
    """Verdict next to the data, not in a log.

    The beam is unstable tonight, so 'did this config actually get beam' has to
    survive as a fact on disk — whoever analyses this days from now decides what
    to retake, and they will not have this session. Deliberately follows the
    convention of quick_scripts/flag_beam_quality.py on the Dream side.
    """
    try:
        p = os.path.join(RUNS_DIR, run_name, 'BEAM_QUALITY.json')
        with open(p, 'w') as f:
            json.dump({'run': run_name, 'chip_config': cfg, **stats}, f, indent=2)
    except Exception as e:
        log(f'   could not write BEAM_QUALITY.json: {e}')


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

    # Wait for it to finish, sampling the beam as we go. With the beam this
    # unstable, whether a config actually got beam is as important as whether
    # the DAQ ran.
    deadline = time.time() + (minutes + 3 * OVERHEAD_MIN) * 60
    ev0 = dream_events()          # baseline, for the per-run assumption above
    on = tot = 0
    pulses = []
    last_sample = 0.0
    while time.time() < deadline:
        if time.time() - last_sample >= 60:
            last_sample = time.time()
            beam_on, p10 = beam_sample()
            tot += 1
            on += 1 if beam_on else 0
            pulses.append(p10)
        if not daq_running():
            nbytes, nfiles = run_capture_bytes(run_name)
            frac = (on / tot) if tot else None
            # Trigger count, read while Dream still reports this run.
            # run_events is PER-RUN: it read 1.38M just after a 5-minute run and
            # 24M during a 3-hour one, so the end value is this run's total.
            # ev0 (sampled just after the start) is recorded alongside it, so if
            # that assumption ever breaks the JSON shows it rather than hiding
            # a wrong number behind a plausible one.
            events = dream_events()
            stats = {'beam_on_fraction': round(frac, 3) if frac is not None else None,
                     'beam_samples': tot,
                     'mean_pulses_10min': round(sum(pulses) / len(pulses), 1) if pulses else None,
                     # THE beam metric: triggers, independent of the chip config.
                     'dream_events': events,
                     'dream_events_at_start': ev0,
                     'events_per_min': round(events / minutes, 1) if events else None,
                     # Informational ONLY — bytes track the chip config, not the
                     # beam, so they must never drive a retake decision.
                     'capture_bytes': nbytes, 'capture_files': nfiles,
                     'minutes_requested': minutes}
            write_beam_quality(run_name, cfg, stats)
            log(f'   {run_name} finished — {events:,} triggers'
                f' ({stats["events_per_min"]}/min), beam on {frac:.0%} of {tot} '
                f'samples, mean {stats["mean_pulses_10min"]} pulses/10min, '
                f'{nbytes/1e6:.0f} MB in {nfiles} files'
                if (frac is not None and events) else
                f'   {run_name} finished — {nbytes/1e6:.0f} MB in {nfiles} files '
                f'(NO trigger count from Dream; beam verdict will be unreliable)')
            return {'cfg': cfg, 'run': run_name, **stats}
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
            # Hold the crate biased between runs — only the chip config changes,
            # so cycling HV every time is pure cost. Released before the LAST
            # config so that run powers the crate down normally. Also released
            # in the finally below, so an abort never leaves HV up on a hold
            # nobody is watching.
            last = (cfg == configs[-1])
            if not a.dry_run:
                if last:
                    try:
                        os.remove(HV_HOLD)
                        log('   released HV hold — this is the last config, its '
                            'run will power the crate off')
                    except FileNotFoundError:
                        pass
                elif not os.path.exists(HV_HOLD):
                    with open(HV_HOLD, 'w') as f:
                        f.write('config_scan.py: keep the crate biased between '
                                'runs of this scan; the final run powers off.\n')
                    log('   HV hold in force — crate stays biased between runs')

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
            if res:
                done.append(res)          # dict of that run's beam stats
            else:
                failed.append((cfg, 'failed'))
    finally:
        if not a.dry_run:
            try:
                os.remove(NO_AUTO_RECOVERY)
                log('   re-enabled capture_guard auto-recovery')
            except FileNotFoundError:
                pass
            # Never leave a hold behind: if the scan aborts mid-sequence the
            # crate may still be biased, and the next run must power off
            # normally rather than inherit a hold nobody set.
            try:
                os.remove(HV_HOLD)
                log('   released HV hold (scan over). NOTE: if the scan aborted '
                    'early the crate may still be BIASED — check it.')
            except FileNotFoundError:
                pass

    log(f'CONFIG SCAN END: {len(done)} done, {len(failed)} not')

    # --- retake list -----------------------------------------------------
    # Cross-RUN comparison, because a config scan has one sub-run per run and
    # so has no within-run median to judge against (which is the reference
    # quick_scripts/flag_beam_quality.py uses). Comparing across runs is sound
    # here for the same reason it gives: the trigger is the external TCM
    # coincidence, independent of the chip config, and every run is the same
    # length — so differences in captured volume are beam differences.
    stats = [d for d in done if isinstance(d, dict) and d.get('dream_events')]
    if stats:
        evs = sorted(d['dream_events'] for d in stats)
        median = evs[len(evs) // 2]
        log(f'   median {median:,} triggers over {len(stats)} runs '
            f'(Dream event count — independent of the chip config)')
        log('   | config | triggers | % median | beam on | MB (info only) | verdict |')
        for d in sorted(stats, key=lambda x: x['dream_events']):
            frac = d['dream_events'] / median if median else 0
            verdict = ('NO_BEAM — RETAKE' if frac <= 0.10 else
                       'LOW_BEAM — review' if frac < 0.50 else 'OK')
            bo = d.get('beam_on_fraction')
            log(f'   | {d["cfg"]} | {d["dream_events"]:,} | {frac:.0%} | '
                f'{"?" if bo is None else f"{bo:.0%}"} | '
                f'{d.get("capture_bytes", 0)/1e6:.0f} | {verdict} |')
        log('   MB is shown for information only: captured volume tracks the '
            'chip config (gain/peaktime/thresholds), not the beam, so it must '
            'never decide a retake.')
        retake = [d['cfg'] for d in stats if d['dream_events'] / median <= 0.10] if median else []
        if retake:
            log('   RETAKE THESE (no usable beam):')
            for c in retake:
                log(f'      {c}')
        else:
            log('   no config needs retaking on beam grounds')
    for c in failed:
        log(f'   MISS {c}')
    log('   per-run detail is in each run BEAM_QUALITY.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
