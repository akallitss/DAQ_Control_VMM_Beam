#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exception files for the pedestal-and-noise measurement with the VMM
internal test pulser (method of I. Mandjavidze, "On software utilities for
P2/Basket health evaluation", 3 Aug 2026).

Principle: neighbour read-out on (sng 1); the internal pulser feeds every
third channel (st 1 on channels ch % 3 == phase); when a pulsed channel fires,
its two neighbours are read out too, below threshold, with their ADC value.
Those neighbour ADC values sample the baseline (pedestal) and its spread
(noise) of the neighbours. Three phases (0, 1, 2) cover every channel as
lower and upper neighbour (channel 0 only as lower, 63 only as upper).

This only writes text files (standard library + vmm_trim). Starting from an
existing exception file it, for the chosen chips:
    - removes earlier `st` lines (test pulse) of those chips,
    - optionally removes their masks (--unmask; a masked channel gives no data),
    - sets st 1 on the channels with ch % 3 == phase,
    - sets sng 1 (neighbour read-out) and the pulser amplitude sdp10.
Everything else (thresholds, trims, gain, peaking time) is kept.

    pulser_files.py BASE.txt --out NEW.txt --chips 2,3 --phase 0 --sdp10 40 [--unmask]
    pulser_files.py BASE.txt --out-prefix PREFIX --chips 2,3 --sdp10 40 --all-phases [--unmask]
        -> PREFIX_p0.txt, PREFIX_p1.txt, PREFIX_p2.txt

The pulse RATE is not in these files: it is set by FEC registers in the base
YAML (tp_number pulses per 4096-BC frame of 92.16 us -> N x 10.85 kHz).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmm_trim  # noqa: E402

N_CHANNELS = 64


def pulsed_channels(phase):
    return [ch for ch in range(N_CHANNELS) if ch % 3 == phase]


def set_global(ext, chip, reg, field, value, note):
    """Replace (or append) the chip-level line reg/field of a chip."""
    hyb, vmm = vmm_trim.hyb_vmm(chip)
    keep = []
    for ln in ext.lines:
        m = vmm_trim.LINE_RE.match(ln)
        if m and m['field'] == field and m['reg'] == reg and int(m['hyb']) == hyb and int(m['vmm']) == vmm:
            continue
        keep.append(ln)
    ext.lines = keep
    ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg %s Field %s %d # %s' % (hyb, vmm, reg, field, value, note))


def make(base, chips, phase, sdp10, unmask, sng=1):
    ext = vmm_trim.ExtFile.load(base)
    pulsed = pulsed_channels(phase)
    report = {}
    ext.lines.append('')
    ext.lines.append('# ---- pedestal/noise with the internal pulser: phase %d (st 1 on ch %% 3 == %d), '
                     'sng %d, sdp10 %d, chips %s' % (phase, phase, sng, sdp10, ','.join(map(str, chips))))
    for c in chips:
        hyb, vmm = vmm_trim.hyb_vmm(c)
        unmasked = ext.unmask(c, list(range(N_CHANNELS))) if unmask else []
        keep = []
        for ln in ext.lines:                       # drop earlier test-pulse lines of this chip
            m = vmm_trim.LINE_RE.match(ln)
            if (m and m['field'] == 'st' and int(m['hyb']) == hyb and int(m['vmm']) == vmm
                    and vmm_trim.CHAN_RE.match(m['reg'])):
                continue
            keep.append(ln)
        ext.lines = keep
        set_global(ext, c, 'bank1spi2', 'sng', sng, 'neighbour read-out (pedestal/noise measurement)')
        set_global(ext, c, 'bank1spi1', 'sdp10', sdp10, 'test pulse amplitude DAC')
        for ch in pulsed:
            ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field st   1' % (hyb, vmm, ch))
        masked_left = ext.channel_bits('sm').get(c, [])
        report[c] = {'pulsed': pulsed, 'unmasked': unmasked, 'still_masked': masked_left}
    return ext, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('base', help='exception file to start from')
    ap.add_argument('--out', help='output file (one phase)')
    ap.add_argument('--out-prefix', help='with --all-phases: PREFIX_p0.txt ... PREFIX_p2.txt')
    ap.add_argument('--chips', required=True, help='chips (0..19), e.g. 2,3 or 2-19')
    ap.add_argument('--phase', type=int, choices=(0, 1, 2), default=0)
    ap.add_argument('--all-phases', action='store_true')
    ap.add_argument('--sdp10', type=int, required=True, help='pulser DAC (0..1023); 40 gave 100%% efficiency at K=-18')
    ap.add_argument('--unmask', action='store_true', help='remove the masks of these chips (masked channels give no data)')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    if not 0 <= a.sdp10 <= 1023:
        ap.error('sdp10 must be 0..1023')
    chips = vmm_trim.parse_list(a.chips, 0, vmm_trim.N_CHIPS - 1, 'chip')
    if a.all_phases:
        if not a.out_prefix:
            ap.error('--all-phases needs --out-prefix')
        jobs = [(p, '%s_p%d.txt' % (a.out_prefix, p)) for p in (0, 1, 2)]
    else:
        if not a.out:
            ap.error('--out is required (or --all-phases --out-prefix)')
        jobs = [(a.phase, a.out)]
    for phase, out in jobs:
        ext, rep = make(a.base, chips, phase, a.sdp10, a.unmask)
        ext.write(out, a.force)
        for c, r in rep.items():
            print('chip %2d phase %d: %d pulsed channels, unmasked %s, still masked %s'
                  % (c, phase, len(r['pulsed']), r['unmasked'] or '-', r['still_masked'] or '-'))
        print('wrote', out)


if __name__ == '__main__':
    main()
