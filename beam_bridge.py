#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beam-state bridge (banco side of the SPS beam monitor).

NXCALS only runs on the CERN network, so the beam-intensity watcher runs on
lxplus and publishes beam_state.json + per-day CSVs to EOS. banco (on the CEA
network) cannot reach NXCALS but CAN reach EOS via xrootd — this bridge pulls
the published files down every poll so the Flask GUI's /beam tab reads a fresh
state exactly as if a local watcher wrote it.

    lxplus  ── NXCALS ──►  /eos/.../beam_monitor/{beam_state.json, beam_intensity_*.csv}
    banco   ── xrdcp ────►  <repo>/config/beam_state.json  +  BEAM_LOG_DIR/*.csv

Runs in the 'beam_watcher' tmux session (GUI "Start Beam Watcher" button).
Uses the same ~/bin/xrdcp + Kerberos setup as backup_watcher.py.

Config: env vars (all optional)
  SPS_BEAM_EOS_URL   xrootd endpoint         (default root://eosproject.cern.ch)
  SPS_BEAM_EOS_DIR   EOS beam_monitor dir    (default the salsachip path below)
  SPS_BEAM_POLL_S    seconds between pulls    (default 20)
  SPS_BEAM_STALE_S   mark beam data stale after this many s without an EOS update
"""

import os
import sys
import json
import time
import subprocess
import datetime

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
# Point kinit/xrdcp at the repo CERN krb5 config and ~/bin (same as backup_watcher).
os.environ.setdefault('KRB5_CONFIG', os.path.join(_REPO_DIR, 'config', 'krb5_cern.conf'))
os.environ['PATH'] = os.path.expanduser('~/bin') + os.pathsep + os.environ.get('PATH', '')

sys.path.insert(0, _REPO_DIR)
from beam_monitor.beam_intensity_controller import BEAM_STATE_PATH, BEAM_LOG_DIR, BEAM_UNIT
from sps_monitor.sps_spill_controller import SPS_STATE_PATH, SPS_LOG_DIR, SPS_UNIT

# User EOS, not the salsachip project space: the project quota filled up on
# 2026-07-25 (runs backup + epic_tests ~= the whole 1 TB) and blocked even the
# 9 KB beam_state.json, freezing the feed. The user space has its own 2 TB
# quota, so the beam feed no longer shares fate with the run backups. Must
# match EOS_BEAM_DIR in beam_monitor/lxplus_beam_watcher.sh (the publisher).
EOS_URL = os.environ.get('SPS_BEAM_EOS_URL', 'root://eosuser.cern.ch')
EOS_DIR = os.environ.get('SPS_BEAM_EOS_DIR',
                         '/eos/user/a/akallits/beam_monitor')
POLL_S = float(os.environ.get('SPS_BEAM_POLL_S', 20))
STALE_S = float(os.environ.get('SPS_BEAM_STALE_S', 300))
KINIT_INTERVAL = 3600


def _xrdcp(remote_name, local_path):
    """Copy EOS_DIR/remote_name -> local_path. Returns True on success."""
    url = f'{EOS_URL}/{EOS_DIR}/{remote_name}'
    tmp = local_path + '.part'
    r = subprocess.run(['xrdcp', '-f', '-s', url, tmp], capture_output=True, text=True)
    if r.returncode == 0 and os.path.isfile(tmp):
        os.replace(tmp, local_path)
        return True
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def _eos_ls():
    """{filename: size} for the EOS beam_monitor dir, or {} if it can't be listed.

    `ls -l` in one round trip rather than a stat per file: the archive is a few
    hundred files and a stat each would take longer than the transfers.
    """
    r = subprocess.run(['xrdfs', EOS_URL, 'ls', '-l', EOS_DIR],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f'[beam_bridge] xrdfs ls failed: {r.stderr.strip()}', flush=True)
        return {}
    out = {}
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        # The only all-digit field is the size: the date carries dashes and the
        # time colons, so neither can be mistaken for it.
        sizes = [f for f in fields[:-1] if f.isdigit()]
        out[os.path.basename(fields[-1])] = int(sizes[-1]) if sizes else None
    return out


# Which local directory each published file belongs in. Anything not matching is
# ignored rather than dumped somewhere arbitrary.
_CATCHUP_PREFIXES = (
    ('beam_intensity_', lambda: BEAM_LOG_DIR),
    ('sps_spill_', lambda: SPS_LOG_DIR),
    ('sps_profile_', lambda: SPS_LOG_DIR),
)


# How many backlog files to pull per poll. The catch-up MUST NOT delay the live
# state: this ran as one blocking pass at startup on 2026-07-27 and froze
# beam_state.json for the ~35 min the 376 MB profile archive took to copy, so the
# GUI correctly reported the beam state stale while the feed itself was healthy.
# A few files per 20 s poll drains a day's backlog in minutes and costs the live
# pull nothing.
CATCHUP_PER_POLL = int(os.environ.get('SPS_BEAM_CATCHUP_PER_POLL', 3))


def _catch_up_plan():
    """Historical log files EOS has that banco does not, newest first.

    The steady-state loop deliberately pulls only the current day/hour — that is
    all a live GUI needs, and re-copying the whole archive every 20 s would be
    absurd. But it means anything published while this bridge was not running is
    never fetched, including a backfill run on lxplus after the fact. So the
    backlog is computed once at startup and then drained a few files at a time.

    Files are compared by name and size: a local file smaller than the remote one
    is a partial day (or the exact case of a backfill extending a day the watcher
    only half-covered), so it is re-pulled. Equal size means done.

    Newest first, because if the backlog is long the recent history is the part
    someone is most likely to want plotted while the rest trickles in.
    """
    remote = _eos_ls()
    if not remote:
        return []
    local_sizes = {}
    for prefix, dirfn in _CATCHUP_PREFIXES:
        d = dirfn()
        try:
            for fn in os.listdir(d):
                if fn.startswith(prefix):
                    local_sizes[fn] = os.path.getsize(os.path.join(d, fn))
        except OSError:
            pass

    plan = []
    for name, remote_size in remote.items():
        target_dir = next((dirfn() for prefix, dirfn in _CATCHUP_PREFIXES
                           if name.startswith(prefix)), None)
        if target_dir is None:
            continue
        have = local_sizes.get(name)
        if have is not None and remote_size is not None and have >= remote_size:
            continue
        plan.append((name, os.path.join(target_dir, name)))
    plan.sort(key=lambda p: p[0], reverse=True)
    return plan


def _drain_catch_up(plan):
    """Pull up to CATCHUP_PER_POLL backlog files. Mutates and returns `plan`."""
    for _ in range(min(CATCHUP_PER_POLL, len(plan))):
        name, dest = plan.pop(0)
        if _xrdcp(name, dest):
            print(f'[beam_bridge] catch-up: {name} ({len(plan)} left)', flush=True)
        else:
            # Leave it out of the plan rather than retrying forever; the next
            # bridge restart recomputes the backlog and will pick it up again.
            print(f'[beam_bridge] catch-up FAILED: {name} ({len(plan)} left)', flush=True)
    return plan


def _write_waiting_state(msg):
    """Publish a 'no beam data' state so the GUI shows a clear status, not stale."""
    state = {
        'connected': False,
        'beam_on': None,
        'unit': BEAM_UNIT,
        'last_error': msg,
        'source': 'bridge',
        'updated': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    os.makedirs(os.path.dirname(BEAM_STATE_PATH), exist_ok=True)
    with open(BEAM_STATE_PATH, 'w') as f:
        json.dump(state, f)


def _write_waiting_sps_state(msg):
    """Same, for the SPS spill state behind the Beam2 tab.

    spill_on and h4_open go to None rather than False: a bridge that cannot
    reach EOS knows nothing about the beam line, and rendering that as "spill
    off / line closed" would be a claim we cannot support.
    """
    state = {
        'connected': False,
        'spill_on': None,
        'h4_open': None,
        'unit': SPS_UNIT,
        'last_error': msg,
        'source': 'bridge',
        'updated': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    os.makedirs(os.path.dirname(SPS_STATE_PATH), exist_ok=True)
    with open(SPS_STATE_PATH, 'w') as f:
        json.dump(state, f)


def _state_age_s(path):
    """Age in seconds of the state we just pulled, from ITS OWN timestamp.

    A successful xrdcp is not evidence of live beam data: if the lxplus watcher
    dies (or its Kerberos ticket expires) the same frozen beam_state.json keeps
    copying down forever, and the local file's mtime keeps looking fresh. Only
    the payload timestamp can tell. Returns None if it can't be read."""
    try:
        with open(path) as f:
            state = json.load(f)
        stamp = state.get('timestamp') or state.get('updated')
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(stamp)).total_seconds()
    except Exception:
        return None


def _refresh_kerberos():
    subprocess.run(['kinit', '-R'], capture_output=True)  # renew; ignore failure


def _pull_sps(now_dt):
    """Pull the SPS spill state, today's per-cycle CSV and the fine-precision
    profile archive down from EOS.

    The profile archive is written in HOURLY gzipped files by the watcher, which
    is what makes this affordable: xrdcp has no partial transfer, so a day-long
    file would be re-copied in full on every poll. Current and previous hour are
    both pulled — the previous one because a spill that lands near the hour
    boundary is written after the rollover, so the file keeps growing for a
    short while after its hour ends.
    """
    got = _xrdcp('sps_state.json', SPS_STATE_PATH)

    day = now_dt.date().isoformat()
    csv_name = f'sps_spill_{day}.csv'
    _xrdcp(csv_name, os.path.join(SPS_LOG_DIR, csv_name))

    for dt in (now_dt, now_dt - datetime.timedelta(hours=1)):
        prof = f'sps_profile_{dt:%Y-%m-%d_%H}.jsonl.gz'
        _xrdcp(prof, os.path.join(SPS_LOG_DIR, prof))
    return got


def main():
    os.makedirs(BEAM_LOG_DIR, exist_ok=True)
    os.makedirs(SPS_LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(BEAM_STATE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(SPS_STATE_PATH), exist_ok=True)
    print(f'[beam_bridge] EOS {EOS_URL}/{EOS_DIR}', flush=True)
    print(f'[beam_bridge] -> state {BEAM_STATE_PATH}  logs {BEAM_LOG_DIR}  poll {POLL_S}s', flush=True)
    print(f'[beam_bridge] -> sps   {SPS_STATE_PATH}  logs {SPS_LOG_DIR}', flush=True)
    last_kinit = 0.0
    last_ok = None
    last_sps_ok = None
    _refresh_kerberos()
    catch_up = _catch_up_plan()
    if catch_up:
        print(f'[beam_bridge] {len(catch_up)} historical file(s) to catch up, '
              f'{CATCHUP_PER_POLL} per poll', flush=True)
    while True:
        now = time.time()
        now_dt = datetime.datetime.now()
        if now - last_kinit >= KINIT_INTERVAL:
            _refresh_kerberos()
            last_kinit = now

        got_state = _xrdcp('beam_state.json', BEAM_STATE_PATH)
        # today's CSV for the /beam/history plot (best-effort; name matches the watcher)
        day = datetime.date.today().isoformat()
        csv_name = f'beam_intensity_{day}.csv'
        _xrdcp(csv_name, os.path.join(BEAM_LOG_DIR, csv_name))

        # SPS spill + H4 feed behind the Beam2 tab. Judged for freshness exactly
        # like the beam state: a successful copy of a frozen file is not data.
        got_sps = _pull_sps(now_dt)
        sps_age = _state_age_s(SPS_STATE_PATH) if got_sps else None
        if got_sps and sps_age is not None and sps_age <= STALE_S:
            last_sps_ok = now
        elif got_sps:
            _write_waiting_sps_state(
                f'lxplus watcher last published SPS spill data {int(sps_age)}s ago '
                f'— spill and H4 line state unknown'
                if sps_age is not None else
                'sps_state.json from EOS has no usable timestamp — spill state unknown')
        elif last_sps_ok is None or now - last_sps_ok > STALE_S:
            _write_waiting_sps_state(
                'no sps_state.json on EOS yet — is the lxplus watcher running a '
                'version with the SPS spill monitor?'
                if last_sps_ok is None else
                f'sps_state.json not updated for {int(now - last_sps_ok)}s (lxplus watcher / EOS?)')

        age = _state_age_s(BEAM_STATE_PATH) if got_state else None
        if got_state and age is not None and age <= STALE_S:
            last_ok = now
        elif got_state:
            # Copy succeeded but the content is frozen (or undatable): overwrite the
            # local copy with an explicit unknown so the GUI can't render an old BEAM ON.
            _write_waiting_state(
                f'lxplus watcher last published {int(age)}s ago '
                f'— beam state unknown (watcher stopped, or its Kerberos ticket expired?)'
                if age is not None else
                'beam_state.json from EOS has no usable timestamp — beam state unknown')
        elif last_ok is None or now - last_ok > STALE_S:
            _write_waiting_state(
                'no beam_state.json on EOS yet — is the lxplus NXCALS watcher running?'
                if last_ok is None else
                f'beam_state.json not updated for {int(now - last_ok)}s (lxplus watcher / EOS?)')

        # LAST, and only a few files: everything above is what the GUI reads live,
        # and the archive backlog must never be allowed to delay it.
        if catch_up:
            catch_up = _drain_catch_up(catch_up)
            if not catch_up:
                print('[beam_bridge] catch-up complete', flush=True)
        time.sleep(POLL_S)


if __name__ == '__main__':
    main()
