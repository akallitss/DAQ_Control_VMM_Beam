#!/usr/bin/env python3
"""Split cosmics runs into pseudo-runs of at most CAP bytes of consecutive capture files, as symlink trees
pseudo/run_<run*100+k>/cfg_<same>/raw_daq_data/*.pcapng, so channel_efficiency.py dump fits in the box's RAM.
Writes chunks.json {pseudo_id: {run, cfg, files}}."""
import glob
import json
import os
import sys

RUNS = '/local/p2/p2data/cosmics_3det_202610/runs'
CAP = 85e6
out = sys.argv[1]
runs = [int(x) for x in sys.argv[2:]]
chunks = {}
for r in runs:
    cfg = sorted(glob.glob(f'{RUNS}/run_{r}/cfg_*'))[0]
    files = sorted(glob.glob(f'{cfg}/raw_daq_data/*.pcapng'))
    groups, cur, size = [], [], 0
    for f in files:
        s = os.path.getsize(f)
        if cur and size + s > CAP:
            groups.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += s
    if cur:
        groups.append(cur)
    assert len(groups) < 100
    for k, g in enumerate(groups, 1):
        pid = r * 100 + k
        d = f'{out}/run_{pid}/{os.path.basename(cfg)}/raw_daq_data'
        os.makedirs(d, exist_ok=True)
        for f in g:
            dst = f'{d}/{os.path.basename(f)}'
            if not os.path.lexists(dst):
                os.symlink(f, dst)
        chunks[pid] = dict(run=r, cfg=os.path.basename(cfg), files=len(g), bytes=sum(os.path.getsize(f) for f in g))
json.dump(chunks, open(f'{out}/chunks.json', 'w'), indent=1)
print(len(chunks), 'pseudo-runs')
for r in runs:
    print(r, sum(1 for c in chunks.values() if c['run'] == r))
