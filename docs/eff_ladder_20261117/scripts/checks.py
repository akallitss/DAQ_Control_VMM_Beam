#!/usr/bin/env python3
"""Cross-checks of the channel_efficiency.py result, per configuration and chamber.

1. Paired pads: eps per configuration on the SAME pads (pads clean and with dist >= 0 in every configuration).
2. Per chip: eps per configuration and chip (the same estimator, pads of one chip).
3. From the tags: scintillator-confirmed tags (140-310 ns) whose predicted point lies within 6 mm of a clean live pad:
   A(r) = P(any pad fires within r of the prediction) in the muon window - the same in the sideband window
   (chamber-level efficiency x containment; model-free, no multiplicity effect),
   M = mean number of pads fired within 20 mm, muon window - sideband (cluster-size proxy, rises with charge sharing).
Usage: checks.py MEASURE_DIR TAGS_DIR PSEUDO_DIR CVA_DIR"""
import glob
import json
import re
import sys

import numpy as np
import pandas as pd

mdir, tags_dir, pseudo, cva = sys.argv[1:5]
sys.path.insert(0, cva)
import channel_efficiency as CE  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402

chunks = {int(k): v for k, v in json.load(open(f'{pseudo}/chunks.json')).items()}
lab = lambda p: chunks[p]['cfg'].replace('cfg_gain4.5_peaktime200_opt_cosmic_', '').replace('_v1_hvm', '')  # noqa
ORDER = ['trimmed_Km18', 'bscan_hvon2', 'bscan_hvon5', 'bscan_hvon10', 'bscan_hvon20', 'bscan_hvon50']
Z = np.load(f'{mdir}/curves.npz')
cfg = np.array([lab(int(p)) for p in Z['run']])
use = Z['clean'] & (Z['dist_mV'] >= 0)
det, vmm, ch = Z['det'], Z['vmm'], Z['ch']


def fit(m):
    g = CE.fit_group(Z['n'][m], Z['k'][m], Z['s'][m], Z['v'][m], Z['area'][m], Z['radii'], float(Z['r_dens']))
    return g['eps'], g['err'], float(np.mean(Z['dist_mV'][m])), int(m.sum())


pd.set_option('display.width', 250)
# 1. paired pads
pad = np.array([f'{v}:{c}' for v, c in zip(vmm, ch)])
common = set.intersection(*(set(pad[use & (cfg == c)]) for c in ORDER))
inc = np.isin(pad, list(common))
rows = []
for d in CE.NAME.values():
    for c in ORDER:
        e, er, dm, n = fit(use & inc & (cfg == c) & (det == d))
        rows.append(dict(det=d, config=c, eps=e, err=er, dist_mV=dm, pad_chunks=n))
P = pd.DataFrame(rows)
print(f'== 1. paired: the same {len(common)} pads in every configuration')
print(P.pivot(index='config', columns='det', values='eps').reindex(ORDER).round(3))
print(P.pivot(index='config', columns='det', values='err').reindex(ORDER).round(3))
print(P.pivot(index='config', columns='det', values='dist_mV').reindex(ORDER).round(1))
# 2. per chip
rows = []
for v in range(2, 20):
    for c in ORDER:
        m = use & (cfg == c) & (vmm == v)
        if m.sum() > 50:
            e, er, dm, n = fit(m)
            rows.append(dict(chip=v, det=det[vmm == v][0], config=c, eps=e, err=er, dist_mV=dm))
C = pd.DataFrame(rows)
C.to_csv(f'{mdir}/by_chip_config.csv', index=False)
print('== 2. per chip: eps (dist_mV) per configuration')
t = C.assign(s=C.apply(lambda r: f'{r.eps:.2f}+-{r.err:.2f} ({r.dist_mV:.0f})', axis=1))
print(t.pivot(index='chip', columns='config', values='s')[ORDER].to_string())

# 3. from the tags
X, Y, _ = CE.pad_geometry()
acc = {}
for fz in sorted(glob.glob(f'{tags_dir}/tags_run*.npz')):
    p = int(re.search(r'run(\d+)', fz)[1])
    info = CE.run_info(tags_dir, p)
    for q in (3, 2, 1):
        T = CE.load_tags(fz, q)
        allp = [(v, c) for v in CE.DETV[q] for c in range(64) if np.isfinite(X[v, c])]
        live = np.array([(X[v, c], Y[v, c]) for v, c in allp if (v, c) not in info['dead']])
        dead = np.array([(X[v, c], Y[v, c]) for v, c in allp if (v, c) in info['dead']]).reshape(-1, 2)
        clean = cKDTree(dead).query(live)[0] > 40 if len(dead) else np.ones(len(live), bool)
        dt = T['dt_scint']
        sel = (dt >= 140) & (dt <= 310)
        dd, ii = cKDTree(live).query(np.c_[T['px'], T['py']])
        sel &= (dd < 6) & clean[ii]
        w = np.where(T['on'], 1.0, -float(T['r_scale']))
        out = {}
        for pre in ('fired', 'side'):
            ti = T[f'{pre}_i']
            r = np.hypot(T[f'{pre}_x'] - T['px'][ti], T[f'{pre}_y'] - T['py'][ti])
            for R in (10, 20, 30):
                anyf = np.zeros(len(w), bool)
                anyf[ti[r < R]] = True
                out[(pre, R)] = anyf
            out[(pre, 'n')] = np.bincount(ti[r < 20], minlength=len(w))
        key = (lab(p), CE.NAME[q])
        a = acc.setdefault(key, dict(W=0.0, **{f'A{R}': 0.0 for R in (10, 20, 30)}, N=0.0, tags=0))
        ws = w[sel]
        a['W'] += ws.sum()
        a['tags'] += int(sel.sum())
        for R in (10, 20, 30):
            a[f'A{R}'] += (ws * (out[('fired', R)][sel].astype(float) - out[('side', R)][sel])).sum()
        a['N'] += (ws * (out[('fired', 'n')][sel] - out[('side', 'n')][sel])).sum()
rows = []
for (c, d), a in acc.items():
    W = a['W']
    rows.append(dict(config=c, det=d, tags=a['tags'], **{f'A{R}': a[f'A{R}'] / W for R in (10, 20, 30)},
                     A20_err=np.sqrt(max(a['A20'] / W * (1 - a['A20'] / W), 1e-4) / W), M20=a['N'] / W))
A = pd.DataFrame(rows)
A['M20_per_A20'] = A.M20 / A.A20
A.to_csv(f'{mdir}/chamber_level.csv', index=False)
print('== 3. chamber level from the tags (scint 140-310 ns, prediction within 6 mm of a clean live pad)')
A['config'] = pd.Categorical(A.config, ORDER)
print(A.sort_values(['det', 'config']).round(3).to_string(index=False))
