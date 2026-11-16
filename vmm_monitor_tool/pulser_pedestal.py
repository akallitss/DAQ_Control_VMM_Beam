#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pedestal and noise per channel from a capture taken with the internal test
pulser and neighbour read-out (files from pulser_files.py).

For every (chip, channel) it separates the hits by the over-threshold flag:
    over = 1   the channel fired by itself (pulsed channels: the pulse; others: noise)
    over = 0   read out only because a neighbour fired -> ADC = baseline + noise
and classifies each channel by its role in the phase:
    P  pulsed channel (st 1)
    U  upper neighbour (pulsed channel + 1)
    L  lower neighbour (pulsed channel - 1)
For the neighbour read-outs it gives n, mean, std (sigma), median and a robust
sigma (1.4826 x MAD), in ADC counts. For the pulsed channels it gives the
over-threshold rate (= pulser rate if every pulse is seen) and the ADC of the
pulse. Several phase captures can be merged later with `merge`.

    pulser_pedestal.py analyse CAPTURE.pcapng --ext PHASE_FILE.txt --chips 2,3 --out RESULT.json
    pulser_pedestal.py merge P0.json P1.json P2.json --out MERGED.json

numpy only; reads pcapng blocks directly (same decoding as
cosmic_vmm_analysis/neighbor_baseline.py: VM3 payload, 6-byte words from
offset 16; chip = d1>>22 & 0x1F, adc = d1>>12 & 0x3FF, channel = d2>>8 & 0x3F,
over-threshold flag = d2>>14 & 1). Run it under a memory cap on the 8 GB box.
"""

import argparse
import json
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmm_trim  # noqa: E402

NCH = 64


def decode(path, limit_mb=0):
    """-> chip, channel, adc, over (numpy arrays) and the time span (s) of the capture."""
    limit = int(limit_mb * 1e6) if limit_mb else None
    out = ([], [], [], [])
    t0 = t1 = None
    tick = 1e-6
    done = 0
    with open(path, 'rb', buffering=16 << 20) as f:
        head = f.read(12)
        if len(head) < 12 or struct.unpack('<I', head[8:12])[0] != 0x1A2B3C4D:
            raise SystemExit('%s: not a little-endian pcapng file' % path)
        f.seek(0)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            btype, blen = struct.unpack('<II', hdr)
            body = f.read(blen - 8)
            if len(body) < blen - 8:
                break
            done += blen
            if limit and done > limit:
                break
            if btype == 1:
                o = 8
                while o + 4 <= len(body) - 4:
                    code, ln = struct.unpack('<HH', body[o:o + 4])
                    if code == 0:
                        break
                    if code == 9 and ln >= 1:
                        r = body[o + 4]
                        tick = 2.0 ** -(r & 0x7F) if r & 0x80 else 10.0 ** -r
                    o += 4 + ((ln + 3) & ~3)
                continue
            if btype != 6:
                continue
            _, ts_hi, ts_lo, caplen, _ = struct.unpack('<IIIII', body[:20])
            data = body[20:20 + caplen]
            off = 14
            if data[12:14] == b'\x81\x00':
                off = 18
            elif data[12:14] != b'\x08\x00':
                continue
            if len(data) < off + 28 or data[off + 9] != 17:
                continue
            p = data[off + (data[off] & 0x0F) * 4 + 8:]
            if len(p) < 22 or p[4:7] != b'VM3':
                continue
            t = ((ts_hi << 32) | ts_lo) * tick
            t0 = t if t0 is None else t0
            t1 = t
            nw = (len(p) - 22 + 5) // 6
            b = np.frombuffer(p, dtype=np.uint8, count=6 * nw, offset=16).reshape(-1, 6).astype(np.uint32)
            d1 = (b[:, 0] << 24) | (b[:, 1] << 16) | (b[:, 2] << 8) | b[:, 3]
            d2 = (b[:, 4] << 8) | b[:, 5]
            m = (d2 & 0x8000) != 0
            if m.any():
                d1, d2 = d1[m], d2[m]
                out[0].append(((d1 >> 22) & 0x1F).astype(np.uint8))
                out[1].append(((d2 >> 8) & 0x3F).astype(np.uint8))
                out[2].append(((d1 >> 12) & 0x3FF).astype(np.uint16))
                out[3].append(((d2 >> 14) & 1).astype(np.uint8))
    arrs = [np.concatenate(x) if x else np.zeros(0, dtype=np.uint16) for x in out]
    return arrs, ((t1 - t0) if t0 is not None else 0.0)


def hist_stats(h):
    """Statistics of an ADC histogram (1024 bins)."""
    n = int(h.sum())
    if n == 0:
        return {'n': 0}
    x = np.arange(h.size)
    mean = float((h * x).sum() / n)
    std = float(np.sqrt(max((h * (x - mean) ** 2).sum() / n, 0.0)))
    c = np.cumsum(h)
    med = int(np.searchsorted(c, (n + 1) / 2.0))
    dev = np.abs(x - med)
    order = np.argsort(dev, kind='stable')
    cd = np.cumsum(h[order])
    mad = float(dev[order][np.searchsorted(cd, (n + 1) / 2.0)])
    lo = int(np.searchsorted(c, 0.01 * n))
    hi = int(np.searchsorted(c, 0.99 * n))
    return {'n': n, 'mean': round(mean, 2), 'std': round(std, 2), 'median': med,
            'sigma_mad': round(1.4826 * mad, 2), 'p01': lo, 'p99': hi}


def roles(pulsed):
    """{channel: role} for one chip: P pulsed, U = pulsed+1, L = pulsed-1 (a channel can be both U and L
    only if two pulsed channels are 2 apart, which the 3-phase pattern avoids)."""
    r = {}
    for ch in pulsed:
        r[ch] = 'P'
    for ch in pulsed:
        if ch + 1 < NCH and ch + 1 not in r:
            r[ch + 1] = 'U'
        if ch - 1 >= 0 and ch - 1 not in r:
            r[ch - 1] = 'L'
    return r


def analyse(pcap, ext_path, chips, limit_mb=0):
    (chip, ch, adc, over), span = decode(pcap, limit_mb)
    ext = vmm_trim.ExtFile.load(ext_path)
    st = ext.channel_bits('st')
    masked = ext.channel_bits('sm')
    fin = ext.final()
    res = {'pcap': os.path.basename(pcap), 'ext': os.path.basename(ext_path), 'live_s': round(span, 3),
           'hits_total': int(chip.size), 'chips': {}}
    for c in chips:
        sel = chip == c
        cc, aa, oo = ch[sel].astype(np.int64), adc[sel].astype(np.int64), over[sel]
        H_nb = np.zeros((NCH, 1024), dtype=np.int64)
        H_ov = np.zeros((NCH, 1024), dtype=np.int64)
        if cc.size:
            np.add.at(H_nb, (cc[oo == 0], aa[oo == 0]), 1)
            np.add.at(H_ov, (cc[oo == 1], aa[oo == 1]), 1)
        pulsed = sorted(st.get(c, []))
        role = roles(pulsed)
        chans = {}
        for k in range(NCH):
            nb, ov = hist_stats(H_nb[k]), hist_stats(H_ov[k])
            chans[str(k)] = {'role': role.get(k, '-'), 'masked': k in masked.get(c, []),
                             'over_rate_hz': round(ov['n'] / span, 1) if span else None,
                             'nb': nb, 'over': ov}
        pr = [chans[str(k)]['over_rate_hz'] for k in pulsed if chans[str(k)]['over_rate_hz'] is not None]
        res['chips'][str(c)] = {
            'pulsed': pulsed,
            'sdp10': fin.get((c, 'bank1spi1', 'sdp10')),
            'sng': fin.get((c, 'bank1spi2', 'sng')),
            'pulsed_rate_hz_median': float(np.median(pr)) if pr else None,
            'channels': chans}
    return res


def print_table(res):
    print('%s  live %.2f s  %d hits' % (res['pcap'], res['live_s'], res['hits_total']))
    for c, d in res['chips'].items():
        print('chip %s: sdp10 %s sng %s, %d pulsed, median pulsed rate %s Hz'
              % (c, d['sdp10'], d['sng'], len(d['pulsed']), d['pulsed_rate_hz_median']))
        print('  ch role  over/s    pulse ADC (mean+-std) | nb n     mean   std  med  sig_mad  p01..p99')
        for k in range(NCH):
            x = d['channels'][str(k)]
            nb, ov = x['nb'], x['over']
            pulse = ('%6.1f +- %5.1f' % (ov['mean'], ov['std'])) if ov['n'] else '      -        '
            nbs = ('%6d %7.1f %5.1f %4d %6.1f  %4d..%d' % (nb['n'], nb['mean'], nb['std'], nb['median'],
                                                         nb['sigma_mad'], nb['p01'], nb['p99'])) if nb['n'] else '     0'
            print('  %2d  %s%s  %8.1f  %s | %s' % (k, x['role'], '*' if x['masked'] else ' ', x['over_rate_hz'] or 0,
                                                pulse, nbs))


def merge(paths):
    """Combine the phases: per channel, its L, U and combined neighbour statistics (combined from the
    pooled moments of L and U)."""
    out = {'phases': [os.path.basename(p) for p in paths], 'chips': {}}
    for p in paths:
        with open(p) as f:
            r = json.load(f)
        for c, d in r['chips'].items():
            oc = out['chips'].setdefault(c, {})
            for k, x in d['channels'].items():
                if x['role'] in ('L', 'U') and x['nb']['n']:
                    oc.setdefault(k, {})[x['role']] = dict(x['nb'], phase_file=r['pcap'])
    for c, oc in out['chips'].items():
        for k, d in oc.items():
            parts = [d[r] for r in ('L', 'U') if r in d]
            n = sum(q['n'] for q in parts)
            mean = sum(q['n'] * q['mean'] for q in parts) / n
            var = sum(q['n'] * (q['std'] ** 2 + (q['mean'] - mean) ** 2) for q in parts) / n
            d['combined'] = {'n': n, 'mean': round(mean, 2), 'std': round(var ** 0.5, 2)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('analyse')
    s.add_argument('pcap')
    s.add_argument('--ext', required=True, help='the exception file the capture was taken with')
    s.add_argument('--chips', required=True)
    s.add_argument('--mb', type=float, default=0, help='read only the first MB (0 = all)')
    s.add_argument('--out', required=True)
    s.add_argument('--quiet', action='store_true')
    m = sub.add_parser('merge')
    m.add_argument('results', nargs='+')
    m.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    if a.cmd == 'analyse':
        res = analyse(a.pcap, a.ext, vmm_trim.parse_list(a.chips, 0, vmm_trim.N_CHIPS - 1, 'chip'), a.mb)
        with open(a.out, 'w') as f:
            json.dump(res, f, indent=1)
        if not a.quiet:
            print_table(res)
        print('wrote', a.out)
    else:
        res = merge(a.results)
        with open(a.out, 'w') as f:
            json.dump(res, f, indent=1)
        print('wrote', a.out)


if __name__ == '__main__':
    main()
