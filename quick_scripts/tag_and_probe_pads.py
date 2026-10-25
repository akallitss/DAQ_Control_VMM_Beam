#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Geometric tag-and-probe efficiency of the three cosmic chambers, per chamber and per chip.

Same definition as the SPS analysis (P2_basket_analysis/sps_beam_analysis/22_tag_probe_efficiency.py and
vmm_dream_matching/urw_vmm_efficiency.py), with the two OTHER chambers as the tracker instead of the uRWELLs:

  * pads: vmm_qa/vmm_stations.build_pad_table() -- the SPS channel->pad map, unchanged;
  * hits (over-threshold only) grouped in time (gap > --gap ns), groups with > --burst hits dropped;
  * a chamber is a TAG when its hits in the group form one clean cluster: 1..--max-tag mapped pads, all within
    --cluster-r mm of their mean; tag position = mean pad centre;
  * prediction in the probe chamber = straight line through the two tag positions (z from STATIONS), then the
    probe frame alignment (rigid fit, Kabsch, on events where the probe also has one clean cluster, after a coarse
    median shift) -- the equivalent of 21_telescope_align / fit_frame;
  * fiducial: the prediction lies within --fid-r mm of a mapped pad centre of the probe;
  * HIT: a probe pad within --probe-r mm of the prediction among the probe hits within --win ns of the fitted
    latency (median probe-minus-tag time); ACCIDENTAL: the same search in a window --sideband ns later.
    corrected = (eff - acc) / (1 - acc);
  * per chip: the chip owning the pad nearest to the prediction; per pad: the same, for a map.

Usage: tag_and_probe_pads.py f1.pcapng[,f2,...] OUT.json [--gap 100] [--burst 36] [--max-tag 6] [--cluster-r 15]
                             [--probe-r 15] [--fid-r 9] [--win 100] [--sideband 4000]
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vmm_qa'))
from coincidence_check import read_times, CHAMBER          # noqa: E402
import vmm_stations as VS                                   # noqa: E402

DET_STATION = {3: 'P2_IN', 2: 'P2_MID', 1: 'P2_OUT'}
Z = {d: VS.STATIONS[s]['z_mm'] for d, s in DET_STATION.items()}


def load(files):
    state = {}
    V, T, C = [], [], []
    for p in files.split(','):
        for v, t, ch in read_times(p, state):
            V.append(v); T.append(t); C.append(ch)
    V, T, C = np.concatenate(V), np.concatenate(T), np.concatenate(C)
    o = np.argsort(T, kind='stable')
    return V[o], T[o], C[o]


def pad_lookup():
    tab = VS.build_pad_table()
    tab = tab[tab['mapped']]
    X = np.full((32, 64), np.nan); Y = np.full((32, 64), np.nan)
    X[tab['vmm'].to_numpy(), tab['ch'].to_numpy()] = tab['pad_cx'].to_numpy()
    Y[tab['vmm'].to_numpy(), tab['ch'].to_numpy()] = tab['pad_cy'].to_numpy()
    return tab, X, Y


def clusters(ev, d, x, y, t, ne, max_tag, cluster_r):
    """per event and chamber: clean-cluster flag, mean x, y, mean t."""
    out = {}
    for c in (1, 2, 3):
        m = (d == c) & np.isfinite(x)
        e = ev[m]
        n = np.bincount(e, minlength=ne)
        mx = np.bincount(e, x[m], ne) / np.maximum(n, 1)
        my = np.bincount(e, y[m], ne) / np.maximum(n, 1)
        mt = np.bincount(e, t[m], ne) / np.maximum(n, 1)
        far = np.hypot(x[m] - mx[e], y[m] - my[e]) > cluster_r
        nfar = np.bincount(e, far.astype(float), ne)
        out[c] = {'ok': (n >= 1) & (n <= max_tag) & (nfar == 0), 'x': mx, 'y': my, 't': mt, 'n': n}
    return out


def rigid(src, dst):
    ms, md = src.mean(0), dst.mean(0)
    U, _, Vt = np.linalg.svd((src - ms).T @ (dst - md))
    s = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, s]) @ U.T
    return R, md - R @ ms


def nearest_hit(tp, px, py, pt, hx, hy, ht, lo, hi):
    """min distance from each prediction to a probe hit with ht - pt in [lo, hi]; hits sorted by ht."""
    a = np.searchsorted(ht, pt + lo, 'left')
    b = np.searchsorted(ht, pt + hi, 'right')
    n = b - a
    dmin = np.full(len(pt), np.inf)
    if n.sum() == 0:
        return dmin
    idx = np.repeat(np.arange(len(pt)), n)
    j = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + np.repeat(a, n)
    dd = np.hypot(hx[j] - px[idx], hy[j] - py[idx])
    np.minimum.at(dmin, idx, dd)
    return dmin


def probe(V, T, X, Y, cl, ev_ok, probe_d, tags, a, tab):
    ta, tb = tags
    sel = ev_ok & cl[ta]['ok'] & cl[tb]['ok']
    za, zb, zp = Z[ta], Z[tb], Z[probe_d]
    f = (zp - za) / (zb - za)
    px0 = cl[ta]['x'][sel] + f * (cl[tb]['x'][sel] - cl[ta]['x'][sel])
    py0 = cl[ta]['y'][sel] + f * (cl[tb]['y'][sel] - cl[ta]['y'][sel])
    pt = 0.5 * (cl[ta]['t'][sel] + cl[tb]['t'][sel])
    out = {'tags': list(tags), 'n_tagged': int(sel.sum())}
    if sel.sum() < 50:
        out['error'] = 'too few tags'
        return out
    # probe hits (mapped pads only), sorted by time
    pm = np.array([CHAMBER.get(int(v), 0) == probe_d for v in V]) & np.isfinite(X)
    hx, hy, ht, hv = X[pm], Y[pm], T[pm], V[pm]
    # latency: median probe-minus-tag time of hits within +-300 ns
    a0 = np.searchsorted(ht, pt - 300); b0 = np.searchsorted(ht, pt + 300)
    dts = np.concatenate([ht[i:j] - p for i, j, p in zip(a0[:20000], b0[:20000], pt[:20000])]) if len(pt) else np.array([])
    lat = float(np.median(dts)) if len(dts) else 0.0
    out['latency_ns'] = lat
    # alignment: probe has one clean cluster in the same group
    both = cl[probe_d]['ok'][sel]
    qx, qy = cl[probe_d]['x'][sel][both], cl[probe_d]['y'][sel][both]
    rx, ry = qx - px0[both], qy - py0[both]
    R, t = np.eye(2), np.zeros(2)
    if both.sum() >= 30:
        sx, sy = np.median(rx), np.median(ry)
        k = np.hypot(rx - sx, ry - sy) < a.frame_win
        if k.sum() >= 20:
            R, t = rigid(np.column_stack([px0[both][k], py0[both][k]]), np.column_stack([qx[k], qy[k]]))
    P = (R @ np.vstack([px0, py0])).T + t
    px, py = P[:, 0], P[:, 1]
    if both.sum():
        r2 = np.column_stack([qx, qy]) - P[both]
        k = np.hypot(r2[:, 0], r2[:, 1]) < 40
        out['alignment'] = {'rot_deg': float(np.degrees(np.arctan2(R[1, 0], R[0, 0]))), 'shift_mm': [float(t[0]), float(t[1])],
                            'n_fit': int(both.sum()), 'coarse_median_mm': [float(np.median(rx)), float(np.median(ry))],
                            'res_rms_mm': [float(np.std(r2[k, 0])), float(np.std(r2[k, 1]))] if k.sum() > 5 else None,
                            'res_hist_mm_0_40': np.histogram(np.hypot(r2[:, 0], r2[:, 1]), bins=np.arange(0, 42, 2))[0].tolist()}
    # fiducial: nearest mapped probe pad
    pt_tab = tab[tab['station'] == DET_STATION[probe_d]]
    cx, cy = pt_tab['pad_cx'].to_numpy(), pt_tab['pad_cy'].to_numpy()
    dpad = np.full(len(px), np.inf); near = np.zeros(len(px), int)
    for s in range(0, len(px), 4000):
        d2 = (px[s:s + 4000, None] - cx[None]) ** 2 + (py[s:s + 4000, None] - cy[None]) ** 2
        near[s:s + 4000] = np.argmin(d2, 1); dpad[s:s + 4000] = np.sqrt(d2.min(1))
    fid = dpad < a.fid_r
    lo, hi = lat - a.win, lat + a.win
    dmin = nearest_hit(None, px, py, pt, hx, hy, ht, lo, hi)
    smin = nearest_hit(None, px, py, pt, hx, hy, ht, lo + a.sideband, hi + a.sideband)
    hit = fid & (dmin < a.probe_r)
    n, k = int(fid.sum()), int(hit.sum())
    acc = float((fid & (smin < a.probe_r)).sum() / n) if n else float('nan')
    e = k / n if n else float('nan')
    out.update({'n_fiducial': n, 'k': k, 'eff': e, 'err': float(np.sqrt(e * (1 - e) / n)) if n else None,
                'accidental': acc, 'eff_corr': (e - acc) / (1 - acc) if n and acc < 1 else None,
                'vs_probe_r': {str(r): float((fid & (dmin < r)).sum() / n) for r in (5, 10, 15, 20, 30, 50)} if n else {},
                'eff_any_hit_in_window': float((fid & np.isfinite(dmin)).sum() / n) if n else None})
    # per chip / per pad
    vm, chn = pt_tab['vmm'].to_numpy(), pt_tab['ch'].to_numpy()
    out['per_chip'] = {}
    for v in sorted(set(vm.tolist())):
        m = fid & (vm[near] == v)
        if m.sum():
            out['per_chip'][str(v)] = {'n': int(m.sum()), 'k': int((m & hit).sum()), 'acc': int((m & (smin < a.probe_r)).sum())}
    tot = np.bincount(near[fid], minlength=len(cx)); got = np.bincount(near[hit], minlength=len(cx))
    out['per_pad'] = [[int(vm[i]), int(chn[i]), round(float(cx[i]), 1), round(float(cy[i]), 1), int(tot[i]), int(got[i])]
                      for i in range(len(cx)) if tot[i]]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--gap', type=float, default=100)
    ap.add_argument('--burst', type=int, default=36)
    ap.add_argument('--max-tag', type=int, default=6)
    ap.add_argument('--cluster-r', type=float, default=15)
    ap.add_argument('--probe-r', type=float, default=15)
    ap.add_argument('--fid-r', type=float, default=9)
    ap.add_argument('--frame-win', type=float, default=25)
    ap.add_argument('--win', type=float, default=100)
    ap.add_argument('--sideband', type=float, default=4000)
    a = ap.parse_args()
    tab, PX, PY = pad_lookup()
    V, T, C = load(a.files)
    X, Y = PX[V, C], PY[V, C]
    d = np.array([CHAMBER.get(int(v), 0) for v in V])
    ev = np.concatenate([[0], np.cumsum(np.diff(T) > a.gap)])
    ne = int(ev[-1]) + 1
    nev = np.bincount(ev, minlength=ne)
    cl = clusters(ev, d, X, Y, T, ne, a.max_tag, a.cluster_r)
    span = float((T[-1] - T[0]) / 1e9)
    res = {'n_hits': int(len(T)), 'span_s': round(span, 1), 'unmapped_hit_frac': float(np.mean(~np.isfinite(X))),
           'params': vars(a)}
    for p, tags in ((2, (3, 1)), (1, (3, 2)), (3, (2, 1))):
        r = probe(V, T, X, Y, cl, nev <= a.burst, p, tags, a, tab)
        res[f'probe_det{p}'] = r
        if 'eff' in r:
            al = r.get('alignment', {})
            print(f'det{p} ({DET_STATION[p]}): tagged {r["n_tagged"]}, fiducial {r["n_fiducial"]}, eff {r["eff"]:.3f} +- {r["err"]:.3f}, '
                  f'acc {r["accidental"]:.4f}, any-hit-in-window {r["eff_any_hit_in_window"]:.3f}; lat {r["latency_ns"]:.0f} ns; '
                  f'align rot {al.get("rot_deg", 0):.1f} deg shift {np.round(al.get("shift_mm", [0, 0]), 1)} res rms {np.round(al.get("res_rms_mm") or [0, 0], 1)}')
            print('   per chip:', {v: round(c['k'] / c['n'], 2) for v, c in r['per_chip'].items()})
        else:
            print(f'det{p}: {r}')
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
