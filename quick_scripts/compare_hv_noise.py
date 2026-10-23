#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare two noise scans of the same chip configs, one with HV on and one with HV off.

Inputs are the noise_scan_summary.csv files of quick_scripts/noise_scan_summary.py. Points are
matched by the threshold step d in the chip-config name (..._d<d>). For every (d, VMM, channel)
that fired in the HV-ON scan with at least --min-hits hits, the HV-OFF rate is looked up
(0 if the channel never fired) and the channel is classified by rate_off / rate_on:

    HV-dependent    < 0.1    the noise goes away with the voltage off: it needs the detector
    partly          0.1-0.5
    HV-independent  >= 0.5   still there with no voltage: the chip, hybrid or cable, not the gas

Because both scans use the same chip configs (same masks), differences are the HV only. The rate
is hits / capture time of the first --mb MB of each file, so channels below ~1/span Hz are not
resolved; --min-hits guards against Poisson noise on both sides.

Usage: compare_hv_noise.py ON.csv OFF.csv OUT_DIR [--min-hits 20]
Writes OUT_DIR/hv_on_off_channels.csv and prints a per-chip table.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict


def load(path, tag):
    rows = {}
    for r in csv.DictReader(open(path)):
        m = re.search(r'_d(\d+)$', r['chip_config'])
        if not m or tag not in r['chip_config']:
            continue
        rows[(int(m.group(1)), int(r['vmm']), int(r['ch']))] = (int(r['hits']), float(r['rate_hz']))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('on_csv')
    ap.add_argument('off_csv')
    ap.add_argument('out_dir')
    ap.add_argument('--min-hits', type=int, default=20)
    ap.add_argument('--on-tag', default='_m2_d')
    ap.add_argument('--off-tag', default='_m2hvoff_d')
    a = ap.parse_args()
    on, off = load(a.on_csv, a.on_tag), load(a.off_csv, a.off_tag)
    os.makedirs(a.out_dir, exist_ok=True)
    out, tally = [], defaultdict(lambda: defaultdict(int))
    for (d, v, ch), (hits, rate) in sorted(on.items()):
        if hits < a.min_hits:
            continue
        h2, r2 = off.get((d, v, ch), (0, 0.0))
        ratio = r2 / rate if rate > 0 else float('nan')
        cls = 'HV-dependent' if ratio < 0.1 else ('partly' if ratio < 0.5 else 'HV-independent')
        out.append([d, v, ch, hits, round(rate, 1), h2, round(r2, 1), round(ratio, 3), cls])
        tally[v][cls] += 1
    with open(os.path.join(a.out_dir, 'hv_on_off_channels.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['d', 'vmm', 'ch', 'hits_on', 'rate_on_hz', 'hits_off', 'rate_off_hz', 'ratio_off_on', 'class'])
        w.writerows(out)
    print(f'channel-points with >= {a.min_hits} hits with HV on: {len(out)}')
    print(f'{"VMM":>4} {"HV-dependent":>13} {"partly":>7} {"HV-indep.":>10}   (channel x d points)')
    for v in sorted(tally):
        t = tally[v]
        print(f'{v:>4} {t["HV-dependent"]:>13} {t["partly"]:>7} {t["HV-independent"]:>10}')
    ind = defaultdict(set)
    for d, v, ch, _, ron, _, roff, ratio, cls in out:
        if cls == 'HV-independent' and ron > 1000:
            ind[v].add(ch)
    print('\nchannels above 1 kHz with HV on that are still there with HV off:')
    for v in sorted(ind):
        print(f'  VMM {v}: ch {sorted(ind[v])}')


if __name__ == '__main__':
    sys.exit(main())
