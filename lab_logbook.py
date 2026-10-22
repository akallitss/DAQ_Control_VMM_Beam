#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Logbook for the lab campaign: what the DAQ knows plus what only a person knows.

Two sources, one page (<base_data_dir>/logbook/LOGBOOK.md, travels with the data):

  AUTOMATIC  one block per run, read from runs/<run>/run_config.json and the
             run tree: start time, chip config applied, gas, cables, sub-runs
             (complete or not), capture volume and files, hits per VMM when the
             run has a RUN_QUALITY.json.
  MANUAL     entries you add here, appended to logbook/entries.jsonl and merged
             by time: notes, and the measured V and I of the six HV channels at
             the start and end of a run. Nothing in the DAQ sets or reads this
             HV, so these entries ARE its record. A value left out is shown as
             missing, never guessed.

Usage (from the repo, with the venv python):
    lab_logbook.py note "moved the scintillator, det2 gas flow 2 l/h"
    lab_logbook.py hv --when start            # prompts for the six channels
    lab_logbook.py hv --when end --run run_3 --set det3_drift=710,0.02 --set det3_mesh=410,0.0
    lab_logbook.py render                     # rebuild LOGBOOK.md once
    lab_logbook.py watch [--interval 60]      # keep it up to date (tmux vmm_logbook)

--run defaults to the run in config/current_run_state.json. V in volts, I in uA.
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from run_config_beam import BASE_DATA_DIR  # noqa: E402

LOG_DIR = os.path.join(BASE_DATA_DIR, 'logbook')
ENTRIES = os.path.join(LOG_DIR, 'entries.jsonl')
OUT = os.path.join(LOG_DIR, 'LOGBOOK.md')
STATE = os.path.join(REPO, 'config', 'current_run_state.json')

# chamber -> (station, channels as wired 2026-10-22)
HV_CHANNELS = [
    ('det3_drift', 'det3 (P2_IN)  drift  CAEN 4ch ch0'),
    ('det3_mesh',  'det3 (P2_IN)  mesh   CAEN 4ch ch1'),
    ('det2_mesh',  'det2 (P2_MID) mesh   rack HV ch0'),
    ('det2_drift', 'det2 (P2_MID) drift  rack HV ch1'),
    ('det1_drift', 'det1 (P2_OUT) drift  CAEN 4ch ch3'),
    ('det1_mesh',  'det1 (P2_OUT) mesh   CAEN 4ch ch4'),
]


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def current_run():
    try:
        return json.load(open(STATE)).get('run_name')
    except Exception:
        return None


def add_entry(kind, **kw):
    os.makedirs(LOG_DIR, exist_ok=True)
    e = {'ts': now(), 'kind': kind, **kw}
    with open(ENTRIES, 'a') as f:
        f.write(json.dumps(e) + '\n')
    render()
    return e


def read_entries():
    out = []
    try:
        for ln in open(ENTRIES):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def run_names():
    def key(p):
        n = os.path.basename(p)
        try:
            return int(n.rsplit('_', 1)[1])
        except Exception:
            return 10**9
    return [os.path.basename(p) for p in sorted(
        glob.glob(os.path.join(BASE_DATA_DIR, 'runs', 'run_*')), key=key)
        if os.path.isdir(p)]


def run_block(run):
    rdir = os.path.join(BASE_DATA_DIR, 'runs', run)
    try:
        cfg = json.load(open(os.path.join(rdir, 'run_config.json')))
    except Exception:
        return ['(no run_config.json yet)']
    subs = cfg.get('sub_runs') or []
    done = [s['sub_run_name'] for s in subs
            if os.path.exists(os.path.join(rdir, s['sub_run_name'], '.subrun_complete'))]
    nbytes = nfiles = 0
    for dp, _, fns in os.walk(rdir):
        for f in fns:
            if f.endswith('.pcapng'):
                nbytes += os.path.getsize(os.path.join(dp, f))
                nfiles += 1
    cab = sorted({c.get('cable') for d in cfg.get('detectors', [])
                  for c in d.get('vmm_map', {}).get('enp4s0f1', {}).get('connectors', {}).values()})
    ch = {d['name']: d.get('chamber') for d in cfg.get('detectors', [])}
    lines = [
        f"- started {cfg.get('start_time')}; plan `{cfg.get('run_plan')}`; measuring {cfg.get('beam_type')}",
        f"- chip config: `{cfg.get('chip_config')}`",
        f"- gas: {cfg.get('gas')}; cables: {', '.join(c for c in cab if c)}; chambers: "
        + ', '.join(f'{k}={v}' for k, v in ch.items()),
        f"- sub-runs: {len(done)}/{len(subs)} complete "
        f"({subs[0]['run_time'] if subs else '?'} min each); {nfiles} capture files, {nbytes / 1e9:.2f} GB",
        f"- DAQ code: {(cfg.get('daq_code_version') or {}).get('commit', cfg.get('daq_code_version'))}",
    ]
    try:
        q = json.load(open(os.path.join(rdir, 'RUN_QUALITY.json')))
        hpv = q.get('hits_per_vmm') or {}
        if hpv:
            top = sorted(hpv.items(), key=lambda kv: -kv[1])[:4]
            lines.append(f"- hits {q.get('hits_total'):,}; busiest VMMs: "
                         + ', '.join(f'{k}: {v:,}' for k, v in top))
    except Exception:
        pass
    return lines


def render():
    os.makedirs(LOG_DIR, exist_ok=True)
    entries = read_entries()
    by_run = {}
    free = []
    for e in entries:
        (by_run.setdefault(e['run'], []) if e.get('run') else free).append(e)

    def fmt(e):
        if e['kind'] == 'hv':
            vals = e.get('values', {})
            rows = []
            for k, label in HV_CHANNELS:
                v = vals.get(k)
                rows.append(f"    - {label}: " + (f"{v['V']} V, {v['I_uA']} uA" if v else 'NOT RECORDED'))
            return f"- {e['ts']} **HV {e.get('when', '?')}**\n" + '\n'.join(rows)
        return f"- {e['ts']} {e.get('text', '')}"

    out = ['# Lab logbook — cosmic-muon threshold scan', '',
           f'Rebuilt {now()} from `{BASE_DATA_DIR}`. Manual entries: `logbook/entries.jsonl`. '
           'HV is NOT read by the DAQ: only the HV entries below record it.', '']
    for run in run_names():
        out += [f'## {run}', ''] + run_block(run) + ['']
        if by_run.get(run):
            out += ['Entries:'] + [fmt(e) for e in by_run[run]] + ['']
        else:
            out += ['Entries: none — no HV V/I written down for this run yet.', '']
    orphan = [e for r, es in by_run.items() if r not in run_names() for e in es]
    if free or orphan:
        out += ['## Other notes', ''] + [fmt(e) for e in sorted(free + orphan, key=lambda e: e['ts'])] + ['']
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(out))
    os.replace(tmp, OUT)


def cmd_hv(a):
    vals = {}
    given = dict(s.split('=', 1) for s in (a.set or []))
    for k, label in HV_CHANNELS:
        if k in given:
            raw = given[k]
        elif a.set:
            continue                      # non-interactive: only what was given
        else:
            raw = input(f'{label}  "V,uA" (blank = skip): ').strip()
        if not raw:
            continue
        v, i = (x.strip() for x in raw.split(','))
        vals[k] = {'V': float(v), 'I_uA': float(i)}
    run = a.run or current_run()
    e = add_entry('hv', run=run, when=a.when, values=vals)
    print(f'logged HV {a.when} for {run}: {len(vals)}/{len(HV_CHANNELS)} channels')
    return e


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    n = sub.add_parser('note')
    n.add_argument('text')
    n.add_argument('--run')
    h = sub.add_parser('hv')
    h.add_argument('--when', choices=['start', 'end'], required=True)
    h.add_argument('--run')
    h.add_argument('--set', action='append', help='det3_drift=710,0.02 (V,uA); repeatable')
    sub.add_parser('render')
    w = sub.add_parser('watch')
    w.add_argument('--interval', type=int, default=60)
    a = ap.parse_args()

    if a.cmd == 'note':
        add_entry('note', run=a.run or current_run(), text=a.text)
        print(f'noted; see {OUT}')
    elif a.cmd == 'hv':
        cmd_hv(a)
    elif a.cmd == 'render':
        render()
        print(OUT)
    else:
        print(f'watching; writing {OUT} every {a.interval} s', flush=True)
        while True:
            try:
                render()
            except Exception as e:
                print(f'render failed: {e}', flush=True)
            time.sleep(a.interval)


if __name__ == '__main__':
    main()
