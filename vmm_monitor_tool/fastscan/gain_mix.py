#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mixed-gain exception file: each detector chip at the highest gain where it is still healthy.

    gain_mix.py --base-gain 9.0 --try 16.0,12.0 --rate 50 --out-ext NAME --summary OUT.json

Inputs per gain g (from the 18 Nov gain scan, ~/trim_work/out/gain_20261118):
  maps/map_g{g}.json                 HV-off monitor map at that gain (baselines)
  hvon_g{g}_R{R}_report.json         bscan_target report (targets per chip, skipped, bursts, clipped)
  config_ext/..._gain{g}_..._gscan_hvon{R}.txt   (4.5: bscan_hvon{R}_v1.txt of 17 Nov)

A chip is HEALTHY at g if: bscan_target did not skip it; <= --max-median-used channels took the chip
median (no crossing); <= --max-burst burst-masked; <= --max-clipped clipped trims; its median baseline
moved by <= --max-db mV vs 4.5 and <= --max-odd channels moved by > 30 mV; and its target in charge
units (median target mV / g) is <= --max-fc-ratio x the same at the base gain (the noise in fC did not
blow up). The chip then takes ALL its lines (sg, sdt, trims, masks) from the g file; otherwise from
the base-gain file. Also writes the per-chip table "threshold at R in fC" vs gain (the physics summary).
"""

import argparse
import json
import os
import re
import statistics as S

EXT_DIR = '/local/p2/p2testbench/TestBenchCERN/config_ext'
W = os.path.expanduser('~/trim_work/out/gain_20261118')
P45 = 'p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_'
LINE = re.compile(r'^\s*Sys\s+0\s+Bec\s+0\s+Hyb\s+(\d+)\s+Vmm\s+(\d+)\s')


def target_file(g, R):
    if g == '4.5':
        return P45 + 'bscan_hvon%d_v1.txt' % R
    return 'p2b-config-cern-ext_gain%s_peaktime200_opt_cosmic_gscan_hvon%d.txt' % (g, R)


def report(g, R):
    if g == '4.5':
        p = os.path.expanduser('~/trim_work/out/bscan_20261116/hvon%d_v1_report.json' % R)
    else:
        p = os.path.join(W, 'hvon_g%s_R%d_report.json' % (g, R))
    return json.load(open(p)) if os.path.exists(p) else {}


def map_shift(g, c, ref):
    m = json.load(open(os.path.join(W, 'maps', 'map_g%s.json' % g)))
    b, rb = m['baseline'].get(str(c), {}), ref['baseline'].get(str(c), {})
    d = [b[k] - rb[k] for k in b if k in rb]
    if not d:
        return None, 64
    return S.median(d), sum(1 for x in d if abs(x) > 30)


def chip_lines(path):
    out = {}
    for ln in open(path):
        m = LINE.match(ln)
        if m and int(m.group(1)) >= 1:
            out.setdefault(int(m.group(1)) * 2 + int(m.group(2)), []).append(ln.rstrip('\n'))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-gain', default='9.0')
    ap.add_argument('--try', dest='tries', default='16.0,12.0', help='gains to try, highest first')
    ap.add_argument('--all-gains', default='4.5,6.0,9.0,12.0,16.0', help='for the fC table')
    ap.add_argument('--rate', type=int, default=50)
    ap.add_argument('--max-median-used', type=int, default=8)
    ap.add_argument('--max-burst', type=int, default=4)
    ap.add_argument('--max-clipped', type=int, default=8)
    ap.add_argument('--max-db', type=float, default=5.0)
    ap.add_argument('--max-odd', type=int, default=3)
    ap.add_argument('--max-fc-ratio', type=float, default=1.3)
    ap.add_argument('--out-ext', required=True)
    ap.add_argument('--summary', required=True)
    a = ap.parse_args()
    ref = json.load(open(os.path.join(W, 'maps', 'map_g4.5.json')))
    R = a.rate
    gains = a.all_gains.split(',')
    reps = {g: report(g, R) for g in gains}
    table = {}
    for c in range(2, 20):
        row = {}
        for g in gains:
            r = reps[g].get(str(c), {})
            if 'target_median_mV' in r:
                row[g] = {'mV': r['target_median_mV'], 'fC': round(r['target_median_mV'] / float(g), 3)}
            elif r:
                row[g] = {'skipped': r.get('skipped')}
        table[str(c)] = row
    base = a.base_gain
    choice, why = {}, {}
    for c in range(2, 20):
        choice[c] = base
        rb = reps[base].get(str(c), {})
        fc_base = rb['target_median_mV'] / float(base) if 'target_median_mV' in rb else None
        for g in a.tries.split(','):
            r = reps[g].get(str(c), {})
            dB, odd = map_shift(g, c, ref)
            reasons = []
            if not r or 'skipped' in r:
                reasons.append('skipped/no report')
            else:
                if len(r.get('chip_median_used', [])) > a.max_median_used:
                    reasons.append('%d without crossing' % len(r['chip_median_used']))
                if len(r.get('burst_masked', [])) > a.max_burst:
                    reasons.append('%d bursts' % len(r['burst_masked']))
                if len(r.get('clipped', [])) > a.max_clipped:
                    reasons.append('%d clipped' % len(r['clipped']))
                fc = r['target_median_mV'] / float(g)
                if fc_base is None or fc > a.max_fc_ratio * fc_base:
                    reasons.append('fC %.2f vs %s at base' % (fc, '%.2f' % fc_base if fc_base else 'none'))
            if dB is None or abs(dB) > a.max_db:
                reasons.append('baseline shift %s' % dB)
            if odd > a.max_odd:
                reasons.append('%d odd baselines' % odd)
            why.setdefault(c, {})[g] = reasons or ['ok']
            if not reasons:
                choice[c] = g
                break
    files = {g: os.path.join(EXT_DIR, target_file(g, R)) for g in set(choice.values()) | {base}}
    lines = {g: chip_lines(p) for g, p in files.items()}
    out = []
    for ln in open(files[base]):
        m = LINE.match(ln)
        if m and int(m.group(1)) >= 1:
            c = int(m.group(1)) * 2 + int(m.group(2))
            if choice.get(c, base) != base:
                continue
        out.append(ln.rstrip('\n'))
    out.append('# gain_mix: chips below taken from other gains: %s'
               % ', '.join('%d:%s' % (c, g) for c, g in sorted(choice.items()) if g != base))
    for c, g in sorted(choice.items()):
        if g != base:
            out.extend(lines[g].get(c, []))
    with open(os.path.join(EXT_DIR, a.out_ext), 'w') as f:
        f.write('\n'.join(out) + '\n')
    json.dump({'rate': R, 'choice': {str(c): g for c, g in choice.items()}, 'why': {str(c): v for c, v in why.items()},
               'fC_table': table}, open(a.summary, 'w'), indent=1)
    print('chip gains: ' + ' '.join('%d:%s' % kv for kv in sorted(choice.items())))
    for c in range(2, 20):
        print('  chip %2d -> %s  %s' % (c, choice[c], why.get(c)))
    print('wrote %s' % a.out_ext)


if __name__ == '__main__':
    main()
