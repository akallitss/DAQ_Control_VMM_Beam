#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exception files for a noise rate scan done with the per-channel TRIM of a
few channels, instead of the chip threshold (sdt): only the chosen channels
move towards their noise, the rest of the chip stays where it is (no chip-wide
flood, no dead time shared by 64 channels at once).

For every step N in --steps, the chosen channels get trim = their trim in the
base file + N (a trim lowers the threshold), capped at 31. Everything else is
the base file. Standard library + vmm_trim.

    trimscan_files.py BASE.txt --chip 3 --channels 3,7,10 --steps 0,2,4 --out-prefix DIR/NAME
        -> DIR/NAME_t0.txt, DIR/NAME_t2.txt, ...
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmm_trim  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('base')
    ap.add_argument('--chip', type=int, required=True)
    ap.add_argument('--channels', required=True)
    ap.add_argument('--steps', required=True, help='trim steps added, e.g. 0,2,4,6')
    ap.add_argument('--out-prefix', required=True)
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    chans = vmm_trim.parse_list(a.channels, 0, 63, 'channel')
    steps = [int(s) for s in a.steps.split(',')]
    base = vmm_trim.ExtFile.load(a.base)
    t0 = vmm_trim.full_trims(base, [a.chip])[a.chip]
    for n in steps:
        ext = vmm_trim.ExtFile.load(a.base)
        trims = {c: dict(v) for c, v in ext.trims().items()}
        trims[a.chip] = dict(t0)
        for ch in chans:
            trims[a.chip][ch] = min(31, t0[ch] + n)
        ext.set_trims(trims, note='trim scan: chip %d channels %s at base trim + %d (max 31)' % (a.chip, a.channels, n))
        out = '%s_t%d.txt' % (a.out_prefix, n)
        ext.write(out, a.force)
        print('step +%2d: %s -> %s' % (n, ' '.join('%d:%d' % (ch, trims[a.chip][ch]) for ch in chans), out))


if __name__ == '__main__':
    main()
