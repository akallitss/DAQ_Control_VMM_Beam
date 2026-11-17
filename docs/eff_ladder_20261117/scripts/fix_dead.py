#!/usr/bin/env python3
"""Give every chunk of a run the RUN-level dead/masked list (pads with no hit in ANY chunk of the run), i.e. what
efficiency_tp.py (--min-hits 1) would have found on the whole run. A pad silent in one chunk only is live there.
Originals are kept as eff_runN.json.orig. Usage: fix_dead.py PSEUDO_DIR TAGS_DIR"""
import json
import os
import shutil
import sys

pseudo, tags = sys.argv[1:3]
chunks = json.load(open(f'{pseudo}/chunks.json'))
by_run = {}
for pid, c in chunks.items():
    by_run.setdefault(c['run'], []).append(pid)
for run, pids in sorted(by_run.items()):
    have = [p for p in pids if os.path.exists(f'{tags}/eff_run{p}.json')]
    if len(have) != len(pids):
        print(f'run_{run}: {len(pids) - len(have)} chunks missing, skipped')
        continue
    for p in have:
        if not os.path.exists(f'{tags}/eff_run{p}.json.orig'):
            shutil.copy2(f'{tags}/eff_run{p}.json', f'{tags}/eff_run{p}.json.orig')
    J = {p: json.load(open(f'{tags}/eff_run{p}.json.orig')) for p in have}
    keys = J[have[0]]['dead_or_masked'].keys()
    inter = {k: set.intersection(*(set(J[p]['dead_or_masked'][k]) for p in have)) for k in keys}
    per = [sum(len(v) for v in J[p]['dead_or_masked'].values()) for p in have]
    for p in have:
        J[p]['dead_or_masked'] = {k: sorted(inter[k], key=lambda s: tuple(map(int, s.split(':')))) for k in keys}
        J[p]['dead_or_masked_note'] = 'run-level (intersection over the chunks of run_%d), fix_dead.py' % run
        json.dump(J[p], open(f'{tags}/eff_run{p}.json', 'w'), indent=1)
    print(f'run_{run}: {len(have)} chunks, dead/masked per chunk {min(per)}-{max(per)} -> run-level '
          f'{ {k.split()[1]: len(v) for k, v in inter.items()} }')
