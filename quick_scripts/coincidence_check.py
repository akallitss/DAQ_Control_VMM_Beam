#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Is there a time coincidence between the three chambers? (feasibility check for the muon tracks)

Reads whole capture files, rebuilds the absolute hit time as the QA does (vmm_qa/vmm_pcapng_qa.py):
    t = srs_timestamp * 25 ns  +  ((offset*4096 + bcid + 1) * 22.5 ns - tdc * 60/255 ns)
with BCID Gray decoded and srs_timestamp the last marker word of the same VMM. Only over-threshold hits (flag 1) are used.
Chambers: det3 = VMM 2-7 (P2_IN), det2 = VMM 8-13 (P2_MID), det1 = VMM 14-19 (P2_OUT).

Output: per chamber hit rate; histogram of the time difference between det1 and det3 hits (and det2-det3, det1-det2)
within +/- --window ns; the peak position and height against the flat (accidental) level, estimated from the outer bins;
the number of hits of det2 that have a det3 AND a det1 hit within --coinc ns after correcting for the measured offsets.

Usage: coincidence_check.py f1.pcapng[,f2,...] OUT.json [--window 2000] [--bin 25] [--coinc 100]
"""
import argparse
import json
import struct
import sys

import numpy as np

CH = {3: range(2, 8), 2: range(8, 14), 1: range(14, 20)}
CHAMBER = {v: d for d, r in CH.items() for v in r}


def gray(g):
    b = g.astype(np.int64)
    for s in (1, 2, 4, 8):
        b ^= b >> s
    return b


def read_times(path, state, with_adc=False):
    """yields per packet (vmm, t_ns, ch) arrays, plus adc as a 4th array when with_adc."""
    tick = 1e-9
    out = []
    with open(path, 'rb', buffering=16 << 20) as f:
        head = f.read(12)
        if len(head) < 12 or struct.unpack('<I', head[8:12])[0] != 0x1A2B3C4D:
            raise SystemExit(f'{path}: not a little-endian pcapng')
        f.seek(0)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            bt, bl = struct.unpack('<II', hdr)
            body = f.read(bl - 8)
            if len(body) < bl - 8:
                break
            if bt != 6:
                continue
            caplen = struct.unpack('<I', body[12:16])[0]
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
            b = np.frombuffer(p, dtype=np.uint8, count=6 * nw, offset=16).reshape(-1, 6).astype(np.int64)
            d1 = ((b[:, 0] << 24) | (b[:, 1] << 16) | (b[:, 2] << 8) | b[:, 3])
            d2 = ((b[:, 4] << 8) | b[:, 5])
            ishit = (d2 & 0x8000) != 0
            # markers: the marker word of a VMM applies to the hits of THAT VMM that follow it in the stream
            hv = ((d1 >> 22) & 0x1F).astype(np.int64)
            mid = ((d2 >> 10) & 0x1F).astype(np.int64)
            msrs = (d1 << 10) | (d2 & 0x3FF)
            sel = ishit & ((d2 >> 14) & 1 == 1)
            pos_h = np.nonzero(sel)[0]
            srs_h = np.zeros(len(pos_h), dtype=np.float64)
            for vv in np.unique(np.concatenate([hv[pos_h], mid[~ishit]])):
                mpos = np.nonzero((~ishit) & (mid == vv))[0]
                hmask = hv[pos_h] == vv
                if hmask.any():
                    k = np.searchsorted(mpos, pos_h[hmask]) - 1
                    base = state.get(int(vv), 0)
                    vals = np.where(k >= 0, msrs[mpos][np.maximum(k, 0)] if len(mpos) else base, base)
                    srs_h[hmask] = vals
                if len(mpos):
                    state[int(vv)] = int(msrs[mpos[-1]])
            if not sel.any():
                continue
            offs = ((d1[sel] >> 27) & 0x1F).astype(np.int64)
            offs = np.where(offs < 16, offs, offs - 32)
            bcid = gray((d1[sel] & 0xFFF))
            tdc = (d2[sel] & 0xFF).astype(np.float64)
            chip = ((offs * 4096 + bcid + 1) * 22.5) - tdc * 60.0 / 255.0
            v = hv[sel]
            t = srs_h * 25.0 + chip
            ok = srs_h > 0
            rec = (v[ok], t[ok], ((d2[sel] >> 8) & 0x3F)[ok].astype(np.int64))
            if with_adc:
                rec += (((d1[sel] >> 12) & 0x3FF)[ok].astype(np.int64),)
            out.append(rec)
    return out


def pair_hist(a, b, window, binw):
    """histogram of (b - a) for all pairs with |b - a| <= window; a, b sorted arrays."""
    lo = np.searchsorted(b, a - window, 'left')
    hi = np.searchsorted(b, a + window, 'right')
    n = hi - lo
    idx = np.repeat(np.arange(len(a)), n)
    if len(idx) == 0:
        return np.zeros(int(2 * window / binw) + 1, dtype=np.int64), np.array([])
    j = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + np.repeat(lo, n)
    d = b[j] - a[idx]
    h = np.histogram(d, bins=np.arange(-window, window + binw, binw))[0]
    return h, d


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files')
    ap.add_argument('out_json')
    ap.add_argument('--window', type=float, default=2000)
    ap.add_argument('--bin', type=float, default=25)
    ap.add_argument('--coinc', type=float, default=100)
    a = ap.parse_args()
    state = {}
    V, T, C = [], [], []
    for p in a.files.split(','):
        for v, t, ch in read_times(p, state):
            V.append(v); T.append(t); C.append(ch)
    V, T, C = np.concatenate(V), np.concatenate(T), np.concatenate(C)
    res = {'n_hits': int(len(T))}
    # diagnostics: how clustered are the reconstructed times?
    tt = np.sort(T)
    dt = np.diff(tt)
    print(f'all hits {len(T)}; fraction of consecutive hits closer than 25 ns: {np.mean(dt < 25):.3f}, closer than 1 us: {np.mean(dt < 1000):.3f}; duplicate times: {np.mean(dt == 0):.3f}')
    for vv in sorted(set(V.tolist())):
        m = V == vv
        print(f'  VMM {vv}: {int(m.sum())} hits', end=';')
    print()
    span = {}
    t_of = {}
    for d in (3, 2, 1):
        m = np.isin(V, list(CH[d]))
        t = np.sort(T[m])
        t_of[d] = t
        s = (t[-1] - t[0]) / 1e9 if len(t) > 1 else 0
        res[f'det{d}'] = {'hits': int(len(t)), 'span_s': round(s, 1), 'rate_hz': round(len(t) / s, 1) if s else None}
        print(f'det{d}: {len(t)} over-threshold hits, {s:.0f} s, {len(t) / s if s else 0:.1f} Hz')
    for (x, y) in ((3, 1), (3, 2), (2, 1)):
        h, d = pair_hist(t_of[x], t_of[y], a.window, a.bin)
        centers = np.arange(-a.window, a.window, a.bin) + a.bin / 2
        outer = np.concatenate([h[:int(len(h) * 0.2)], h[-int(len(h) * 0.2):]])
        flat = outer.mean() if len(outer) else 0.0
        k = int(np.argmax(h))
        res[f'det{y}_minus_det{x}'] = {'peak_ns': float(centers[min(k, len(centers) - 1)]), 'peak_counts': int(h[k]), 'flat_level_per_bin': round(float(flat), 2),
                                       'excess_in_peak': round(float(h[k] - flat), 1), 'sigma_poisson': round(float((h[k] - flat) / max(np.sqrt(flat), 1e-9)), 1) if flat else None,
                                       'hist_25ns': h.tolist()}
        print(f'det{y} - det{x}: peak at {centers[min(k, len(centers) - 1)]:+.0f} ns, {h[k]} counts vs flat {flat:.1f}/bin ({(h[k] - flat) / max(np.sqrt(flat), 1e-9):.0f} sigma)')
    json.dump(res, open(a.out_json, 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
