#!/usr/bin/env python3
"""
One table per take, one column per cycle: the short version of `fw_compare.sh compare`.

    python3 fw_summary.py <OUT_ROOT> <label> [<label>...]

Reads what fw_compare.sh leaves in <OUT_ROOT>/<label>/: runs.txt, sys_regs.txt / fw_version.txt, and per
cycle link_status.txt, warm_reset.log and bursts_<cycle>.json (written by `compare`; `summary` runs the
decode first where it is missing). Rows: warm reset, link status of hybrids 0-9 and of hybrid 11, capture
volume, every channel that was above the limit in at least one cycle (its rate in each), the trigger
scintillators (H0 V0), and the range of the other detector VMMs without their hot channels.
"""

import glob
import json
import os
import re
import sys


def first(path, pattern, group=1, default=None):
    try:
        with open(path, errors='replace') as f:
            for line in f:
                m = re.search(pattern, line)
                if m:
                    return m.group(group)
    except OSError:
        pass
    return default


def links(path):
    out = {}
    try:
        with open(path, errors='replace') as f:
            for line in f:
                m = re.search(r'hybrid\[(\d+)\]\.linkStatus\s+\S+\s+(\d+)', line)
                if m:
                    out.setdefault(int(m.group(1)), int(m.group(2)))
    except OSError:
        pass
    return out


def fmt_rate(r):
    if r is None:
        return '-'
    return f'{r / 1000:.0f} kHz' if r >= 10000 else (f'{r / 1000:.1f} kHz' if r >= 1000 else f'{r:.0f} Hz')


def table(rows, header):
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: '  ' + ' | '.join(str(c).ljust(w) for c, w in zip(r, widths))
    print(line(header))
    print('  ' + '-+-'.join('-' * w for w in widths))
    for r in rows:
        print(line(r))


def summarize(root, label):
    d = os.path.join(root, label)
    try:
        takes = [t.strip() for t in open(os.path.join(d, 'runs.txt')) if t.strip()]
    except OSError:
        print(f'\n{label}: no takes (no {d}/runs.txt)')
        return
    fec = first(os.path.join(d, 'sys_regs.txt'), r'ReadSystemRegisters\.fwVersion\s+(\S+)', default='?')
    hyb = first(os.path.join(d, 'fw_version.txt'), r'firmwareVersion\s+(\S+)', default='?')
    cfg = first(os.path.join(d, 'meta.txt'), r'^config:\s+(\S+)', default='?')
    print(f'\n{label}:  FEC firmware {fec}, hybrid firmware {hyb}')
    print(f'  config {os.path.basename(cfg)}')

    names = [os.path.basename(t) for t in takes]
    data = []
    for t, n in zip(takes, names):
        j = {}
        try:
            j = json.load(open(os.path.join(d, f'bursts_{n}.json')))
        except (OSError, ValueError):
            pass
        mb = sum(os.path.getsize(f) for f in glob.glob(os.path.join(t, '*', 'raw_daq_data', '*.pcapng'))) / 1e6
        data.append({'json': j, 'links': links(os.path.join(t, 'link_status.txt')), 'mb': mb,
                     'wr': first(os.path.join(t, 'warm_reset.log'), r'(\d+) failed hybrids')})
    limit = next((x['json'].get('limit_hz') for x in data if x['json']), None)

    rows = []
    rows.append(['warm reset'] + [f"{x['wr']} failed" if x['wr'] is not None else '-' for x in data])

    def h09(l):
        if not l:
            return '-'
        bad = [f'H{p}={l.get(p, "?")}' for p in range(10) if l.get(p) != 4]
        return 'all 4 (ready)' if not bad else ' '.join(bad)
    rows.append(['link status H0-H9'] + [h09(x['links']) for x in data])
    rows.append(['hybrid 11'] + [str(x['links'].get(11, '-')) for x in data])
    rows.append(['volume'] + [f"{x['mb']:.0f} MB" for x in data])

    hot = {}
    for i, x in enumerate(data):
        for c in x['json'].get('channels', []):
            hot.setdefault((c['vmm'], c['ch']), [None] * len(data))[i] = c
    for (vmm, ch), cells in sorted(hot.items(), key=lambda kv: -max(c['rate_hz'] for c in kv[1] if c)):
        rows.append([f'VMM {vmm} (H{vmm // 2}V{vmm % 2}) ch{ch}'] + [fmt_rate(c['rate_hz']) if c else '-' for c in cells])
    if not hot and any(x['json'] for x in data):
        rows.append([f'channels > {limit:g} Hz'] + ['none' for _ in data])

    def chip(x, v, key='chip_rate_hz'):
        r = x['json'].get(key, {})
        return r.get(str(v), r.get(v))
    rows.append(['H0 V0 (scintillators)'] + [fmt_rate(chip(x, 0)) for x in data])

    def others(x):
        vals = [chip(x, v, 'chip_rate_hz_without_hot') for v in range(2, 20)]
        vals = [v for v in vals if v is not None]
        return f'{min(vals):.0f}-{max(vals):.0f} Hz' if vals else '-'
    rows.append([f'other detector VMMs (w/o {limit:g}+ Hz ch)' if limit else 'other detector VMMs'] +
                [others(x) for x in data])
    table(rows, [''] + names)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for label in sys.argv[2:]:
        summarize(sys.argv[1], label)


if __name__ == '__main__':
    main()
