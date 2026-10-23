#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Does a channel's ADC baseline (neighbour read-out) predict where its noise rate peaks?

Idea under test: a channel fires most when the threshold sits at its own baseline, so a channel with
a higher baseline should peak at a higher threshold step d. For every channel that has

  * a baseline ADC from neighbour read-out (neighbor_baseline.csv, >= --min-nb read-outs), and
  * a clear interior peak in the HV-off noise scan (noise_scan_summary.csv, config tag m2hvoff:
    the maximum rate is not at the first or last step, with >= --min-hits hits at the peak),

this compares x = baseline ADC minus the chip's median baseline with y = peak step d minus the chip's
median peak step. A positive correlation supports the idea.

Usage: baseline_vs_peak.py neighbor_baseline.csv noise_scan_summary.csv OUT.csv [--min-nb 50] [--min-hits 30]
"""

import argparse
import csv
import re
import statistics as st
import sys
from collections import defaultdict


def ranks(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2
        i = j + 1
    return r


def pearson(x, y):
    mx, my = st.mean(x), st.mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return float('nan')
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sxx * syy) ** 0.5


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('baseline_csv')
    ap.add_argument('noise_csv')
    ap.add_argument('out_csv')
    ap.add_argument('--min-nb', type=int, default=50)
    ap.add_argument('--min-hits', type=int, default=30)
    ap.add_argument('--tag', default='m2hvoff')
    a = ap.parse_args()

    base = {}
    for r in csv.DictReader(open(a.baseline_csv)):
        if r['adc_nb_median'] != '' and int(r['n_nb']) >= a.min_nb:
            base[(int(r['vmm']), int(r['ch']))] = float(r['adc_nb_median'])
    curves = defaultdict(dict)
    for r in csv.DictReader(open(a.noise_csv)):
        m = re.search(r'_%s_d(\d+)$' % a.tag, r['chip_config'])
        if m:
            curves[(int(r['vmm']), int(r['ch']))][int(m.group(1))] = (int(r['hits']), float(r['rate_hz']))
    rows = []
    for key, bl in base.items():
        c = curves.get(key)
        if not c or len(c) < 3:
            continue
        ds = sorted(c)
        dpk = max(ds, key=lambda d: c[d][1])
        if dpk in (ds[0], ds[-1]) or c[dpk][0] < a.min_hits:
            continue
        rows.append([key[0], key[1], bl, dpk, round(c[dpk][1], 1)])
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    out = []
    for v, rs in by.items():
        mb = st.median([r[2] for r in rs])
        md = st.median([r[3] for r in rs])
        for r in rs:
            out.append([r[0], r[1], r[2], r[3], r[4], r[2] - mb, r[3] - md])
    with open(a.out_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['vmm', 'ch', 'baseline_adc', 'peak_d', 'peak_rate_hz', 'dx_adc_minus_chip_median', 'dy_peak_d_minus_chip_median'])
        w.writerows(out)
    x = [r[5] for r in out]
    y = [r[6] for r in out]
    print(f'channels with a baseline and a clear peak: {len(out)} (from {len(by)} chips)')
    if len(out) > 3:
        rx, ry = ranks(x), ranks(y)
        print(f'Pearson r = {pearson(x, y):+.2f}, Spearman r = {pearson(rx, ry):+.2f}')
        for v in sorted(by):
            xs = [r[5] for r in out if r[0] == v]
            ys = [r[6] for r in out if r[0] == v]
            if len(xs) >= 4:
                print(f'  VMM {v:>2}: n={len(xs):>2}  Spearman {pearson(ranks(xs), ranks(ys)):+.2f}')


if __name__ == '__main__':
    sys.exit(main())
