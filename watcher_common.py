#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared machinery for the beam watchers (qa_watcher, vmm_processor_watcher).

Everything here is "run a subprocess politely on a machine the DAQ is using,
and remember what already worked":

  * memory kill      — poll system RAM, terminate the child before the box
                       swaps or earlyoom picks the wrong victim
  * stall watchdog   — kill a child whose output has stopped growing, and a
                       hard cap on any single invocation (ported from the DREAM
                       processor_watcher, where a decoder could spin at 100 %
                       CPU on certain files and block the pipeline forever)
  * CPU throttling   — taskset + nice + ionice, and a cap on numpy/BLAS threads
  * attempt tracking — persisted done/failed state so a restart resumes, and a
                       file that fails max_attempts times is set aside
  * reset signal     — the flask UI drops a JSON file to re-queue runs

It lives in one module so the two watchers cannot drift apart: before this,
only qa_watcher had the memory kill and only the DREAM processor had the stall
watchdog, and each would have grown its own copy of the other.

Nothing in here knows what a pcapng or a hit is — that belongs to the callers.
"""

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

__all__ = [
    "Logger", "load_state", "save_state", "pop_reset_signal",
    "mem_usage_pct", "build_throttled_command", "thread_limited_env",
    "run_monitored", "run_is_stale", "newest_first",
]


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

class Logger:
    """Append-only structured log, one line per event.

    The component name is a field rather than being baked into the format, so
    qa_watcher and the processor can share a log file and still be told apart.
    """

    def __init__(self, log_file, component):
        self.path = Path(log_file)
        self.component = component

    def __call__(self, event: str, **details):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            detail_str = " | ".join(f"{k}={v}" for k, v in details.items())
            line = f"{ts} | {event:<16} | {self.component:<12} | {detail_str}\n"
            with open(self.path, "a") as f:
                f.write(line)
        except Exception as e:
            print(f"[{self.component}] Warning: could not write to log: {e}")


# ---------------------------------------------------------------------------
# persisted state  (done files + failure counts, keyed by (run, subrun))
# ---------------------------------------------------------------------------

def load_state(state_path) -> tuple:
    """Return (done_files, fail_counts).

    Accepts the legacy qa_watcher format where each run/subrun maps to a plain
    list of done basenames with no failure counts.
    """
    if state_path is None or not Path(state_path).exists():
        return {}, {}
    try:
        with open(state_path) as f:
            raw = json.load(f)
        done, fails = {}, {}
        for k, v in raw.items():
            key = tuple(k.split("/", 1))
            if isinstance(v, list):                 # legacy format
                done[key] = set(v)
            else:
                done[key] = set(v.get("done", []))
                fails[key] = {n: int(c) for n, c in v.get("failed", {}).items()}
        return done, fails
    except Exception as e:
        print(f"Could not load state from {state_path}: {e}")
        return {}, {}


def save_state(state_path, done_files: dict, fail_counts: dict):
    if state_path is None:
        return
    try:
        raw = {}
        for key in set(done_files) | set(fail_counts):
            raw[f"{key[0]}/{key[1]}"] = {
                "done": sorted(done_files.get(key, ())),
                "failed": fail_counts.get(key) or {},
            }
        tmp = Path(f"{state_path}.tmp")
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, state_path)      # atomic: a crash never truncates state
    except Exception as e:
        print(f"Could not save state to {state_path}: {e}")


def pop_reset_signal(signal_path):
    """Consume a reset signal dropped by the flask UI.

    False -> no signal.  None -> reset every run.  set -> reset those runs.
    """
    signal_path = Path(signal_path)
    if not signal_path.exists():
        return False
    try:
        with open(signal_path) as f:
            data = json.load(f)
        signal_path.unlink()
        runs = data.get("runs")
        return set(runs) if runs else None
    except Exception as e:
        print(f"Error reading reset signal: {e}")
        try:
            signal_path.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

def _read_meminfo() -> tuple:
    """(mem_total_kb, mem_available_kb); (0, 0) if /proc/meminfo is unreadable."""
    total, avail = 0, 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total and avail:
                    break
    except Exception:
        pass
    return total, avail


def mem_usage_pct() -> tuple:
    """(used_pct 0-100, free_mb). (0.0, inf) if /proc/meminfo is unreadable."""
    total, avail = _read_meminfo()
    if total == 0:
        return 0.0, float("inf")
    return (total - avail) / total * 100, avail / 1024


# ---------------------------------------------------------------------------
# CPU throttling
# ---------------------------------------------------------------------------

def build_throttled_command(cmd: list, cpu_nice, cpu_affinity) -> list:
    """Wrap a command with taskset + nice/ionice so it never starves the DAQ.

    Each wrapper execs the next, so the final PID is still the python process
    and signals from run_monitored reach it.
    """
    wrapped = list(cmd)
    if cpu_affinity:
        cores = ",".join(str(int(c)) for c in cpu_affinity)
        wrapped = ["taskset", "-c", cores] + wrapped
    if cpu_nice is not None:
        wrapped = ["nice", "-n", str(int(cpu_nice)), "ionice", "-c", "3"] + wrapped
    return wrapped


def thread_limited_env(threads) -> dict:
    """os.environ with numpy/BLAS thread pools capped."""
    env = os.environ.copy()
    if threads:
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[var] = str(int(threads))
    return env


# ---------------------------------------------------------------------------
# the monitored subprocess
# ---------------------------------------------------------------------------

def run_monitored(cmd, *, label="", memory_kill_pct=80, monitor_interval=1.0,
                  cpu_nice=19, cpu_affinity=None, threads=None,
                  stall_timeout_s=None, hard_timeout_s=None, watch_path=None,
                  log=None, component="watcher"):
    """Run `cmd`, killing it if the machine or the job misbehaves.

    Returns (ok: bool, reason: str). reason is "" on success and one of
    "memory", "stall", "timeout", "exit<N>" otherwise, so the caller can decide
    whether the failure is worth counting against max_attempts.

    watch_path + stall_timeout_s arm the stall watchdog: if that path does not
    grow for stall_timeout_s the child is killed. hard_timeout_s caps a single
    invocation regardless of progress.
    """
    full = build_throttled_command(list(cmd), cpu_nice, cpu_affinity)
    env = thread_limited_env(threads)
    proc = subprocess.Popen(full, env=env)

    # Make this tree the kernel's (and earlyoom's) preferred OOM victim, so the
    # box sacrifices restartable processing before the live DAQ. oom_score_adj
    # survives the nice/taskset exec chain; raising it needs no privilege.
    try:
        with open(f"/proc/{proc.pid}/oom_score_adj", "w") as f:
            f.write("500\n")
    except OSError:
        pass

    t_start = time.time()
    last_size, last_growth = -1, time.time()

    def _kill(reason, **details):
        if log:
            log("KILLED", file=label, reason=reason, **details)
        print(f"\n[{component}] killing {label}: {reason}")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    while proc.poll() is None:
        time.sleep(monitor_interval)
        now = time.time()

        mem_pct, free_mb = mem_usage_pct()
        if mem_pct >= memory_kill_pct:
            _kill("memory", mem_pct=f"{mem_pct:.1f}%", free_mb=f"{free_mb:.0f}",
                  threshold=f"{memory_kill_pct}%")
            return False, "memory"

        if hard_timeout_s and (now - t_start) > hard_timeout_s:
            _kill("timeout", elapsed_s=f"{now - t_start:.0f}")
            return False, "timeout"

        if watch_path and stall_timeout_s:
            size = _path_size(watch_path)
            if size != last_size:
                last_size, last_growth = size, now
            elif (now - last_growth) > stall_timeout_s:
                _kill("stall", frozen_s=f"{now - last_growth:.0f}", bytes=size)
                return False, "stall"

    rc = proc.returncode
    return (True, "") if rc == 0 else (False, f"exit{rc}")


def _path_size(path) -> int:
    """Size of a file, or the total size of a directory tree. -1 if absent."""
    p = Path(path)
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return -1


# ---------------------------------------------------------------------------
# run selection
# ---------------------------------------------------------------------------

def run_is_stale(run_dir: Path, raw_inner: str, stale_days: float) -> bool:
    """True if no raw file under run_dir/*/raw_inner is newer than stale_days."""
    cutoff = time.time() - stale_days * 86400
    newest = 0.0
    found_any = False
    for subrun in Path(run_dir).iterdir():
        raw_dir = subrun / raw_inner
        if not raw_dir.is_dir():
            continue
        for f in raw_dir.iterdir():
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            found_any = True
            newest = max(newest, mtime)
    return found_any and newest < cutoff


def newest_first(dirs):
    """Sort run/subrun dirs newest-first by trailing number, then mtime.

    Online monitoring wants the freshest file, not the oldest unprocessed one:
    when the watcher is behind, chronological order shows the least useful
    state. Backfill is what the backlog is for.
    """
    def key(p):
        name = p.name
        num = -1
        for i in range(len(name) - 1, -1, -1):
            if not name[i].isdigit():
                tail = name[i + 1:]
                num = int(tail) if tail else -1
                break
        else:
            num = int(name) if name.isdigit() else -1
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (num, mtime)

    return sorted(dirs, key=key, reverse=True)
