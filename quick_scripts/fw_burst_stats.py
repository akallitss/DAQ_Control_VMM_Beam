#!/usr/bin/env python3
"""
Burst signature per channel, for the firmware comparison (fw_compare.sh compare).

For every detector channel above --limit-hz: rate, ADC distribution (median, 10-90 %) and the time between
consecutive hits on that channel (median, and the fraction under 1 us). The bursts reported in
LAB_COSMICS_OBSERVATIONS.md (summary for the developers, item 1) re-fire about 600 ns apart with a nearly
fixed ADC, so these two numbers tell whether a hot channel is the same effect under both firmwares, not just
a similar rate.

Run with the p2-readout python (it needs p2_decode), memory-capped:
    systemd-run --user --scope -q -p MemoryMax=2G /local/p2/p2-readout/venv/bin/python -I \\
        fw_burst_stats.py --limit-hz 1000 --json out.json <take dir | run_N | files...>

A take dir is searched for */raw_daq_data/*.pcapng; run_N means /local/p2/p2data/cosmics_3det_202610/runs/run_N.
Files are streamed chunk by chunk (a 5-min cycle of a bursting config is ~1.4 GB). Live time is the span of
the packet timestamps of each file. Hit-to-hit times are taken within a decoder chunk (pairs across chunk
boundaries are dropped: a negligible fraction for a channel firing at kHz-MHz).
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import p2_decode

RUNS = '/local/p2/p2data/cosmics_3det_202610/runs'
NCH = 20 * 64
DT_BIN_NS = 25.0
DT_NBINS = 800              # 0-20 us in 25 ns bins; last bin = overflow


def take_files(arg):
    if os.path.isfile(arg):
        return [arg]
    base = arg if os.path.isabs(arg) else os.path.join(RUNS, arg)
    files = glob.glob(os.path.join(base, '*', 'raw_daq_data', '*.pcapng'))
    return sorted((f for f in files if os.path.getsize(f) > 10_000), key=os.path.getmtime)


def hist_quantile(h, q, bin_width=1.0):
    tot = h.sum()
    if tot == 0:
        return None
    i = int(np.searchsorted(np.cumsum(h), q * tot))
    return (i + 0.5) * bin_width


def stats(files):
    counts = np.zeros(NCH, np.int64)
    adc_h = np.zeros(NCH * 1024, np.int64)
    dt_h = np.zeros(NCH * DT_NBINS, np.int64)
    live = 0.0
    for f in files:
        tmin = tmax = None
        for h in p2_decode.iter_chunks(f, meta={}):
            pt = np.asarray(h.get('pkt_time_ns', []))
            if len(pt):
                tmin = pt.min() if tmin is None else min(tmin, pt.min())
                tmax = pt.max() if tmax is None else max(tmax, pt.max())
            p2_decode.derive(h)
            good = np.asarray(h['hit_valid'], bool)
            v = np.asarray(h['vmm'])[good].astype(np.int64)
            c = np.asarray(h['ch'])[good].astype(np.int64)
            a = np.minimum(np.asarray(h['adc'])[good].astype(np.int64), 1023)
            t = np.asarray(h['abs_time_ns'])[good]
            ok = (v >= 0) & (v < 20) & (c >= 0) & (c < 64)
            idx, a, t = v[ok] * 64 + c[ok], a[ok], t[ok]
            del h
            if not len(idx):
                continue
            counts += np.bincount(idx, minlength=NCH)
            adc_h += np.bincount(idx * 1024 + a, minlength=NCH * 1024)
            order = np.lexsort((t, idx))
            i_s, t_s = idx[order], t[order]
            same = i_s[1:] == i_s[:-1]
            dt = np.diff(t_s)[same]
            b = np.clip((dt / DT_BIN_NS).astype(np.int64), 0, DT_NBINS - 1)
            dt_h += np.bincount(i_s[1:][same] * DT_NBINS + b, minlength=NCH * DT_NBINS)
        if tmin is not None:
            live += float(tmax - tmin) / 1e9
    return counts, adc_h.reshape(NCH, 1024), dt_h.reshape(NCH, DT_NBINS), live


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('take', nargs='+', help='take dir, run_N, or pcapng files')
    ap.add_argument('--limit-hz', type=float, default=1000.0)
    ap.add_argument('--json', help='also write the table as JSON here')
    a = ap.parse_args()
    files = [f for t in a.take for f in take_files(t)]
    if not files:
        sys.exit(f'no capture files found for {a.take}')
    counts, adc_h, dt_h, live = stats(files)
    live = max(live, 1e-9)
    rows = []
    for i in np.argsort(-counts):
        rate = counts[i] / live
        if rate <= a.limit_hz:
            break
        vmm, ch = divmod(int(i), 64)
        dts = dt_h[i]
        rows.append({'vmm': vmm, 'hyb': vmm // 2, 'hyb_vmm': vmm % 2, 'ch': ch, 'rate_hz': round(rate, 1),
                     'adc_median': hist_quantile(adc_h[i], 0.5), 'adc_p10': hist_quantile(adc_h[i], 0.1),
                     'adc_p90': hist_quantile(adc_h[i], 0.9),
                     # None = median beyond the 20 us range (a slow channel, not a re-firing burst)
                     'dt_median_ns': (lambda m: None if m is None or m >= (DT_NBINS - 1) * DT_BIN_NS else m)(
                         hist_quantile(dts, 0.5, DT_BIN_NS)),
                     'frac_dt_below_1us': round(float(dts[:int(1000 / DT_BIN_NS)].sum() / max(dts.sum(), 1)), 3)})
    chip_rate = {v: round(counts[v * 64:(v + 1) * 64].sum() / live, 1) for v in range(20)}
    # the same without the channels listed above (> limit): the chip's level apart from its bursts
    rates = counts / live
    quiet = np.where(rates > a.limit_hz, 0, counts)
    chip_rate_quiet = {v: round(quiet[v * 64:(v + 1) * 64].sum() / live, 1) for v in range(20)}
    chip_n_hot = {v: int((rates[v * 64:(v + 1) * 64] > a.limit_hz).sum()) for v in range(20)}
    chip_n_alive = {v: int((counts[v * 64:(v + 1) * 64] > 0).sum()) for v in range(20)}
    print(f'{len(files)} file(s), live {live:.0f} s; channels over {a.limit_hz:g} Hz:')
    if not rows:
        print('   none')
    else:
        print('   VMM (H/V)   ch       rate Hz   ADC med [p10-p90]   dt med ns   dt<1us')
    for r in rows:
        dtm = f"{r['dt_median_ns']:9.0f}" if r['dt_median_ns'] is not None else '   >20 us'
        print(f"   {r['vmm']:3d} (H{r['hyb']}V{r['hyb_vmm']})  {r['ch']:3d}  {r['rate_hz']:12.1f}   "
              f"{r['adc_median']:5.0f} [{r['adc_p10']:4.0f}-{r['adc_p90']:4.0f}]   {dtm}   {r['frac_dt_below_1us']:6.3f}")
    print(f'   per hybrid, Hz: all channels / without the {a.limit_hz:g}+ Hz ones '
          f'[channels with hits, hot channels]   (H0 = trigger scintillators)')
    for hyb in range(10):
        cells = []
        for hv in (0, 1):
            v = 2 * hyb + hv
            cells.append(f"V{hv} {chip_rate[v]:10.1f} / {chip_rate_quiet[v]:8.1f} "
                         f"[{chip_n_alive[v]:2d}, {chip_n_hot[v]}]")
        print(f'   H{hyb} (VMM {2 * hyb:2d},{2 * hyb + 1:2d})   ' + '   '.join(cells))
    if a.json:
        with open(a.json, 'w') as f:
            json.dump({'files': files, 'live_s': live, 'limit_hz': a.limit_hz, 'channels': rows,
                       'chip_rate_hz': chip_rate, 'chip_rate_hz_without_hot': chip_rate_quiet,
                       'chip_channels_with_hits': chip_n_alive, 'chip_hot_channels': chip_n_hot}, f, indent=1)


if __name__ == '__main__':
    main()
