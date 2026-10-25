#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Are there muons in the cosmic captures? Two checks that do not depend on the channel->pad map.

1. ADC: hits are sorted by the time group they belong to (gap > --gap ns starts a new group):
   lone hits (group of 1), hits of groups confined to one chamber, hits of groups touching all three chambers
   with <= --burst hits, and burst hits (> --burst). Muon hits should sit higher in ADC than noise hits.
2. Time coincidence chip by chip: for every pair (chip in det3, chip in det1) the histogram of t(det1) - t(det3)
   over +-2000 ns (all hits except bursts); peak excess within +-100 ns over the flat level of the outer 40 %
   of the range. Muons through a stack give the excess on the chip pairs that face each other; correlated
   electronic noise gives it everywhere or between chips sharing a cable / hybrid.
   Repeated for det3-det2 and det2-det1, and with an ADC cut (--adc-cut) on both hits.

Usage: muon_signal_check.py f1.pcapng[,f2,...] OUT.json [--gap 100] [--burst 36] [--adc-cut 150]
"""
import argparse
import json
import sys

import numpy as np

from coincidence_check import read_times, CHAMBER, CH, pair_hist


def load(files):
    state = {}
    cols = [[], [], [], []]
    for p in files.split(','):
        for rec in read_times(p, state, with_adc=True):
            for k in range(4):
                cols[k].append(rec[k])
    V, T, C, A = (np.concatenate(c) for c in cols)
    o = np.argsort(T, kind='stable')
    return V[o], T[o], C[o], A[o]


def pct(a):
    return [int(x) for x in np.percentile(a, (10, 25, 50, 75, 90))] if len(a) else None


def excess(h, binw, peak_ns=100):
    n = len(h)
    centers = (np.arange(n) - n / 2 + 0.5) * binw
    flat = np.concatenate([h[:int(n * 0.2)], h[-int(n * 0.2):]]).mean()
    pk = np.abs(centers) <= peak_ns
    obs = h[pk].sum(); exp = flat * pk.sum()
    return float(obs - exp), float((obs - exp) / np.sqrt(max(exp, 1)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--gap', type=float, default=100)
    ap.add_argument('--burst', type=int, default=36)
    ap.add_argument('--adc-cut', type=int, default=150)
    a = ap.parse_args()
    V, T, C, A = load(a.files)
    d = np.array([CHAMBER.get(int(v), 0) for v in V])
    ev = np.concatenate([[0], np.cumsum(np.diff(T) > a.gap)])
    ne = int(ev[-1]) + 1
    n = np.bincount(ev, minlength=ne)
    has = {c: np.bincount(ev[d == c], minlength=ne) > 0 for c in (1, 2, 3)}
    nch = has[1].astype(int) + has[2] + has[3]
    span = float((T[-1] - T[0]) / 1e9)
    res = {'n_hits': int(len(T)), 'span_s': round(span, 1), 'params': vars(a), 'adc': {}}
    kinds = {'lone': n[ev] == 1, 'one_chamber_group': (n[ev] > 1) & (nch[ev] == 1) & (n[ev] <= a.burst),
             'two_chamber_group': (nch[ev] == 2) & (n[ev] <= a.burst),
             'three_chamber_group': (nch[ev] == 3) & (n[ev] <= a.burst), 'burst': n[ev] > a.burst}
    print(f'{len(T)} hits over {span:.0f} s; groups: three-chamber small {int(((nch == 3) & (n <= a.burst)).sum())}')
    for c in (3, 2, 1):
        res['adc'][f'det{c}'] = {}
        for k, m in kinds.items():
            sel = m & (d == c)
            res['adc'][f'det{c}'][k] = {'hits': int(sel.sum()), 'pct_10_25_50_75_90': pct(A[sel]),
                                        'frac_above_cut': float(np.mean(A[sel] > a.adc_cut)) if sel.any() else None}
        print(f'det{c} ADC median: ' + ', '.join(f'{k} {res["adc"][f"det{c}"][k]["pct_10_25_50_75_90"][2] if res["adc"][f"det{c}"][k]["pct_10_25_50_75_90"] else "-"} '
                                                 f'({res["adc"][f"det{c}"][k]["hits"]})' for k in kinds))
    # chip-pair coincidences
    keep = n[ev] <= a.burst
    binw, win = 25.0, 2000.0
    res['pairs'] = {}
    for x, y in ((3, 1), (3, 2), (2, 1)):
        for tag, cut in (('all', 0), (f'adc>{a.adc_cut}', a.adc_cut)):
            mat = {}
            tot = None
            for vx in CH[x]:
                tx = T[keep & (V == vx) & (A > cut)]
                for vy in CH[y]:
                    ty = T[keep & (V == vy) & (A > cut)]
                    h, _ = pair_hist(tx, ty, win, binw)
                    h = h[:int(2 * win / binw)]
                    tot = h if tot is None else tot + h
                    ex, sig = excess(h, binw)
                    mat[f'{vx}-{vy}'] = [round(ex, 1), round(sig, 1)]
            ex, sig = excess(tot, binw)
            res['pairs'][f'det{x}-det{y} {tag}'] = {'total_excess': round(ex, 1), 'total_sigma': round(sig, 1),
                                                     'hist_total': tot.tolist(), 'chip_pairs': mat}
            print(f'\ndet{y} - det{x} ({tag}): total excess {ex:.0f} in +-100 ns ({sig:.1f} sigma); sigma per chip pair (rows det{x}, cols det{y}):')
            print('       ' + ''.join(f'{vy:>7}' for vy in CH[y]))
            for vx in CH[x]:
                print(f'{vx:>7}' + ''.join(f'{mat[f"{vx}-{vy}"][1]:>7.1f}' for vy in CH[y]))
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
