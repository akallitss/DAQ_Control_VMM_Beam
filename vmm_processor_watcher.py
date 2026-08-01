#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous decode watcher for the P2 VMM beam data.

Watches every run under runs_dir and turns each finalized pcapng into a column
store plus its reduction:

    <subrun>/raw_daq_data/<capture>.pcapng
        -> <subrun>/hits_store/<capture>/        columns + counts.npz + scalars.json

Mirrors the DREAM processor_watcher on the banco machine, minus the stages VMM
does not have (no waveform analysis -- the VMM ships no 16-sample waveforms --
and no FEU combining, since there is one FEC).

Split of responsibility with qa_watcher:

    processor_watcher   reads pcapng, writes hits_store        (this file)
    qa_watcher          reads hits_store, writes plots         (never sees pcapng)

The handoff is an atomic rename: vmm_reduce builds each store under
"<name>.partial" and renames it into place only when everything succeeded. A
directory appearing under hits_store/ is therefore complete by construction, so
the two watchers never race over a half-written store and qa_watcher needs no
size-stability heuristics of its own.

Files are taken newest-first: when the machine is behind, the freshest capture
is the one worth looking at; the backlog is backfill.

Usage:
    python vmm_processor_watcher.py <processor_config_json_path>

Config keys (see vmm_processor_config.py to generate the JSON):
  runs_dir            : top-level directory containing run_N/ subdirs
  raw_inner_dir       : subdir holding captures            (default: 'raw_daq_data')
  store_inner_dir     : subdir for the column stores       (default: 'hits_store')
  capture_duration_s  : dumpcap rotation interval; a capture with no higher-seq
                        sibling and no .capture_done marker finalizes after 2x this
  data_format         : 'SRS' or 'TRG'
  calibration         : vmm-sdat calibration JSON path, or null
  do_efficiency       : fold trigger-referenced efficiency into scalars.json
  eff_window          : coincidence window in ns (matches vmm_pcapng_qa default)
  keep_columns        : keep the per-hit .npy columns. False keeps only
                        counts.npz + scalars.json -- the columns are
                        regenerable from the pcapng in ~0.5 s
  save_pcapng         : keep the raw capture after decoding. DEFAULT TRUE and
                        it should stay true: the pcapng is primary data, the
                        store is derived
  include_runs / exclude_runs / poll_interval / stale_run_days
  memory_kill_pct / max_attempts / cpu_nice / cpu_affinity / threads
  stall_timeout_s     : kill a decode whose store stops growing (default 300)
  hard_timeout_s      : absolute cap on one decode (default 3600)
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common_functions import parse_pcapng_name
import watcher_common as wc

CAPTURE_DONE_MARKER = '.capture_done'   # written by vmm_daq_control at sub-run end
_SPINNER = '|/-\\'


def main():
    if len(sys.argv) != 2:
        print("Usage: python vmm_processor_watcher.py <processor_config_json_path>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        config = json.load(f)
    run_watcher(config)


def run_watcher(config: dict):
    runs_dir = Path(config['runs_dir'])
    raw_inner = config.get('raw_inner_dir', 'raw_daq_data')
    store_inner = config.get('store_inner_dir', 'hits_store')
    capture_duration_s = config.get('capture_duration_s', 60)
    data_format = config.get('data_format', 'SRS')
    calibration = config.get('calibration')
    do_efficiency = config.get('do_efficiency', True)
    eff_window = config.get('eff_window', 1000.0)
    keep_columns = config.get('keep_columns', True)
    save_pcapng = config.get('save_pcapng', True)

    include_runs = set(config.get('include_runs') or [])
    exclude_runs = set(config.get('exclude_runs') or [])
    poll_interval = config.get('poll_interval', 10)
    stale_run_days = config.get('stale_run_days', 1)

    memory_kill_pct = config.get('memory_kill_pct', 80)
    max_attempts = config.get('max_attempts', 3)
    cpu_nice = config.get('cpu_nice', 19)
    cpu_affinity = config.get('cpu_affinity')
    threads = config.get('threads', 4)
    stall_timeout_s = config.get('stall_timeout_s', 300)
    hard_timeout_s = config.get('hard_timeout_s', 3600)

    here = Path(__file__).parent
    python_exe = config.get('python', str(here / '.venv' / 'bin' / 'python'))
    worker = Path(config.get('worker', str(here / 'vmm_qa' / 'vmm_reduce.py')))
    state_path = Path(config.get('state_file', here / 'config' / 'processor_state.json'))
    reset_signal = Path(config.get('reset_signal', here / 'config' / 'processor_reset.json'))

    log = wc.Logger(here / 'logs' / 'processor_watcher.log', 'processor')

    print(f"[processor] runs_dir        : {runs_dir}")
    print(f"[processor] store_inner_dir : {store_inner}")
    print(f"[processor] worker          : {worker}")
    print(f"[processor] save_pcapng     : {save_pcapng}   keep_columns: {keep_columns}")
    print(f"[processor] memory_kill_pct : {memory_kill_pct}%   max_attempts: {max_attempts}")
    print(f"[processor] stall/hard      : {stall_timeout_s}s / {hard_timeout_s}s")
    log('START', runs_dir=str(runs_dir), save_pcapng=save_pcapng,
        memory_kill_pct=f'{memory_kill_pct}%', cpu_nice=cpu_nice)

    done_files, fail_counts = wc.load_state(state_path)
    last_sizes = {}
    checked_stale = set()
    # Files memory-killed once, to be retried without the efficiency stage.
    # Retrying a memory kill with identical parameters cannot succeed — the file
    # is the same size and the box is the same size — so a plain retry just
    # re-does the decode and throws it away. The efficiency stage is what does
    # not fit (it builds a resident hits table); dropping it keeps the decode,
    # counts and scalars, which are the products the trend dashboard reads.
    # Deliberately not persisted: after a restart one full attempt is cheap, and
    # the box may genuinely have more headroom than it did last time.
    mem_degraded = set()
    idle_ticks = 0
    idle_line = False

    def _end_idle():
        nonlocal idle_line
        if idle_line:
            sys.stdout.write('\n')
            idle_line = False

    def _scan_once() -> bool:
        """Process at most one capture. True if something was done."""
        nonlocal done_files, fail_counts

        reset = wc.pop_reset_signal(reset_signal)
        if reset is not False:
            if reset is None:
                done_files, fail_counts = {}, {}
                checked_stale.clear()
                mem_degraded.clear()
                print("\n[processor] reset: all runs re-queued")
            else:
                for key in list(done_files):
                    if key[0] in reset:
                        done_files.pop(key, None)
                        fail_counts.pop(key, None)
                checked_stale.difference_update(reset)
                # a reset asks for a clean full attempt, efficiency included
                mem_degraded.difference_update(
                    {k for k in mem_degraded if k[0] in reset})
                print(f"\n[processor] reset: {sorted(reset)}")
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
            if wc.run_is_stale(run_dir, raw_inner, stale_run_days):
                checked_stale.add(run_dir.name)
                _end_idle()
                print(f"[processor] stale, skipping: {run_dir.name}")
                continue

            subruns = [d for d in run_dir.iterdir() if d.is_dir()]
            for subrun in wc.newest_first(subruns):
                raw_dir = subrun / raw_inner
                if not raw_dir.is_dir():
                    continue
                key = (run_dir.name, subrun.name)
                done = done_files.setdefault(key, set())
                fails = fail_counts.setdefault(key, {})
                store_root = subrun / store_inner

                for pcap in _finalized_pcapngs(raw_dir, capture_duration_s, last_sizes):
                    store_dir = store_root / pcap.stem
                    if (store_dir / 'scalars.json').exists():
                        done.add(pcap.name)          # output-derived idempotency
                        continue
                    if pcap.name in done or fails.get(pcap.name, 0) >= max_attempts:
                        continue

                    _end_idle()
                    mem_pct, free_mb = wc.mem_usage_pct()
                    size_mb = pcap.stat().st_size / 1e6
                    print(f"[processor] {run_dir.name}/{subrun.name}  {pcap.name}  "
                          f"({size_mb:.0f} MB, mem {mem_pct:.0f}%)")
                    log('DECODE_LAUNCH', run=run_dir.name, subrun=subrun.name,
                        file=pcap.name, size_mb=f'{size_mb:.0f}',
                        mem_pct=f'{mem_pct:.1f}%', free_mb=f'{free_mb:.0f}')

                    store_root.mkdir(parents=True, exist_ok=True)
                    cmd = [python_exe, str(worker), str(pcap),
                           '--store-dir', str(store_dir),
                           '--format', data_format,
                           '--eff-window', str(eff_window)]
                    mem_key = (run_dir.name, subrun.name, pcap.name)
                    degraded = mem_key in mem_degraded
                    if not do_efficiency or degraded:
                        cmd.append('--no-efficiency')
                    if not keep_columns:
                        cmd.append('--drop-columns')
                    if calibration:
                        cmd += ['--calibration', str(calibration)]

                    t0 = time.time()
                    ok, reason = wc.run_monitored(
                        cmd, label=pcap.name,
                        memory_kill_pct=memory_kill_pct,
                        cpu_nice=cpu_nice, cpu_affinity=cpu_affinity,
                        threads=threads,
                        stall_timeout_s=stall_timeout_s,
                        hard_timeout_s=hard_timeout_s,
                        watch_path=str(store_dir) + '.partial',
                        log=log, component='processor')
                    dt = time.time() - t0

                    if ok:
                        done.add(pcap.name)
                        fails.pop(pcap.name, None)
                        n_hits = _store_hits(store_dir)
                        print(f"[processor]   done in {dt:.1f}s  ({n_hits:,} hits)")
                        log('DECODE_DONE', run=run_dir.name, subrun=subrun.name,
                            file=pcap.name, wall_s=f'{dt:.1f}', n_hits=n_hits)
                        if not save_pcapng:
                            try:
                                pcap.unlink()
                                log('PCAPNG_REMOVED', file=pcap.name)
                            except OSError as e:
                                log('PCAPNG_REMOVE_FAILED', file=pcap.name, error=str(e))
                    else:
                        # a partial store must never be mistaken for a real one
                        shutil.rmtree(str(store_dir) + '.partial', ignore_errors=True)

                        # First memory kill costs no attempt: the next pass runs
                        # the same file without the efficiency stage, which is a
                        # genuinely different (and much smaller) job. A second
                        # memory kill means even the decode does not fit, and
                        # falls through to the normal attempt counting below.
                        if reason == 'memory' and not degraded:
                            mem_degraded.add(mem_key)
                            print(f"[processor]   memory-killed after {dt:.1f}s "
                                  f"— re-queued without the efficiency stage")
                            log('DECODE_DEGRADE', run=run_dir.name, subrun=subrun.name,
                                file=pcap.name, retry_as='--no-efficiency')
                            wc.save_state(state_path, done_files, fail_counts)
                            return True

                        n = fails.get(pcap.name, 0) + 1
                        fails[pcap.name] = n
                        print(f"[processor]   FAILED ({reason}) after {dt:.1f}s "
                              f"— attempt {n}/{max_attempts}")
                        log('DECODE_FAILED', run=run_dir.name, subrun=subrun.name,
                            file=pcap.name, reason=reason, attempts=n)
                        if n >= max_attempts:
                            print(f"[processor]   giving up on {pcap.name} "
                                  f"(processor_reset to retry)")
                            log('DECODE_GIVEUP', file=pcap.name, attempts=n)

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
            print(f"[processor] scan error: {type(e).__name__}: {e}")
            log('SCAN_ERROR', error=f'{type(e).__name__}: {e}')

        idle_ticks += 1
        ts = time.strftime('%H:%M:%S')
        sp = _SPINNER[idle_ticks % 4]
        sys.stdout.write(f"\r[processor] {sp} idle  #{idle_ticks}  "
                         f"{idle_ticks * poll_interval}s  {ts}          ")
        sys.stdout.flush()
        idle_line = True
        time.sleep(poll_interval)


def _store_hits(store_dir: Path) -> int:
    try:
        with open(store_dir / 'scalars.json') as f:
            return int(json.load(f).get('n_hits', 0))
    except Exception:
        return 0


def _finalized_pcapngs(raw_dir: Path, capture_duration_s: float,
                       last_sizes: dict) -> list:
    """Captures safe to decode, newest first.

    Ported from qa_watcher, which is where this was worked out: a file is final
    iff size > 0 AND (a higher-seq file for the same iface exists, OR the
    sub-run ended, OR rotation is overdue), AND its size did not change since
    the previous poll -- which guards the race where dumpcap has opened the next
    file but is still flushing this one.

    Returned newest-first so the freshest capture is decoded first.
    """
    entries = []
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
    for iface, seq, f in sorted(entries, key=lambda e: (e[0], e[1]), reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        rotated = seq < max_seq[iface]
        overdue = (now - st.st_mtime) > 2 * capture_duration_s
        if not (rotated or capture_done or overdue):
            continue
        prev = last_sizes.get(str(f))
        last_sizes[str(f)] = st.st_size
        if prev is not None and prev == st.st_size:
            final.append(f)
    return final


if __name__ == '__main__':
    main()
