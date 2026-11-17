#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exception file with every channel at the threshold where ITS measured rate (baseline_scan curves) is R.

    bscan_target.py --scan DIR [--scan DIR2 --chips2 14-19 --index2 p04-p15] --eq-file NAME --map-eq MAP
                    --rate 50 --out-ext NAME [--margin 0.5] [--burst-hz 1000]

The x of every point is the channel's own distance above its monitor baseline (baseline_scan), so the
target distance d_t(ch) is where the curve crosses R from above (log-linear, bscan_analyze.crossing),
+ margin, at least --floor mV. Channels without a crossing get their chip median. A channel still
above --burst-hz at the highest X it was measured (a burst, not threshold noise) is masked.
From the equalised file (trim t_eq, chip sdt_eq, distance d_eq from its map):
    d(ch) = d_eq + (S - sdt_eq) k - (t - t_eq) step  ->  t = t_eq + floor((d_eq + dS k - d_t) / step)
dS per chip = the smallest that leaves every normal channel at t >= --headroom; t > 31 is clipped
(the channel stays a little high). --scan2/--chips2/--index2: replace the points of some chips by
those of another run (17 Nov: P2_OUT, chips 14-19, was off during points p04-p15 of the HV-on scan).
"""

import argparse
import json
import math
import os
import statistics as S
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import bscan_analyze as BA  # noqa: E402
import vmm_trim as VT  # noqa: E402
import vmm_monitor_scan as MS  # noqa: E402

EXT_DIR = '/local/p2/p2testbench/TestBenchCERN/config_ext'
SDTCAL = os.path.expanduser('~/trim_work/out/sdtcal.json')
TRIMCURVE = os.path.expanduser('~/trim_work/out/trimcurve_ch5_40.json')


def merged_points(a):
    pts = BA.load_points(a.scan, 'p')
    if not a.scan2:
        return pts
    chips2 = {str(c) for c in VT.parse_list(a.chips2, 0, 19, 'chip')}
    lo, hi = (int(x.lstrip('p')) for x in a.index2.split('-'))
    out = []
    for p in pts:
        if lo <= int(p['index'].lstrip('p')) <= hi:     # drop chips2 from these points
            p = dict(p, distance_mV={c: v for c, v in p['distance_mV'].items() if c not in chips2})
        out.append(p)
    for p in BA.load_points(a.scan2, 'p'):              # and take them from the other run
        out.append(dict(p, distance_mV={c: v for c, v in p['distance_mV'].items() if c in chips2}))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scan', required=True)
    ap.add_argument('--scan2'); ap.add_argument('--chips2', default='14-19'); ap.add_argument('--index2', default='p04-p15')
    ap.add_argument('--eq-file', required=True); ap.add_argument('--map-eq', required=True)
    ap.add_argument('--rate', type=float, default=50.0); ap.add_argument('--margin', type=float, default=0.5)
    ap.add_argument('--floor', type=float, default=6.0); ap.add_argument('--burst-hz', type=float, default=1000.0)
    ap.add_argument('--headroom', type=int, default=1)
    ap.add_argument('--chips', default='2-19')
    ap.add_argument('--mask-extra', default='', help='chip:ch,... masked as well (known burst channels)')
    ap.add_argument('--skip-raise', type=int, default=0,
                    help='a chip with too few crossings: sdt = sdt_eq + this (0 = leave it at the equalised file)')
    ap.add_argument('--out-ext', required=True); ap.add_argument('--report')
    a = ap.parse_args()
    chips = VT.parse_list(a.chips, 2, 19, 'chip')
    cur = BA.curves(merged_points(a))
    m = json.load(open(a.map_eq))
    eq = VT.ExtFile.load(os.path.join(EXT_DIR, a.eq_file))
    t_eq_all = eq.trims()
    sdt_eq = {c: int(m['sdt'][str(c)]) for c in chips}
    step = {int(c): v for c, v in MS._per_chip(TRIMCURVE, 'mV_per_trim', 1.1).items()}
    k = {int(c): v for c, v in MS._per_chip(SDTCAL, 'mV_per_sdt', 0.77).items()}
    eq_masks = eq.channel_bits('sm')
    new_trims, new_sdt, masks, rep = {}, {}, {}, {}
    for c in chips:
        b, thr = m['baseline'][str(c)], m['threshold'][str(c)]
        d_eq = {int(ch): thr[ch] - b[ch] for ch in b}
        t_eq = {ch: int(t_eq_all.get(c, {}).get(ch, 0)) for ch in range(64)}
        dt, why, burst = {}, {}, []
        for ch in range(64):
            cv = cur.get((c, ch))
            if ch in eq_masks.get(c, []):
                continue
            if not cv:
                why[ch] = 'no curve'
                continue
            top = max(cv)                                  # highest-X point it was measured at
            if top[1] > a.burst_hz:
                burst.append(ch)
                continue
            x = BA.crossing(cv, a.rate)
            if x is None:
                why[ch] = 'no crossing'
                continue
            dt[ch] = max(a.floor, x + a.margin)
        if len(dt) < 8:
            rep[c] = {'skipped': 'only %d channels with a crossing' % len(dt)}
            if a.skip_raise:                 # park the chip far above its noise instead of leaving it at X0
                new_sdt[c] = sdt_eq[c] + a.skip_raise
                rep[c]['sdt'] = new_sdt[c]
                print('chip %2d: skipped (%d crossings), sdt %d -> %d (parked)' % (c, len(dt), sdt_eq[c], new_sdt[c]))
            continue
        med = S.median(dt.values())
        for ch in why:
            dt[ch] = med
        normal = [ch for ch in dt if ch not in why]
        need = max((dt[ch] - d_eq[ch] - t_eq[ch] * step[c] + a.headroom * step[c]) / k[c] for ch in normal)
        dS = int(math.ceil(need - 1e-9))
        trims, clipped = {}, []
        for ch in dt:
            t = t_eq[ch] + int(math.floor((d_eq[ch] + dS * k[c] - dt[ch]) / step[c] + 1e-9))
            if t > 31:
                clipped.append(ch)
                t = 31
            trims[ch] = max(0, t)
        for ch in burst + [ch for ch in range(64) if ch not in dt and ch not in burst]:
            trims.setdefault(ch, t_eq[ch])
        new_trims[c], new_sdt[c] = trims, sdt_eq[c] + dS
        extra = {int(x.split(':')[1]) for x in a.mask_extra.split(',') if x and int(x.split(':')[0]) == c}
        masks[c] = sorted(set(burst) | set(eq_masks.get(c, [])) | extra)
        exp = [d_eq[ch] + dS * k[c] - (trims[ch] - t_eq[ch]) * step[c] for ch in normal]
        rep[c] = {'target_median_mV': round(med, 2), 'target_p10_p90': [round(BA.pct(list(dt.values()), .1), 1),
                                                                        round(BA.pct(list(dt.values()), .9), 1)],
                  'sdt_eq': sdt_eq[c], 'sdt': sdt_eq[c] + dS, 'trim_min': min(trims.values()),
                  'trim_max': max(trims.values()), 'clipped': clipped, 'burst_masked': burst,
                  'chip_median_used': sorted(why), 'expected_median_mV': round(S.median(exp), 2),
                  'targets': {str(ch): round(v, 2) for ch, v in dt.items()}}
        print('chip %2d: target median %.1f mV [p10 %.1f p90 %.1f], sdt %d -> %d, trims %d..%d, clipped %d, '
              'burst-masked %s, chip median for %d' % (c, med, rep[c]['target_p10_p90'][0], rep[c]['target_p10_p90'][1],
                                                      sdt_eq[c], sdt_eq[c] + dS, rep[c]['trim_min'], rep[c]['trim_max'],
                                                      len(clipped), burst, len(why)))
    ext = VT.ExtFile(list(eq.lines))
    for c, v in new_sdt.items():
        ext.set_sdt(c, v)
    t = ext.trims()
    for c, d in new_trims.items():
        t[c] = d
    ext.set_trims(t, 'bscan_target: each channel where its measured rate is %g Hz (+%.1f mV), scan %s%s'
                  % (a.rate, a.margin, os.path.basename(a.scan.rstrip('/')), ' + ' + a.scan2 if a.scan2 else ''))
    for c, chans in masks.items():
        hyb, vmm = VT.hyb_vmm(c)
        for ch in chans:
            ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sm   1' % (hyb, vmm, ch))
    ext.write(os.path.join(EXT_DIR, a.out_ext), force=True)
    print('wrote %s (%d masked)' % (a.out_ext, sum(len(v) for v in masks.values())))
    if a.report:
        json.dump(rep, open(a.report, 'w'), indent=1)


if __name__ == '__main__':
    main()
