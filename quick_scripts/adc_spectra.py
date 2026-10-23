#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-chip hit rate and ADC spectrum of over-threshold hits of one capture: a health check of the chambers.

Cosmic-muon signals give a broad ADC distribution above the baseline; a chamber without gain (HV problem, the July det3
failure) gives a narrow spectrum close to the baseline or almost no hits. Reads the first --mb MB (0 = whole file) with the
reader of neighbor_baseline.py. Chips are grouped by chamber: det3 = VMM 2-7 (P2_IN), det2 = 8-13 (P2_MID), det1 = 14-19 (P2_OUT).

Usage: adc_spectra.py file.pcapng OUT.json [--mb 0]
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neighbor_baseline import decode_raw  # noqa: E402

CH = {'det3 (IN)': range(2, 8), 'det2 (MID)': range(8, 14), 'det1 (OUT)': range(14, 20)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('pcap')
    ap.add_argument('out_json')
    ap.add_argument('--mb', type=float, default=0)
    a = ap.parse_args()
    chunks, span = decode_raw(a.pcap, a.mb)
    H = np.zeros((32, 1024), dtype=np.int64)
    NB = np.zeros(32, dtype=np.int64)
    for v, c, ad, ot in chunks:
        m = ot == 1
        if m.any():
            H += np.bincount(v[m].astype(np.int64) * 1024 + ad[m], minlength=32 * 1024).reshape(32, 1024)
        NB += np.bincount(v[~m], minlength=32)
    res = {'span_s': span, 'chips': {}}
    bins = np.arange(1024)
    print(f'{a.pcap.split("/")[-1]}: {span:.1f} s of capture')
    print(f'{"chamber":<11}{"VMM":>4}{"hits/s":>9}{"median":>8}{"p10":>6}{"p90":>6}{"p90-p10":>8}{"frac>80":>9}')
    for name, rng in CH.items():
        for v in rng:
            n = int(H[v].sum())
            if n == 0:
                print(f'{name:<11}{v:>4}{0:>9}')
                res['chips'][str(v)] = {'hits': 0}
                continue
            cum = H[v].cumsum()
            p = lambda q: int((cum >= q * n).argmax())
            f80 = float(H[v][81:].sum()) / n
            res['chips'][str(v)] = {'hits': n, 'rate_hz': round(n / span, 2), 'median': p(.5), 'p10': p(.1), 'p90': p(.9), 'frac_above_80': round(f80, 3), 'n_neighbour_readouts': int(NB[v])}
            print(f'{name:<11}{v:>4}{n / span:>9.1f}{p(.5):>8}{p(.1):>6}{p(.9):>6}{p(.9) - p(.1):>8}{f80:>9.2f}')
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
