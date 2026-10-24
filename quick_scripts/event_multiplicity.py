#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Group over-threshold hits into events (gap larger than --gap ns starts a new one) and count their size, per chamber.

A cosmic muon through the stack gives a few hits in each of the three chambers within tens of ns. Large simultaneous bursts
over many channels / chips (discharges, pick-up) must be removed before any track finding. Reuses the time
reconstruction of coincidence_check.py.

Usage: event_multiplicity.py f1.pcapng[,f2,...] OUT.json [--gap 100] [--max-per-chamber 12]
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coincidence_check import read_times, CHAMBER  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--gap', type=float, default=100)
    ap.add_argument('--max-per-chamber', type=int, default=12)
    a = ap.parse_args()
    state = {}
    V, T = [], []
    for p in a.files.split(','):
        for v, t, ch in read_times(p, state):
            V.append(v); T.append(t)
    V, T = np.concatenate(V), np.concatenate(T)
    o = np.argsort(T, kind='stable')
    V, T = V[o], T[o]
    ev = np.concatenate([[0], np.cumsum(np.diff(T) > a.gap)])
    n_ev = int(ev[-1]) + 1
    size = np.bincount(ev, minlength=n_ev)
    cham = np.array([CHAMBER.get(int(v), 0) for v in range(32)])[V]
    per = {d: np.bincount(ev[cham == d], minlength=n_ev) for d in (3, 2, 1)}
    span = (T[-1] - T[0]) / 1e9
    print(f'{len(T)} hits in {span:.0f} s grouped into {n_ev} events (gap {a.gap:g} ns)')
    edges = [1, 2, 3, 5, 10, 20, 50, 100, 1000, 10 ** 9]
    h = np.histogram(size, bins=edges)[0]
    print('events by size (hits):', {f'{edges[i]}-{edges[i + 1] - 1}': int(h[i]) for i in range(len(h))})
    print('hits contained in events of size:', {f'{edges[i]}-{edges[i + 1] - 1}': int(size[(size >= edges[i]) & (size < edges[i + 1])].sum()) for i in range(len(h))})
    allthree = (per[3] > 0) & (per[2] > 0) & (per[1] > 0)
    m = a.max_per_chamber
    cand = allthree & (per[3] <= m) & (per[2] <= m) & (per[1] <= m)
    big = size > 3 * m
    print(f'events with a hit in all three chambers: {int(allthree.sum())} ({allthree.sum() / span:.3f} Hz)')
    print(f'   of which at most {m} hits per chamber (muon-like): {int(cand.sum())} ({cand.sum() / span:.3f} Hz, {cand.sum() / span * 60:.1f} per minute)')
    print(f'events larger than {3 * m} hits: {int(big.sum())} ({big.sum() / span:.3f} Hz) carrying {int(size[big].sum())} hits = {100 * size[big].sum() / len(T):.0f} % of all hits')
    print('chambers hit in the muon-like events, hits per chamber (median):', {d: float(np.median(per[d][cand])) if cand.any() else None for d in (3, 2, 1)})
    # time offsets between chambers inside the muon-like events (first hit of each chamber)
    ci = np.nonzero(cand)[0]
    first = {}
    for d in (3, 2, 1):
        m_ = cham == d
        f = np.full(n_ev, np.nan)
        idx = np.nonzero(m_)[0]
        # first hit time of chamber d in each event (T is sorted, so the first occurrence is the earliest)
        evs, pos = np.unique(ev[idx], return_index=True)
        f[evs] = T[idx][pos]
        first[d] = f
    for x, y in ((3, 2), (3, 1), (2, 1)):
        dd = first[y][ci] - first[x][ci]
        dd = dd[np.isfinite(dd)]
        if len(dd):
            med = float(np.median(dd)); mad = float(np.median(np.abs(dd - med)) * 1.4826)
            print(f'   det{y} - det{x} in muon-like events: median {med:+.1f} ns, spread (MAD x 1.48) {mad:.1f} ns, 10-90 % range {np.percentile(dd, 10):+.0f} .. {np.percentile(dd, 90):+.0f} ns')
    two = ((per[3] > 0).astype(int) + (per[2] > 0) + (per[1] > 0)) == 2
    print(f'events with exactly two chambers hit and at most {m} hits each: {int((two & (size <= 2 * m)).sum())}')
    json.dump({'hits': int(len(T)), 'span_s': span, 'events': n_ev, 'events_by_size': {f'{edges[i]}-{edges[i + 1] - 1}': int(h[i]) for i in range(len(h))},
               'three_chambers': int(allthree.sum()), 'muon_like': int(cand.sum()), 'large_events': int(big.sum()),
               'hits_in_large_events_pct': float(100 * size[big].sum() / len(T))}, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
