#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous on-the-fly QA watcher for the P2 VMM SPS DAQ.

Watches all runs under a top-level runs directory and runs the pcapng QA script
(vmm_qa/vmm_pcapng_qa.py) on every capture file as soon as it is finalized —
i.e. dumpcap/tcpdump has rotated past it or the sub-run has ended. Runs
independently of daq_control.py; start/stop from the flask UI.

Adapted from Dylan Neff's nTof qa_watcher: same framework (poll loop, persistent
state json, memory kill, nice/affinity throttling, qa_reset signal), but the
trigger is finalized .pcapng files in raw_daq_data/ instead of combined ROOT
files, and each file gets an independent QA output directory under the analysis
tree (per-file mode only — pcapng QA has no cross-file accumulation).

Usage:
    python qa_watcher.py <qa_config_json_path>

Config keys (see qa_config.py to generate the JSON):
  runs_dir            : top-level directory containing run_N/ subdirs
  analysis_dir        : path to the repository holding the QA script
  qa_script_rel_path  : QA entry script, relative to analysis_dir
                        (default: 'vmm_qa/vmm_pcapng_qa.py')
  qa_python_rel_path  : python interpreter, relative to analysis_dir
                        (default: '.venv/bin/python')
  raw_inner_dir       : subdir of each subrun holding capture files (default: 'raw_daq_data')
  qa_out_base         : base directory for QA outputs; results land in
                        <qa_out_base>/<run>/<subrun>/<pcap_basename>/
  capture_duration_s  : dumpcap rotation interval; a capture file with no
                        higher-sequence sibling and no .capture_done marker is
                        considered final once its mtime is older than
                        2 x capture_duration_s (default: 60)
  data_format         : 'SRS' or 'TRG', passed to the QA script (default: 'SRS')
  calibration         : vmm-sdat calibration JSON passed to the QA script (null = none)
  max_packets         : optional --max-packets guard for the QA script (null = no cap)
  include_runs        : list of run directory names to process exclusively (null = all)
  exclude_runs        : list of run directory names to skip (null = none)
  poll_interval       : seconds between scans   (default: 10)
  stale_run_days      : runs with no new capture files for this many days are skipped (default: 4)
  memory_kill_pct     : kill the QA process if system RAM usage exceeds this % (default: 80)
                          The QA is always launched; memory is monitored during the run and
                          the process is terminated if the system crosses the threshold.
                          A killed file is NOT marked done and will be retried next poll.
  cpu_nice            : nice level for the QA subprocess (default: 19, lowest priority).
                          Also runs the process at ionice idle class so DAQ I/O wins.
                          null disables both niceing and ionice.
  cpu_affinity        : list of CPU core ids to pin the QA subprocess to via taskset
                          (default: null = all cores).  e.g. [2, 3, 4, 5] reserves cores
                          0-1 for the DAQ on a 6-core box.
  qa_threads          : cap numpy/BLAS thread pools to this many threads
                          (default: null = derived from len(cpu_affinity), else unlimited).
"""

import os
import sys
import json
import time
import datetime
import subprocess
from pathlib import Path

from common_functions import parse_pcapng_name

_LOG_FILE = Path(__file__).parent / 'logs' / 'qa_watcher.log'

CAPTURE_DONE_MARKER = '.capture_done'  # written by vmm_daq_control.py at sub-run end


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
    runs_dir     = Path(config['runs_dir'])
    analysis_dir = Path(config['analysis_dir'])
    raw_inner    = config.get('raw_inner_dir', 'raw_daq_data')
    qa_out_base  = Path(config['qa_out_base'])

    capture_duration_s = config.get('capture_duration_s', 60)
    data_format        = config.get('data_format', 'SRS')
    calibration        = config.get('calibration')
    max_packets        = config.get('max_packets')

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

    qa_script = analysis_dir / config.get('qa_script_rel_path', 'vmm_qa/vmm_pcapng_qa.py')
    qa_python = analysis_dir / config.get('qa_python_rel_path', '.venv/bin/python')

    print(f"[qa_watcher] runs_dir        : {runs_dir}")
    print(f"[qa_watcher] qa_script       : {qa_script}")
    print(f"[qa_watcher] python          : {qa_python}")
    print(f"[qa_watcher] qa_out_base     : {qa_out_base}")
    print(f"[qa_watcher] data_format     : {data_format}")
    print(f"[qa_watcher] calibration     : {calibration if calibration else 'none'}")
    print(f"[qa_watcher] finalize_after  : {2 * capture_duration_s}s (2 x capture_duration_s)")
    if include_runs:
        print(f"[qa_watcher] include_runs    : {sorted(include_runs)}")
    if exclude_runs:
        print(f"[qa_watcher] exclude_runs    : {sorted(exclude_runs)}")
    print(f"[qa_watcher] poll            : {poll_interval}s  stale_after={stale_run_days}d")
    print(f"[qa_watcher] memory_kill_pct : {memory_kill_pct}%")
    print(f"[qa_watcher] cpu_nice        : {cpu_nice}")
    print(f"[qa_watcher] cpu_affinity    : {cpu_affinity if cpu_affinity else 'all cores'}")
    print(f"[qa_watcher] qa_threads      : {qa_threads if qa_threads else 'unlimited'}")
    _log('START', runs_dir=runs_dir, memory_kill_pct=f'{memory_kill_pct}%',
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

    # (run_name, subrun_name) -> set of processed pcapng basenames (persisted)
    done_files: dict = _load_state(state_path)
    # pcap path -> size at last poll; a file must hold its size for one full
    # poll on top of the finalize conditions before QA is launched.
    last_sizes: dict = {}

    while True:
        found_new = False

        if reset_signal_path:
            reset = _pop_reset_signal(reset_signal_path)
            if reset is not False:
                if reset is None:
                    done_files.clear()
                    checked_stale_runs.clear()
                    _save_state(state_path, done_files)
                    _end_idle()
                    print("[qa_watcher] Reset: all runs will be reprocessed")
                else:
                    for key in list(done_files):
                        if key[0] in reset: del done_files[key]
                    checked_stale_runs -= reset
                    _save_state(state_path, done_files)
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

                is_stale = _run_is_stale(run_dir, raw_inner, stale_run_days)

                for subrun_dir in sorted(run_dir.iterdir()):
                    if not subrun_dir.is_dir():
                        continue

                    raw_dir = subrun_dir / raw_inner
                    if not raw_dir.exists():
                        continue

                    key = (run_dir.name, subrun_dir.name)
                    done = done_files.setdefault(key, set())

                    for pcap in _finalized_pcapngs(raw_dir, capture_duration_s, last_sizes):
                        if pcap.name in done:
                            continue
                        _end_idle()
                        mem_pct, free_mb = _mem_usage_pct()
                        size_mb = pcap.stat().st_size / 1024 ** 2
                        print(f"[qa_watcher] {run_dir.name}/{subrun_dir.name}/{pcap.name}"
                              f"  size={size_mb:.0f}MB  mem={mem_pct:.1f}%  free={free_mb:.0f}MB")
                        _log('QA_LAUNCH', run=run_dir.name, subrun=subrun_dir.name,
                             file=pcap.name, size_mb=f'{size_mb:.0f}',
                             mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}')
                        out_dir = qa_out_base / run_dir.name / subrun_dir.name / pcap.stem
                        completed_ok = _run_qa_monitored(
                            qa_python, qa_script, pcap, out_dir,
                            data_format=data_format, calibration=calibration,
                            max_packets=max_packets, memory_kill_pct=memory_kill_pct,
                            cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                            qa_threads=qa_threads)
                        if completed_ok:
                            done.add(pcap.name)
                            _save_state(state_path, done_files)
                            _log('QA_DONE', run=run_dir.name, subrun=subrun_dir.name,
                                 file=pcap.name)
                        found_new = True

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
# Finalized-capture detection
# ---------------------------------------------------------------------------

def _finalized_pcapngs(raw_dir: Path, capture_duration_s: float,
                       last_sizes: dict) -> list:
    """
    Return capture files in raw_dir that are safe to analyze, sorted by
    (iface, seq).  A file is final iff its size > 0 AND at least one of:
      - a higher-sequence file for the same iface exists (capture rotated past it),
      - the sub-run has ended (.capture_done marker present),
      - its mtime is older than 2 x capture_duration_s (rotation overdue —
        covers a capture that died without a marker),
    AND its size has not changed since the previous poll (guards the race where
    dumpcap has just opened the next file but is still flushing this one).
    """
    entries = []  # (iface, seq, Path)
    for f in raw_dir.iterdir():
        parsed = parse_pcapng_name(f.name)
        if parsed is None:
            continue
        iface, seq, _ts = parsed
        entries.append((iface, seq, f))

    if not entries:
        return []

    max_seq = {}
    for iface, seq, _f in entries:
        max_seq[iface] = max(max_seq.get(iface, -1), seq)

    capture_done = (raw_dir / CAPTURE_DONE_MARKER).exists()
    now = time.time()

    final = []
    for iface, seq, f in sorted(entries, key=lambda e: (e[0], e[1])):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        rotated  = seq < max_seq[iface]
        overdue  = (now - st.st_mtime) > 2 * capture_duration_s
        if not (rotated or capture_done or overdue):
            continue
        # Size must be stable across one full poll interval.
        prev = last_sizes.get(str(f))
        last_sizes[str(f)] = st.st_size
        if prev is not None and prev == st.st_size:
            final.append(f)
    return final


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state(state_path: Path) -> dict:
    if state_path is None or not state_path.exists():
        return {}
    try:
        with open(state_path) as f:
            raw = json.load(f)
        return {tuple(k.split('/', 1)): set(v) for k, v in raw.items()}
    except Exception as e:
        print(f"[qa_watcher] Could not load state from {state_path}: {e}")
        return {}


def _save_state(state_path: Path, done_files: dict):
    if state_path is None:
        return
    try:
        raw = {f"{k[0]}/{k[1]}": sorted(v) for k, v in done_files.items()}
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
    """Copy os.environ and cap numpy/BLAS thread pools if qa_threads is set."""
    env = os.environ.copy()
    if qa_threads:
        for var in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                    'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
            env[var] = str(int(qa_threads))
    return env


def _run_qa_monitored(qa_python, qa_script: Path, pcap: Path, out_dir: Path,
                      data_format: str = 'SRS', calibration=None, max_packets=None,
                      memory_kill_pct: float = 80, monitor_interval: float = 1.0,
                      cpu_nice=19, cpu_affinity=None, qa_threads=None) -> bool:
    """
    Launch vmm_pcapng_qa.py on one capture file and monitor system RAM while it
    runs.

    Polls every monitor_interval seconds (default 1 s).  If system RAM usage
    crosses memory_kill_pct (default 80%), the process is terminated (SIGTERM,
    then SIGKILL after 5 s) and False is returned.
    Returns True if the process completed without being killed.
    A killed file is NOT marked done in the caller's state — it will be retried.

    cpu_nice / cpu_affinity / qa_threads throttle CPU use so the QA yields to the
    DAQ (see _build_qa_command and _thread_limited_env).
    """
    cmd = [str(qa_python), str(qa_script), str(pcap),
           '--out-dir', str(out_dir),
           '--events-json',
           '--format', data_format]
    if calibration:
        cmd += ['--calibration', str(calibration)]
    if max_packets:
        cmd += ['--max-packets', str(int(max_packets))]

    cmd = _build_qa_command(cmd, cpu_nice, cpu_affinity)
    env = _thread_limited_env(qa_threads)

    run_label = f"{pcap.parent.parent.parent.name}/{pcap.parent.parent.name}/{pcap.name}"
    proc = subprocess.Popen(cmd, env=env)

    while proc.poll() is None:
        time.sleep(monitor_interval)
        mem_pct, free_mb = _mem_usage_pct()
        if mem_pct >= memory_kill_pct:
            print(f"\n[qa_watcher] Memory {mem_pct:.1f}% >= {memory_kill_pct}%"
                  f" — killing QA process ({run_label})")
            _log('QA_KILLED', file=pcap.name,
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


def _run_is_stale(run_dir: Path, raw_inner: str, stale_days: float) -> bool:
    cutoff = time.time() - stale_days * 86400
    newest = 0.0
    found_any = False
    for subrun in run_dir.iterdir():
        if not subrun.is_dir():
            continue
        d = subrun / raw_inner
        if d.exists():
            found_any = True
            mtime = d.stat().st_mtime
            if mtime > newest:
                newest = mtime
    if not found_any:
        return False  # No capture files yet — run is new, not stale
    return newest < cutoff


if __name__ == '__main__':
    main()
