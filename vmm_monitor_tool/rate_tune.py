#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Closed-loop trim tuning of a target-mode file on the MEASURED noise rate of every channel
(HV off, no DAQ run; uses noise_probe.py).

The monitor reads thresholds in 1 mV steps, so placing channels at a noise rate from maps alone
leaves ~1.3 mV rms, i.e. a factor ~5 in rate (16 Nov). Here each iteration:

    GUI apply + capture (--seconds) -> rate R_ch of every channel
    slope s_ch = -d ln(rate)/d(mV) of the channel's own scan curve near its present threshold
    dd = ln(R_ch / R) / s_ch        (mV the channel must move up; negative = may move down)
    R_ch > --hot x R  : trim -= ceil(dd / step)                 (raise)
    R_ch < R / --quiet: trim -= max(-2, floor(dd / step))       (lower, at most 2 steps, never
                        closer than --min-d mV to its baseline, estimated from --map)
    R_ch > --burst-hz : a burst channel (developer summary item 1), listed, not tuned
A channel that would need a trim < 0 is listed (it cannot go higher with the trims).

    rate_tune.py --ext START.txt --map MAP_OF_START.json --fit FIT.json --tag t50 [--iter 3]
writes config_ext/<START>_rt<i>.txt and <out-dir>/rt<i>.json; configures the GUI file at the end.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import noise_probe as NP  # noqa: E402
import vmm_monitor_scan as VS  # noqa: E402
import vmm_trim  # noqa: E402

EXT_DIR = os.path.join(NP.BENCH, 'config_ext')


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def local_slope(points, d, default):
    """-d ln(rate)/dd of the scan curve around d (mV^-1), from the two tail points around it."""
    pts = [(x, r) for x, r in VS._usable(points) if r > 0]
    best = None
    for (d1, r1), (d2, r2) in zip(pts, pts[1:]):
        if d2 > d1 and r1 > r2:
            s = (math.log(r1) - math.log(r2)) / (d2 - d1)
            dist = 0 if d1 <= d <= d2 else min(abs(d - d1), abs(d - d2))
            if best is None or dist < best[0]:
                best = (dist, s)
    return best[1] if best and best[0] < 3.0 else default


def decide(rates, live, trims, d_est, fitc, step, rate, hot, quiet, burst_hz, min_d, default_slope=1.0):
    """New trims of one chip. Returns (new trims, actions {ch: text})."""
    new, act = dict(trims), {}
    for ch, t in trims.items():
        r = rates.get(ch, 0.0)
        if r > burst_hz:
            act[ch] = 'burst %.0f Hz, not tuned' % r
            continue
        x = fitc.get(str(ch), {})
        s = local_slope(x.get('points', []), d_est.get(ch, 0.0), default_slope)
        if r > hot * rate:
            n = int(math.ceil(math.log(r / rate) / s / step))
            if t - n < 0:
                act[ch] = 'needs %d steps up, only %d left: at trim 0' % (n, t)
                n = t
            if n:
                new[ch] = t - n
                act[ch] = act.get(ch, '') or 'raised %d (%.0f Hz)' % (n, r)
        elif r < rate / quiet:
            rr = max(r, 1.0 / max(live, 1.0))
            n = min(2, int(math.floor(math.log(rate / rr) / s / step)))
            n = min(n, vmm_trim.TRIM_MAX - t, int(math.floor((d_est.get(ch, 99.0) - min_d) / step)))
            if n > 0:
                new[ch] = t + n
                act[ch] = 'lowered %d (%.1f Hz)' % (n, r)
    return new, act


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ext', required=True, help='start file, name in config_ext/')
    ap.add_argument('--map', required=True, help='monitor map of the start file (threshold - baseline per channel)')
    ap.add_argument('--fit', required=True)
    ap.add_argument('--trimcurve', default=os.path.expanduser('~/trim_work/out/trimcurve_ch5_40.json'))
    ap.add_argument('--chips', default='2-19')
    ap.add_argument('--rate', type=float, default=50.0)
    ap.add_argument('--hot', type=float, default=2.0)
    ap.add_argument('--quiet', type=float, default=4.0)
    ap.add_argument('--burst-hz', type=float, default=1e4)
    ap.add_argument('--min-d', type=float, default=6.0)
    ap.add_argument('--iter', type=int, default=3)
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--out-dir', required=True)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    chips = vmm_trim.parse_list(a.chips, 0, vmm_trim.N_CHIPS - 1, 'chip')
    fit, m = json.load(open(a.fit)), json.load(open(a.map))
    step = VS._per_chip(a.trimcurve, 'mV_per_trim', 1.1)
    if NP.daq_run_active() or NP.capture_active():
        raise SystemExit('a DAQ run or a capture is active: not touching anything')
    original = NP.gui('/chip_config/status').get('selected')
    base = a.ext[:-4]
    name = a.ext
    ext = vmm_trim.ExtFile.load(os.path.join(EXT_DIR, name))
    trims0 = vmm_trim.full_trims(ext, chips)
    d_est = {c: {ch: m['threshold'][str(c)][str(ch)] - m['baseline'][str(c)][str(ch)] for ch in range(64)
                 if str(ch) in m['baseline'].get(str(c), {})} for c in chips}
    masked = ext.channel_bits('sm')
    try:
        for it in range(a.iter + 1):
            NP.select_and_apply(name)
            pcap = os.path.join(a.out_dir, 'rt%d.pcapng' % it)
            mb = NP.capture(pcap, a.seconds, 300)
            res = NP.count(pcap)
            os.remove(pcap)
            live = res['live_s']
            trims = vmm_trim.full_trims(ext, chips)
            rec = {'iteration': it, 'ext': name, 'time': datetime.now().isoformat(timespec='seconds'), 'mb': mb,
                   'live_s': live, 'hits': res['hits'], 'actions': {}}
            all_r = []
            new_t, n_act = {}, 0
            for c in chips:
                rates = {int(k): v / live for k, v in res['hits'].get(str(c), {}).items()}
                live_ch = [rates.get(ch, 0.0) for ch in range(64) if ch not in masked.get(c, [])]
                all_r += live_ch
                nt, act = decide(rates, live, {ch: v for ch, v in trims[c].items() if ch not in masked.get(c, [])},
                                 {ch: d_est[c].get(ch, 99.0) - (trims[c][ch] - trims0[c][ch]) * step[str(c)]
                                  for ch in trims[c]},
                                 fit['chips'].get(str(c), {}).get('channels', {}), step[str(c)], a.rate, a.hot,
                                 a.quiet, a.burst_hz, a.min_d)
                new_t[c] = {**trims[c], **nt}
                rec['actions'][str(c)] = {str(k): v for k, v in act.items()}
                n_act += sum(1 for ch in nt if nt[ch] != trims[c][ch])
            all_r.sort()
            q = lambda p: all_r[min(len(all_r) - 1, int(p * len(all_r)))]
            no_burst = [r for r in all_r if r <= a.burst_hz]
            rec['summary'] = {'median': q(.5), 'p75': q(.75), 'p90': q(.9), 'p99': q(.99),
                              'over_2R': sum(1 for r in all_r if r > 2 * a.rate), 'sum_no_burst_Hz': sum(no_burst)}
            json.dump(rec, open(os.path.join(a.out_dir, 'rt%d.json' % it), 'w'))
            log('iteration %d (%s): per-channel median %.0f Hz, p75 %.0f, p90 %.0f, p99 %.0f; %d channels > %.0f Hz; '
                'sum without bursts %.0f Hz; %d trim changes next'
                % (it, name, q(.5), q(.75), q(.9), q(.99), rec['summary']['over_2R'], 2 * a.rate,
                   sum(no_burst), n_act))
            if it == a.iter or n_act == 0:
                break
            ext.set_trims(new_t, 'rate_tune iteration %d from %s' % (it + 1, a.ext))
            name = '%s_rt%d.txt' % (base, it + 1)
            ext.write(os.path.join(EXT_DIR, name), force=True)
    finally:
        if original:
            log('restoring %s' % original)
            NP.gui('/chip_config/select', {'file': original})
            NP.select_and_apply(original)
        log('ALLDONE last file %s' % name)


if __name__ == '__main__':
    main()
