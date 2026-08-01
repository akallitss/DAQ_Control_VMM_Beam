#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot QA backfill: give every sub-run its monitoring plots, one job at a time.

Why this exists rather than "just let the watchers catch up":

The two watchers are built for the online case, where the question is "is the
data that is arriving right now healthy". Between them they decode EVERY capture
(48 per run, ~35 s each) before the plotting side draws anything, and on this
7 GB box running both at once is what has been driving MemAvailable into the
floor. Offline, the question is different -- "what did run N look like" -- and
plot_policy='subrun' already answers it from the FIRST store of each sub-run.
One store per sub-run is therefore the whole requirement: a few dozen sub-runs
of backlog instead of ~1500 captures, and the other 47 captures per run stay on
disk for whenever the full decode is actually wanted.

The memory discipline is structural, not advisory: exactly one child process is
alive at any moment, and it is spawned through the same wc.run_monitored used by
the watchers, so it inherits the memory kill, the nice level, the thread cap and
oom_score_adj=500. Peak footprint is one decode or one plot job -- never both,
and never two of either.

Idempotent by output, like the watchers: a sub-run whose analysis directory
already holds an events.json is skipped, so this can be interrupted at any time
(Ctrl-C between jobs is clean) and re-run to pick up where it stopped. It writes
no state file of its own.

Ordering is cheapest-first. Pass 1 is every sub-run whose store already exists
and only needs drawing (~45 s each); pass 2 is the sub-runs that need a decode
first (~35 s + ~45 s). That way the bulk of the backlog is on disk early, which
matters when the window is a beam stop of unknown length.

Degrading, when a sub-run will not fit the box at all (the gain4.5 runs, where a
capture is ~800 MB and a store is 30M hits):

  * the store with the FEWEST hits is chosen, not the first by name -- every
    capture in a sub-run shares one configuration, so any of them answers the
    monitoring question, but they do not cost the same to draw
  * a plot that is still memory-killed falls back to reading the pcapng under a
    descending packet cap, probing down until it fits

The cap is probed rather than predicted because the ceiling is not a function of
hit count: a 24.2M-hit store drew fine while a 17.7M-hit one was killed. The
resulting plots hold fewer entries per histogram, so they are good for spotting
problems but NOT comparable with full plots for absolute rates -- the log line
and the QA_DONE_CAPPED event both record the cap that was used.

Usage:
    python qa_catchup.py                     # runs 33+, both passes, live progress
    python qa_catchup.py --dry-run           # list the work, touch nothing
    python qa_catchup.py --min-run 33 --max-run 52
    python qa_catchup.py --plots-only        # pass 1 only: no decoding at all
    python qa_catchup.py --threads 4         # beam is off, take the cores
    python qa_catchup.py --trend             # also refresh per-run trend dashboards

Safety: refuses to start while vmm_processor_watcher.py or qa_watcher.py is
running, since the entire point is that those two are not competing for the
memory. Stop them first, or pass --force if you know better.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common_functions import parse_pcapng_name
import watcher_common as wc

HERE = Path(__file__).parent
# A sub-run needs at least this many finalized captures before the middle one is
# a fair representative; below it, the sub-run is a stub (an aborted config
# point, or a run that died at start) and the single capture is used as-is.
_STUB_CAPTURES = 3
# A pcapng carrying no packets at all is 272 bytes: the section header block and
# one interface description block, which dumpcap writes when it opens the file.
# Three sub-runs on this campaign (run_33/driftscan_gap300V, run_37, and
# run_55/meshscan_m30V) consist entirely of these -- the readout was off, or the
# trigger never arrived, and no data exists to plot. They are reported as
# no-data rather than queued, because feeding one to the decoder produces an
# empty store and a set of blank PNGs that then look like real, checked output.
_EMPTY_PCAPNG_BYTES = 4096
CAPTURE_DONE_MARKER = '.capture_done'   # written by vmm_daq_control at sub-run end
# A capture untouched for this long is not the one dumpcap has open, whatever the
# sequence numbers say. Generous against capture_duration_s (44.4 s) because
# guessing wrong here means reading a growing file.
_OVERDUE_S = 600


def main():
    ap = argparse.ArgumentParser(
        description="Backfill QA monitoring plots one sub-run at a time.")
    ap.add_argument('--config', default=str(HERE / 'config' / 'qa_config.json'),
                    help='qa_config.json -- supplies runs_dir, qa_out_base, format')
    ap.add_argument('--processor-config',
                    default=str(HERE / 'config' / 'processor_config.json'),
                    help='processor_config.json -- supplies the decode settings')
    ap.add_argument('--min-run', type=int, default=33,
                    help='lowest run number to consider (default 33)')
    ap.add_argument('--max-run', type=int, default=None)
    ap.add_argument('--only-run', action='append', default=None,
                    help='restrict to these runs, e.g. --only-run run_44 (repeatable)')
    ap.add_argument('--plots-only', action='store_true',
                    help='pass 1 only: never decode, only draw sub-runs that have a store')
    ap.add_argument('--decode-only', action='store_true',
                    help='pass 2 only: decode the missing representatives, draw nothing')
    ap.add_argument('--max-decode-mb', type=float, default=400.0,
                    help='prefer a representative capture under this size (default 400)')
    ap.add_argument('--max-packets', type=int, default=22000,
                    help='packet cap when a sub-run has NO capture under '
                         '--max-decode-mb and must be plotted straight from the '
                         'pcapng. 22000 packets is about 200 MB, the size that '
                         'decodes comfortably on this box today')
    ap.add_argument('--threads', type=int, default=2,
                    help='thread cap per job. 2 while the DAQ runs, 4 during a stop')
    ap.add_argument('--nice', type=int, default=19)
    ap.add_argument('--memory-kill-pct', type=float, default=None,
                    help='override the qa_config memory ceiling')
    ap.add_argument('--min-free-mb', type=float, default=1200.0,
                    help='wait before each job until this much memory is free')
    ap.add_argument('--trend', action='store_true',
                    help='also refresh the per-run trend dashboard after each run')
    ap.add_argument('--no-efficiency', action='store_true',
                    help='skip the efficiency stage when decoding (much less memory)')
    ap.add_argument('--limit', type=int, default=None,
                    help='stop after N jobs -- useful to time one before committing')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true',
                    help='run even if a watcher is alive')
    args = ap.parse_args()

    with open(args.config) as f:
        qa_cfg = json.load(f)
    with open(args.processor_config) as f:
        pr_cfg = json.load(f)

    if not args.dry_run and not args.force:
        alive = _watchers_alive()
        if alive:
            print("[catchup] REFUSING to start -- these are still running:")
            for pid, name in alive:
                print(f"            pid {pid}  {name}")
            print("[catchup] stop them first (that is the whole point), or pass --force")
            return 2

    runs_dir = Path(qa_cfg['runs_dir'])
    qa_out_base = Path(qa_cfg.get('qa_out_base', str(runs_dir.parent / 'analysis')))
    store_inner = qa_cfg.get('store_inner_dir', 'hits_store')
    raw_inner = pr_cfg.get('raw_inner_dir', 'raw_daq_data')
    data_format = qa_cfg.get('data_format', 'SRS')
    calibration = qa_cfg.get('calibration')
    mem_kill = args.memory_kill_pct or qa_cfg.get('memory_kill_pct', 80)

    python_exe = qa_cfg.get('qa_python', str(HERE / '.venv' / 'bin' / 'python'))
    qa_script = Path(qa_cfg.get('qa_script', str(HERE / 'vmm_qa' / 'vmm_pcapng_qa.py')))
    trend_script = Path(qa_cfg.get('trend_script', str(HERE / 'vmm_qa' / 'vmm_trend.py')))
    reduce_script = Path(pr_cfg.get('worker', str(HERE / 'vmm_qa' / 'vmm_reduce.py')))
    eff_window = pr_cfg.get('eff_window', 1000.0)

    log = wc.Logger(HERE / 'logs' / 'qa_catchup.log', 'qa_catchup')

    only = set(args.only_run or [])
    ready, needs_decode, skipped = _survey(
        runs_dir, qa_out_base, store_inner, raw_inner,
        args.min_run, args.max_run, only, args.max_decode_mb)

    if args.plots_only:
        needs_decode = []
    if args.decode_only:
        ready = []

    print(f"[catchup] runs_dir     : {runs_dir}")
    print(f"[catchup] qa_out_base  : {qa_out_base}")
    print(f"[catchup] threads {args.threads}  nice {args.nice}  "
          f"mem-kill {mem_kill}%  min-free {args.min_free_mb:.0f} MB")
    print()
    print(f"[catchup] pass 1 -- store ready, plot only : {len(ready)} sub-runs")
    for r in ready:
        print(f"            {r['run']:8} {r['subrun']:35} {r['store'].name}")
    print(f"[catchup] pass 2 -- decode one, then plot  : {len(needs_decode)} sub-runs")
    for r in needs_decode:
        print(f"            {r['run']:8} {r['subrun']:35} "
              f"{r['pcap'].name}  {r['mb']:.0f} MB")
    if skipped:
        print(f"[catchup] skipped ({len(skipped)}):")
        for run, sub, why in skipped:
            print(f"            {run:8} {sub:35} {why}")

    jobs = ready + needs_decode
    if args.limit:
        jobs = jobs[:args.limit]
    # ~50 s to draw a store that exists, ~85 s when a decode has to run first
    est = sum(50 if j.get('store') is not None else 85 for j in jobs)
    print()
    print(f"[catchup] {len(jobs)} jobs queued, rough estimate {est / 60:.0f} min "
          f"(one process at a time)")
    if args.dry_run:
        print("[catchup] dry run -- nothing executed")
        return 0
    if not jobs:
        print("[catchup] nothing to do")
        return 0

    log('START', jobs=len(jobs), plot_only=len(ready), with_decode=len(needs_decode),
        threads=args.threads, mem_kill=f'{mem_kill}%')

    n_ok = n_fail = 0
    touched_runs = []
    t_all = time.time()
    for i, job in enumerate(jobs, 1):
        run, sub = job['run'], job['subrun']
        _wait_for_memory(args.min_free_mb)
        mem_pct, free_mb = wc.mem_usage_pct()
        print(f"\n[catchup] [{i}/{len(jobs)}] {run}/{sub}   "
              f"(mem {mem_pct:.0f}%, {free_mb:.0f} MB free)")

        store = job.get('store')
        if store is None and job.get('oversized'):
            # Every capture in this sub-run is too big to decode. Plot straight
            # from the pcapng with a packet cap: no store is produced, but the
            # PNGs are the thing that was missing, and the memory stays bounded.
            pcap = job['pcap']
            out_dir = qa_out_base / run / sub / pcap.stem
            print(f"[catchup]   {pcap.name} is {job['mb']:.0f} MB and every capture "
                  f"here is oversized")
            print(f"[catchup]   plotting direct from pcapng -> {out_dir}")
            ok = _plot_capped(pcap, out_dir, args.max_packets, python_exe, qa_script,
                              data_format, calibration, mem_kill, args, pr_cfg,
                              log, run, sub)
            if ok:
                n_ok += 1
                touched_runs.append((run, job['run_dir']))
            else:
                n_fail += 1
            continue

        if store is None:
            pcap = job['pcap']
            store = job['store_dir']
            print(f"[catchup]   decode {pcap.name} ({job['mb']:.0f} MB) -> {store.name}")
            cmd = [python_exe, str(reduce_script), str(pcap),
                   '--store-dir', str(store),
                   '--format', data_format,
                   '--eff-window', str(eff_window)]
            if args.no_efficiency:
                cmd.append('--no-efficiency')
            if calibration:
                cmd += ['--calibration', str(calibration)]
            store.parent.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            ok, reason = wc.run_monitored(
                cmd, label=pcap.name, memory_kill_pct=mem_kill,
                cpu_nice=args.nice, threads=args.threads,
                stall_timeout_s=pr_cfg.get('stall_timeout_s', 300),
                hard_timeout_s=pr_cfg.get('hard_timeout_s', 3600),
                watch_path=str(store) + '.partial',
                log=log, component='qa_catchup')
            dt = time.time() - t0
            if not ok:
                # never leave a half-written store where the watchers will
                # mistake it for a real one
                _rmtree(str(store) + '.partial')
                # a memory kill on the efficiency stage is worth one cheaper
                # retry: the decode itself is what the plots actually need
                if reason == 'memory' and not args.no_efficiency:
                    print(f"[catchup]   memory-killed after {dt:.0f}s "
                          f"-- retrying without the efficiency stage")
                    log('DECODE_DEGRADE', run=run, subrun=sub, file=pcap.name)
                    _wait_for_memory(args.min_free_mb)
                    t0 = time.time()
                    ok, reason = wc.run_monitored(
                        cmd + ['--no-efficiency'], label=pcap.name,
                        memory_kill_pct=mem_kill, cpu_nice=args.nice,
                        threads=args.threads,
                        stall_timeout_s=pr_cfg.get('stall_timeout_s', 300),
                        hard_timeout_s=pr_cfg.get('hard_timeout_s', 3600),
                        watch_path=str(store) + '.partial',
                        log=log, component='qa_catchup')
                    dt = time.time() - t0
                    if not ok:
                        _rmtree(str(store) + '.partial')
            if not ok:
                print(f"[catchup]   DECODE FAILED ({reason}) after {dt:.0f}s")
                log('DECODE_FAILED', run=run, subrun=sub, file=pcap.name, reason=reason)
                n_fail += 1
                continue
            print(f"[catchup]   decoded in {dt:.0f}s")
            log('DECODE_DONE', run=run, subrun=sub, file=pcap.name, wall_s=f'{dt:.0f}')
            if args.decode_only:
                n_ok += 1
                touched_runs.append((run, job['run_dir']))
                continue

        out_dir = qa_out_base / run / sub / store.name
        print(f"[catchup]   plot -> {out_dir}")
        cmd = [python_exe, str(qa_script), str(store),
               '--out-dir', str(out_dir),
               '--events-json', '--format', data_format]
        if calibration:
            cmd += ['--calibration', str(calibration)]
        _wait_for_memory(args.min_free_mb)
        t0 = time.time()
        ok, reason = wc.run_monitored(
            cmd, label=store.name, memory_kill_pct=mem_kill,
            cpu_nice=args.nice, threads=args.threads,
            log=log, component='qa_catchup')
        dt = time.time() - t0
        if ok:
            n_png = len(list(out_dir.glob('*.png'))) if out_dir.is_dir() else 0
            print(f"[catchup]   plots done in {dt:.0f}s ({n_png} PNGs)")
            log('QA_DONE', run=run, subrun=sub, file=store.name,
                wall_s=f'{dt:.0f}', n_png=n_png)
            n_ok += 1
            touched_runs.append((run, job['run_dir']))
        else:
            # A store too big to draw has a bounded fallback: the pcapng it came
            # from, read under a packet cap. run_49's cheapest store is still
            # 17.5M hits, so no choice of store gets that sub-run under the
            # ceiling -- capping the read is the only route to its plots.
            fallback = job.get('raw_dir', Path('')) / (store.name + '.pcapng')
            if reason == 'memory' and fallback.is_file():
                print(f"[catchup]   memory-killed after {dt:.0f}s -- retrying from "
                      f"{fallback.name} under a packet cap")
                log('QA_DEGRADE', run=run, subrun=sub, file=store.name)
                ok = _plot_capped(fallback, out_dir, args.max_packets, python_exe,
                                  qa_script, data_format, calibration, mem_kill,
                                  args, pr_cfg, log, run, sub)
            if ok:
                n_ok += 1
                touched_runs.append((run, job['run_dir']))
            else:
                print(f"[catchup]   PLOTS FAILED ({reason}) after {dt:.0f}s")
                log('QA_FAILED', run=run, subrun=sub, file=store.name, reason=reason)
                n_fail += 1

    if args.trend and touched_runs:
        seen = set()
        for run, run_dir in touched_runs:
            if run in seen:
                continue
            seen.add(run)
            _refresh_trend(python_exe, trend_script, run_dir,
                           qa_out_base / run / '_trend.png', run, log)

    dt_all = time.time() - t_all
    print(f"\n[catchup] finished: {n_ok} ok, {n_fail} failed, "
          f"{dt_all / 60:.0f} min total")
    log('FINISH', ok=n_ok, failed=n_fail, wall_min=f'{dt_all / 60:.1f}')
    return 0 if n_fail == 0 else 1


def _survey(runs_dir, qa_out_base, store_inner, raw_inner,
            min_run, max_run, only, max_decode_mb):
    """Split the sub-runs missing plots into plot-only and decode-first work."""
    ready, needs_decode, skipped = [], [], []
    for run_dir in sorted([d for d in runs_dir.iterdir() if d.is_dir()], key=_run_num):
        n = _run_num(run_dir)
        if n < min_run or (max_run is not None and n > max_run):
            continue
        if only and run_dir.name not in only:
            continue
        for subrun in sorted([d for d in run_dir.iterdir() if d.is_dir()]):
            out_sub = qa_out_base / run_dir.name / subrun.name
            if out_sub.is_dir() and any((c / 'events.json').exists()
                                        for c in out_sub.iterdir() if c.is_dir()):
                continue                      # already plotted -- output idempotency

            store_root = subrun / store_inner
            stores = ([d for d in store_root.iterdir() if (d / 'scalars.json').exists()]
                      if store_root.is_dir() else [])
            if stores:
                # Cheapest store, not the first by name. Every capture in a
                # sub-run shares one configuration, so any of them answers "what
                # did this sub-run look like" -- but they do not cost the same to
                # draw. run_48 holds a 7.1M-hit store next to two 31.5M-hit ones,
                # and picking by name took a 31.5M store straight into the memory
                # ceiling when an equivalent plot was three times cheaper.
                ready.append({'run': run_dir.name, 'run_dir': run_dir,
                              'subrun': subrun.name,
                              'raw_dir': subrun / raw_inner,
                              'store': min(stores, key=_store_hits)})
                continue

            pcap, mb, why, oversized = _representative(subrun / raw_inner, max_decode_mb)
            if pcap is None:
                skipped.append((run_dir.name, subrun.name, why))
                continue
            needs_decode.append({'run': run_dir.name, 'run_dir': run_dir,
                                 'subrun': subrun.name, 'pcap': pcap, 'mb': mb,
                                 'oversized': oversized,
                                 'raw_dir': subrun / raw_inner,
                                 'store_dir': store_root / pcap.stem})
    return ready, needs_decode, skipped


def _representative(raw_dir, max_decode_mb):
    """Pick one finalized capture that fairly represents its sub-run.

    Only rotated captures are eligible -- a capture that is still the highest
    sequence number for its interface may be the one dumpcap has open right now,
    and decoding a file that is still growing is how you get a truncated store.
    That rule is what makes this safe to run against the sub-run being taken.

    Among those, the median-sized capture is chosen rather than the middle one
    in time: both are steady-state, but the median cannot land on a size outlier
    that would be a memory kill for no extra information.

    Returns (path, size_mb, why_skipped, oversized). `oversized` is True when NO
    capture in the sub-run fits max_decode_mb -- the caller then plots from the
    pcapng under a packet cap instead of decoding, since there is no smaller
    file to fall back to.
    """
    if not raw_dir.is_dir():
        return None, 0.0, 'no raw_daq_data', False
    entries = []
    n_empty = 0
    for f in raw_dir.iterdir():
        parsed = parse_pcapng_name(f.name)
        if parsed is None:
            continue
        iface, seq, _ts = parsed
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        if size <= _EMPTY_PCAPNG_BYTES:
            n_empty += 1
            continue
        entries.append((iface, seq, f, size))
    if not entries:
        if n_empty:
            return None, 0.0, f'no data ({n_empty} header-only captures, 0 packets)', False
        return None, 0.0, 'no pcapng', False

    max_seq = {}
    for iface, seq, _f, _s in entries:
        max_seq[iface] = max(max_seq.get(iface, -1), seq)
    # Same finalization rule as vmm_processor_watcher: a capture is safe to read
    # once a higher-seq sibling exists, or the sub-run wrote .capture_done, or
    # rotation is long overdue. Rotation alone is not enough -- a sub-run that
    # ended after a single capture (run_56/meshscan_m80V) has no sibling and
    # would otherwise be excluded forever despite being complete.
    done_marker = (raw_dir / CAPTURE_DONE_MARKER).exists()
    now = time.time()
    rotated = [e for e in entries
               if e[1] < max_seq[e[0]] or done_marker
               or (now - e[2].stat().st_mtime) > _OVERDUE_S]
    if not rotated:
        return None, 0.0, 'only the open capture (sub-run still being written)', False

    if len(rotated) >= _STUB_CAPTURES:
        # drop the first capture: it covers the configuration settling at the
        # start of the sub-run and is not what the sub-run looked like
        min_seq = min(e[1] for e in rotated)
        trimmed = [e for e in rotated if e[1] > min_seq] or rotated
    else:
        trimmed = rotated

    under = [e for e in trimmed if e[3] / 1e6 <= max_decode_mb]
    pool = sorted(under or trimmed, key=lambda e: e[3])
    iface, seq, f, size = pool[len(pool) // 2]
    # No capture in the whole sub-run fits the ceiling (run_44 is ~800 MB per
    # file across all 48). Decoding one of those builds a resident hits table
    # several times the box's free memory, so it is a guaranteed memory kill and
    # a wasted half hour. Such a sub-run is plotted straight from the pcapng
    # with a packet cap instead: bounded memory, no store, and the plots still
    # describe the sub-run because the captures are steady state.
    return f, size / 1e6, '', not under


def _refresh_trend(python_exe, trend_script, run_dir, out_png, title, log):
    out_png.parent.mkdir(parents=True, exist_ok=True)
    print(f"[catchup] trend {title} -> {out_png}")
    try:
        subprocess.run([python_exe, str(trend_script), str(run_dir),
                        '--out', str(out_png),
                        '--title', f'VMM trend — {title}'],
                       check=True, capture_output=True, timeout=600)
        log('TREND_DONE', target=title)
    except Exception as e:
        print(f"[catchup]   trend failed: {type(e).__name__}: {e}")
        log('TREND_FAILED', target=title, error=f'{type(e).__name__}: {e}')


def _wait_for_memory(min_free_mb, poll=5.0, announce_after=2):
    """Block until the box has room, so a job is never started into a squeeze."""
    waited = 0
    while True:
        _mem_pct, free_mb = wc.mem_usage_pct()
        if free_mb >= min_free_mb:
            if waited:
                print(f"\n[catchup]   ...{free_mb:.0f} MB free, going")
            return
        waited += 1
        if waited == announce_after:
            print(f"[catchup]   waiting for memory "
                  f"({free_mb:.0f} MB free, need {min_free_mb:.0f})", end='', flush=True)
        elif waited > announce_after:
            print('.', end='', flush=True)
        time.sleep(poll)


def _watchers_alive():
    """The two processes this script exists to replace. Empty list if neither runs."""
    out = []
    for name in ('vmm_processor_watcher.py', 'qa_watcher.py'):
        try:
            res = subprocess.run(['pgrep', '-f', name],
                                 capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        for pid in res.stdout.split():
            if pid.strip() and int(pid) != os.getpid():
                out.append((pid.strip(), name))
    return out


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _plot_capped(pcap, out_dir, first_cap, python_exe, qa_script, data_format,
                 calibration, mem_kill, args, pr_cfg, log, run, sub):
    """Plot from a pcapng under a packet cap, probing downward until it fits.

    The plotting stage's memory ceiling is not a clean function of hit count on
    this box: a 24.2M-hit store (run_53) drew fine while a 17.7M-hit one
    (run_49) was killed, so the limit depends on occupancy and on what else the
    machine is doing at that second, not on any number we can compute up front.

    Rather than predict the threshold, probe it. Each rung is a quarter of the
    one before, and a rung that does not fit dies in 8-16 s -- so the whole
    ladder costs less than one successful plot. A capture is steady state, so a
    quarter of its packets describes the sub-run just as well, with fewer
    entries per histogram.
    """
    caps = [first_cap, max(first_cap // 4, 1), max(first_cap // 16, 1)]
    for n, cap in enumerate(caps, 1):
        _wait_for_memory(args.min_free_mb)
        print(f"[catchup]   cap {cap:,} packets (rung {n}/{len(caps)})")
        cmd = [python_exe, str(qa_script), str(pcap),
               '--out-dir', str(out_dir),
               '--events-json', '--format', data_format,
               '--max-packets', str(cap)]
        if calibration:
            cmd += ['--calibration', str(calibration)]
        t0 = time.time()
        ok, reason = wc.run_monitored(
            cmd, label=pcap.name, memory_kill_pct=mem_kill,
            cpu_nice=args.nice, threads=args.threads,
            hard_timeout_s=pr_cfg.get('hard_timeout_s', 3600),
            log=log, component='qa_catchup')
        dt = time.time() - t0
        if ok:
            n_png = len(list(out_dir.glob('*.png'))) if out_dir.is_dir() else 0
            print(f"[catchup]   plots done in {dt:.0f}s ({n_png} PNGs, "
                  f"capped at {cap:,} packets)")
            log('QA_DONE_CAPPED', run=run, subrun=sub, file=pcap.name,
                wall_s=f'{dt:.0f}', n_png=n_png, max_packets=cap)
            return True
        print(f"[catchup]   rung {n} failed ({reason}) after {dt:.0f}s")
        if reason != 'memory':
            break               # a non-memory failure will not fix itself by shrinking
    log('QA_FAILED_CAPPED', run=run, subrun=sub, file=pcap.name, tried=str(caps))
    return False


def _store_hits(store_dir):
    """n_hits from a store's scalars.json; a store we cannot read sorts last."""
    try:
        with open(store_dir / 'scalars.json') as f:
            return int(json.load(f).get('n_hits', 0))
    except Exception:
        return float('inf')


def _run_num(p):
    try:
        return int(p.name.split('_')[1])
    except (IndexError, ValueError):
        return -1


if __name__ == '__main__':
    sys.exit(main())
