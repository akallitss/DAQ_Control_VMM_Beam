#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analysis of a baseline_scan.py run: rate of every channel vs its threshold above baseline.

    bscan_analyze.py DIR [--compare DIR_HVOFF] [--out DIR/analysis.json]

Per channel, from the sweep points (point_pNN.json; repeats point_rNN.json separately):
  curve     : (d_mV, rate_Hz, counts, live_s) at every point where it was not masked
  gauss     : fit ln r = ln f0 - d^2 / (2 sigma^2) on points with 3 Hz < r < 5 kHz (weighted by counts)
  tail      : points with d >= 4 sigma whose rate is > 10x the Gaussian prediction and >= 5 counts
  d_at_R    : where the measured curve crosses R (default 1, 10, 50 Hz), log-linear between points
Per chip and X: median / p90 / p99 of the channel rates, number of channels above 1, 10, 100 Hz.
--compare: the same channel in another run (HV off): ratio of rates at the same X, and the
d at 10 Hz difference -> what HV adds (pads, gas, cosmics).
"""

import argparse
import glob
import json
import math
import os
import statistics as S

R_LEVELS = (1.0, 10.0, 50.0)


def load_points(d, kind='p'):
    pts = []
    for f in sorted(glob.glob(os.path.join(d, 'point_%s*.json' % kind))):
        with open(f) as fh:
            pts.append(json.load(fh))
    return pts


def curves(points):
    """{(chip, ch): [(d, rate, counts, live)]} for unmasked channels of swept chips."""
    out = {}
    for p in points:
        live = p['live_s']
        if not live or live < 0.5:
            continue
        for cs in p['distance_mV']:
            masked = set(p['masked'].get(cs, []))
            hits = p['hits'].get(cs, {})
            for chs, dmv in p['distance_mV'][cs].items():
                ch = int(chs)
                if ch in masked:
                    continue
                n = hits.get(chs, 0)
                out.setdefault((int(cs), ch), []).append((dmv, n / live, n, live))
    for k in out:
        out[k].sort()
    return out


def _solve3(m, v):
    """Solve the 3x3 system m x = v (Cramer)."""
    def det(q):
        return (q[0][0] * (q[1][1] * q[2][2] - q[1][2] * q[2][1]) - q[0][1] * (q[1][0] * q[2][2] - q[1][2] * q[2][0])
                + q[0][2] * (q[1][0] * q[2][1] - q[1][1] * q[2][0]))
    D = det(m)
    if abs(D) < 1e-300:
        return None
    out = []
    for j in range(3):
        q = [row[:] for row in m]
        for i in range(3):
            q[i][j] = v[i]
        out.append(det(q) / D)
    return out


def gauss_fit(curve, lo=3.0, hi=5e3):
    """Weighted fit of ln r = A + B d + C d^2 (a Gaussian with a free centre) on 3 Hz < r < 5 kHz.
    sigma = sqrt(-1/2C), centre mu = -B/2C (mV relative to the monitor baseline)."""
    pts = [(d, math.log(r), n) for d, r, n, _ in curve if lo < r < hi and n >= 5]
    if len(pts) < 4:
        return None
    m = [[0.0] * 3 for _ in range(3)]
    v = [0.0] * 3
    for d, y, w in pts:
        f = (1.0, d, d * d)
        for i in range(3):
            v[i] += w * f[i] * y
            for j in range(3):
                m[i][j] += w * f[i] * f[j]
    sol = _solve3(m, v)
    if sol is None:
        return None
    A, B, C = sol
    if C >= 0:
        return {'bad': 'not Gaussian-like (curvature >= 0)', 'n': len(pts)}
    sigma = math.sqrt(-1.0 / (2 * C))
    mu = -B / (2 * C)
    if sigma > 20 or abs(mu) > 40 or A - B * B / (4 * C) > 50:
        return {'bad': 'fit off scale (sigma %.1f, mu %.1f)' % (sigma, mu), 'n': len(pts)}
    res = [y - (A + B * d + C * d * d) for d, y, _ in pts]
    return {'sigma_mV': round(sigma, 3), 'mu_mV': round(mu, 2), 'f0_Hz': round(math.exp(A - B * B / (4 * C)), 1),
            'n': len(pts), 'rms_ln': round(math.sqrt(sum(r * r for r in res) / len(res)), 3),
            'd_range': [min(d for d, _, _ in pts), max(d for d, _, _ in pts)]}


def crossing(curve, R):
    """Smallest d above which the rate stays below R (log-linear interpolation)."""
    c = sorted(curve)
    for i in range(len(c) - 1, 0, -1):
        if c[i - 1][1] >= R > c[i][1]:
            (d1, r1), (d2, r2) = (c[i - 1][0], c[i - 1][1]), (c[i][0], c[i][1])
            if r2 <= 0:
                return round(d2, 2)
            t = (math.log(R) - math.log(r1)) / (math.log(r2) - math.log(r1))
            return round(d1 + t * (d2 - d1), 2)
        if c[i][1] >= R:
            return None          # still above R at the highest d before: not bracketed from above
    return None


def tail_points(curve, g, n_sigma=4.0, factor=10.0, min_counts=5):
    if not g or 'sigma_mV' not in g:
        return []
    s, f0, mu = g['sigma_mV'], g['f0_Hz'], g.get('mu_mV', 0.0)
    out = []
    for d, r, n, live in curve:
        if d - mu >= n_sigma * s and d > g['d_range'][1] and n >= min_counts:
            pred = f0 * math.exp(-(d - mu) ** 2 / (2 * s * s))
            if r > factor * max(pred, 1.0 / live):
                out.append((round(d, 1), round(r, 3), int(n)))
    return out


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


def chip_table(points):
    rows = []
    for p in points:
        live = p['live_s']
        if not live:
            continue
        for cs in sorted(p['distance_mV'], key=int):
            if int(cs) in p.get('left', []):
                continue
            masked = set(p['masked'].get(cs, []))
            hits = p['hits'].get(cs, {})
            r = [hits.get(str(ch), 0) / live for ch in range(64) if ch not in masked]
            if not r:
                continue
            rows.append({'index': p['index'], 'x': p['x_mV'], 'chip': int(cs), 'live_s': round(live, 1),
                         'n_ch': len(r), 'masked': len(masked), 'median': round(S.median(r), 3),
                         'p90': round(pct(r, 0.9), 3), 'p99': round(pct(r, 0.99), 3),
                         'n_gt1': sum(x > 1 for x in r), 'n_gt10': sum(x > 10 for x in r),
                         'n_gt100': sum(x > 100 for x in r), 'sum_Hz': round(sum(r), 1)})
    return rows


def analyse(d):
    pts = load_points(d, 'p')
    cur = curves(pts)
    chans = {}
    for (c, ch), cv in cur.items():
        g = gauss_fit(cv)
        chans['%d:%d' % (c, ch)] = {'curve': [[round(a, 2), round(b, 4), n, round(l, 1)] for a, b, n, l in cv],
                                   'gauss': g, 'tail': tail_points(cv, g),
                                   'd_at': {str(R): crossing(cv, R) for R in R_LEVELS}}
    rep = load_points(d, 'r')
    return {'dir': d, 'n_points': len(pts), 'chip_table': chip_table(pts), 'repeat_table': chip_table(rep),
            'channels': chans, 'repeat_curves': {'%d:%d' % k: v for k, v in curves(rep).items()}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dir')
    ap.add_argument('--compare')
    ap.add_argument('--out')
    a = ap.parse_args()
    res = analyse(a.dir)
    ch = res['channels']
    sig = [v['gauss']['sigma_mV'] for v in ch.values() if v['gauss'] and 'sigma_mV' in v['gauss']]
    print('%s: %d points, %d channels with a curve, %d with a Gaussian core fit (sigma median %.2f mV)'
          % (a.dir, res['n_points'], len(ch), len(sig), S.median(sig) if sig else float('nan')))
    tails = {k: v['tail'] for k, v in ch.items() if v['tail']}
    mus = [v['gauss']['mu_mV'] for v in ch.values() if v['gauss'] and 'mu_mV' in v['gauss']]
    if mus:
        print('Gaussian centre vs monitor baseline: median %+.2f mV, p10 %+.2f, p90 %+.2f' % (S.median(mus), pct(mus, .1), pct(mus, .9)))
    print('channels with a non-Gaussian tail (beyond the fitted range, d >= mu + 4 sigma, > 10x the core, >= 5 counts): %d'
          % len(tails))
    for R in R_LEVELS:
        dd = [v['d_at'][str(R)] for v in ch.values() if v['d_at'][str(R)] is not None]
        if dd:
            print('  d where the rate falls below %g Hz: median %.1f mV, p90 %.1f, p99 %.1f (%d channels)'
                  % (R, S.median(dd), pct(dd, 0.9), pct(dd, 0.99), len(dd)))
    print('\n X   chips  median   p90     p99    n>1Hz n>10 n>100  sum kHz (all swept chips)')
    by_x = {}
    for r in res['chip_table']:
        by_x.setdefault((r['index'], r['x']), []).append(r)
    for (i, x), rows in sorted(by_x.items()):
        print('%3d  %5d  %6.2f %7.2f %7.1f  %5d %4d %5d  %8.1f' % (
            x, len(rows), S.median(r['median'] for r in rows), S.median(r['p90'] for r in rows),
            max(r['p99'] for r in rows), sum(r['n_gt1'] for r in rows), sum(r['n_gt10'] for r in rows),
            sum(r['n_gt100'] for r in rows), sum(r['sum_Hz'] for r in rows) / 1e3))
    if a.compare:
        other = analyse(a.compare)
        oc = other['channels']
        diff10 = [ch[k]['d_at']['10.0'] - oc[k]['d_at']['10.0'] for k in ch
                  if k in oc and ch[k]['d_at']['10.0'] is not None and oc[k]['d_at']['10.0'] is not None]
        if diff10:
            print('\nvs %s: d at 10 Hz, this minus that: median %+.2f mV, p10 %+.2f, p90 %+.2f (%d channels)'
                  % (a.compare, S.median(diff10), pct(diff10, 0.1), pct(diff10, 0.9), len(diff10)))
        res['compare'] = {'dir': a.compare, 'd10_diff_median': S.median(diff10) if diff10 else None}
    out = a.out or os.path.join(a.dir, 'analysis.json')
    with open(out, 'w') as f:
        json.dump(res, f)
    print('wrote %s' % out)


if __name__ == '__main__':
    main()
