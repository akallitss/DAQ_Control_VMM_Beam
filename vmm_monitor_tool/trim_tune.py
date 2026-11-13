#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lower the chip thresholds and keep the noisy channels alive by RAISING them
one by one with their trim, instead of masking them.

Starting from an exception file whose channels have trims with room to go up
(made by `vmm_monitor_scan.py equalize`), optionally shifted by --k DAC counts
on the detector chips, it repeats:

    short noise capture -> rate of every channel -> every channel above
    --ch-limit-hz is raised by --steps trim steps (about 1.1 mV each)

until no channel is above the limit (or --max-iter). A channel that is already
at trim 0 cannot go higher: it is reported, and masked only with --mask-at-limit.

Uses noise_probe.py: NO DAQ run and NO high voltage. So this tunes against
electronic noise with the detectors unpowered; noise that only appears with HV
on is for the adaptive scan (which can use the same raise step) to handle.

    trim_tune.py --ext START.txt --k -8 --tag night1 [--steps 3] [--ch-limit-hz 50]

Writes config_ext/<START>_tune<tag>_K<k>_i<n>.txt for every iteration; the
last one is the result. A JSON log of every decision goes to --out-dir.
"""

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import noise_probe  # noqa: E402
import vmm_trim  # noqa: E402

CFG_DIR = os.path.join(noise_probe.BENCH, 'config_ext')
DET_CHIPS = list(range(2, 20))


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def klabel(k):
    return 'p%d' % k if k > 0 else ('m%d' % -k if k < 0 else '0')


def rates_of(res):
    live = max(res['live_s'], 1.0)
    return {int(c): {int(ch): n / live for ch, n in d.items()} for c, d in res['hits'].items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ext', required=True, help='starting file (name in config_ext/)')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--k', type=int, default=0, help='shift of the chip sdt of every tuned chip, DAC counts')
    ap.add_argument('--chips', default='2-19', help='chips to tune (default the detector chips 2-19)')
    ap.add_argument('--steps', type=int, default=3, help='trim steps a hot channel is raised per iteration')
    ap.add_argument('--ch-limit-hz', type=float, default=50.0)
    ap.add_argument('--max-iter', type=int, default=8)
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--max-mb', type=int, default=400)
    ap.add_argument('--unmask', help='chips whose masks are removed at the start (e.g. 2-19), '
                                     'so that the tuner decides between raising and nothing')
    ap.add_argument('--mask-at-limit', action='store_true',
                    help='mask a channel that is still hot at trim 0 (default: leave it and report)')
    ap.add_argument('--out-dir', default=os.path.expanduser('~/trim_work/out/tune'))
    ap.add_argument('--restore', action='store_true', help='configure the chips with the GUI selection at the end')
    a = ap.parse_args()

    if noise_probe.daq_run_active() or noise_probe.capture_active():
        raise SystemExit('a DAQ run or a capture is active: not touching anything')
    chips = vmm_trim.parse_list(a.chips, 0, 19, 'chip')
    start = os.path.basename(a.ext)
    ext = vmm_trim.ExtFile.load(os.path.join(CFG_DIR, start))
    sdt = ext.sdt()
    for c in chips:
        ext.set_sdt(c, sdt[c] + a.k)
    trims = vmm_trim.full_trims(ext, chips)
    if a.unmask:
        for c in vmm_trim.parse_list(a.unmask, 0, 19, 'chip'):
            was = ext.unmask(c, list(range(64)))
            if was:
                log('chip %d: %d masks removed at the start' % (c, len(was)))
    os.makedirs(a.out_dir, exist_ok=True)
    stem = '%s_tune%s_K%s' % (start[:-4], a.tag, klabel(a.k))
    history = {'start': start, 'k': a.k, 'steps': a.steps, 'ch_limit_hz': a.ch_limit_hz, 'chips': chips,
               'time': datetime.now().isoformat(timespec='seconds'), 'iterations': []}
    hist_path = os.path.join(a.out_dir, stem + '.json')
    original = noise_probe.gui('/chip_config/status').get('selected')
    raised_total, stuck, final = {}, {}, None
    try:
        for it in range(1, a.max_iter + 1):
            name = '%s_i%d.txt' % (stem, it)
            all_trims = ext.trims()
            all_trims.update(trims)
            ext.set_trims(all_trims, 'trim_tune %s: K=%+d from %s, iteration %d; raised so far: %s'
                          % (a.tag, a.k, start, it, json.dumps(raised_total, sort_keys=True)))
            ext.write(os.path.join(CFG_DIR, name), force=True)
            log('iteration %d: %s' % (it, name))
            noise_probe.select_and_apply(name)
            pcap = os.path.join(a.out_dir, name[:-4] + '.pcapng')
            mb = noise_probe.capture(pcap, a.seconds, a.max_mb)
            res = noise_probe.count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
            if os.path.exists(pcap):
                os.remove(pcap)
            r = rates_of(res)
            masked = ext.channel_bits('sm')
            hot = {c: {ch: round(v, 1) for ch, v in r.get(c, {}).items()
                       if v > a.ch_limit_hz and ch not in masked.get(c, [])} for c in chips}
            hot = {c: d for c, d in hot.items() if d}
            chip_hz = {c: round(sum(r.get(c, {}).values()), 1) for c in range(20)}
            rec = {'cfg': name, 'mb': round(mb, 1), 'live_s': round(res['live_s'], 1), 'chip_hz': chip_hz,
                   'hot': {str(c): {str(ch): v for ch, v in d.items()} for c, d in hot.items()}, 'raised': {}, 'stuck': {}}
            n_hot = sum(len(d) for d in hot.values())
            log('   %.1f MB in %.0f s; %d channels above %g Hz' % (mb, res['live_s'], n_hot, a.ch_limit_hz))
            if not hot:
                final = name
                history['iterations'].append(rec)
                break
            for c, d in hot.items():
                for ch in d:
                    cur = trims[c][ch]
                    if cur == 0:
                        stuck.setdefault(c, {})[ch] = d[ch]
                        rec['stuck'].setdefault(str(c), {})[str(ch)] = d[ch]
                        if a.mask_at_limit:
                            ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sm   1   # trim_tune: hot at trim 0'
                                             % (c // 2, c % 2, ch))
                        continue
                    new = max(0, cur - a.steps)
                    trims[c][ch] = new
                    raised_total.setdefault(str(c), {})[str(ch)] = raised_total.get(str(c), {}).get(str(ch), 0) + cur - new
                    rec['raised'].setdefault(str(c), {})[str(ch)] = [cur, new]
            history['iterations'].append(rec)
            with open(hist_path, 'w') as f:
                json.dump(history, f, indent=1)
            only_stuck = all(trims[c][ch] == 0 and ch in stuck.get(c, {}) for c, d in hot.items() for ch in d)
            if only_stuck and not a.mask_at_limit and not rec['raised']:
                log('   the remaining hot channels are all at trim 0: nothing more to raise')
                final = name
                break
    finally:
        history['final'] = final
        history['raised_total_steps'] = raised_total
        history['hot_at_trim_0'] = {str(c): d for c, d in stuck.items()}
        with open(hist_path, 'w') as f:
            json.dump(history, f, indent=1)
        if original:
            try:
                noise_probe.gui('/chip_config/select', {'file': original})
                if a.restore:
                    noise_probe.select_and_apply(original)
            except BaseException as e:  # noqa: BLE001
                log('could not restore the GUI selection: %s' % e)
    n_r = sum(len(d) for d in raised_total.values())
    log('RESULT: %s; %d channels raised (%d steps in total); %d channels still hot at trim 0; log %s'
        % (final or 'NOT clean after %d iterations' % a.max_iter, n_r,
           sum(sum(d.values()) for d in raised_total.values()), sum(len(d) for d in stuck.values()), hist_path))


if __name__ == '__main__':
    main()
