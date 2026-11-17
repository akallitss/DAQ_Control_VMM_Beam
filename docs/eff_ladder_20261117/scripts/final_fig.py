#!/usr/bin/env python3
"""Final figure: (a) pads binned by threshold - baseline, all configs pooled; (b) the same 744 pads in every
configuration; (c) chamber-level any-pad efficiency within 30 mm (model-free). Usage: final_fig.py OUT_DIR PSEUDO CVA"""
import json
import sys

import numpy as np
import pandas as pd

mdir, pseudo, cva = sys.argv[1:4]
sys.path.insert(0, cva)
import channel_efficiency as CE  # noqa: E402
import matplotlib  # noqa: E402
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from event_display import style, INK, MUTED, COL, MRK  # noqa: E402

ORDER = ['trimmed_Km18', 'bscan_hvon2', 'bscan_hvon5', 'bscan_hvon10', 'bscan_hvon20', 'bscan_hvon50']
SHORT = {'trimmed_Km18': 'Km18', 'bscan_hvon2': 'R=2', 'bscan_hvon5': 'R=5', 'bscan_hvon10': 'R=10',
         'bscan_hvon20': 'R=20', 'bscan_hvon50': 'R=50'}
Z = np.load(f'{mdir}/curves.npz')
chunks = json.load(open(f'{pseudo}/chunks.json'))
cfg = np.array([chunks[str(int(p))]['cfg'].replace('cfg_gain4.5_peaktime200_opt_cosmic_', '').replace('_v1_hvm', '')
                for p in Z['run']])
use = Z['clean'] & (Z['dist_mV'] >= 0)
pad = np.array([f'{v}:{c}' for v, c in zip(Z['vmm'], Z['ch'])])
common = set.intersection(*(set(pad[use & (cfg == c)]) for c in ORDER))
inc = np.isin(pad, list(common))
rows = []
for d in CE.NAME.values():
    for c in ORDER:
        m = use & inc & (cfg == c) & (Z['det'] == d)
        g = CE.fit_group(Z['n'][m], Z['k'][m], Z['s'][m], Z['v'][m], Z['area'][m], Z['radii'], float(Z['r_dens']))
        rows.append(dict(det=d, config=c, eps=g['eps'], err=g['err'], eps_rlo56=g['eps_rlo56'],
                         dist_mV=float(np.mean(Z['dist_mV'][m])), pads=len(common)))
P = pd.DataFrame(rows)
P.to_csv(f'{mdir}/paired.csv', index=False)
BB = pd.read_csv(f'{mdir}/by_bin.csv')
A = pd.read_csv(f'{mdir}/chamber_level.csv')
off = {'P2_IN': -0.2, 'P2_MID': 0.0, 'P2_OUT': 0.2}
fig, ax = plt.subplots(1, 3, figsize=(15, 4.6), sharey=True)
for x in ax:
    x.axhspan(0.75, 0.79, color=MUTED, alpha=0.13, lw=0)
    x.axhline(0.95, color=MUTED, lw=0.9, ls='--')
    x.set_ylim(0.4, 1.1)
    style(x)
ax[2].text(5.4, 0.957, 'expected ~0.95', color=MUTED, fontsize=7, va='bottom', ha='right')
ax[2].text(5.4, 0.745, 'wk2 plateau: 0.75-0.79', color=MUTED, fontsize=7, va='top', ha='right')
for d, p in CE.PNUM.items():
    b = BB[(BB.det == d) & (BB.pads > 1000)]
    ax[0].errorbar(b.dist_mV + off[d], b.eps, b.err, marker=MRK[p], color=COL[p], ms=5, lw=1.4, elinewidth=1,
                   label=d)
    q = P[P.det == d].sort_values('dist_mV')
    ax[1].errorbar(q.dist_mV + off[d] / 4, q.eps, q.err, marker=MRK[p], color=COL[p], ms=6, lw=0, elinewidth=1,
                   label=d)
    a = A[A.det == d].set_index('config').reindex(ORDER)
    xs = np.arange(len(ORDER)) + off[d] / 2
    ax[2].errorbar(xs, a.A30, np.sqrt(a.A30 * (1 - a.A30) / a.tags), marker=MRK[p], color=COL[p], ms=6, lw=0,
                   elinewidth=1, label=d)
q = P[P.det == 'P2_MID'].set_index('config').reindex(ORDER)
for c, r in q.iterrows():
    ax[1].annotate(SHORT[c], (r.dist_mV, 0.43), fontsize=7, color=MUTED, ha='center')
ax[2].set_xticks(range(len(ORDER)), [SHORT[c] for c in ORDER], fontsize=8)
ax[0].set_title('(a) pads binned by threshold - baseline, configs pooled\n(mixes pads: noisy pads sit high)',
                fontsize=9, color=INK, loc='left')
ax[1].set_title(f'(b) the same {len(common)} pads in every configuration\n(x = their mean threshold - baseline)',
                fontsize=9, color=INK, loc='left')
ax[2].set_title('(c) chamber level, model-free: any pad within 30 mm\n(random hits subtracted)', fontsize=9,
                color=INK, loc='left')
for x in ax[:2]:
    x.set_xlabel('threshold - baseline of the channel (mV)', fontsize=8, color=INK)
ax[2].set_xlabel('configuration (R = per-channel HV-on noise rate, Hz)', fontsize=8, color=INK)
ax[0].set_ylabel('efficiency (scintillator-confirmed tags, HV on)', fontsize=8, color=INK)
for x in ax:
    x.legend(frameon=False, fontsize=7, loc='lower right')
fig.tight_layout()
fig.savefig(f'{mdir}/eff_ladder_20261117.png', dpi=150)
print(P.pivot(index='config', columns='det', values='eps_rlo56').reindex(ORDER).round(3))
print('wrote', f'{mdir}/eff_ladder_20261117.png')
