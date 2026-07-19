#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Disk-space management for the P2 SPS VMM data (adapted from Dylan Neff's
nTof_x17_DAQ space_manager via DAQ_Control_Dream_Beam).

Provides a read-only *scan/check* and a heavily guarded *delete* for freeing
space in the local run store, plus a *restore* that pulls runs back from EOS:

  <source_dir>/<runs_subdir>/<run>    processed runs, backed up to EOS

(x17 managed two disks — an HDD run store and an SSD raw-staging dir; our
setup is a single data tree, and the simulation captures / derived analysis
are excluded from backup entirely, so only the runs tree is managed here.)

Safety model — a run is only ever "safe to delete" when its data is provably
preserved on EOS: EVERY file in the run tree must be present on EOS at
matching size (relative path + byte size; data is write-once). This is
exactly the check backup_watcher uses, over the SAME file set — the full
recursive tree, so subrun raw data AND any processing outputs (QA
events.json, ROOT files) and dotfile markers all count.

Extra guards beyond x17's:
  * the run named in config/current_run_state.json (actively acquiring) is
    never deletable;
  * the NEWEST run on disk (by mtime) is never deletable — between runs the
    state file may already point at the next run while this one still has
    files in flight;
  * a run with any subrun directory missing its .subrun_complete marker is
    never deletable (possibly still being written / crashed mid-subrun).

Nothing here trusts a caller-supplied verdict: delete_run() re-runs the full
verification itself immediately before it removes anything, and refuses any
path that is not a plain run directory directly under the runs root.

All locations come from config/backup_config.json (source_dir, runs_subdir,
xrootd_url, eos_dir), so this always agrees with the backup watcher about
what is backed up where. EOS access is native xrootd (xrdfs/xrdcp) — the
legacy FUSE mount is not used anywhere.
"""

import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# Repo root = parent of flask_app/ (this module lives in flask_app/).
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# banco has no CERN realm in its system krb5.conf and xrdfs/xrdcp live in
# ~/bin — same environment fixes backup_watcher.py applies for itself.
os.environ.setdefault('KRB5_CONFIG', os.path.join(REPO_DIR, 'config', 'krb5_cern.conf'))
os.environ['PATH'] = os.path.expanduser('~/bin') + os.pathsep + os.environ.get('PATH', '')

BACKUP_CONFIG_PATH = os.path.join(REPO_DIR, 'config', 'backup_config.json')
CURRENT_RUN_STATE  = os.path.join(REPO_DIR, 'config', 'current_run_state.json')
DELETE_LOG         = os.path.join(REPO_DIR, 'logs', 'space_manager.log')

RUN_NAME_RE = re.compile(r'^run_\d+$')

# Single managed disk. The label/root/fs are resolved from backup_config.json
# at call time (see _cfg) so a regenerated config is picked up without a
# restart; only the key + label live here.
DISKS = {
    'data': {'label': 'Data disk (runs)'},
}


# --- Config ----------------------------------------------------------------

def _cfg():
    """(runs_root, fs_path, xrootd_url, eos_runs_dir) from the backup watcher's
    config — the one source of truth for what is backed up where."""
    with open(BACKUP_CONFIG_PATH) as f:
        cfg = json.load(f)
    source_dir = Path(cfg['source_dir'])
    runs_root = source_dir / cfg.get('runs_subdir', 'runs')
    url = cfg.get('xrootd_url', 'root://eospublic.cern.ch').rstrip('/')
    eos_runs = str(Path(cfg['eos_dir']) / cfg.get('runs_subdir', 'runs'))
    return runs_root, str(source_dir), url, eos_runs


def _runs_root() -> Path:
    return _cfg()[0]


# --- Size maps -------------------------------------------------------------

def _local_size_map(root: Path) -> dict:
    """{relpath: size} for every regular file under root — the FULL tree, so
    processing outputs (QA events.json, ROOT files) and dotfile markers are all
    included, matching what backup_watcher syncs (same rglob)."""
    out = {}
    for f in root.rglob('*'):
        try:
            if f.is_file() and not f.is_symlink():
                out[f.relative_to(root).as_posix()] = f.stat().st_size
        except OSError:
            pass
    return out


def _remote_size_map(eos_dir: str):
    """{relpath: size} for every file under eos_dir on EOS via native xrdfs,
    or None on a listing error (so the caller can treat 'could not verify' as
    NOT safe).

    An absent directory lists cleanly as empty ({}), which correctly reads as
    'nothing backed up'. A genuine xrdfs failure (auth, network) returns None.
    Parses `xrdfs <url> ls -l -R` lines the same way backup_watcher does:
    '<flags> <owner> <group> <size> <date> <time> <path>'.
    """
    _, _, url, _ = _cfg()
    try:
        result = subprocess.run(
            ['xrdfs', url, 'ls', '-l', '-R', eos_dir],
            capture_output=True, text=True,
        )
    except OSError:
        return None   # xrdfs not installed / not on PATH -> cannot verify
    if result.returncode != 0:
        err = (result.stderr or '').lower()
        if 'not found' in err or 'no such file' in err or '3011' in err:
            return {}
        return None
    base = eos_dir.rstrip('/') + '/'
    sizes = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 7 or parts[0].startswith('d'):
            continue
        try:
            size = int(parts[3])
        except ValueError:
            continue
        path = parts[-1]
        if path.startswith(base):
            sizes[path[len(base):]] = size
    return sizes


# --- Helpers ---------------------------------------------------------------

def _run_num(name: str) -> int:
    m = re.search(r'(\d+)', name)
    return int(m.group(1)) if m else -1


def active_run() -> str:
    """Name of the run currently being acquired (never deletable), or ''."""
    try:
        with open(CURRENT_RUN_STATE) as f:
            return json.load(f).get('run_name', '') or ''
    except Exception:
        return ''


def newest_run() -> str:
    """Name of the run dir with the most recent mtime (never deletable — it may
    still be receiving files even if the state file already points elsewhere),
    or ''."""
    newest, newest_t = '', -1.0
    try:
        for p in _runs_root().iterdir():
            if p.is_dir() and RUN_NAME_RE.match(p.name):
                try:
                    t = p.stat().st_mtime
                except OSError:
                    continue
                if t > newest_t:
                    newest, newest_t = p.name, t
    except OSError:
        pass
    return newest


def incomplete_subruns(run_root: Path) -> list:
    """Subrun dirs under run_root missing their .subrun_complete marker
    (daq_control writes it when a subrun finishes cleanly). A run with any
    incomplete subrun may still be mid-write — never deletable."""
    out = []
    try:
        for sub in sorted(run_root.iterdir()):
            if sub.is_dir() and not (sub / '.subrun_complete').is_file():
                out.append(sub.name)
    except OSError:
        pass
    return out


def _dir_size(root: Path) -> int:
    total = 0
    for f in root.rglob('*'):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def human(n: int) -> str:
    f = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(f) < 1024 or unit == 'TB':
            return f"{f:.1f} {unit}" if unit != 'B' else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def disk_usage() -> dict:
    """Free/used/total for the data filesystem."""
    out = {}
    try:
        _, fs_path, _, _ = _cfg()
    except Exception as e:
        return {'data': {'label': DISKS['data']['label'], 'error': str(e)}}
    for key, d in DISKS.items():
        try:
            u = shutil.disk_usage(fs_path)
            out[key] = {'label': d['label'], 'fs': fs_path,
                        'total': u.total, 'used': u.used, 'free': u.free,
                        'pct': round(100.0 * u.used / u.total, 1) if u.total else 0.0}
        except OSError as e:
            out[key] = {'label': d['label'], 'fs': fs_path, 'error': str(e)}
    return out


# --- Verification ----------------------------------------------------------

def verify_run(disk: str, run: str) -> dict:
    """Compare a local run against EOS, file by file (relpath + size) over the
    complete run tree — raw subrun data, processing outputs, loose files and
    markers alike."""
    runs_root, _, _, eos_runs = _cfg()
    root = runs_root / run
    res = {'run': run, 'disk': disk, 'size': 0, 'files': 0,
           'ok': 0, 'missing': 0, 'mismatch': 0,
           'safe': False, 'reason': '', 'unverifiable': False}
    if not root.is_dir():
        res['reason'] = 'run directory not found locally'
        return res
    local = _local_size_map(root)
    res['files'] = len(local)
    res['size'] = sum(local.values())
    # Compare against the EOS copy of THIS run.
    remote = _remote_size_map(f"{eos_runs}/{run}")
    if remote is None:
        res['unverifiable'] = True
        res['reason'] = 'could not list run on EOS (Kerberos/network?) — NOT safe'
        return res
    missing = mismatch = ok = 0
    for rel, sz in local.items():
        rsz = remote.get(rel)
        if rsz == sz:
            ok += 1
        elif rsz is None:
            missing += 1
        else:
            mismatch += 1
    res.update(ok=ok, missing=missing, mismatch=mismatch)
    if missing == 0 and mismatch == 0 and ok == len(local) and len(local) > 0:
        res['safe'] = True
        res['reason'] = f'all {ok} files verified on EOS'
    elif len(local) == 0:
        res['reason'] = 'run directory is empty'
    else:
        res['reason'] = f'{missing} missing + {mismatch} size-mismatched on EOS'
    return res


def _apply_local_guards(v: dict, run: str, act: str, newest: str) -> dict:
    """Downgrade a verify verdict for runs that must never be deleted no matter
    what EOS says: the active run, the newest run on disk, and runs with
    incomplete subruns."""
    v['active'] = (run == act)
    v['newest'] = (run == newest)
    if v['active']:
        v['safe'] = False
        v['reason'] = 'currently acquiring — never deletable while active'
        return v
    if v['newest']:
        v['safe'] = False
        v['reason'] = 'newest run on disk (possibly still being written) — refusing'
        return v
    inc = incomplete_subruns(_runs_root() / run)
    if inc:
        v['safe'] = False
        v['reason'] = (f'{len(inc)} subrun(s) missing .subrun_complete '
                       f'(possibly mid-write) — refusing')
    return v


# --- Scan ------------------------------------------------------------------

def list_runs(disk: str) -> list:
    root = _runs_root()
    if not root.is_dir():
        return []
    runs = [p.name for p in root.iterdir() if p.is_dir() and RUN_NAME_RE.match(p.name)]
    return sorted(runs, key=_run_num)


def scan(disk: str, runs=None) -> dict:
    """Verify every run (or a subset); return per-run verdicts."""
    if disk not in DISKS:
        raise ValueError(f'unknown disk {disk!r}')
    names = runs if runs else list_runs(disk)
    act = active_run()
    newest = newest_run()
    results = []
    for run in names:
        v = verify_run(disk, run)
        v = _apply_local_guards(v, run, act, newest)
        v['size_h'] = human(v.get('size', 0))
        results.append(v)
    safe_bytes = sum(r['size'] for r in results if r['safe'])
    return {
        'disk': disk, 'label': DISKS[disk]['label'],
        'runs': results,
        'n_runs': len(results),
        'n_safe': sum(1 for r in results if r['safe']),
        'safe_bytes': safe_bytes, 'safe_bytes_h': human(safe_bytes),
        'active_run': act,
        'usage': disk_usage().get(disk, {}),
        'scanned_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# --- Delete ----------------------------------------------------------------

def _log_delete(msg: str):
    try:
        os.makedirs(os.path.dirname(DELETE_LOG), exist_ok=True)
        with open(DELETE_LOG, 'a') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")
    except Exception:
        pass


def delete_run(disk: str, run: str) -> dict:
    """Delete one run directory, but ONLY after re-verifying, here, that it is
    safe. Never trusts a caller verdict.

    Guards, in order:
      1. disk is known; run matches ^run_\\d+$.
      2. target resolves to a real directory sitting DIRECTLY under the runs
         root (no symlinks, no traversal, no partial-name tricks).
      3. run is not the active run, not the newest run on disk, and has no
         subrun missing its .subrun_complete marker.
      4. a fresh verify_run() says SAFE (every file on EOS at matching size).
    """
    if disk not in DISKS:
        return {'success': False, 'message': f'unknown disk {disk!r}'}
    if not RUN_NAME_RE.match(run or ''):
        return {'success': False, 'message': f'invalid run name {run!r}'}

    root = _runs_root().resolve()
    target = _runs_root() / run
    try:
        rtarget = target.resolve()
    except OSError as e:
        return {'success': False, 'message': f'cannot resolve path: {e}'}
    if target.is_symlink():
        return {'success': False, 'message': 'refusing to delete a symlink'}
    if not rtarget.is_dir():
        return {'success': False, 'message': f'{run} is not a directory on {disk}'}
    if rtarget.parent != root or rtarget == root:
        return {'success': False, 'message': 'path is not a run directly under the runs root'}

    verdict = verify_run(disk, run)
    verdict = _apply_local_guards(verdict, run, active_run(), newest_run())
    if not verdict['safe']:
        _log_delete(f"REFUSED {disk}/{run}: {verdict['reason']}")
        return {'success': False, 'message': f"not safe to delete: {verdict['reason']}",
                'verdict': verdict}

    size = _dir_size(rtarget)
    try:
        shutil.rmtree(rtarget)
    except Exception as e:
        _log_delete(f"ERROR deleting {disk}/{run}: {e}")
        return {'success': False, 'message': f'delete failed: {e}'}

    _log_delete(f"DELETED {disk}/{run}  freed={human(size)}  ({verdict['reason']})")
    return {'success': True, 'run': run, 'disk': disk,
            'freed_bytes': size, 'freed_h': human(size),
            'message': f'Deleted {disk}/{run}, freed {human(size)}'}


def delete_runs(disk: str, runs: list) -> dict:
    """Delete several runs; each is independently re-verified. Stops nothing on
    a single failure — reports per-run outcomes."""
    results = []
    freed = 0
    for run in runs:
        r = delete_run(disk, run)
        results.append(r)
        if r.get('success'):
            freed += r.get('freed_bytes', 0)
    return {'results': results, 'freed_bytes': freed, 'freed_h': human(freed),
            'n_deleted': sum(1 for r in results if r.get('success')),
            'n_failed': sum(1 for r in results if not r.get('success'))}


# --- Restore (EOS -> local) -------------------------------------------------
# The inverse of delete: pull a run back from EOS onto the local data disk.
# EOS mirrors the local layout, so restore targets the same runs root. Only
# files missing or size-mismatched locally are fetched (xrdcp -f), so it is
# idempotent and cheap to re-run — exactly the reverse of the backup sync.

def _xrdcp_download(eos_file: str, local_path: Path):
    """Copy one file EOS -> local via native xrdcp. Returns (ok, stderr)."""
    _, _, url, _ = _cfg()
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, str(e)
    src = f"{url}//{eos_file.lstrip('/')}"
    try:
        r = subprocess.run(['xrdcp', '-f', '--nopbar', src, str(local_path)],
                           capture_output=True, text=True)
    except OSError as e:
        return False, f'xrdcp not available: {e}'
    return (r.returncode == 0), (r.stderr or '').strip()


def list_eos_runs():
    """Sorted run_N names present on EOS, or None if the listing failed."""
    _, _, url, eos_runs = _cfg()
    try:
        r = subprocess.run(['xrdfs', url, 'ls', eos_runs], capture_output=True, text=True)
    except OSError:
        return None   # xrdfs not installed / not on PATH
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        name = line.rstrip('/').rsplit('/', 1)[-1]
        if RUN_NAME_RE.match(name):
            out.append(name)
    return sorted(out, key=_run_num)


def scan_restore() -> dict:
    """List every run on EOS and, for each, how it compares to the local disk:
    complete (already local), partial, or missing. 'To fetch' is the bytes that
    would be pulled (files absent or size-mismatched locally)."""
    runs = list_eos_runs()
    if runs is None:
        raise RuntimeError('could not list runs on EOS (Kerberos/network?)')
    act = active_run()
    runs_root, _, _, eos_runs = _cfg()
    results = []
    fetch_total = 0
    for run in runs:
        remote = _remote_size_map(f"{eos_runs}/{run}")
        r = {'run': run, 'disk': 'data', 'active': run == act}
        if remote is None:
            r.update(status='error', restorable=False, eos_bytes=0, size_h='—',
                     total=0, have=0, fetch_files=0, fetch_bytes=0, fetch_h='—')
            results.append(r)
            continue
        eos_bytes = sum(remote.values())
        total = len(remote)
        local_root = runs_root / run
        local = _local_size_map(local_root) if local_root.is_dir() else {}
        have = fetch_bytes = 0
        for rel, sz in remote.items():
            if local.get(rel) == sz:
                have += 1
            else:
                fetch_bytes += sz
        fetch_files = total - have
        status = 'complete' if fetch_files == 0 else ('missing' if not local else 'partial')
        restorable = fetch_files > 0 and not r['active']
        r.update(status=status, restorable=restorable, eos_bytes=eos_bytes,
                 size_h=human(eos_bytes), total=total, have=have,
                 fetch_files=fetch_files, fetch_bytes=fetch_bytes, fetch_h=human(fetch_bytes))
        if restorable:
            fetch_total += fetch_bytes
        results.append(r)
    return {
        'runs': results, 'n_runs': len(results),
        'n_restorable': sum(1 for r in results if r['restorable']),
        'fetch_bytes_total': fetch_total, 'fetch_bytes_total_h': human(fetch_total),
        'active_run': act, 'usage': disk_usage().get('data', {}),
        'scanned_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def restore_run(run: str) -> dict:
    """Pull one run from EOS onto the local disk. Copies only files missing or
    size-mismatched locally. Refuses the active run (would clobber live writes)
    and aborts if the disk lacks free space for the fetch."""
    res = {'run': run, 'disk': 'data', 'success': False, 'restored_files': 0,
           'fetched_bytes': 0, 'fetched_h': '0 B', 'message': ''}
    if not RUN_NAME_RE.match(run or ''):
        res['message'] = f'invalid run name {run!r}'
        return res
    if run == active_run():
        res['message'] = f'{run} is the active run — refusing'
        return res
    runs_root, fs_path, _, eos_runs = _cfg()
    eos_run = f"{eos_runs}/{run}"
    remote = _remote_size_map(eos_run)
    if remote is None:
        res['message'] = 'could not list run on EOS (Kerberos/network?)'
        return res
    if not remote:
        res['message'] = 'run not found on EOS'
        return res

    local_root = runs_root / run
    to_fetch = []
    for rel, sz in remote.items():
        lp = local_root / rel
        try:
            match = lp.is_file() and lp.stat().st_size == sz
        except OSError:
            match = False
        if not match:
            to_fetch.append((rel, sz))

    need = sum(sz for _, sz in to_fetch)
    if need == 0:
        res['success'] = True
        res['message'] = 'already complete locally (nothing to fetch)'
        return res

    try:
        free = shutil.disk_usage(fs_path).free
    except OSError:
        free = None
    MARGIN = 5 * 1024 ** 3   # keep 5 GB headroom on the disk
    if free is not None and need > free - MARGIN:
        res['message'] = f'not enough free space: need {human(need)}, have {human(free)}'
        return res

    fetched = nfiles = 0
    failed = []
    for rel, sz in to_fetch:
        ok, err = _xrdcp_download(f"{eos_run}/{rel}", local_root / rel)
        if ok:
            fetched += sz
            nfiles += 1
        else:
            failed.append(rel)
    res.update(restored_files=nfiles, fetched_bytes=fetched, fetched_h=human(fetched))
    if failed:
        res['success'] = False
        res['message'] = f'{len(failed)} file(s) failed to copy; {nfiles} restored'
        _log_delete(f"RESTORE partial data/{run}: {nfiles} ok, {len(failed)} failed")
    else:
        res['success'] = True
        res['message'] = f'restored {nfiles} files ({human(fetched)})'
        _log_delete(f"RESTORED data/{run}: {nfiles} files, {human(fetched)}")
    return res


def restore_runs(runs: list) -> dict:
    """Restore several runs; each independent. Reports per-run outcomes."""
    results = []
    fetched = 0
    for run in runs:
        r = restore_run(run)
        results.append(r)
        if r.get('success'):
            fetched += r.get('fetched_bytes', 0)
    return {'results': results, 'fetched_bytes': fetched, 'fetched_h': human(fetched),
            'n_restored': sum(1 for r in results if r.get('success')),
            'n_failed': sum(1 for r in results if not r.get('success'))}


if __name__ == '__main__':
    out = scan('data')
    u = out['usage']
    if u and not u.get('error'):
        print(f"{out['label']}: {human(u.get('free', 0))} free of {human(u.get('total', 0))} "
              f"({u.get('pct', 0)}% used)")
    print(f"{'RUN':10} {'SIZE':>10}  {'SAFE':>5}  REASON")
    print('-' * 78)
    for r in out['runs']:
        print(f"{r['run']:10} {r['size_h']:>10}  {'YES' if r['safe'] else 'no':>5}  {r['reason']}")
    print('-' * 78)
    print(f"{out['n_safe']}/{out['n_runs']} runs safe to delete — "
          f"would free {out['safe_bytes_h']}")
