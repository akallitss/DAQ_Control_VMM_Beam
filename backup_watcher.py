#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous EOS backup watcher for nTof DREAM DAQ data.

Syncs the entire source_dir to eos_dir, excluding specified subdirectories.
The runs_subdir gets smart per-subrun sync (waits for each subrun to be stable
before transferring).  All other subdirs are rsynced wholesale on a slower
extra_sync_interval cadence.

Handles Kerberos via kinit -R (renewal) and falls back to a GPG-encrypted
password for a full re-kinit when renewal fails.

Usage:
    python backup_watcher.py <backup_config_json_path>

Config keys (see backup_config.py to generate the JSON):
  source_dir          : local top-level data directory (e.g. /mnt/data/x17/beam_may/)
  eos_dir             : EOS destination (locally FUSE-mounted, same structure)
  runs_subdir         : name of the runs subdir that gets smart per-subrun sync
  exclude_dirs        : list of subdir names to never sync (e.g. ['dream_run'])
  gpg_pass_file       : path to GPG-encrypted CERN password (~/.cern_pass.gpg)
  cern_principal      : Kerberos principal (e.g. dneff@CERN.CH)
  kinit_interval      : seconds between kinit renewal attempts   (default: 3600)
  include_runs        : list of run dir names to sync exclusively (null = all)
  exclude_runs        : list of run dir names to skip             (null = none)
  poll_interval       : seconds between runs-dir scans           (default: 30)
  stale_run_days      : runs with no new data for N days skipped (default: 10)
  extra_sync_interval : seconds between full syncs of non-runs dirs (default: 300)
  rsync_extra_args    : extra arguments passed verbatim to rsync  (default: [])
"""

import sys
import json
import time
import datetime
import subprocess
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("Usage: python backup_watcher.py <backup_config_json_path>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        config = json.load(f)
    run_watcher(config, Path(sys.argv[1]))


# ---------------------------------------------------------------------------
# Main watcher loop
# ---------------------------------------------------------------------------

def run_watcher(config: dict, config_path: Path):
    source_dir    = Path(config['source_dir'])
    eos_dir       = Path(config['eos_dir'])
    runs_subdir   = config.get('runs_subdir', 'runs')
    exclude_dirs  = set(config.get('exclude_dirs', []))
    gpg_pass_file = Path(config['gpg_pass_file'])
    cern_principal = config['cern_principal']

    kinit_interval      = config.get('kinit_interval',      3600)
    extra_sync_interval = config.get('extra_sync_interval',  300)
    poll_interval       = config.get('poll_interval',         30)
    stale_run_days      = config.get('stale_run_days',        10)
    rsync_extra         = config.get('rsync_extra_args',      [])

    include_runs = set(config['include_runs']) if config.get('include_runs') else None
    exclude_runs = set(config['exclude_runs']) if config.get('exclude_runs') else set()

    runs_dir     = source_dir / runs_subdir
    eos_runs_dir = eos_dir    / runs_subdir

    print(f"[backup] source_dir         : {source_dir}")
    print(f"[backup] eos_dir            : {eos_dir}")
    print(f"[backup] runs_subdir        : {runs_subdir}")
    print(f"[backup] exclude_dirs       : {sorted(exclude_dirs)}")
    print(f"[backup] principal          : {cern_principal}")
    print(f"[backup] kinit_interval     : {kinit_interval}s")
    print(f"[backup] extra_sync_interval: {extra_sync_interval}s")
    if include_runs:
        print(f"[backup] include_runs       : {sorted(include_runs)}")
    if exclude_runs:
        print(f"[backup] exclude_runs       : {sorted(exclude_runs)}")
    print(f"[backup] poll               : {poll_interval}s  stale_after={stale_run_days}d")

    state_path = config_path.parent / 'backup_state.json'
    # (run_name, subrun_name) -> total dir size at last successful rsync
    synced_sizes: dict = _load_state(state_path)
    # (run_name, subrun_name) -> total dir size from previous poll (stable check)
    prev_sizes: dict = {}

    checked_stale_runs: set = set()

    last_kinit_check  = -kinit_interval   # trigger immediately on first iteration
    last_extra_sync   = -extra_sync_interval
    kerberos_ok       = False

    idle_ticks = 0
    idle_line  = False
    _SPINNER   = ['-', '\\', '|', '/']

    def _end_idle():
        nonlocal idle_line
        if idle_line:
            sys.stdout.write('\n')
            sys.stdout.flush()
            idle_line = False

    while True:
        now = time.time()

        # --- Kerberos refresh ---
        if now - last_kinit_check >= kinit_interval:
            ok, method = _refresh_kerberos(cern_principal, gpg_pass_file)
            last_kinit_check = now
            kerberos_ok = ok
            _end_idle()
            if ok:
                print(f"[backup] Kerberos OK ({method})")
            else:
                print(f"[backup] Kerberos FAILED: {method}")

        found_new = False

        if not source_dir.exists():
            pass
        elif not kerberos_ok:
            _end_idle()
            print("[backup] Skipping scan — Kerberos not authenticated")
        else:
            # --- Smart per-subrun sync for runs_subdir ---
            if runs_dir.exists():
                for run_dir in sorted(runs_dir.iterdir()):
                    if not run_dir.is_dir():
                        continue
                    if include_runs is not None and run_dir.name not in include_runs:
                        continue
                    if run_dir.name in exclude_runs:
                        continue
                    if run_dir.name in checked_stale_runs:
                        continue

                    is_stale = _run_is_stale(run_dir, stale_run_days)

                    for subrun_dir in sorted(run_dir.iterdir()):
                        if not subrun_dir.is_dir():
                            continue

                        key = (run_dir.name, subrun_dir.name)
                        current_size = _dir_size(subrun_dir)

                        # Stable check: size must match the previous poll
                        if prev_sizes.get(key) != current_size:
                            prev_sizes[key] = current_size
                            continue

                        # Skip if already rsynced at this exact size
                        if synced_sizes.get(key) == current_size:
                            continue

                        _end_idle()
                        mb = current_size // (1024 * 1024)
                        print(f"[backup] {run_dir.name}/{subrun_dir.name}  size={mb}MB")

                        ok = _rsync_subrun(subrun_dir, eos_runs_dir / run_dir.name, rsync_extra)
                        if ok:
                            _rsync_run_config(run_dir, eos_runs_dir / run_dir.name)
                            synced_sizes[key] = current_size
                            _save_state(state_path, synced_sizes)
                            found_new = True
                        else:
                            print(f"[backup] rsync FAILED for {run_dir.name}/{subrun_dir.name}")

                    if is_stale:
                        checked_stale_runs.add(run_dir.name)
                        _end_idle()
                        print(f"[backup] Marked stale (will skip): {run_dir.name}")

            # --- Periodic full sync for all other subdirs ---
            if now - last_extra_sync >= extra_sync_interval:
                last_extra_sync = now
                for subdir in sorted(source_dir.iterdir()):
                    if not subdir.is_dir():
                        continue
                    if subdir.name == runs_subdir:
                        continue
                    if subdir.name in exclude_dirs:
                        continue
                    _end_idle()
                    print(f"[backup] extra sync: {subdir.name}/")
                    _rsync_dir(subdir, eos_dir / subdir.name, rsync_extra)

        if found_new:
            idle_ticks = 0
        else:
            idle_ticks += 1
            elapsed = idle_ticks * poll_interval
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            sp = _SPINNER[idle_ticks % 4]
            if not source_dir.exists():
                msg = f'[backup] {sp} waiting for source_dir  #{idle_ticks}  {ts}'
            elif not kerberos_ok:
                msg = f'[backup] {sp} AUTH ERROR — Kerberos not valid  {ts}'
            else:
                msg = f'[backup] {sp} idle  #{idle_ticks}  {elapsed}s  {ts}'
            sys.stdout.write(f'\r{msg}          ')
            sys.stdout.flush()
            idle_line = True

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Kerberos
# ---------------------------------------------------------------------------

def _refresh_kerberos(principal: str, gpg_pass_file: Path) -> tuple:
    """Try kinit -R first; fall back to GPG-decrypted password re-kinit."""
    result = subprocess.run(['kinit', '-R'], capture_output=True)
    if result.returncode == 0:
        return True, 'renewed'

    if not gpg_pass_file.exists():
        return False, f'GPG password file not found: {gpg_pass_file}'

    # Decrypt via gpg-agent — prompts for GPG passphrase via pinentry once per
    # boot; subsequent calls use the cached passphrase from the agent.
    gpg = subprocess.run(
        ['gpg', '--batch', '--yes', '--decrypt', str(gpg_pass_file)],
        capture_output=True,
    )
    if gpg.returncode != 0:
        stderr = gpg.stderr.decode(errors='replace').strip()
        return False, f'gpg decrypt failed (passphrase not cached?): {stderr}'

    kinit = subprocess.run(
        ['kinit', principal],
        input=gpg.stdout,
        capture_output=True,
    )
    if kinit.returncode == 0:
        return True, 'full kinit'
    stderr = kinit.stderr.decode(errors='replace').strip()
    return False, f'kinit failed: {stderr}'


# ---------------------------------------------------------------------------
# rsync helpers
# ---------------------------------------------------------------------------

_RSYNC_BASE = ['rsync', '-rlt', '--update', '--no-perms', '--omit-dir-times', '--info=progress2']


def _rsync_subrun(subrun_dir: Path, eos_run_dir: Path, extra: list) -> bool:
    """rsync subrun_dir/ into eos_run_dir/subrun_name/. Returns True on success."""
    dest = eos_run_dir / subrun_dir.name
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[backup] Cannot create dest dir {dest}: {e}")
        return False
    print(f"[backup] rsync -> {dest}")
    result = subprocess.run(_RSYNC_BASE + extra + [str(subrun_dir) + '/', str(dest) + '/'])
    if result.returncode == 0:
        print(f"[backup] rsync done: {subrun_dir.name}")
        return True
    print(f"[backup] rsync exit {result.returncode}: {subrun_dir.name}")
    return False


def _rsync_run_config(run_dir: Path, eos_run_dir: Path):
    """Sync the run-level run_config.json if present."""
    cfg = run_dir / 'run_config.json'
    if not cfg.exists():
        return
    try:
        eos_run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    subprocess.run(
        ['rsync', '-lt', '--update', '--no-perms', str(cfg), str(eos_run_dir) + '/'],
        capture_output=True,
    )


def _rsync_dir(src: Path, dest: Path, extra: list):
    """rsync an entire directory wholesale."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[backup] Cannot create dest dir {dest}: {e}")
        return
    result = subprocess.run(_RSYNC_BASE + extra + [str(src) + '/', str(dest) + '/'])
    if result.returncode == 0:
        print(f"[backup] extra sync done: {src.name}/")
    else:
        print(f"[backup] extra sync FAILED (exit {result.returncode}): {src.name}/")


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob('*'):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _run_is_stale(run_dir: Path, stale_days: float) -> bool:
    cutoff = time.time() - stale_days * 86400
    newest = 0.0
    found_any = False
    for subrun in run_dir.iterdir():
        if not subrun.is_dir():
            continue
        found_any = True
        mtime = subrun.stat().st_mtime
        if mtime > newest:
            newest = mtime
    if not found_any:
        return False
    return newest < cutoff


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        with open(state_path) as f:
            raw = json.load(f)
        return {tuple(k.split('/', 1)): v for k, v in raw.items()}
    except Exception as e:
        print(f"[backup] Could not load state from {state_path}: {e}")
        return {}


def _save_state(state_path: Path, synced_sizes: dict):
    try:
        raw = {f"{k[0]}/{k[1]}": v for k, v in synced_sizes.items()}
        with open(state_path, 'w') as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        print(f"[backup] Could not save state to {state_path}: {e}")


if __name__ == '__main__':
    main()
