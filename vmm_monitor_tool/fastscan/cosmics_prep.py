#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Before cosmics runs with HV on: make sure each config stays under the rate guard.

    cosmics_prep.py --out-dir DIR --mask C:CH,... CFG1 CFG2 ...   -> prints the config names to run

For each config_ext file: write CFG_hvm.txt = CFG + the masks given (known burst channels), configure
it, capture 60 s (noise_probe.py), and if it is above --max-mb-min, mask the channels above --hot-hz
in that capture (into the same _hvm file) and check again (up to 2 rounds). A config still above the
limit after that is left out. The rate guard of the DAQ stops runs above 100 MB/min.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import vmm_trim as VT  # noqa: E402

EXT_DIR = '/local/p2/testbench/TestBenchCERN/config_ext'
PROBE = os.path.join(os.path.dirname(HERE), 'noise_probe.py')


def add_masks(src, dst, masks, note):
    ext = VT.ExtFile.load(os.path.join(EXT_DIR, src))
    have = ext.channel_bits('sm')
    ext.lines.append('# cosmics_prep: %s' % note)
    for c, ch in sorted(masks):
        if ch in have.get(c, []):
            continue
        hyb, vmm = VT.hyb_vmm(c)
        ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sm   1' % (hyb, vmm, ch))
    ext.write(os.path.join(EXT_DIR, dst), force=True)


def probe(name, out_dir, seconds=60):
    subprocess.run(['python3', PROBE, '--ext', name, '--seconds', str(seconds), '--max-mb', '300',
                    '--out-dir', out_dir], check=True)
    with open(os.path.join(out_dir, name[:-4] + '.rates.json')) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--mask', default='')
    ap.add_argument('--max-mb-min', type=float, default=70.0)
    ap.add_argument('--hot-hz', type=float, default=5000.0)
    ap.add_argument('configs', nargs='+')
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    base = {(int(x.split(':')[0]), int(x.split(':')[1])) for x in a.mask.split(',') if x}
    ok = []
    for cfg in a.configs:
        dst = cfg[:-4] + '_hvm.txt'
        masks = set(base)
        for rnd in range(3):
            add_masks(cfg, dst, masks, 'known burst channels + hot channels at HV on (round %d)' % rnd)
            r = probe(dst, os.path.join(a.out_dir, 'r%d' % rnd))
            live = r['live_s'] or 1e-9
            mbm = r['mb'] / live * 60
            hot = {(int(c), int(ch)) for c, h in r['hits'].items() if int(c) >= 2
                   for ch, n in h.items() if n / live > a.hot_hz}
            tot = sum(n for c, h in r['hits'].items() if int(c) >= 2 for n in h.values()) / live
            print('%s round %d: %.0f MB/min, detector chips %.0f kHz, %d channels > %.0f Hz: %s'
                  % (dst, rnd, mbm, tot / 1e3, len(hot), a.hot_hz, sorted(hot)[:20]), flush=True)
            if mbm <= a.max_mb_min:
                ok.append(dst)
                break
            masks |= hot
        else:
            print('%s left out: still above %.0f MB/min' % (dst, a.max_mb_min), flush=True)
    print('CONFIGS ' + ','.join(ok), flush=True)


if __name__ == '__main__':
    main()
