#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QA watcher: turns decoded stores into plots and the trend dashboard.

This no longer reads pcapng. vmm_processor_watcher decodes each capture into
<subrun>/hits_store/<capture>/ and renames it into place atomically, so a store
directory that exists is complete by construction -- there is nothing to poll
for size stability and the two watchers never race.

    processor_watcher   pcapng -> hits_store         (decode + counts + scalars)
    qa_watcher          hits_store -> plots + trend   (this file)

What it draws, and why not everything:

  * The full 36-PNG QA set is rendered for the FIRST store of each sub-run
    (plot_policy='subrun'). Those plots are how the mapping and offset problems
    were found, so they are worth having -- but at ~43 s each they cannot run on
    every capture, and a sub-run boundary is exactly where conditions change
    (each mesh-scan point is its own sub-run).
  * Every store contributes to the TREND dashboard, which is the thing that
    answers "is something drifting" -- the question 36 per-file PNGs cannot.
  * Any capture can still be rendered on demand: the store keeps the columns,
    so vmm_pcapng_qa.py <store_dir> reproduces its plots without the pcapng.

Usage:
    python qa_watcher.py <qa_config_json_path>

Config keys (see qa_config.py to generate the JSON):
  runs_dir            : top-level directory containing run_N/ subdirs
  store_inner_dir     : subdir holding the decoded stores  (default: 'hits_store')
  qa_out_base         : QA outputs land in <qa_out_base>/<run>/<subrun>/<capture>/
  plot_policy         : 'subrun' (first store per sub-run, default)
                        'always' (every store -- only if you have the headroom)
                        'never'  (trend only)
  do_trend            : refresh the trend dashboard        (default: true)
  trend_scope         : 'subrun' | 'run' | 'both'          (default: 'both')
  include_runs / exclude_runs / poll_interval / stale_run_days
  memory_kill_pct / max_attempts / cpu_nice / cpu_affinity / qa_threads
  data_format / calibration
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher_common as wc

_SPINNER = '|/-\\'


def main():
    if len(sys.argv) != 2:
        print("Usage: python qa_watcher.py <qa_config_json_path>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        config = json.load(f)
    run_watcher(config)


def run_watcher(config: dict):
    runs_dir = Path(config['runs_dir'])
    store_inner = config.get('store_inner_dir', 'hits_store')
    qa_out_base = Path(config.get('qa_out_base', str(runs_dir.parent / 'analysis')))
    plot_policy = config.get('plot_policy', 'subrun')
    do_trend = config.get('do_trend', True)
    trend_scope = config.get('trend_scope', 'both')

    include_runs = set(config.get('include_runs') or [])
    exclude_runs = set(config.get('exclude_runs') or [])
    poll_interval = config.get('poll_interval', 10)
    stale_run_days = config.get('stale_run_days', 1)

    memory_kill_pct = config.get('memory_kill_pct', 80)
    max_attempts = config.get('max_attempts', 3)
    cpu_nice = config.get('cpu_nice', 19)
    cpu_affinity = config.get('cpu_affinity')
    qa_threads = config.get('qa_threads', 4)
    data_format = config.get('data_format', 'SRS')
    calibration = config.get('calibration')

    here = Path(__file__).parent
    python_exe = config.get('qa_python', str(here / '.venv' / 'bin' / 'python'))
    qa_script = Path(config.get('qa_script', str(here / 'vmm_qa' / 'vmm_pcapng_qa.py')))
    trend_script = Path(config.get('trend_script', str(here / 'vmm_qa' / 'vmm_trend.py')))
    state_path = Path(config.get('state_file', here / 'config' / 'qa_state.json'))
    reset_signal = Path(config.get('reset_signal', here / 'config' / 'qa_reset.json'))

    log = wc.Logger(here / 'logs' / 'qa_watcher.log', 'qa_watcher')

    print(f"[qa_watcher] runs_dir        : {runs_dir}")
    print(f"[qa_watcher] store_inner_dir : {store_inner}   (reads stores, not pcapng)")
    print(f"[qa_watcher] qa_out_base     : {qa_out_base}")
    print(f"[qa_watcher] plot_policy     : {plot_policy}   trend: {do_trend} ({trend_scope})")
    print(f"[qa_watcher] memory_kill_pct : {memory_kill_pct}%   max_attempts: {max_attempts}")
    log('START', runs_dir=str(runs_dir), plot_policy=plot_policy,
        memory_kill_pct=f'{memory_kill_pct}%', cpu_nice=cpu_nice)

    done_files, fail_counts = wc.load_state(state_path)
    checked_stale = set()
    idle_ticks = 0
    idle_line = False

    def _end_idle():
        nonlocal idle_line
        if idle_line:
            sys.stdout.write('\n')
            idle_line = False

    def _scan_once() -> bool:
        nonlocal done_files, fail_counts

        reset = wc.pop_reset_signal(reset_signal)
        if reset is not False:
            if reset is None:
                done_files, fail_counts = {}, {}
                checked_stale.clear()
                print("\n[qa_watcher] reset: all runs re-queued")
            else:
                for key in list(done_files):
                    if key[0] in reset:
                        done_files.pop(key, None)
                        fail_counts.pop(key, None)
                checked_stale.difference_update(reset)
                print(f"\n[qa_watcher] reset: {sorted(reset)}")
            wc.save_state(state_path, done_files, fail_counts)

        if not runs_dir.is_dir():
            return False

        run_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
        if include_runs:
            run_dirs = [d for d in run_dirs if d.name in include_runs]
        if exclude_runs:
            run_dirs = [d for d in run_dirs if d.name not in exclude_runs]

        for run_dir in wc.newest_first(run_dirs):
            if run_dir.name in checked_stale:
                continue
            if wc.run_is_stale(run_dir, store_inner, stale_run_days):
                checked_stale.add(run_dir.name)
                _end_idle()
                print(f"[qa_watcher] stale, skipping: {run_dir.name}")
                continue

            for subrun in wc.newest_first([d for d in run_dir.iterdir() if d.is_dir()]):
                store_root = subrun / store_inner
                if not store_root.is_dir():
                    continue
                key = (run_dir.name, subrun.name)
                done = done_files.setdefault(key, set())
                fails = fail_counts.setdefault(key, {})

                stores = [d for d in store_root.iterdir()
                          if d.is_dir() and (d / 'scalars.json').exists()]
                if not stores:
                    continue
                pending = [d for d in wc.newest_first(stores)
                           if d.name not in done
                           and fails.get(d.name, 0) < max_attempts]
                if not pending:
                    continue

                store = pending[0]
                out_dir = qa_out_base / run_dir.name / subrun.name / store.name
                # first store of the sub-run == nothing plotted here yet
                plotted_any = any((qa_out_base / run_dir.name / subrun.name /
                                   s.name / 'events.json').exists() for s in stores)
                want_plots = (plot_policy == 'always' or
                              (plot_policy == 'subrun' and not plotted_any))

                _end_idle()
                ok = True
                if want_plots:
                    mem_pct, free_mb = wc.mem_usage_pct()
                    print(f"[qa_watcher] {run_dir.name}/{subrun.name}  {store.name}  "
                          f"-> full QA plots (mem {mem_pct:.0f}%)")
                    log('QA_LAUNCH', run=run_dir.name, subrun=subrun.name,
                        file=store.name, mem_pct=f'{mem_pct:.1f}%',
                        free_mb=f'{free_mb:.0f}', policy=plot_policy)
                    cmd = [python_exe, str(qa_script), str(store),
                           '--out-dir', str(out_dir),
                           '--events-json', '--format', data_format]
                    if calibration:
                        cmd += ['--calibration', str(calibration)]
                    t0 = time.time()
                    ok, reason = wc.run_monitored(
                        cmd, label=store.name, memory_kill_pct=memory_kill_pct,
                        cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                        threads=qa_threads, log=log, component='qa_watcher')
                    dt = time.time() - t0
                    if ok:
                        print(f"[qa_watcher]   plots done in {dt:.1f}s")
                        log('QA_DONE', run=run_dir.name, subrun=subrun.name,
                            file=store.name, wall_s=f'{dt:.1f}')
                    else:
                        n = fails.get(store.name, 0) + 1
                        fails[store.name] = n
                        print(f"[qa_watcher]   FAILED ({reason}) — attempt {n}/{max_attempts}")
                        log('QA_FAILED', run=run_dir.name, subrun=subrun.name,
                            file=store.name, reason=reason, attempts=n)
                else:
                    log('QA_SKIP_PLOTS', run=run_dir.name, subrun=subrun.name,
                        file=store.name, policy=plot_policy)

                if ok:
                    done.add(store.name)
                    fails.pop(store.name, None)
                    if do_trend:
                        _refresh_trend(python_exe, trend_script, run_dir, subrun,
                                       qa_out_base, trend_scope, log)
                wc.save_state(state_path, done_files, fail_counts)
                return True
        return False

    while True:
        try:
            if _scan_once():
                idle_ticks = 0
                continue
        except Exception as e:
            _end_idle()
            print(f"[qa_watcher] scan error: {type(e).__name__}: {e}")
            log('SCAN_ERROR', error=f'{type(e).__name__}: {e}')

        idle_ticks += 1
        ts = time.strftime('%H:%M:%S')
        sp = _SPINNER[idle_ticks % 4]
        sys.stdout.write(f"\r[qa_watcher] {sp} idle  #{idle_ticks}  "
                         f"{idle_ticks * poll_interval}s  {ts}          ")
        sys.stdout.flush()
        idle_line = True
        time.sleep(poll_interval)


def _refresh_trend(python_exe, trend_script, run_dir, subrun, qa_out_base,
                   scope, log):
    """Rebuild the trend dashboard(s) covering this sub-run.

    Cheap enough to redo on every store (it reads only scalars.json, ~3 kB
    each), which keeps the dashboard current without any scheduling.
    """
    targets = []
    if scope in ('subrun', 'both'):
        targets.append((subrun, qa_out_base / run_dir.name / subrun.name /
                        '_trend.png', f'{run_dir.name}/{subrun.name}'))
    if scope in ('run', 'both'):
        targets.append((run_dir, qa_out_base / run_dir.name / '_trend.png',
                        run_dir.name))
    for root, out_png, title in targets:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        try:
            import subprocess
            subprocess.run([python_exe, str(trend_script), str(root),
                            '--out', str(out_png),
                            '--title', f'VMM trend — {title}'],
                           check=True, capture_output=True, timeout=300)
        except Exception as e:
            # the trend is a convenience; never let it fail a QA cycle
            log('TREND_FAILED', target=title, error=f'{type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
