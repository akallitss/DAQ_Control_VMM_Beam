#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tag-and-probe efficiency of one chamber from cosmic-muon coincidences (chamber level, no pad geometry yet).

Hits (over-threshold only) of a capture are grouped in time (gap > --gap ns starts a new group). Groups with more
than --burst hits are dropped (the ~1 Hz bursts). In a remaining group the two TAG chambers must each have
1..--max-tag hits (a muon crossed both); the PROBE chamber is "efficient" if it has >= 1 hit in the same group.
All three roles are rotated (probe = det2, det1, det3). Chance level: the same count after shifting the probe
chamber's hit times by --shift ns (default 1 ms), i.e. the probability that noise alone gives a probe hit inside
a group; corrected efficiency = (eff - chance) / (1 - chance).
Per probe chip: share of probe-hit groups in which that chip fired (which chips carry the efficiency), and the
same for the near-vertical subset where both tags sit on the same connector column (pair of VMMs c4/c5/c6).

Usage: tag_and_probe.py f1.pcapng[,f2,...] OUT.json [--gap 100] [--burst 36] [--max-tag 12] [--shift 1e6]
"""
import argparse
import json
import sys

import numpy as np

from coincidence_check import read_times, CHAMBER

# connector column of each VMM inside a chamber (c4: first pair of VMMs, c5: second, c6: third)
COL = {}
for base in (2, 8, 14):
    for k in range(3):
        COL[base + 2 * k] = k
        COL[base + 2 * k + 1] = k


def load(files):
    state = {}
    V, T = [], []
    for p in files.split(','):
        for v, t, _ in read_times(p, state):
            V.append(v); T.append(t)
    V, T = np.concatenate(V), np.concatenate(T)
    o = np.argsort(T, kind='stable')
    return V[o], T[o]


def groups(V, T, gap, burst):
    ev = np.concatenate([[0], np.cumsum(np.diff(T) > gap)])
    n = np.bincount(ev)
    keep = n[ev] <= burst
    return ev, keep, n


def analyse(V, T, gap, burst, max_tag, shift):
    res = {}
    dets = np.array([CHAMBER[int(v)] for v in V])
    for probe, tags in ((2, (3, 1)), (1, (3, 2)), (3, (2, 1))):
        out = {}
        for label, TT in (('data', T), ('shifted', None)):
            if TT is None:
                TT = T.copy()
                TT[dets == probe] += shift
                o = np.argsort(TT, kind='stable')
                v, t, d = V[o], TT[o], dets[o]
            else:
                v, t, d = V, TT, dets
            ev, keep, n = groups(v, t, gap, burst)
            ne = ev.max() + 1
            cnt = {c: np.bincount(ev[d == c], minlength=ne) for c in (1, 2, 3)}
            tag_ok = np.ones(ne, bool)
            for c in tags:
                tag_ok &= (cnt[c] >= 1) & (cnt[c] <= max_tag)
            tag_ok &= (n <= burst)
            hit = cnt[probe] >= 1
            out[label] = {'tagged': int(tag_ok.sum()), 'probe_hit': int((tag_ok & hit).sum())}
            if label == 'data':
                sel = tag_ok & hit
                per = {}
                for chip in sorted(x for x in set(v.tolist()) if CHAMBER[int(x)] == probe):
                    m = (v == chip) & sel[ev]
                    per[int(chip)] = int(len(np.unique(ev[m])))
                out['per_chip_groups'] = per
                # near-vertical subset: both tags on the same connector column
                cols = {c: {} for c in tags}
                for c in tags:
                    for col in range(3):
                        vm = [x for x in set(v.tolist()) if CHAMBER[int(x)] == c and COL[int(x)] == col]
                        cols[c][col] = np.bincount(ev[np.isin(v, vm)], minlength=ne) >= 1
                for col in range(3):
                    sub = tag_ok & cols[tags[0]][col] & cols[tags[1]][col] \
                        & ~sum(cols[tags[0]][o2].astype(int) for o2 in range(3) if o2 != col).astype(bool) \
                        & ~sum(cols[tags[1]][o2].astype(int) for o2 in range(3) if o2 != col).astype(bool)
                    pv = [x for x in set(v.tolist()) if CHAMBER[int(x)] == probe and COL[int(x)] == col]
                    ph = np.bincount(ev[np.isin(v, pv)], minlength=ne) >= 1
                    out[f'col{col}'] = {'tagged': int(sub.sum()), 'probe_same_col_hit': int((sub & ph).sum()),
                                        'probe_any_hit': int((sub & hit).sum())}
        nt = out['data']['tagged']
        eff = out['data']['probe_hit'] / nt if nt else float('nan')
        chance = out['shifted']['probe_hit'] / out['shifted']['tagged'] if out['shifted']['tagged'] else float('nan')
        out['eff_raw'] = eff
        out['chance'] = chance
        out['eff_corr'] = (eff - chance) / (1 - chance) if nt and chance < 1 else float('nan')
        out['err'] = float(np.sqrt(max(eff * (1 - eff), 1e-9) / nt)) if nt else float('nan')
        res[f'probe_det{probe}'] = out
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--gap', type=float, default=100)
    ap.add_argument('--burst', type=int, default=36)
    ap.add_argument('--max-tag', type=int, default=12)
    ap.add_argument('--shift', type=float, default=1e6)
    a = ap.parse_args()
    V, T = load(a.files)
    span = (T[-1] - T[0]) / 1e9 if len(T) > 1 else 0
    res = {'n_hits': int(len(T)), 'span_s': round(span, 1), 'params': vars(a)}
    res.update(analyse(V, T, a.gap, a.burst, a.max_tag, a.shift))
    for k, r in res.items():
        if k.startswith('probe'):
            print(f'{k}: tagged {r["data"]["tagged"]}, probe hit {r["data"]["probe_hit"]}: eff {r["eff_raw"]:.3f} '
                  f'chance {r["chance"]:.3f} corrected {r["eff_corr"]:.3f} +- {r["err"]:.3f}')
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
