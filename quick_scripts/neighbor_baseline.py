#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-channel ADC baseline from neighbour read-out (sng 1).

With sng = 1 a channel is read out, with its ADC value, whenever a neighbour fires, even if it
is itself below threshold. Those hits carry the over-threshold flag = 0, so their ADC is a direct
measurement of the channel's baseline (no threshold, no pulse). This reads the first --mb
megabytes of a capture and, for every (VMM, channel), accumulates:

    n_over       hits with the over-threshold flag set (the channel itself fired)
    n_nb         hits with the flag clear (neighbour read-outs)
    adc_nb_*     median / mean / std of the ADC of the neighbour read-outs
    adc_over_med median ADC of the over-threshold hits, for reference

Decode (same words as vmm_qa/vmm_pcapng_qa.parse_block): VM3 payload, 6-byte words from offset 16,
d1 = >I, d2 = >H, hit word when d2 bit 15 is set; vmm = d1>>22 & 0x1F; adc = d1>>12 & 0x3FF;
channel = d2>>8 & 0x3F; over-threshold flag = d2>>14 & 1.

Usage:  neighbor_baseline.py <file.pcapng[,file2.pcapng,...]> <out_dir> [--mb 100] [--min-nb 50] [--shared 38]
        Several files (comma separated) are accumulated; the CSV then also says in which file each channel
        was first covered (>= --min-nb neighbour read-outs), and the JSON gives the coverage after each file.
Writes: <out_dir>/neighbor_baseline.csv   vmm,ch,n_over,n_nb,adc_nb_median,adc_nb_mean,adc_nb_std,adc_over_median,first_file
        <out_dir>/neighbor_baseline.json  per-chip summary
and prints a per-chip table: hits, neighbour fraction, channels covered, spread of the baselines.
"""

import argparse
import csv
import json
import os
import sys

import struct

import numpy as np


def decode_head(path, mb, tmp):
    from scapy.all import PcapReader, UDP   # only the slow reader needs scapy
    with open(path, 'rb') as f, open(tmp, 'wb') as g:
        g.write(f.read(int(mb * 1e6)))
    chunks, t0, t1 = [], None, None
    batch_v, batch_c, batch_a, batch_o = [], [], [], []

    def flush():
        if batch_v:
            chunks.append((np.concatenate(batch_v), np.concatenate(batch_c),
                           np.concatenate(batch_a), np.concatenate(batch_o)))
            for b in (batch_v, batch_c, batch_a, batch_o):
                b.clear()

    n = 0
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
                nw = (len(p) - 22 + 5) // 6
                b = np.frombuffer(p, dtype=np.uint8, count=6 * nw, offset=16).reshape(-1, 6).astype(np.uint32)
                d1 = (b[:, 0] << 24) | (b[:, 1] << 16) | (b[:, 2] << 8) | b[:, 3]
                d2 = (b[:, 4] << 8) | b[:, 5]
                m = (d2 & 0x8000) != 0
                if m.any():
                    d1, d2 = d1[m], d2[m]
                    batch_v.append(((d1 >> 22) & 0x1F).astype(np.uint8))
                    batch_c.append(((d2 >> 8) & 0x3F).astype(np.uint8))
                    batch_a.append(((d1 >> 12) & 0x3FF).astype(np.uint16))
                    batch_o.append(((d2 >> 14) & 1).astype(np.uint8))
                n += 1
                if n % 20000 == 0:
                    flush()
        except Exception:
            pass                        # head cut mid-packet
    flush()
    return chunks, (t1 - t0) if t0 is not None else 0.0


def decode_raw(path, mb):
    """Fast reader: walks the pcapng blocks itself (no scapy, no temporary copy). mb = 0 reads the whole
    file. Handles little-endian pcapng with Enhanced Packet Blocks, Ethernet (+ one VLAN tag) / IPv4 / UDP."""
    limit = int(mb * 1e6) if mb else None
    chunks, t0, t1, n = [], None, None, 0
    bv, bc, ba, bo = [], [], [], []

    def flush():
        if bv:
            chunks.append((np.concatenate(bv), np.concatenate(bc), np.concatenate(ba), np.concatenate(bo)))
            for b in (bv, bc, ba, bo):
                b.clear()

    done = 0
    tick = 1e-6                                         # default pcapng resolution; the interface block can override it
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
                break                                   # truncated last block (file still being written)
            done += blen
            if limit and done > limit:
                break
            if btype == 1:                              # Interface Description Block: look for if_tsresol (option 9)
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
            if btype != 6:                              # Enhanced Packet Block only
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
                bv.append(((d1 >> 22) & 0x1F).astype(np.uint8))
                bc.append(((d2 >> 8) & 0x3F).astype(np.uint8))
                ba.append(((d1 >> 12) & 0x3FF).astype(np.uint16))
                bo.append(((d2 >> 14) & 1).astype(np.uint8))
            n += 1
            if n % 4000 == 0:
                flush()
    flush()
    return chunks, (t1 - t0) if t0 is not None else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('pcap')
    ap.add_argument('out_dir')
    ap.add_argument('--mb', type=float, default=100, help='megabytes read from the start of each file (0 = whole file)')
    ap.add_argument('--reader', choices=['raw', 'scapy'], default='raw')
    ap.add_argument('--min-nb', type=int, default=50, help='neighbour read-outs needed to call a channel covered')
    ap.add_argument('--shared', type=float, default=38, help='the single ADC baseline used for every channel so far')
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    tmp = os.path.join(a.out_dir, '.head.pcapng')
    H_nb = np.zeros((32 * 64, 1024), dtype=np.int64)
    H_ov = np.zeros((32 * 64, 1024), dtype=np.int64)
    first_file = np.full(32 * 64, -1, dtype=np.int64)
    spans, cov_after = [], []
    files = [f for f in a.pcap.split(',') if f]
    for fi, path in enumerate(files):
        chunks, span = decode_raw(path, a.mb) if a.reader == 'raw' else decode_head(path, a.mb, tmp)
        spans.append(span)
        for v, c, ad, ot in chunks:
            key = v.astype(np.int64) * 64 + c.astype(np.int64)
            for H, sel in ((H_nb, ot == 0), (H_ov, ot == 1)):
                if sel.any():
                    H += np.bincount(key[sel] * 1024 + ad[sel], minlength=32 * 64 * 1024).reshape(32 * 64, 1024)
        nb_now = H_nb.sum(axis=1)
        newly = (nb_now >= a.min_nb) & (first_file < 0)
        first_file[newly] = fi
        cov_after.append({str(v): int((nb_now[v * 64:v * 64 + 64] >= a.min_nb).sum()) for v in range(32)
                          if nb_now[v * 64:v * 64 + 64].sum() > 0})
    span = sum(spans)
    if os.path.exists(tmp):
        os.remove(tmp)
    bins = np.arange(1024)

    def stats(H):
        n = H.sum(axis=1)
        mean = (H * bins).sum(axis=1) / np.maximum(n, 1)
        sd = np.sqrt(np.maximum((H * bins ** 2).sum(axis=1) / np.maximum(n, 1) - mean ** 2, 0))
        cum = H.cumsum(axis=1)
        med = (cum >= (n / 2)[:, None]).argmax(axis=1)
        return n, med, mean, sd

    n_nb, med_nb, mean_nb, sd_nb = stats(H_nb)
    n_ov, med_ov, _, _ = stats(H_ov)
    rows, per = [], {}
    for k in range(32 * 64):
        if n_nb[k] + n_ov[k] == 0:
            continue
        v, c = divmod(k, 64)
        rows.append([v, c, int(n_ov[k]), int(n_nb[k]),
                     int(med_nb[k]) if n_nb[k] else '', round(float(mean_nb[k]), 2) if n_nb[k] else '',
                     round(float(sd_nb[k]), 2) if n_nb[k] else '', int(med_ov[k]) if n_ov[k] else '',
                     int(first_file[k])])
    with open(os.path.join(a.out_dir, 'neighbor_baseline.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['vmm', 'ch', 'n_over', 'n_nb', 'adc_nb_median', 'adc_nb_mean', 'adc_nb_std', 'adc_over_median', 'first_file'])
        w.writerows(rows)
    print(f'{len(files)} file(s): ' + ('first %g MB of each' % a.mb if a.mb else 'whole files') + f', {span:.2f} s of capture in total')
    print(f'{"VMM":>4} {"hits":>10} {"nb %":>6} {"covered":>8} {"median":>7} {"min":>5} {"max":>5} {"std":>6} {"|med-shared|>3":>15}')
    for v in range(32):
        sl = slice(v * 64, v * 64 + 64)
        tot = int(n_nb[sl].sum() + n_ov[sl].sum())
        if tot == 0:
            continue
        cov = n_nb[sl] >= a.min_nb
        meds = med_nb[sl][cov]
        per[v] = {'hits': tot, 'nb_fraction': round(float(n_nb[sl].sum()) / tot, 4), 'covered': int(cov.sum()),
                  'median_of_medians': float(np.median(meds)) if len(meds) else None,
                  'min': int(meds.min()) if len(meds) else None, 'max': int(meds.max()) if len(meds) else None,
                  'std': round(float(meds.std()), 2) if len(meds) else None,
                  'n_far_from_shared': int((np.abs(meds - a.shared) > 3).sum()) if len(meds) else 0}
        q = per[v]
        print(f'{v:>4} {tot:>10} {100 * q["nb_fraction"]:>6.1f} {q["covered"]:>5}/64 '
              f'{q["median_of_medians"] if q["median_of_medians"] is not None else "-":>7} '
              f'{q["min"] if q["min"] is not None else "-":>5} {q["max"] if q["max"] is not None else "-":>5} '
              f'{q["std"] if q["std"] is not None else "-":>6} {q["n_far_from_shared"]:>15}')
    with open(os.path.join(a.out_dir, 'neighbor_baseline.json'), 'w') as f:
        json.dump({'pcap': files, 'mb': a.mb, 'span_s': span, 'span_each_s': spans, 'min_nb': a.min_nb,
                   'shared_adc': a.shared, 'covered_after_each_file': cov_after, 'per_vmm': per}, f, indent=1)


if __name__ == '__main__':
    sys.exit(main())
