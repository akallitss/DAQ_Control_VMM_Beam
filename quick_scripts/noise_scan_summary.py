#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-chip, per-channel hit rate for each point of a noise scan, straight from the pcapng.

Why not the QA: the QA reads decoded hit stores (vmm_processor_watcher must have run) and its
per-file job needs several GB for a multi-GB capture. This reads only the first --mb megabytes
of each run's first capture file, counts hits per (VMM, channel), and divides by the capture
time spanned by those packets (pcapng packet timestamps). The decode is the same as
vmm_pcapng_qa.run_live: a VM3 payload, 6-byte hits from offset 16, vmm = (d1>>22)&0x1F,
channel = (d2>>8)&0x3F, hit valid when d2&0x8000.

The rate is only as good as the capture: if dumpcap drops packets at the highest rates the
numbers are a floor. A short head of a very large file also covers little time (~0.5 s at
4 GB/min), so channel rates below ~1/span Hz are not resolved there.

Usage:  noise_scan_summary.py <runs_dir> <out_dir> [--mb 40] [--runs run_4,run_5,...]
Writes: <out_dir>/noise_scan_summary.csv   run,chip_config,vmm,ch,hits,span_s,rate_hz
        <out_dir>/noise_scan_summary.json  per run: span_s, total_khz, khz_per_vmm
and prints a table of kHz per VMM per run.
"""

import argparse
import csv
import glob
import json
import os
import struct
import sys

import numpy as np
from scapy.all import PcapReader, UDP


def count_head(path, mb, tmp):
    with open(path, 'rb') as f, open(tmp, 'wb') as g:
        g.write(f.read(int(mb * 1e6)))
    cnt, t0, t1 = {}, None, None
    with PcapReader(tmp) as r:
        try:
            for pkt in r:
                if UDP not in pkt:
                    continue
                p = bytes(pkt[UDP].payload)
                if len(p) < 22 or p[4:7] != b'VM3':
                    continue
                t = float(pkt.time)
                t0 = t if t0 is None else t0
                t1 = t
                for j in range(0, len(p) - 22, 6):
                    d1, d2 = struct.unpack_from('>IH', p, j + 16)
                    if d2 & 0x8000:
                        cnt.setdefault((d1 >> 22) & 0x1F, np.zeros(64, dtype=np.int64))[(d2 >> 8) & 0x3F] += 1
        except Exception:
            pass                       # the head is cut mid-packet: expected
    return cnt, (t1 - t0) if t0 is not None else 0.0


def run_key(n):
    try:
        return int(n.split('_')[1])
    except Exception:
        return 10**9


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('runs_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--mb', type=float, default=40)
    ap.add_argument('--runs')
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    names = (a.runs.split(',') if a.runs else
             sorted((os.path.basename(p) for p in glob.glob(os.path.join(a.runs_dir, 'run_*'))), key=run_key))
    tmp = os.path.join(a.out_dir, '.head.pcapng')
    summary, rows = {}, []
    for run in names:
        files = sorted(glob.glob(os.path.join(a.runs_dir, run, '*', 'raw_daq_data', '*.pcapng')))
        if not files:
            continue
        cfg = os.path.basename(os.path.dirname(os.path.dirname(files[0])))
        cnt, span = count_head(files[0], a.mb, tmp)
        tot = sum(int(c.sum()) for c in cnt.values())
        summary[run] = {'chip_config': cfg, 'span_s': round(span, 4), 'hits': tot,
                        'total_khz': round(tot / span / 1e3, 3) if span > 0 else None,
                        'khz_per_vmm': {str(v): round(int(c.sum()) / span / 1e3, 4)
                                        for v, c in sorted(cnt.items())} if span > 0 else {}}
        for v, c in sorted(cnt.items()):
            for ch in np.nonzero(c)[0]:
                rows.append([run, cfg, v, int(ch), int(c[ch]), round(span, 4),
                             round(int(c[ch]) / span, 2) if span > 0 else ''])
        print(f'{run} {cfg}: {tot} hits in {span:.2f} s', flush=True)
    if os.path.exists(tmp):
        os.remove(tmp)
    with open(os.path.join(a.out_dir, 'noise_scan_summary.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['run', 'chip_config', 'vmm', 'ch', 'hits', 'span_s', 'rate_hz'])
        w.writerows(rows)
    with open(os.path.join(a.out_dir, 'noise_scan_summary.json'), 'w') as f:
        json.dump(summary, f, indent=1)

    vmms = sorted({int(v) for s in summary.values() for v in s['khz_per_vmm']})
    print('\nkHz per VMM (rows: run)')
    print('run      cfg' + ' ' * 38 + ''.join(f'{v:>7}' for v in vmms))
    for run, s in summary.items():
        print(f'{run:<8} {s["chip_config"][-28:]:<40}' +
              ''.join(f'{s["khz_per_vmm"].get(str(v), 0):>7.2f}' for v in vmms))


if __name__ == '__main__':
    sys.exit(main())
