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
def measure_map(mon, channels, repeat=1):
    """baseline (smx 0) and threshold (smx 1) of the given channels, all chips."""
    res = {}
    for name, smx in (('baseline', 0), ('threshold', 1)):
        t0 = time.time()
        rows = mon.run('chan', channels, all_channel_lines('smx', smx), repeat)
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
        out.update(measure_map(mon, channels, a.repeat))
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
def equalize(m, mv_per_trim, mv_per_sdt, chips, headroom, keep_distance=None):
    """Trim table that puts every channel of a chip at the same distance above
    its own baseline.

    For a chip, d(ch) = threshold(ch) - baseline(ch) as measured with the
    trims the map was taken with. Removing those trims gives the distance at
    trim 0, d0(ch) = d(ch) + old_trim(ch) * step. The trims can only lower a
    channel, so every channel is brought down to a common target
        target = min(d0) - headroom * step
    (`headroom` trim steps are kept free so that single channels can later be
    RAISED by up to that many steps). The chip sdt is then raised so that the
    common distance equals what the chip had before (its median distance), or
    `keep_distance` mV if given.
    """
    step = abs(mv_per_trim)
    table, sdt_new, info = {}, {}, {}
    for c in chips:
        cs = str(c)
        if cs not in m['baseline']:
            continue
        b, t = m['baseline'][cs], m['threshold'][cs]
        old = m.get('trims', {}).get(cs, {})
        chans = sorted(b, key=int)
        d0 = {ch: t[ch] - b[ch] + old.get(ch, 0) * step for ch in chans}
        d_before = statistics.median(t[ch] - b[ch] for ch in chans)
        target = min(d0.values()) - headroom * step
        trims = {}
        for ch in chans:
            trims[ch] = int(round((d0[ch] - target) / step))
        if max(trims.values()) > 31:
            # the spread of this chip is larger than the trim range: keep the
            # majority and let the highest channels stay high (they are the safe side)
            over = [ch for ch in chans if trims[ch] > 31]
            for ch in over:
                trims[ch] = 31
        want = d_before if keep_distance is None else keep_distance
        dsdt = int(round((want - target) / mv_per_sdt))
        table[cs] = {ch: v for ch, v in trims.items()}
        if 'sdt' in m and cs in m['sdt']:
            sdt_new[cs] = m['sdt'][cs] + dsdt
        resid = [d0[ch] - trims[ch] * step - target for ch in chans]
        info[cs] = {'distance_before_median_mV': d_before,
                    'spread_before_rms_mV': statistics.pstdev([t[ch] - b[ch] for ch in chans]),
                    'expected_spread_after_rms_mV': statistics.pstdev(resid),
                    'target_distance_mV': want, 'sdt_change': dsdt,
                    'trim_min': min(trims.values()), 'trim_max': max(trims.values()),
                    'channels_clipped_at_31': sorted((ch for ch in chans if d0[ch] - 31 * step - target > step), key=int)}
    return table, sdt_new, info


def cmd_equalize(a):
    with open(a.map) as f:
        m = json.load(f)
    chips = vmm_trim.parse_list(a.chips, 0, N_CHIPS - 1, 'chip')
    table, sdt_new, info = equalize(m, a.mv_per_trim, a.mv_per_sdt, chips, a.headroom, a.distance)
    save(a.out, {'source_map': os.path.basename(a.map), 'source_ext': m['meta'].get('ext'),
                 'made': datetime.now().isoformat(timespec='seconds'), 'effective_trim': True,
                 'headroom_steps': a.headroom, 'mv_per_trim': a.mv_per_trim, 'mv_per_sdt': a.mv_per_sdt,
                 'trims': table, 'sdt': sdt_new, 'info': info})
    print('chip  spread before -> expected after (mV rms)   distance kept (mV)   sdt change   trims min..max   clipped')
    for c in sorted(info, key=int):
        i = info[c]
        print('%4s %10.1f -> %5.1f %28.0f %12+d %10d..%-2d   %s' % (
            c, i['spread_before_rms_mV'], i['expected_spread_after_rms_mV'], i['target_distance_mV'],
            i['sdt_change'], i['trim_min'], i['trim_max'], ','.join(i['channels_clipped_at_31'])))


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
    s.add_argument('--headroom', type=int, default=8,
                   help='trim steps kept free so that single channels can later be raised (default 8, about 9 mV)')
    s.add_argument('--distance', type=float, help='common threshold-baseline distance wanted, mV (default: keep the chip median)')
    s.add_argument('--mv-per-trim', type=float, default=1.1); s.add_argument('--mv-per-sdt', type=float, default=0.8)
    s.set_defaults(fn=cmd_equalize)

    s = sub.add_parser('report'); s.add_argument('map'); s.set_defaults(fn=cmd_report)

    a = p.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
