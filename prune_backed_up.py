#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep the data disk from filling by deleting runs that are safely on EOS.

Runs on BOTH DAQ machines (banco and the VMM box) — it locates its own repo from
__file__ and uses that repo's flask_app/space_manager, so the deletion rules are
the same ones the Disk Space tab applies when a human presses the button. It
adds only the scheduling: nobody is awake at 4 a.m. to press it.

WHAT IT WILL AND WILL NOT DELETE. It never decides for itself. space_manager
.verify_run compares the local run against EOS file by file (relative path and
size) and returns safe=True only if every file is there and matches;
delete_run then RE-VERIFIES before unlinking anything and refuses on its own
guards — the active run, the newest run, runs with incomplete sub-runs, symlinks,
anything not directly under the runs root. If EOS cannot be listed (expired
Kerberos, network) verify_run reports NOT safe, so an outage makes this stop
deleting rather than start guessing.

WATER MARKS, not "delete everything backed up". It only prunes when free space
drops below LOW_FREE_GB, and stops as soon as TARGET_FREE_GB is reached, oldest
run first. Recent runs therefore stay on local disk for analysis while the disk
still cannot fill: deleting every backed-up run the moment it lands would empty
the machine of everything anyone might still want to look at.

Usage: prune_backed_up.py [--low GB] [--target GB] [--interval S] [--dry-run]
"""

import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, 'flask_app'))
LOG = os.path.join(REPO, 'logs', 'prune_backed_up.log')

import space_manager as sm  # noqa: E402  (needs the path above)


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} | {msg}'
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def free_gb():
    try:
        u = sm.disk_usage()
        d = u.get('data') or {}
        if 'free' in d:
            return float(d['free']) / 1e9
        return float(d['usage']['free']) / 1e9
    except Exception as e:
        log(f'cannot read disk usage: {e}')
        return None


def sweep(low, target, dry):
    fg = free_gb()
    if fg is None:
        return
    if fg >= low:
        return                                   # nothing to do, stay quiet
    log(f'free {fg:.0f} GB < {low} GB — looking for runs safely on EOS')

    freed = 0
    for run in sm.list_runs('data'):             # oldest first
        fg = free_gb()
        if fg is None or fg >= target:
            break
        try:
            # verify_run alone does NOT apply the local guards — those live in
            # scan() and delete_run(). Re-applying them here keeps the decision
            # (and the dry run) honest, instead of proposing deletions that
            # delete_run would then refuse.
            v = sm.verify_run('data', run)
            v = sm._apply_local_guards(v, run, sm.active_run(), sm.newest_run())
        except Exception as e:
            log(f'  {run}: verify failed ({e}) — skipping')
            continue
        if not v.get('safe'):
            continue
        size = v.get('local_bytes') or v.get('size') or 0
        if dry:
            log(f'  WOULD delete {run} ({size/1e9:.1f} GB) — {v.get("ok")} files on EOS')
            continue
        r = sm.delete_run('data', run)
        if r.get('success'):
            freed += size
            log(f'  deleted {run} ({size/1e9:.1f} GB verified on EOS) — '
                f'free now {free_gb():.0f} GB')
        else:
            log(f'  {run} NOT deleted: {r.get("message")}')
    if freed:
        log(f'swept {freed/1e9:.1f} GB')
    else:
        log('nothing was safely on EOS yet — leaving the disk alone')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--low', type=float, default=150.0)
    ap.add_argument('--target', type=float, default=250.0)
    ap.add_argument('--interval', type=int, default=300)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    log(f'PRUNER START repo={REPO} prune below {a.low} GB, up to {a.target} GB, '
        f'every {a.interval}s{" (DRY RUN)" if a.dry_run else ""}; '
        f'free now {free_gb():.0f} GB')
    while True:
        try:
            sweep(a.low, a.target, a.dry_run)
        except Exception as e:
            # Never exit: a pruner that dies leaves the disk unguarded overnight,
            # which is exactly when nobody is watching it.
            log(f'ALERT sweep failed: {type(e).__name__}: {e} — continuing')
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
