#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory guardian — kill the runaway compute process, not the computer.

The Python pcapng QA (vmm_qa/vmm_pcapng_qa.py, spawned per finalized capture by
qa_watcher) can balloon in memory on a big file. If system memory is exhausted
the machine thrashes/freezes and can take the live DAQ down with it. This daemon
watches MemAvailable and, when it drops into the danger zone, SIGTERM/SIGKILLs
the single largest-RSS process among an ALLOW-LIST of compute processes — never
the DAQ (vmm_daq_control, dumpcap, hv/lv_control, daq_control, flask), never
itself. qa_watcher simply re-runs the killed QA job later.

MemAvailable (not "free") is the metric: it counts reclaimable page cache, so it
is the kernel's own estimate of what a new allocation can get without swapping.

Runs in its own tmux session (started by start_servers.sh). No root needed — it
only signals processes owned by the same user, and reads /proc.

Config: optional JSON path as argv[1] (keys below); otherwise built-in defaults.
  kill_avail_mb : kill the biggest allow-listed process below this MemAvailable
  warn_avail_mb : just log a warning below this
  poll_s        : seconds between checks
  recover_s     : after a kill, pause this long for memory to be reclaimed
  killable      : substrings; a process is eligible iff its cmdline contains one
  protect       : substrings that VETO a kill even if 'killable' matched
"""

import os
import re
import sys
import json
import time
import signal
import datetime

DEFAULTS = {
    # Tuned for the ~8 GB bench box (dedippce185): kill a runaway QA job well
    # before the kernel OOM/freeze, keeping headroom for the DAQ. Raise these on
    # a larger machine (a 62 GB box would use ~4000/8000) via a JSON config.
    "kill_avail_mb": 900,
    "warn_avail_mb": 1800,
    "poll_s": 1.5,
    "recover_s": 20.0,
    # Only these (heavy, restartable) compute processes may be killed. The QA is
    # the one memory-hungry offline job in the VMM stack; qa_watcher re-runs it.
    "killable": [
        "vmm_pcapng_qa.py",
    ],
    # Safety veto: never kill the live DAQ, its watchers, or the guardian itself,
    # even if a substring above happened to match part of their command line.
    # NOTE: use full script names (e.g. 'vmm_daq_control.py') — a bare 'vmm_daq'
    # would also match the data path 'vmm_daq_bench/...' in the QA cmdline and
    # wrongly veto the kill.
    "protect": [
        "vmm_daq_control.py", "daq_control.py", "hv_control.py", "lv_control.py",
        "qa_watcher.py", "backup_watcher.py", "beam_watcher.py", "dumpcap",
        "Server.py", "start_flask", "flask", "mem_guardian.py",
    ],
}

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "mem_guardian.log")


def _log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def mem_available_mb():
    """MemAvailable from /proc/meminfo, in MiB."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None  # very old kernels lack it; guardian then no-ops safely


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _proc_rss_kb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def biggest_killable(killable, protect):
    """(pid, rss_kb, cmdline) of the largest-RSS eligible process, or None."""
    self_pid = os.getpid()
    best = None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == self_pid:
            continue
        cmd = _proc_cmdline(pid)
        if not cmd:
            continue
        if not any(k in cmd for k in killable):
            continue
        if any(p in cmd for p in protect):
            continue
        rss = _proc_rss_kb(pid)
        if best is None or rss > best[1]:
            best = (pid, rss, cmd)
    return best


def _kill(pid, rss_kb, cmd):
    _log(f"KILL pid={pid} rss={rss_kb // 1024}MB  cmd='{cmd[:160]}'")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        _log(f"  SIGTERM failed: {e}")
        return
    for _ in range(30):  # up to 3 s to exit cleanly
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            _log(f"  pid={pid} exited on SIGTERM")
            return
    try:
        os.kill(pid, signal.SIGKILL)
        _log(f"  pid={pid} SIGKILLed (did not exit on SIGTERM)")
    except OSError:
        pass


def main():
    cfg = dict(DEFAULTS)
    if len(sys.argv) > 1:
        try:
            cfg.update(json.load(open(sys.argv[1])))
        except Exception as e:
            _log(f"could not read config {sys.argv[1]} ({e}); using defaults")

    _log(f"START kill<{cfg['kill_avail_mb']}MB warn<{cfg['warn_avail_mb']}MB "
         f"poll={cfg['poll_s']}s  (MemAvailable now {mem_available_mb()}MB)")
    warned = False
    while True:
        avail = mem_available_mb()
        if avail is None:
            time.sleep(cfg["poll_s"])
            continue
        if avail < cfg["kill_avail_mb"]:
            victim = biggest_killable(cfg["killable"], cfg["protect"])
            if victim:
                _log(f"MemAvailable {avail}MB < {cfg['kill_avail_mb']}MB — killing biggest compute job")
                _kill(*victim)
                time.sleep(cfg["recover_s"])
            else:
                _log(f"MemAvailable {avail}MB critical but no killable compute process found — "
                     f"leaving the kernel OOM to act")
                time.sleep(cfg["poll_s"])
            warned = False
        elif avail < cfg["warn_avail_mb"]:
            if not warned:
                _log(f"WARN MemAvailable {avail}MB < {cfg['warn_avail_mb']}MB")
                warned = True
            time.sleep(cfg["poll_s"])
        else:
            warned = False
            time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    main()
