#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noise sigma per channel, in mV, from a noise rate scan, and its comparison
with the pulser (neighbour read-out) sigma in ADC counts.

Rate scan: the same chips captured at several chip thresholds (sdt), no pulser,
no neighbour read-out. For a channel with Gaussian noise of width sigma, the
rate of noise hits at a threshold d (mV) above its baseline is (Rice):

    R(d) = f0 * exp(-(d - d0)^2 / (2 sigma^2))

so ln R is a parabola in d. d for every channel and capture comes from our
monitor map (threshold - baseline, mV, at the map's sdt) plus the sdt shift of
the capture times the chip's mV per sdt count (sdtcal). Two fits per channel:
    fixed : d0 = 0  (the monitor baseline is the noise centre); ln R linear in d^2
    free  : d0 free (offset between the monitor reading and the comparator)
Points used: rate between --rmin and --rmax (default 0.3 Hz .. 30 kHz), weight =
number of hits (Poisson). Standard library only.

    noise_ratefit.py fit --map MAP.json --sdtcal sdtcal.json --rates DIR --chips 2,3 --out FIT.json
    noise_ratefit.py compare --fit FIT.json --ped A80_P0.ped.json ... --ped2 A160_P0.ped.json ... --out CMP.json
"""

import argparse
import glob
import json
import math
import os
import statistics as S
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmm_trim  # noqa: E402

BENCH_EXT = '/local/p2/p2testbench/TestBenchCERN/config_ext'


def wlstsq(X, y, w):
    """Weighted least squares for small problems: X list of rows. Returns coefficients."""
    n = len(X[0])
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    for row, yy, ww in zip(X, y, w):
        for i in range(n):
            b[i] += ww * row[i] * yy
            for j in range(n):
                A[i][j] += ww * row[i] * row[j]
    # Gaussian elimination
    M = [A[i] + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-30:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_channel(pts, rmin, rmax):
    """pts = [(d_mV, rate_Hz, hits)]. Returns dict with the fixed and free fits."""
    use = [(d, r, h) for d, r, h in pts if rmin <= r <= rmax and h >= 30]
    out = {'n_points': len(use)}
    # Poisson weight (1/var of ln R = hits), capped: beyond a few hundred hits the model, not the
    # counting statistics, limits a point, and the highest-rate points must not dominate.
    w = [min(h, 400) for _, _, h in use]

    def safe_exp(x):
        return round(math.exp(x), 1) if x < 50 else None

    if len(use) >= 2:
        c = wlstsq([[1.0, d * d] for d, _, _ in use], [math.log(r) for _, r, _ in use], w)
        if c and c[1] < 0:
            out['fixed'] = {'sigma_mV': round(math.sqrt(-1 / (2 * c[1])), 3), 'f0_Hz': safe_exp(c[0])}
    if len(use) >= 3:
        c = wlstsq([[1.0, d, d * d] for d, _, _ in use], [math.log(r) for _, r, _ in use], w)
        if c and c[2] < 0:
            s2 = -1 / (2 * c[2])
            d0 = -c[1] / (2 * c[2])
            out['free'] = {'sigma_mV': round(math.sqrt(s2), 3), 'd0_mV': round(d0, 2),
                           'f0_Hz': safe_exp(c[0] - c[1] ** 2 / (4 * c[2]))}
    return out


def cmd_fit(a):
    m = json.load(open(a.map))
    cal = json.load(open(a.sdtcal))['fit']
    chips = vmm_trim.parse_list(a.chips, 0, vmm_trim.N_CHIPS - 1, 'chip')
    files = sorted(glob.glob(os.path.join(a.rates, '*.rates.json')))
    scans = []
    for f in files:
        r = json.load(open(f))
        fin = vmm_trim.ExtFile.load(os.path.join(a.ext_dir, r['ext'])).final()
        sdt = {c: 64 * fin[(c, 'bank1spi2', 'sdt')] + fin[(c, 'bank1spi1', 'sdt')] for c in chips}
        scans.append((r, sdt))
    res = {'map': os.path.basename(a.map), 'rates': [os.path.basename(f) for f in files], 'chips': {}}
    for c in chips:
        cs = str(c)
        k = cal[cs]['mV_per_sdt']
        sdt_map = m['sdt'][cs]
        chans = {}
        for ch in range(64):
            k_ = str(ch)
            if k_ not in m['baseline'][cs]:
                continue
            d_map = m['threshold'][cs][k_] - m['baseline'][cs][k_]
            pts = []
            for r, sdt in scans:
                h = r['hits'].get(cs, {}).get(k_, 0)
                pts.append((d_map + (sdt[c] - sdt_map) * k, h / r['live_s'], h))
            fit = fit_channel(pts, a.rmin, a.rmax)
            fit['baseline_mV'] = m['baseline'][cs][k_]
            fit['d_at_map_mV'] = round(d_map, 2)
            fit['points'] = [(round(d, 2), round(rr, 2)) for d, rr, _ in sorted(pts)]
            chans[k_] = fit
        res['chips'][cs] = {'mV_per_sdt': k, 'sdt_map': sdt_map, 'channels': chans}
        fx = [x['fixed']['sigma_mV'] for x in chans.values() if 'fixed' in x]
        fr = [x['free']['sigma_mV'] for x in chans.values() if 'free' in x]
        d0 = [x['free']['d0_mV'] for x in chans.values() if 'free' in x]
        f0 = [x['free']['f0_Hz'] for x in chans.values() if 'free' in x and x['free']['f0_Hz']]
        print('chip %s: fixed fit %d ch, sigma median %.2f mV [%.2f..%.2f] | free fit %d ch, sigma median %.2f mV, '
              'd0 median %.1f mV, f0 median %.2f MHz'
              % (cs, len(fx), S.median(fx) if fx else -1, min(fx) if fx else -1, max(fx) if fx else -1,
                 len(fr), S.median(fr) if fr else -1, S.median(d0) if d0 else 0, (S.median(f0) / 1e6) if f0 else 0))
    json.dump(res, open(a.out, 'w'), indent=1)
    print('wrote', a.out)


def neighbour_sigma(paths):
    """{chip: {ch: (n, mean, std)}} combining L and U read-outs of all given phase results."""
    acc = {}
    for p in paths:
        r = json.load(open(p))
        for c, d in r['chips'].items():
            for k, x in d['channels'].items():
                if x['role'] in ('L', 'U') and x['nb']['n']:
                    acc.setdefault(c, {}).setdefault(k, []).append(x['nb'])
    out = {}
    for c, chs in acc.items():
        for k, parts in chs.items():
            n = sum(q['n'] for q in parts)
            mean = sum(q['n'] * q['mean'] for q in parts) / n
            var = sum(q['n'] * (q['std'] ** 2 + (q['mean'] - mean) ** 2) for q in parts) / n
            out.setdefault(c, {})[k] = {'n': n, 'mean': mean, 'std': math.sqrt(var), 'parts': parts}
    return out


def pearson(x, y):
    mx, my = S.mean(x), S.mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else float('nan')


def cmd_compare(a):
    fit = json.load(open(a.fit))
    s1 = neighbour_sigma(a.ped)
    s2 = neighbour_sigma(a.ped2) if a.ped2 else None
    out = {'fit': os.path.basename(a.fit), 'ped': [os.path.basename(p) for p in a.ped],
           'ped2': [os.path.basename(p) for p in (a.ped2 or [])], 'amp1': a.amp1, 'amp2': a.amp2, 'chips': {}}
    for c, d in fit['chips'].items():
        rows = {}
        for k, x in d['channels'].items():
            if k not in s1.get(c, {}):
                continue
            q1 = s1[c][k]
            row = {'sigma_adc_a1': round(q1['std'], 3), 'ped_adc_a1': round(q1['mean'], 2)}
            if s2 and k in s2.get(c, {}):
                q2 = s2[c][k]
                row['sigma_adc_a2'] = round(q2['std'], 3)
                row['ped_adc_a2'] = round(q2['mean'], 2)
                # linear in pulse amplitude -> value at zero pulse
                t = a.amp1 / (a.amp2 - a.amp1)
                row['sigma_adc_0'] = round(q1['std'] - t * (q2['std'] - q1['std']), 3)
                row['ped_adc_0'] = round(q1['mean'] - t * (q2['mean'] - q1['mean']), 2)
            for kind in ('fixed', 'free'):
                if kind in x:
                    row['sigma_mV_' + kind] = x[kind]['sigma_mV']
            row['baseline_mV'] = x['baseline_mV']
            rows[k] = row
        summ = {}
        for kind in ('fixed', 'free'):
            for adc in ('sigma_adc_a1', 'sigma_adc_0'):
                pairs = [(r['sigma_mV_' + kind], r[adc]) for r in rows.values()
                         if ('sigma_mV_' + kind) in r and adc in r and r[adc] > 0.5]
                if len(pairs) >= 5:
                    ratio = [p / q for p, q in pairs]
                    summ['%s_vs_%s' % (kind, adc)] = {
                        'n': len(pairs), 'k_mV_per_adc_median': round(S.median(ratio), 3),
                        'k_spread_rel': round(S.pstdev(ratio) / S.mean(ratio), 3),
                        'pearson_r': round(pearson([p for p, _ in pairs], [q for _, q in pairs]), 3)}
        out['chips'][c] = {'summary': summ, 'channels': rows}
        print('chip %s:' % c)
        for key, v in summ.items():
            print('   %-28s n=%2d  k=%.3f mV/ADC  rel spread %.0f%%  r=%.2f'
                  % (key, v['n'], v['k_mV_per_adc_median'], 100 * v['k_spread_rel'], v['pearson_r']))
    json.dump(out, open(a.out, 'w'), indent=1)
    print('wrote', a.out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fit')
    f.add_argument('--map', required=True)
    f.add_argument('--sdtcal', required=True)
    f.add_argument('--rates', required=True, help='directory with the *.rates.json of the scan')
    f.add_argument('--ext-dir', default=BENCH_EXT)
    f.add_argument('--chips', required=True)
    f.add_argument('--rmin', type=float, default=1.0)
    f.add_argument('--rmax', type=float, default=30000.0)
    f.add_argument('--out', required=True)
    f.set_defaults(fn=cmd_fit)
    c = sub.add_parser('compare')
    c.add_argument('--fit', required=True)
    c.add_argument('--ped', nargs='+', required=True, help='pulser_pedestal results, amplitude 1 (all phases)')
    c.add_argument('--ped2', nargs='*', help='same at amplitude 2 (for the zero-pulse extrapolation)')
    c.add_argument('--amp1', type=float, default=80)
    c.add_argument('--amp2', type=float, default=160)
    c.add_argument('--out', required=True)
    c.set_defaults(fn=cmd_compare)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == '__main__':
    main()
