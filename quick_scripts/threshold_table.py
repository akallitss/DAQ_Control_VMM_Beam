#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-chip and per-channel table from the lab campaign of 22-24 Oct 2026 (gain 4.5 mV/fC, peaking time 200 ns).

Chip table: for every VMM the absolute sdt at which its noise passes 1 kHz (chip total), with the HV on and off, and the setting used
for the weekend "safe" runs (HV-on onset + 8 DAC). sdt = assumed baseline + d, so the sdt values do not depend on the baseline
estimates, only d does. Channel table: ADC baseline of every channel (neighbour read-out, HV on where available, else HV off).

Usage: threshold_table.py DATA_DIR OUT_DIR      (DATA_DIR = .../vmm_threshold_scan)
"""
import csv
import json
import os
import re
import sys

BASE = {2: 208, 3: 182, 4: 199, 5: 212, 7: 200, 8: 197, 9: 191, 10: 210, 11: 204, 12: 210, 13: 188, 14: 201, 15: 180,
        16: 199, 17: 187, 18: 182, 19: 204}          # July ADC-edge estimates, the 'baseline' of the d scale
FIXED = {6: 230}                                      # VMM 6 was held at an absolute sdt of 230
SAFE_D = {2: 24, 3: 35, 4: 30, 5: 35, 7: 34, 8: 32, 9: 35, 10: 26, 11: 28, 12: 38, 13: 32, 14: 38, 15: 42, 16: 30, 17: 32, 18: 30, 19: 38}


def ladder(path, tag):
    out = {}
    for run, v in json.load(open(path)).items():
        m = re.search(r'_%s_d(\d+)$' % tag, v['chip_config'] if 'chip_config' in v else run)
        if not m:
            continue
        for c, kHz in v['khz_per_vmm'].items():
            out.setdefault(int(c), {})[int(m.group(1))] = kHz
    return out


def ladder_burst(path, tag):
    out = {}
    for k, v in json.load(open(path)).items():
        m = re.search(r'_%s_d(\d+)$' % tag, k)
        if not m:
            continue
        for c, r in v.items():
            out.setdefault(int(c), {})[int(m.group(1))] = r['hits'] / max(r['span_s'], 1e-9) / 1e3
    return out


def onset(curve, ds_desc=None):
    for d in sorted(curve, reverse=True):
        if curve[d] >= 1.0:
            return d
    return None


def main():
    D, O = sys.argv[1], sys.argv[2]
    os.makedirs(O, exist_ok=True)
    on = ladder(os.path.join(D, 'analysis_noise_scan/noise_scan_summary.json'), 'm2')
    off = ladder(os.path.join(D, 'analysis_noise_scan_hvoff/noise_scan_summary.json'), 'm2hvoff')
    on479 = ladder_burst(os.path.join(D, 'analysis_hvon/burst_anatomy_hvon.json'), 'n479hvon')
    off479 = ladder_burst(os.path.join(D, 'analysis_n479_hvoff/burst_anatomy_A_C.json'), 'n479hvoff')
    # baselines
    def load(p):
        d = {}
        for r in csv.DictReader(open(p)):
            if r['adc_nb_median'] != '' and int(r['n_nb']) >= 50:
                d[(int(r['vmm']), int(r['ch']))] = (float(r['adc_nb_median']), int(r['n_nb']), float(r['adc_nb_std']))
        return d
    bl_on = load(os.path.join(D, 'analysis_hvon/sng/neighbor_baseline.csv'))
    bl_off = load(os.path.join(D, 'analysis_sng1_hvoff/neighbor_baseline.csv'))
    for k, v in load(os.path.join(D, 'analysis_n479_hvoff/sng/neighbor_baseline.csv')).items():
        bl_off.setdefault(k, v)
    chips = []
    for v in range(2, 20):
        if v in FIXED:
            chips.append([v, 'fixed', '', '', '', '', '', FIXED[v], 'VMM 6 was held at sdt 230 in every scan (silent with HV off, few hits with HV on): onset not measured'])
            continue
        if v in (4, 7, 9):
            # Only the smooth rise below the burst region is a noise onset (chips' hot-channel bursts are threshold independent).
            # With HV on the burst on one channel contaminates the same range (e.g. VMM 9 ch 19), so only the HV-off onset is given.
            lim = {4: 24, 7: 20, 9: 27}[v]
            con = {}
            coff = {d: x for d, x in off479.get(v, {}).items() if d <= lim}
            note = 'unmasked: onset = smooth rise below the bursts, HV off only (HV-on ladder is contaminated by bursts); bursts at higher d are not a threshold effect'
        else:
            con, coff, note = on.get(v, {}), off.get(v, {}), ''
        d_on, d_off = onset(con), onset(coff)
        s_on = BASE[v] + d_on if d_on else None
        s_off = BASE[v] + d_off if d_off else None
        safe = BASE[v] + SAFE_D[v]
        if not d_on and not d_off:
            note = (note + '; ' if note else '') + 'no onset reached down to d=16 (or 14): the chip is quieter than the scan range, so the sdt shown as "safe" is not a measured margin'
        chips.append([v, BASE[v], d_on or '', s_on or '', d_off or '', s_off or '', SAFE_D[v], safe, note])
    with open(os.path.join(O, 'chip_thresholds_gain4.5.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['vmm', 'assumed_baseline_sdt', 'onset_d_HVon', 'onset_sdt_HVon', 'onset_d_HVoff', 'onset_sdt_HVoff', 'safe_d_used', 'safe_sdt_used', 'note'])
        w.writerows(chips)
    with open(os.path.join(O, 'channel_baselines_gain4.5.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['vmm', 'ch', 'baseline_adc_HVon', 'n_readouts_HVon', 'baseline_adc_HVoff', 'n_readouts_HVoff', 'adc_std_HVon', 'high_block_flag'])
        for v in range(2, 20):
            hi = sorted(ch for ch in range(64) if (v, ch) in bl_on and bl_on[(v, ch)][0] > 80)
            for ch in range(64):
                a, b = bl_on.get((v, ch)), bl_off.get((v, ch))
                if a or b:
                    w.writerow([v, ch, a[0] if a else '', a[1] if a else '', b[0] if b else '', b[1] if b else '', a[2] if a else '',
                                int(bool(a and a[0] > 80))])
    print('chip table:')
    print(f'{"VMM":>4}{"base":>6}{"onset d HVon":>14}{"sdt":>6}{"onset d HVoff":>15}{"sdt":>6}{"safe d":>8}{"safe sdt":>9}')
    for r in chips:
        print(f'{r[0]:>4}{str(r[1]):>6}{str(r[2]):>14}{str(r[3]):>6}{str(r[4]):>15}{str(r[5]):>6}{str(r[6]):>8}{str(r[7]):>9}')


if __name__ == '__main__':
    main()
