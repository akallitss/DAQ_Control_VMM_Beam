#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous on-the-fly QA watcher for nTof DREAM DAQ data.

Watches all runs under a top-level runs directory and runs the QA analysis
script whenever new combined_hits files appear.  Runs independently of
daq_control.py and processor_watcher.py; start/stop from the flask UI.

Usage:
    python qa_watcher.py <qa_config_json_path>

Config keys (see qa_config.py to generate the JSON):
  runs_dir                : top-level directory containing run_N/ subdirs
  analysis_dir            : path to the repository holding the QA script
                            ('ntof_x17_dir' still accepted for backward compat)
  qa_script_rel_path      : QA entry script, relative to analysis_dir
                            (default: 'ntof_daq_analysis/detector_qa.py')
  qa_python_rel_path      : python interpreter, relative to analysis_dir
                            (default: '.venv/bin/python')
  combined_hits_inner_dir : subdir for combined hits files  (default: 'combined_hits_root')
  qa_file_mode            : 'all' | 'first' | 'per_file'   (default: 'all')
                              all      — rerun QA on all accumulated files whenever a new one appears
                              first    — run QA once per subrun using only file_num=0
                              per_file — independent QA output per file_num
  include_runs            : list of run directory names to process exclusively (null = all)
  exclude_runs            : list of run directory names to skip (null = none)
  poll_interval           : seconds between scans   (default: 10)
  stale_run_days          : runs with no new combined_hits for this many days are skipped (default: 4)
  memory_kill_pct         : kill the QA process if system RAM usage exceeds this % (default: 80)
                              The QA is always launched; memory is monitored during the run and
                              the process is terminated if the system crosses the threshold.
                              A killed subrun is NOT marked done and will be retried next poll.
  cpu_nice                : nice level for the QA subprocess (default: 19, lowest priority).
                              Also runs the process at ionice idle class so DAQ I/O wins.
                              null disables both niceing and ionice.
  cpu_affinity            : list of CPU core ids to pin the QA subprocess to via taskset
                              (default: null = all cores).  e.g. [2, 3, 4, 5] reserves cores
                              0-1 for the DAQ on a 6-core box.
  qa_threads              : cap numpy/BLAS/uproot thread pools to this many threads
                              (default: null = derived from len(cpu_affinity), else unlimited).
"""

import os
import re
import sys
import json
import time
import datetime
import subprocess
from pathlib import Path

_LOG_FILE = Path(__file__).parent / 'logs' / 'qa_watcher.log'


def _log(event: str, **details):
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts         = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        detail_str = ' | '.join(f'{k}={v}' for k, v in details.items())
        line       = f"{ts} | {event:<16} | qa_watcher   | {detail_str}\n"
        with open(_LOG_FILE, 'a') as f:
            f.write(line)
    except Exception as e:
        print(f"[qa_watcher] Warning: could not write to log: {e}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python qa_watcher.py <qa_config_json_path>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    with open(config_path) as f:
        config = json.load(f)

    reset_signal_path = config_path.parent / 'qa_reset.json'
    run_watcher(config, reset_signal_path)


# ---------------------------------------------------------------------------
# Main watcher loop
# ---------------------------------------------------------------------------

def run_watcher(config: dict, reset_signal_path: Path = None):
    runs_dir       = Path(config['runs_dir'])
    # 'analysis_dir' is the new generic key; 'ntof_x17_dir' kept for backward compat.
    analysis_dir   = Path(config.get('analysis_dir') or config['ntof_x17_dir'])
    combined_inner = config.get('combined_hits_inner_dir', 'combined_hits_root')
    mode           = config.get('qa_file_mode', 'all')

    include_runs = set(config['include_runs']) if config.get('include_runs') else None
    exclude_runs = set(config['exclude_runs']) if config.get('exclude_runs') else set()

    poll_interval   = config.get('poll_interval',   10)
    stale_run_days  = config.get('stale_run_days',    4)
    memory_kill_pct = config.get('memory_kill_pct',  80)
    cpu_nice        = config.get('cpu_nice',         19)
    cpu_affinity    = config.get('cpu_affinity')  # list[int] or None
    qa_threads      = config.get('qa_threads')    # int or None
    if qa_threads is None and cpu_affinity:
        qa_threads = len(cpu_affinity)

    # QA entry point and interpreter, relative to analysis_dir (defaults = nTof layout).
    qa_script  = analysis_dir / config.get('qa_script_rel_path', 'ntof_daq_analysis/detector_qa.py')
    qa_python  = analysis_dir / config.get('qa_python_rel_path', '.venv/bin/python')

    print(f"[qa_watcher] runs_dir        : {runs_dir}")
    print(f"[qa_watcher] qa_script       : {qa_script}")
    print(f"[qa_watcher] python          : {qa_python}")
    print(f"[qa_watcher] mode            : {mode}")
    if include_runs:
        print(f"[qa_watcher] include_runs    : {sorted(include_runs)}")
    if exclude_runs:
        print(f"[qa_watcher] exclude_runs    : {sorted(exclude_runs)}")
    print(f"[qa_watcher] poll            : {poll_interval}s  stale_after={stale_run_days}d")
    print(f"[qa_watcher] memory_kill_pct : {memory_kill_pct}%")
    print(f"[qa_watcher] cpu_nice        : {cpu_nice}")
    print(f"[qa_watcher] cpu_affinity    : {cpu_affinity if cpu_affinity else 'all cores'}")
    print(f"[qa_watcher] qa_threads      : {qa_threads if qa_threads else 'unlimited'}")
    _log('START', runs_dir=runs_dir, mode=mode, memory_kill_pct=f'{memory_kill_pct}%',
         cpu_nice=cpu_nice, cpu_affinity=cpu_affinity, qa_threads=qa_threads)

    state_path = reset_signal_path.parent / 'qa_state.json' if reset_signal_path else None

    checked_stale_runs: set = set()
    idle_ticks = 0
    idle_line = False
    _SPINNER = ['-', '\\', '|', '/']

    def _end_idle():
        nonlocal idle_line
        if idle_line:
            sys.stdout.write('\n')
            sys.stdout.flush()
            idle_line = False

    # Per-mode tracking state, keyed by (run_name, subrun_name)
    seen_files:  dict = _load_state(state_path)  # 'all' mode: frozenset of filenames at last QA run
    done_first:  set  = set()  # 'first' mode: subruns already processed
    done_fnums:  dict = {}  # 'per_file' mode: set of completed file_nums

    while True:
        found_new = False

        if reset_signal_path:
            reset = _pop_reset_signal(reset_signal_path)
            if reset is not False:
                if reset is None:
                    seen_files.clear()
                    done_first.clear()
                    done_fnums.clear()
                    checked_stale_runs.clear()
                    _save_state(state_path, seen_files)
                    _end_idle()
                    print("[qa_watcher] Reset: all runs will be reprocessed")
                else:
                    for key in list(seen_files):
                        if key[0] in reset: del seen_files[key]
                    done_first -= {k for k in done_first if k[0] in reset}
                    for key in list(done_fnums):
                        if key[0] in reset: del done_fnums[key]
                    checked_stale_runs -= reset
                    _save_state(state_path, seen_files)
                    _end_idle()
                    print(f"[qa_watcher] Reset: {sorted(reset)} will be reprocessed")

        if not runs_dir.exists():
            pass
        else:
            for run_dir in sorted(runs_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                if include_runs is not None and run_dir.name not in include_runs:
                    continue
                if run_dir.name in exclude_runs:
                    continue
                if run_dir.name in checked_stale_runs:
                    continue

                run_config_path = run_dir / 'run_config.json'
                if not run_config_path.exists():
                    continue

                is_stale = _run_is_stale(run_dir, combined_inner, stale_run_days)

                for subrun_dir in sorted(run_dir.iterdir()):
                    if not subrun_dir.is_dir():
                        continue

                    combined_dir = subrun_dir / combined_inner
                    if not combined_dir.exists():
                        continue

                    stable = _stable_combined_files(combined_dir)
                    if not stable:
                        continue

                    key = (run_dir.name, subrun_dir.name)

                    if mode == 'all':
                        current = frozenset(stable)
                        if current != seen_files.get(key):
                            _end_idle()
                            mem_pct, free_mb = _mem_usage_pct()
                            print(f"[qa_watcher] {run_dir.name}/{subrun_dir.name}"
                                  f"  n_files={len(stable)}  mem={mem_pct:.1f}%  free={free_mb:.0f}MB")
                            _log('QA_LAUNCH', run=run_dir.name, subrun=subrun_dir.name,
                                 n_files=len(stable), mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}')
                            completed_ok = _run_qa_monitored(
                                qa_python, qa_script, subrun_dir, run_config_path,
                                'all', memory_kill_pct=memory_kill_pct,
                                cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                                qa_threads=qa_threads)
                            if completed_ok:
                                seen_files[key] = current
                                _save_state(state_path, seen_files)
                                _log('QA_DONE', run=run_dir.name, subrun=subrun_dir.name)
                            found_new = True

                    elif mode == 'first':
                        if key not in done_first:
                            if any(_file_num(f) == 0 for f in stable):
                                _end_idle()
                                mem_pct, free_mb = _mem_usage_pct()
                                print(f"[qa_watcher] {run_dir.name}/{subrun_dir.name}"
                                      f"  file_num=0  mem={mem_pct:.1f}%  free={free_mb:.0f}MB")
                                _log('QA_LAUNCH', run=run_dir.name, subrun=subrun_dir.name,
                                     file_num=0, mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}')
                                completed_ok = _run_qa_monitored(
                                    qa_python, qa_script, subrun_dir, run_config_path,
                                    'first', memory_kill_pct=memory_kill_pct,
                                    cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                                    qa_threads=qa_threads)
                                if completed_ok:
                                    done_first.add(key)
                                    _log('QA_DONE', run=run_dir.name, subrun=subrun_dir.name)
                                found_new = True

                    elif mode == 'per_file':
                        completed = done_fnums.get(key, set())
                        new_fnums = {_file_num(f) for f in stable} - {None} - completed
                        for fnum in sorted(new_fnums):
                            _end_idle()
                            mem_pct, free_mb = _mem_usage_pct()
                            print(f"[qa_watcher] {run_dir.name}/{subrun_dir.name}"
                                  f"  file_num={fnum:03d}  mem={mem_pct:.1f}%  free={free_mb:.0f}MB")
                            _log('QA_LAUNCH', run=run_dir.name, subrun=subrun_dir.name,
                                 file_num=fnum, mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}')
                            completed_ok = _run_qa_monitored(
                                qa_python, qa_script, subrun_dir, run_config_path,
                                'per_file', file_num=fnum, memory_kill_pct=memory_kill_pct,
                                cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                                qa_threads=qa_threads)
                            if completed_ok:
                                completed.add(fnum)
                                _log('QA_DONE', run=run_dir.name, subrun=subrun_dir.name,
                                     file_num=fnum)
                            found_new = True
                        done_fnums[key] = completed

                if is_stale:
                    checked_stale_runs.add(run_dir.name)
                    _end_idle()
                    print(f"[qa_watcher] Marked stale (will skip): {run_dir.name}")

        if found_new:
            idle_ticks = 0
        else:
            idle_ticks += 1
            elapsed = idle_ticks * poll_interval
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            sp = _SPINNER[idle_ticks % 4]
            if not runs_dir.exists():
                msg = f'[qa_watcher] {sp} waiting for runs_dir  #{idle_ticks}  {ts}'
            else:
                msg = f'[qa_watcher] {sp} idle  #{idle_ticks}  {elapsed}s  {ts}'
            sys.stdout.write(f'\r{msg}          ')
            sys.stdout.flush()
            idle_line = True
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state(state_path: Path) -> dict:
    if state_path is None or not state_path.exists():
        return {}
    try:
        with open(state_path) as f:
            raw = json.load(f)
        return {tuple(k.split('/', 1)): frozenset(v) for k, v in raw.items()}
    except Exception as e:
        print(f"[qa_watcher] Could not load state from {state_path}: {e}")
        return {}


def _save_state(state_path: Path, seen_files: dict):
    if state_path is None:
        return
    try:
        raw = {f"{k[0]}/{k[1]}": sorted(v) for k, v in seen_files.items()}
        with open(state_path, 'w') as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        print(f"[qa_watcher] Could not save state to {state_path}: {e}")


def _pop_reset_signal(signal_path: Path):
    """
    Check for a reset signal file.
    Returns False  — no file present (no reset needed).
    Returns None   — reset all runs.
    Returns set    — reset only the named runs.
    """
    if not signal_path.exists():
        return False
    try:
        with open(signal_path) as f:
            data = json.load(f)
        signal_path.unlink()
        runs = data.get('runs')
        return set(runs) if runs else None
    except Exception as e:
        print(f"[qa_watcher] Error reading reset signal: {e}")
        try:
            signal_path.unlink()
        except OSError:
            pass
        return False


def _read_meminfo() -> tuple:
    """
    Read /proc/meminfo and return (mem_total_kb, mem_available_kb).
    Returns (0, 0) on error.
    """
    total, avail = 0, 0
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    avail = int(line.split()[1])
                if total and avail:
                    break
    except Exception:
        pass
    return total, avail


def _mem_usage_pct() -> tuple:
    """
    Return (used_pct, free_mb).
    used_pct = percentage of total RAM that is in use (0-100).
    Returns (0.0, inf) if /proc/meminfo is unreadable.
    """
    total, avail = _read_meminfo()
    if total == 0:
        return 0.0, float('inf')
    used_pct = (total - avail) / total * 100
    free_mb  = avail / 1024
    return used_pct, free_mb


def _build_qa_command(cmd: list, cpu_nice, cpu_affinity) -> list:
    """
    Wrap the QA command with taskset (CPU affinity) + nice/ionice (priority) so
    the QA never starves the DAQ.  Each wrapper execs the next, so the final PID
    is still the python process (signals from _run_qa_monitored reach it).
    """
    wrapped = list(cmd)
    if cpu_affinity:
        cores   = ','.join(str(int(c)) for c in cpu_affinity)
        wrapped = ['taskset', '-c', cores] + wrapped
    if cpu_nice is not None:
        wrapped = ['nice', '-n', str(int(cpu_nice)), 'ionice', '-c', '3'] + wrapped
    return wrapped


def _thread_limited_env(qa_threads) -> dict:
    """Copy os.environ and cap numpy/BLAS/uproot thread pools if qa_threads is set."""
    env = os.environ.copy()
    if qa_threads:
        for var in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                    'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
            env[var] = str(int(qa_threads))
    return env


def _run_qa_monitored(qa_python, qa_script: Path, subrun_dir: Path,
                       run_config_path: Path, mode: str, file_num: int = None,
                       memory_kill_pct: float = 80, monitor_interval: float = 1.0,
                       cpu_nice=19, cpu_affinity=None, qa_threads=None) -> bool:
    """
    Launch detector_qa.py as a subprocess and monitor system RAM while it runs.

    Polls every monitor_interval seconds (default 1 s).  If system RAM usage
    crosses memory_kill_pct (default 80%), the process is terminated (SIGTERM,
    then SIGKILL after 5 s) and False is returned.
    Returns True if the process completed without being killed.
    A killed run is NOT marked done in the caller's state — it will be retried.

    cpu_nice / cpu_affinity / qa_threads throttle CPU use so the QA yields to the
    DAQ (see _build_qa_command and _thread_limited_env).
    """
    cmd = [str(qa_python), str(qa_script),
           '--subrun_dir', str(subrun_dir),
           '--run_config', str(run_config_path),
           '--mode', mode]
    if file_num is not None:
        cmd += ['--file_num', str(file_num)]

    cmd = _build_qa_command(cmd, cpu_nice, cpu_affinity)
    env = _thread_limited_env(qa_threads)

    run_label = f"{subrun_dir.parent.name}/{subrun_dir.name}"
    proc = subprocess.Popen(cmd, env=env)

    while proc.poll() is None:
        time.sleep(monitor_interval)
        mem_pct, free_mb = _mem_usage_pct()
        if mem_pct >= memory_kill_pct:
            print(f"\n[qa_watcher] Memory {mem_pct:.1f}% >= {memory_kill_pct}%"
                  f" — killing QA process ({run_label})")
            _log('QA_KILLED', run=subrun_dir.parent.name, subrun=subrun_dir.name,
                 mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}', threshold=f'{memory_kill_pct}%')
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            print(f"[qa_watcher] QA process killed ({run_label}) — will retry next poll")
            return False

    return proc.returncode == 0


def _stable_combined_files(combined_dir: Path) -> list:
    """Return sorted filenames of feu-combined ROOT files with size > 0."""
    result = []
    for f in combined_dir.iterdir():
        if f.suffix != '.root' or '_datrun_' not in f.name or 'feu-combined' not in f.name:
            continue
        try:
            if f.stat().st_size > 0:
                result.append(f.name)
        except OSError:
            continue
    return sorted(result)


def _file_num(filename: str):
    m = re.match(r'.*_(\d{3})_feu-combined', filename)
    return int(m.group(1)) if m else None


def _run_is_stale(run_dir: Path, combined_inner: str, stale_days: float) -> bool:
    cutoff = time.time() - stale_days * 86400
    newest = 0.0
    found_any = False
    for subrun in run_dir.iterdir():
        if not subrun.is_dir():
            continue
        d = subrun / combined_inner
        if d.exists():
            found_any = True
            mtime = d.stat().st_mtime
            if mtime > newest:
                newest = mtime
    if not found_any:
        return False  # No combined_hits yet — run is new, not stale
    return newest < cutoff


if __name__ == '__main__':
    main()
