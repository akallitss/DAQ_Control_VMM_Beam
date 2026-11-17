#!/usr/bin/env python3
"""Regroup the counting curves of channel_efficiency.py measure (curves.npz) by configuration and real run.

Uses channel_efficiency.fit_group unchanged (same estimator, same R_lo, same clean/blind selection as measure):
pads with clean = True and dist_mV >= 0. Pseudo-run ids are run*100 + chunk (chunks.json).
Writes OUT/by_config.csv, by_run.csv, by_config_bin.csv, by_bin.csv and eff_vs_threshold.png.
Usage: regroup.py MEASURE_DIR PSEUDO_DIR CVA_DIR"""
import json
import os
import sys

import numpy as np
import pandas as pd

mdir, pseudo, cva = sys.argv[1:4]
sys.path.insert(0, cva)
import channel_efficiency as CE  # noqa: E402

Z = np.load(f'{mdir}/curves.npz')
chunks = {int(k): v for k, v in json.load(open(f'{pseudo}/chunks.json')).items()}
pid = Z['run']
run = np.array([chunks[int(p)]['run'] for p in pid])
cfg = np.array([chunks[int(p)]['cfg'].replace('cfg_gain4.5_peaktime200_opt_cosmic_', '').replace('_v1_hvm', '')
                for p in pid])
use = Z['clean'] & (Z['dist_mV'] >= 0)
det = Z['det']
BINS = [0, 8, 10, 12, 14, 16, 18, 20, 22, 26, 34, 60]
ORDER = ['trimmed_Km18', 'bscan_hvon2', 'bscan_hvon5', 'bscan_hvon10', 'bscan_hvon20', 'bscan_hvon50']


def fit(m):
    if not m.any():
        return None
    g = CE.fit_group(Z['n'][m], Z['k'][m], Z['s'][m], Z['v'][m], Z['area'][m], Z['radii'], float(Z['r_dens']))
    return dict(eps=g['eps'], err=g['err'], chi2ndf=g['chi2ndf'], E=g['E'], pads=int(m.sum()),
                dist_mV=float(np.mean(Z['dist_mV'][m])), dist_med=float(np.median(Z['dist_mV'][m])),
                **{k: g[k] for k in g if k.startswith('eps_rlo')})


rows = []
for c in ORDER:
    for d in CE.NAME.values():
        r = fit(use & (cfg == c) & (det == d))
        if r:
            rows.append(dict(config=c, det=d, **r))
BC = pd.DataFrame(rows)
rows = []
for rr in sorted(set(run)):
    for d in CE.NAME.values():
        r = fit(use & (run == rr) & (det == d))
        if r:
            rows.append(dict(run=rr, config=cfg[run == rr][0], det=d, **r))
BR = pd.DataFrame(rows)
rows, rows2 = [], []
for d in CE.NAME.values():
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        inb = (Z['dist_mV'] >= lo) & (Z['dist_mV'] < hi)
        r = fit(use & (det == d) & inb)
        if r:
            rows.append(dict(det=d, bin=f'{lo}-{hi}', **r))
        for c in ORDER:
            r = fit(use & (det == d) & inb & (cfg == c))
            if r:
                rows2.append(dict(config=c, det=d, bin=f'{lo}-{hi}', **r))
BB, BCB = pd.DataFrame(rows), pd.DataFrame(rows2)
blind = fit(Z['clean'] & (Z['dist_mV'] < 0))
for name, T in [('by_config', BC), ('by_run', BR), ('by_bin', BB), ('by_config_bin', BCB)]:
    T.to_csv(f'{mdir}/{name}.csv', index=False)
pd.set_option('display.width', 250)
cols = ['eps', 'err', 'pads', 'dist_mV', 'chi2ndf', 'eps_rlo24', 'eps_rlo40', 'eps_rlo56']
print('== per configuration (all its runs)')
print(BC[['config', 'det'] + cols].round(3).to_string(index=False))
print('== per real run')
print(BR[['run', 'config', 'det', 'eps', 'err', 'pads', 'dist_mV']].round(3).to_string(index=False))
print('== per threshold-above-baseline bin (all configurations)')
print(BB[['det', 'bin'] + cols].round(3).to_string(index=False))
print('== blind pads (baseline above threshold):', {k: round(v, 4) for k, v in (blind or {}).items()
                                                    if k in ('eps', 'err', 'pads')})

import matplotlib  # noqa: E402
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from event_display import style, INK, MUTED, COL, MRK  # noqa: E402
fig, ax = plt.subplots(1, 2, figsize=(12, 4.4), sharey=True)
off = {'P2_IN': -0.25, 'P2_MID': 0.0, 'P2_OUT': 0.25}
for x in ax:
    x.axhspan(0.75, 0.79, color=MUTED, alpha=0.12, lw=0)
    x.axhline(0.95, color=MUTED, lw=0.9, ls='--')
ax[0].text(0.5, 0.955, 'expected ~0.95', color=MUTED, fontsize=7, va='bottom')
ax[0].text(0.5, 0.735, 'wk2 plateau 0.75-0.79 (18-40 mV)', color=MUTED, fontsize=7, va='top')
for d, p in CE.PNUM.items():
    b = BB[(BB.det == d) & BB.eps.notna() & (BB.E > 0)]
    ax[0].errorbar(b.dist_mV + off[d], b.eps, b.err, marker=MRK[p], color=COL[p], ms=5, lw=1.5, elinewidth=1,
                   capsize=0, label=d)
    c = BC[BC.det == d].set_index('config').reindex(ORDER)
    ax[1].errorbar(c.dist_mV + off[d], c.eps, c.err, marker=MRK[p], color=COL[p], ms=6, lw=0, elinewidth=1,
                   label=d)
    for cf, r in c.iterrows():
        if d == 'P2_MID' and np.isfinite(r.dist_mV):
            ax[1].annotate(cf.replace('bscan_hvon', 'R=').replace('trimmed_', '') + (' Hz' if 'hvon' in cf else ''),
                           (r.dist_mV, 0.06), fontsize=7, color=MUTED, ha='center')
ax[0].set_title('efficiency vs threshold - baseline (pads binned, all runs)', fontsize=9, color=INK, loc='left')
ax[1].set_title('efficiency per configuration (x = mean threshold - baseline of its pads)', fontsize=9, color=INK,
                loc='left')
for x in ax:
    x.set_xlabel('threshold - baseline of the channel (mV)', fontsize=8, color=INK)
    x.set_ylim(0, 1.05)
    x.legend(frameon=False, fontsize=7, loc='lower right')
    style(x)
ax[0].set_ylabel('single-channel efficiency (scintillator-confirmed tags)', fontsize=8, color=INK)
fig.tight_layout()
fig.savefig(f'{mdir}/eff_vs_threshold.png', dpi=150)
print('wrote', f'{mdir}/eff_vs_threshold.png')
