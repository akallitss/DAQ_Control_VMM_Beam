#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""What do the bursts of the noisy chips (VMM 4, 7, 9) look like in the raw hits?

For the chips given with --vmm it reads the first --mb megabytes of each capture and reports, per chip:

  * which channels carry the hits, in 8 consecutive slices of the capture (does the hot channel stay or walk?)
  * the ADC spread of the hits of the busiest channel (pulse-like, or a fixed value?)
  * the over-threshold flag of those hits
  * the spacing between consecutive hits of the busiest channel inside one packet, in chip clock ticks
    (22.5 ns): a sharp peak means an oscillation at a fixed period, a broad exponential-like spread
    means random noise. BCID is Gray coded; offset is the 5-bit signed overflow count.

Same hit decode as quick_scripts/neighbor_baseline.py (pcapng reader included from there).

Usage: burst_anatomy.py file1.pcapng[,file2,...] OUT.json [--vmm 4,7,9] [--mb 400]
"""

import argparse
import json
import struct
import sys

import numpy as np


def gray2bin(g):
    b = g.astype(np.uint32)
    for s in (1, 2, 4, 8):
        b ^= b >> s
    return b


def read_hits(path, mb, vmms):
    limit = int(mb * 1e6) if mb else None
    tick = 1e-6
    done = 0
    recs = []
    with open(path, 'rb', buffering=16 << 20) as f:
        head = f.read(12)
        if len(head) < 12 or struct.unpack('<I', head[8:12])[0] != 0x1A2B3C4D:
            raise SystemExit(f'{path}: not a little-endian pcapng file')
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
            nw = (len(p) - 22 + 5) // 6
            b = np.frombuffer(p, dtype=np.uint8, count=6 * nw, offset=16).reshape(-1, 6).astype(np.uint32)
            d1 = (b[:, 0] << 24) | (b[:, 1] << 16) | (b[:, 2] << 8) | b[:, 3]
            d2 = (b[:, 4] << 8) | b[:, 5]
            m = (d2 & 0x8000) != 0
            if not m.any():
                continue
            d1, d2 = d1[m], d2[m]
            v = (d1 >> 22) & 0x1F
            sel = np.isin(v, vmms)
            if not sel.any():
                continue
            d1, d2, v = d1[sel], d2[sel], v[sel]
            t = ((ts_hi << 32) | ts_lo) * tick
            offs = (d1 >> 27) & 0x1F
            offs = np.where(offs < 16, offs, offs.astype(np.int64) - 32)
            recs.append((np.full(len(v), t), len(recs) * np.ones(len(v), dtype=np.int64), v.astype(np.uint8),
                         ((d2 >> 8) & 0x3F).astype(np.uint8), ((d1 >> 12) & 0x3FF).astype(np.uint16),
                         ((d2 >> 14) & 1).astype(np.uint8), (d2 & 0xFF).astype(np.uint8),
                         offs.astype(np.int64), gray2bin(d1 & 0xFFF).astype(np.int64)))
    if not recs:
        return None
    return [np.concatenate(c) for c in zip(*recs)]


def analyse(arr, vmm):
    t, pk, v, ch, adc, ot, tdc, off, bc = arr
    m = v == vmm
    if not m.any():
        return None
    t, pk, ch, adc, ot, tdc, off, bc = (x[m] for x in (t, pk, ch, adc, ot, tdc, off, bc))
    out = {'hits': int(len(t)), 'span_s': round(float(t.max() - t.min()), 3)}
    edges = np.linspace(t.min(), t.max() + 1e-9, 9)
    sl = np.clip(np.digitize(t, edges) - 1, 0, 7)
    out['top_channels_per_slice'] = []
    for k in range(8):
        c = ch[sl == k]
        if len(c) == 0:
            out['top_channels_per_slice'].append([])
            continue
        cnt = np.bincount(c, minlength=64)
        top = np.argsort(-cnt)[:3]
        out['top_channels_per_slice'].append([[int(x), round(100 * cnt[x] / len(c), 1)] for x in top if cnt[x]])
    cnt = np.bincount(ch, minlength=64)
    top = int(np.argmax(cnt))
    out['channel_share_pct'] = {str(c): round(100 * cnt[c] / len(ch), 2) for c in np.argsort(-cnt)[:8] if cnt[c]}
    s = ch == top
    out['busiest_channel'] = top
    a = adc[s]
    out['adc_pct_5_50_95'] = [int(np.percentile(a, q)) for q in (5, 50, 95)]
    out['adc_distinct_values'] = int(len(np.unique(a)))
    out['over_threshold_fraction'] = round(float(ot[s].mean()), 4)
    td = tdc[s]
    out['tdc_pct_5_50_95'] = [int(np.percentile(td, q)) for q in (5, 50, 95)]
    # spacing of consecutive hits of the busiest channel inside one packet, in 22.5 ns ticks
    tt = off[s] * 4096 + bc[s]
    pks = pk[s]
    same = pks[1:] == pks[:-1]
    d = (tt[1:] - tt[:-1])[same]
    d = d[(d > 0) & (d < 4096 * 4)]
    if len(d) > 20:
        h = np.bincount(d.astype(np.int64))
        mode = int(np.argmax(h))
        near = float(h[max(mode - 1, 0):mode + 2].sum()) / len(d)
        out['spacing_ticks'] = {'n': int(len(d)), 'median': float(np.median(d)), 'mode': mode,
                                'mode_period_ns': round(mode * 22.5, 1), 'mode_freq_MHz': round(1000 / (mode * 22.5), 3) if mode else None,
                                'fraction_within_1_tick_of_mode': round(near, 3),
                                'pct_5_50_95': [float(np.percentile(d, q)) for q in (5, 50, 95)]}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--vmm', default='4,7,9')
    ap.add_argument('--mb', type=float, default=400)
    a = ap.parse_args()
    vmms = [int(x) for x in a.vmm.split(',')]
    res = {}
    for path in a.files.split(','):
        arr = read_hits(path, a.mb, vmms)
        key = path.split('/runs/')[-1].split('/')[0] + '/' + path.split('/')[-3]
        res[key] = {}
        if arr is None:
            print(key, 'no hits from', vmms)
            continue
        for v in vmms:
            r = analyse(arr, v)
            if r:
                res[key][str(v)] = r
                sp = r.get('spacing_ticks')
                print(f'{key[:60]:<60} VMM {v}: {r["hits"]:>9} hits, top ch {r["busiest_channel"]} '
                      f'({list(r["channel_share_pct"].values())[0]}%), ADC 5/50/95 {r["adc_pct_5_50_95"]}, '
                      + (f'spacing mode {sp["mode"]} ticks ({sp["mode_freq_MHz"]} MHz, {100 * sp["fraction_within_1_tick_of_mode"]:.0f}% near mode)' if sp else 'no spacing'), flush=True)
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
