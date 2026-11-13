#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure VMM baselines and thresholds per channel with the hybrid ADC, and
turn the result into trims. Runs ON THE DAQ MACHINE, standard library only.

It drives our `vmm_monitor` program (see vmm_monitor.cpp), which rewrites the
VMM registers for every measurement. Therefore:
  * it refuses to run while a capture (dumpcap/tcpdump) is running;
  * when it is done it asks the DAQ GUI to "Configure chip" again (warm reset
    + the file selected in the GUI), so the chips are back in a known state
    and the run gate is armed properly. Use --no-reapply to skip that.

All values are ADC readings of the VMM monitor output, in mV as returned by
the library (1 unit = 1 mV within our precision; see `sdtcal`).

Commands:
  map       baseline and threshold of every channel of every chip
  trimcurve threshold of a few channels versus trim 0..31 (also proves the bit order)
  sdtcal    threshold-DAC voltage versus sdt: mV per sdt count, per chip
  refine    second pass of equalize, from a map taken with the trims in place
  equalize  from a map, compute the trims that put every channel of a chip at
            the same distance above its own baseline; writes a trim table
  report    print a map

Typical night:
  vmm_monitor_scan.py map --ext config_ext/X.txt --out maps/X.json
  vmm_monitor_scan.py equalize --map maps/X.json --out tables/X_eq.json
  vmm_trim.py apply-table config_ext/X.txt --table tables/X_eq.json --out config_ext/X_eq.txt
  vmm_monitor_scan.py map --ext config_ext/X_eq.txt --out maps/X_eq.json     # check
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vmm_trim  # noqa: E402

BENCH = '/local/p2/p2testbench/TestBenchCERN'
DEFAULT_BASE = os.path.join(BENCH, 'config_base', 'p2b-config-cern-base.yaml')
DEFAULT_BIN = os.path.expanduser('~/vmm_monitor_build/vmm_monitor')
GUI = 'http://localhost:5002'
N_CH, N_CHIPS = vmm_trim.N_CHANNELS, vmm_trim.N_CHIPS
KEY_RE = re.compile(r'hybrid\[(\d+)\]\.vmm\[(\d+)\]\.(\w+)$')


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), file=sys.stderr, flush=True)


def capture_active():
    return subprocess.run(['pgrep', '-x', 'dumpcap|tcpdump'], capture_output=True).returncode == 0


def gui(path, post=False, timeout=20):
    req = urllib.request.Request(GUI + path, data=b'' if post else None, method='POST' if post else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def reapply():
    """Ask the GUI for a normal Configure chip and wait for it."""
    try:
        r = gui('/chip_config/apply', post=True)
    except Exception as e:  # GUI not running: say so, the user must apply by hand
        log('COULD NOT re-apply through the GUI (%s). Press "Configure chip" before any run.' % e)
        return False
    if not r.get('success'):
        log('GUI refused to re-apply: %s. Press "Configure chip" before any run.' % r.get('message'))
        return False
    t0 = time.time()
    time.sleep(5)
    while time.time() - t0 < 400:
        st = gui('/chip_config/status')
        if not st.get('running'):
            last = st.get('last') or {}
            ok = last.get('rc') == 0
            log('chips re-configured by the GUI: %s rc=%s' % (last.get('file'), last.get('rc')))
            return ok
        time.sleep(5)
    log('re-apply did not finish in time. Check the GUI.')
    return False


class Monitor:
    def __init__(self, binary, base, ext, workdir):
        self.binary, self.base, self.ext, self.workdir = binary, base, ext, workdir
        if not os.access(binary, os.X_OK):
            raise SystemExit('vmm_monitor program not found at %s (build it: vmm_monitor_tool/build.sh)' % binary)
        if capture_active():
            raise SystemExit('a capture is running: refusing to touch the chips')
        self.n_calls = 0

    def overlay(self, lines):
        fd, path = tempfile.mkstemp(prefix='ov_', suffix='.txt', dir=self.workdir)
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return path

    def run(self, what, channels=None, overlay_lines=(), repeat=1):
        """-> list of (what, channel, {chip: value}) in the order measured."""
        cmd = [self.binary, '--base', self.base]
        if self.ext:
            cmd += ['--ext', self.ext]
        ov = None
        if overlay_lines:
            ov = self.overlay(overlay_lines)
            cmd += ['--ext', ov]
        for w in ([what] if isinstance(what, str) else what):
            cmd += ['--measure', w]
        if channels is not None:
            cmd += ['--channels', ','.join(map(str, channels))]
        cmd += ['--repeat', str(repeat)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        finally:
            if ov:
                os.unlink(ov)
        self.n_calls += 1
        if r.returncode != 0:
            raise SystemExit('vmm_monitor failed (rc %d): %s' % (r.returncode, r.stderr.strip()[-800:]))
        out = []
        for ln in r.stdout.splitlines():
            if not ln.startswith('{'):
                continue
            d = json.loads(ln)
            vals = {}
            for k, (raw, signed) in d['results'].items():
                m = KEY_RE.search(k)
                if m and not m[3].startswith('_'):
                    vals[vmm_trim.chip_of(int(m[1]), int(m[2]))] = signed
            out.append((d['what'], d['channel'], vals))
        return out


def all_channel_lines(field, value, chips=range(N_CHIPS), channels=range(N_CH)):
    return ['Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field %s %d' % (c // 2, c % 2, ch, field, value)
            for c in chips for ch in channels]


def meta(a, mon):
    with open(os.path.join(os.path.dirname(mon.binary), 'BUILT_AGAINST')) as f:
        built = f.read().strip()
    return {'time': datetime.now().isoformat(timespec='seconds'), 'ext': os.path.basename(a.ext) if a.ext else None,
            'ext_path': a.ext, 'base': a.base, 'built_against': built, 'unit': 'mV (hybrid ADC reading)',
            'sd_bit_reversed': vmm_trim.SD_BIT_REVERSED}


def save(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, indent=1)
    log('wrote %s' % path)


# ------------------------------------------------------------------- map ----
def measure_map(mon, channels, repeat=1, extra=()):
    """baseline (smx 0) and threshold (smx 1) of the given channels, all chips.
    `extra`: more overlay lines (e.g. sm 0 everywhere: a masked channel shows no baseline)."""
    res = {}
    for name, smx in (('baseline', 0), ('threshold', 1)):
        t0 = time.time()
        rows = mon.run('chan', channels, list(extra) + all_channel_lines('smx', smx), repeat)
        acc = {}
        for _, ch, vals in rows:
            for chip, v in vals.items():
                acc.setdefault(chip, {}).setdefault(ch, []).append(v)
        res[name] = {str(c): {str(ch): statistics.median(v) for ch, v in d.items()} for c, d in sorted(acc.items())}
        log('%s of %d channels x %d chips: %.0f s' % (name, len(channels), len(acc), time.time() - t0))
    return res


def cmd_map(a):
    mon = Monitor(a.bin, a.base, a.ext, a.workdir)
    channels = vmm_trim.parse_list(a.channels, 0, N_CH - 1, 'channel')
    out = {'meta': meta(a, mon)}
    try:
        glob = mon.run(['temp', 'thrdac'])
        out['temp_tenth_C'] = {str(c): v for c, v in glob[0][2].items()}
        out['thrdac'] = {str(c): v for c, v in glob[1][2].items()}
        extra = all_channel_lines('sm', 0) if a.unmask else []
        out['meta']['measured_unmasked'] = bool(a.unmask)
        out.update(measure_map(mon, channels, a.repeat, extra))
        if a.ext:
            ext = vmm_trim.ExtFile.load(a.ext)
            out['sdt'] = {str(c): v for c, v in ext.sdt().items()}
            out['trims'] = {str(c): {str(ch): v for ch, v in d.items()} for c, d in ext.trims().items()}
            out['masked'] = {str(c): v for c, v in ext.channel_bits('sm').items()}
        save(a.out, out)
    finally:
        if not a.no_reapply:
            reapply()
    print_report(out)


def print_report(m):
    print('map of %s  (%s)' % (m['meta'].get('ext'), m['meta'].get('time')))
    print('chip  sdt  thrDAC  baseline: med  min  max | threshold-baseline: med  min  max  spread(rms) | outliers')
    for c in sorted(m['baseline'], key=int):
        b, t = m['baseline'][c], m['threshold'][c]
        chans = sorted(b, key=int)
        bv = [b[ch] for ch in chans]
        d = [t[ch] - b[ch] for ch in chans]
        med = statistics.median(d)
        rms = statistics.pstdev(d) if len(d) > 1 else 0
        low = [ch for ch in chans if t[ch] - b[ch] < med - 3 * max(rms, 1)]
        print('%4s %4s %7s %13.0f %4.0f %4.0f | %23.0f %4.0f %4.0f %8.1f     | %s' % (
            c, m.get('sdt', {}).get(c, '-'), m.get('thrdac', {}).get(c, '-'),
            statistics.median(bv), min(bv), max(bv), med, min(d), max(d), rms,
            ('low: ' + ','.join(low)) if low else ''))


def cmd_report(a):
    with open(a.map) as f:
        print_report(json.load(f))


# ------------------------------------------------------------- trimcurve ----
def cmd_trimcurve(a):
    mon = Monitor(a.bin, a.base, a.ext, a.workdir)
    channels = vmm_trim.parse_list(a.channels, 0, N_CH - 1, 'channel')
    trims = vmm_trim.parse_list(a.trims, 0, 31, 'trim')
    curve = {}
    try:
        for t in trims:
            lines = all_channel_lines('smx', 1, channels=channels) + \
                all_channel_lines('sd', vmm_trim.to_written(t), channels=channels)
            for _, ch, vals in mon.run('chan', channels, lines):
                for chip, v in vals.items():
                    curve.setdefault(str(chip), {}).setdefault(str(ch), {})[str(t)] = v
            log('trim %d done' % t)
        out = {'meta': meta(a, mon), 'curve': curve}
        # straight-line fit per chip (all its measured channels together, relative to trim 0)
        fit = {}
        for chip, chd in curve.items():
            xs, ys = [], []
            for ch, tv in chd.items():
                if '0' not in tv:
                    continue
                for t, v in tv.items():
                    xs.append(int(t)); ys.append(v - tv['0'])
            if len(set(xs)) > 1:
                mx, my = statistics.mean(xs), statistics.mean(ys)
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
                resid = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
                fit[chip] = {'mV_per_trim': slope, 'max_abs_residual_mV': max(abs(r) for r in resid)}
        out['fit'] = fit
        save(a.out, out)
    finally:
        if not a.no_reapply:
            reapply()
    print('chip  mV per trim step   worst deviation from a straight line (mV)')
    for chip in sorted(fit, key=int):
        print('%4s %14.2f %20.1f' % (chip, fit[chip]['mV_per_trim'], fit[chip]['max_abs_residual_mV']))


# ---------------------------------------------------------------- sdtcal ----
def cmd_sdtcal(a):
    mon = Monitor(a.bin, a.base, a.ext, a.workdir)
    lo, hi, step = (int(x) for x in a.sdt.split(':'))
    chips = vmm_trim.parse_list(a.chips, 0, N_CHIPS - 1, 'chip')
    pts = {}
    try:
        for sdt in range(lo, hi + 1, step):
            lines = []
            for c in chips:
                lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg bank1spi1 Field sdt %d' % (c // 2, c % 2, sdt % 64))
                lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg bank1spi2 Field sdt %d' % (c // 2, c % 2, sdt // 64))
            vals = mon.run('thrdac', None, lines)[0][2]
            for c in chips:
                pts.setdefault(str(c), {})[str(sdt)] = vals[c]
            log('sdt %d done' % sdt)
        fit = {}
        for c, d in pts.items():
            xs, ys = [int(k) for k in d], list(d.values())
            mx, my = statistics.mean(xs), statistics.mean(ys)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            fit[c] = {'mV_per_sdt': slope, 'mV_at_sdt0': my - slope * mx,
                      'max_abs_residual_mV': max(abs(y - (my + slope * (x - mx))) for x, y in zip(xs, ys))}
        save(a.out, {'meta': meta(a, mon), 'points': pts, 'fit': fit})
    finally:
        if not a.no_reapply:
            reapply()
    print('chip  mV per sdt count  mV at sdt 0  worst deviation (mV)')
    for c in sorted(fit, key=int):
        print('%4s %14.3f %12.1f %12.1f' % (c, fit[c]['mV_per_sdt'], fit[c]['mV_at_sdt0'], fit[c]['max_abs_residual_mV']))


# -------------------------------------------------------------- equalize ----
def _per_chip(path, key, default):
    """{chip(str): value} from a trimcurve / sdtcal file, else the default for every chip."""
    out = {str(c): default for c in range(N_CHIPS)}
    if path:
        with open(path) as f:
            for c, d in json.load(f)['fit'].items():
                out[c] = abs(d[key])
    return out


def equalize(m, mv_per_trim, mv_per_sdt, chips, headroom, keep_distance=None, window=(120.0, 220.0),
             max_from_median=15.0):
    """Trim table that puts every channel of a chip at the same distance above
    its own baseline, keeping `headroom` trim steps free on every channel so
    that single channels can later be RAISED.

    mv_per_trim, mv_per_sdt: {chip: mV} (measured per chip).

    For a chip, d(ch) = threshold(ch) - baseline(ch) as measured, with the
    trims the map was taken with. Without those trims the distance would be
    d0(ch) = d(ch) + old_trim(ch) * step. A trim can only lower a channel, so
    all channels are brought down to the lowest one, and then all of them
    `headroom` steps further:
        trim(ch) = headroom + (d0(ch) - min d0) / step
    The chip sdt is raised to compensate, so that the common distance is what
    the chip had before (its median), or `keep_distance` mV if given.

    Channels whose baseline is outside `window` (mV), or whose distance is more
    than `max_from_median` mV away from the median of their chip, are not normal (dead or
    displaced baseline): they are left out of the calculation and get the
    median trim of their chip, so they move with it. They are listed.
    """
    table, sdt_new, info = {}, {}, {}
    for c in chips:
        cs = str(c)
        if cs not in m['baseline']:
            continue
        step, per_sdt = mv_per_trim[cs], mv_per_sdt[cs]
        b, t = m['baseline'][cs], m['threshold'][cs]
        old = m.get('trims', {}).get(cs, {})
        chans = sorted(b, key=int)
        sane = [ch for ch in chans if window[0] <= b[ch] <= window[1]]
        odd = [ch for ch in chans if ch not in sane]
        if len(sane) < 8:
            info[cs] = {'skipped': 'only %d channels with a normal baseline' % len(sane)}
            continue
        d0 = {ch: t[ch] - b[ch] + old.get(ch, 0) * step for ch in sane}
        # a channel far from the rest of its chip (half-displaced baseline) must not set the scale
        med0 = statistics.median(d0.values())
        far = [ch for ch in sane if abs(d0[ch] - med0) > max_from_median]
        sane = [ch for ch in sane if ch not in far]
        odd += far
        d0 = {ch: d0[ch] for ch in sane}
        d_before = [t[ch] - b[ch] for ch in sane]
        lowest = min(d0.values())
        trims = {ch: headroom + int(round((d0[ch] - lowest) / step)) for ch in sane}
        clipped = sorted((ch for ch in sane if trims[ch] > 31), key=int)
        for ch in clipped:
            trims[ch] = 31                     # stays a little high: the safe side
        med_trim = int(statistics.median(trims.values()))
        for ch in odd:
            trims[ch] = med_trim
        common = lowest - headroom * step      # distance of every sane channel after trimming, sdt unchanged
        want = statistics.median(d_before) if keep_distance is None else keep_distance
        dsdt = int(round((want - common) / per_sdt))
        table[cs] = trims
        if cs in m.get('sdt', {}):
            sdt_new[cs] = m['sdt'][cs] + dsdt
        after = [d0[ch] - trims[ch] * step + dsdt * per_sdt for ch in sane]
        info[cs] = {'distance_before_median_mV': statistics.median(d_before),
                    'spread_before_rms_mV': statistics.pstdev(d_before),
                    'spread_before_range_mV': [min(d_before), max(d_before)],
                    'expected_distance_after_mV': statistics.median(after),
                    'expected_spread_after_rms_mV': statistics.pstdev(after),
                    'sdt_change': dsdt, 'trim_min': min(trims[ch] for ch in sane),
                    'trim_max': max(trims[ch] for ch in sane),
                    'clipped_at_31': clipped, 'odd_baseline_channels': sorted(odd, key=int),
                    'mv_per_trim': step, 'mv_per_sdt': per_sdt}
    return table, sdt_new, info


def cmd_equalize(a):
    with open(a.map) as f:
        m = json.load(f)
    chips = vmm_trim.parse_list(a.chips, 0, N_CHIPS - 1, 'chip')
    per_trim = _per_chip(a.trimcurve, 'mV_per_trim', a.mv_per_trim)
    per_sdt = _per_chip(a.sdtcal, 'mV_per_sdt', a.mv_per_sdt)
    lo, hi = (float(x) for x in a.baseline_window.split(':'))
    table, sdt_new, info = equalize(m, per_trim, per_sdt, chips, a.headroom, a.distance, (lo, hi))
    save(a.out, {'source_map': os.path.basename(a.map), 'source_ext': m['meta'].get('ext'),
                 'made': datetime.now().isoformat(timespec='seconds'), 'effective_trim': True,
                 'headroom_steps': a.headroom, 'trims': table, 'sdt': sdt_new, 'info': info})
    print('chip  distance mV   spread rms mV    range before   sdt     trims    clipped  odd-baseline')
    print('      before after  before  after                  change  min..max  at 31    channels')
    for c in sorted(info, key=int):
        i = info[c]
        if 'skipped' in i:
            print('%4s  skipped: %s' % (c, i['skipped']))
            continue
        print('%4s %7.0f %5.0f %7.1f %6.1f %8.0f..%-4.0f %+7d %6d..%-2d %6d    %s' % (
            c, i['distance_before_median_mV'], i['expected_distance_after_mV'], i['spread_before_rms_mV'],
            i['expected_spread_after_rms_mV'], i['spread_before_range_mV'][0], i['spread_before_range_mV'][1],
            i['sdt_change'], i['trim_min'], i['trim_max'], len(i['clipped_at_31']),
            ','.join(i['odd_baseline_channels'])))


def refine(m, mv_per_trim, chips, window=(120.0, 220.0), max_from_median=15.0, min_trim=0):
    """Second pass: the map was taken WITH trims; correct each channel by the
    few steps it is still off the chip median. sdt is not changed. Small
    corrections, so the differences between the trim steps of individual
    channels (which limit the first pass) hardly matter."""
    table, info = {}, {}
    for c in chips:
        cs = str(c)
        if cs not in m['baseline']:
            continue
        step = mv_per_trim[cs]
        b, t = m['baseline'][cs], m['threshold'][cs]
        old = {ch: int(m.get('trims', {}).get(cs, {}).get(ch, 0)) for ch in b}
        sane = [ch for ch in b if window[0] <= b[ch] <= window[1]]
        d = {ch: t[ch] - b[ch] for ch in sane}
        med = statistics.median(d.values())
        sane = [ch for ch in sane if abs(d[ch] - med) <= max_from_median]
        med = statistics.median(d[ch] for ch in sane)
        new = dict(old)
        for ch in sane:
            new[ch] = min(31, max(min_trim, old[ch] + int(round((d[ch] - med) / step))))
        table[cs] = new
        info[cs] = {'distance_median_mV': med, 'spread_rms_mV': statistics.pstdev(d[ch] for ch in sane),
                    'channels_changed': sum(1 for ch in sane if new[ch] != old[ch]),
                    'trim_min': min(new[ch] for ch in sane), 'trim_max': max(new[ch] for ch in sane)}
    return table, info


def cmd_refine(a):
    with open(a.map) as f:
        m = json.load(f)
    chips = vmm_trim.parse_list(a.chips, 0, N_CHIPS - 1, 'chip')
    lo, hi = (float(x) for x in a.baseline_window.split(':'))
    table, info = refine(m, _per_chip(a.trimcurve, 'mV_per_trim', a.mv_per_trim), chips, (lo, hi), min_trim=a.min_trim)
    save(a.out, {'source_map': os.path.basename(a.map), 'source_ext': m['meta'].get('ext'),
                 'made': datetime.now().isoformat(timespec='seconds'), 'effective_trim': True,
                 'trims': table, 'sdt': {}, 'info': info})
    print('chip  distance mV  spread rms mV (as measured)  channels changed  trims min..max')
    for c in sorted(info, key=int):
        i = info[c]
        print('%4s %9.0f %12.1f %22d %10d..%d' % (c, i['distance_median_mV'], i['spread_rms_mV'],
                                                 i['channels_changed'], i['trim_min'], i['trim_max']))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(s, need_out=True):
        s.add_argument('--ext', help='exception file describing the chip state to measure (full path)')
        s.add_argument('--base', default=DEFAULT_BASE)
        s.add_argument('--bin', default=DEFAULT_BIN, help='the vmm_monitor program')
        s.add_argument('--workdir', default=tempfile.gettempdir())
        s.add_argument('--no-reapply', action='store_true',
                       help='do NOT ask the GUI to configure the chips again afterwards')
        s.add_argument('--out', required=need_out)

    s = sub.add_parser('map'); common(s)
    s.add_argument('--channels', default='all'); s.add_argument('--repeat', type=int, default=1)
    s.add_argument('--unmask', action='store_true',
                   help='measure with every mask removed (a masked channel has no baseline to measure); '
                        'the masks of the file are still recorded in the map')
    s.set_defaults(fn=cmd_map)

    s = sub.add_parser('trimcurve'); common(s)
    s.add_argument('--channels', default='5,20,40,60'); s.add_argument('--trims', default='0-31')
    s.set_defaults(fn=cmd_trimcurve)

    s = sub.add_parser('sdtcal'); common(s)
    s.add_argument('--sdt', default='150:400:25', help='lo:hi:step'); s.add_argument('--chips', default='all')
    s.set_defaults(fn=cmd_sdtcal)

    s = sub.add_parser('equalize')
    s.add_argument('--map', required=True); s.add_argument('--out', required=True)
    s.add_argument('--chips', default='2-19')
    s.add_argument('--headroom', type=int, default=12,
                   help='trim steps kept free on every channel so that it can later be raised (default 12, about 13 mV)')
    s.add_argument('--distance', type=float, help='common threshold-baseline distance wanted, mV (default: keep the chip median)')
    s.add_argument('--trimcurve', help='trimcurve result: mV per trim step of each chip')
    s.add_argument('--sdtcal', help='sdtcal result: mV per sdt count of each chip')
    s.add_argument('--mv-per-trim', type=float, default=1.1, help='used for chips without a measurement')
    s.add_argument('--mv-per-sdt', type=float, default=0.77, help='used for chips without a measurement')
    s.add_argument('--baseline-window', default='120:220',
                   help='mV; channels with a baseline outside are not equalised (default 120:220)')
    s.set_defaults(fn=cmd_equalize)

    s = sub.add_parser('refine', help='second equalisation pass from a map taken with trims')
    s.add_argument('--map', required=True); s.add_argument('--out', required=True)
    s.add_argument('--chips', default='2-19'); s.add_argument('--trimcurve')
    s.add_argument('--mv-per-trim', type=float, default=1.1)
    s.add_argument('--baseline-window', default='120:220')
    s.add_argument('--min-trim', type=int, default=0, help='never go below this trim (keeps room to raise)')
    s.set_defaults(fn=cmd_refine)

    s = sub.add_parser('report'); s.add_argument('map'); s.set_defaults(fn=cmd_report)

    a = p.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
