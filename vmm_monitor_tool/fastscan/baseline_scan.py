#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noise (HV off) or noise + cosmics (HV on) rate of every channel vs its threshold ABOVE ITS OWN BASELINE.

Built on fast_ratescan.py (same VMM-only writes, masks, stall recovery). Differences:
  * the x axis is X = threshold - baseline in mV, per channel, not an sdt offset;
  * every channel of a chip is first EQUALISED to the same distance X0 above its monitor
    baseline (vmm_monitor_scan.equalize on a map of START, i.e. trims), and that file is mapped
    (map_eq.json = the measured distance of every channel at X0). A point X then only moves the
    chip sdt:  sdt_c(X) = sdt_c(eq) + round((X - X0_c) / k_c), k_c = sdtcal mV per count, and a
    channel's distance at that point is d_eq(ch) + (sdt_c(X) - sdt_c(eq)) * k_c;
  * points go from high X to low X (the risky low end last), with a capture time per point that
    grows with X (stats where the rate is low), split into chunks;
  * stall check per chunk: chips 0-1 (H0, not swept, ~440 Hz in every capture of 16 Nov) must
    count; 0 hits there = the read-out stopped -> full configure of the point, chunk again;
  * a chip above --chip-max-hz leaves the sweep (all its channels to sdt +40, i.e. far above);
  * --hv on: every point needs the six P2 channels ON at their setpoint (hv_lab.py check), it
    waits up to --hv-wait-s for the guard to recover them, else the point is skipped;
  * channels are pulsed (all channels, test pulser, files of 16 Nov) at the start and the end:
    channels left near their baseline go silent on noise until pulsed (handoff 9.2).

    baseline_scan.py --out-dir DIR [--hv off|on] [--points 40,37,...] [--x0 15]
                     [--eq-file NAME --map-eq MAP]       # reuse the equalised file and its map
    baseline_scan.py --dry-run --out-dir DIR --map-start MAP
"""

import argparse
import json
import math
import os
import shutil
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, TOOL)
import fast_ratescan as FR  # noqa: E402
import noise_probe as NP  # noqa: E402
import vmm_trim as VT  # noqa: E402
import vmm_monitor_scan as MS  # noqa: E402
import hv_lab as HV  # noqa: E402

PFX = FR.PFX
EXT_DIR = FR.EXT_DIR
START = FR.START
EQ_NAME = PFX + 'bscan_eq_x%02d.txt'
POINT_NAME = PFX + 'bscan_point.txt'           # copy of the current point for maps / recovery
HEAL = [PFX + 'diag_c2c3_allpulsed_a40.txt'] + [PFX + 'fastscan_heal_g%d.txt' % i for i in (1, 2, 3, 4)]
REFS = [PFX + 'target50_p2_rt3.txt', PFX + 'trimmed_Km18_v1.txt']
SDTCAL = os.path.expanduser('~/trim_work/out/sdtcal.json')
TRIMCURVE = os.path.expanduser('~/trim_work/out/trimcurve_ch5_40.json')
DEFAULT_POINTS = '40,37,34,31,28,25,23,21,19,17,15,14,13,12,11,10,9,8,7,6,5,4,3'
HEARTBEAT_CHIPS = ('0', '1')
log = FR.log


def seconds_for(x):
    """Capture time per point: long where the rate is low."""
    if x >= 28:
        return 600
    if x >= 17:
        return 180
    if x >= 11:
        return 60
    return 30


# ------------------------------------------------------------------ pure logic (tested) --
def chip_x0(map_eq, chips, window=(120.0, 220.0)):
    """Per chip: median measured distance (threshold - baseline, mV) of its normal channels in
    the map of the equalised file, and {ch: distance} of every channel."""
    x0, d = {}, {}
    for c in chips:
        cs = str(c)
        b, t = map_eq['baseline'].get(cs, {}), map_eq['threshold'].get(cs, {})
        d[c] = {int(ch): t[ch] - b[ch] for ch in b if ch in t}
        normal = [t[ch] - b[ch] for ch in b if ch in t and window[0] <= b[ch] <= window[1]]
        if normal:
            normal.sort()
            x0[c] = normal[len(normal) // 2]
    return x0, d


def sdt_for(x, sdt_eq, x0, k):
    """Chip sdt that puts the chip median at x mV above baseline (chips without x0 are skipped)."""
    return {c: sdt_eq[c] + int(round((x - x0[c]) / k[c])) for c in x0 if c in sdt_eq}


def distances(d_eq, sdt_eq, sdt, k):
    """{chip: {ch: mV above baseline}} at a point."""
    return {c: {ch: round(v + (sdt[c] - sdt_eq[c]) * k[c], 2) for ch, v in d_eq[c].items()}
            for c in sdt if c in d_eq}


def heartbeat(hits):
    return sum(sum(hits.get(c, {}).values()) for c in HEARTBEAT_CHIPS)


def stalled(res, min_total=100):
    """No live time, or no heartbeat hit and (almost) nothing else either."""
    if not res.get('live_s'):
        return True
    tot = sum(sum(v.values()) for v in res.get('hits', {}).values())
    return heartbeat(res.get('hits', {})) == 0 and tot < min_total


def point_file(eq_ext, sdt, chips, masked, left, sdt_eq, leave_by=40):
    ext = VT.ExtFile(list(eq_ext.lines))
    used = {}
    for c in chips:
        if c not in sdt:
            continue
        used[c] = sdt_eq[c] + leave_by if c in left else sdt[c]
        ext.set_sdt(c, used[c])
    ext.lines.append('# baseline_scan: masks below (cumulative)')
    for c in chips:
        hyb, vmm = VT.hyb_vmm(c)
        chans = range(VT.N_CHANNELS) if c in left else sorted(masked.get(c, ()))
        for ch in chans:
            ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sm   1' % (hyb, vmm, ch))
    return ext, used


# ------------------------------------------------------------------------ hardware --
def hv_ok(mode):
    if mode == 'off':
        FR.check_hv()
        return True, 'off'
    try:
        ch, _ = HV.state()
    except Exception as e:  # noqa: BLE001 - banco not readable this time: "not ok", the gate retries
        return False, 'banco not readable: %s' % e
    skip = HV.read_given_up()        # detectors powered off by the guard after a trip
    ok = HV.all_at_target(ch, skip)
    return ok, HV.summary(ch) + (' | off after trip: %s' % sorted(skip) if skip else '')


def hv_gate(mode, wait_s):
    """HV off: abort if on. HV on: wait (guard may be re-ramping) up to wait_s; False = skip."""
    ok, s = hv_ok(mode)
    t0 = time.time()
    while not ok and time.time() - t0 < wait_s:
        time.sleep(15)
        ok, s = hv_ok(mode)
    if not ok:
        log('HV not at the operating point after %.0f s: %s' % (wait_s, s))
    return ok, s


def heal(out_dir, seconds=10):
    """Test pulses on every channel of chips 2-19 (5 files of 16 Nov), 10 s each."""
    for name in HEAL:
        if not os.path.exists(os.path.join(EXT_DIR, name)):
            raise FR.Abort('pulse file missing: %s' % name)
    d = os.path.join(out_dir, 'heal_%s' % datetime.now().strftime('%H%M'))
    for name in HEAL:
        FR.probe(name, seconds, d, 300)


def capture_chunk(pcap, seconds, max_mb):
    mb = NP.capture(pcap, seconds, max_mb)
    res = NP.count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
    if os.path.exists(pcap):
        os.remove(pcap)
    return mb, res


def capture_point(path, seconds, chunk_s, max_mb, out, i):
    """Chunked capture with a stall check on the heartbeat chips. Returns (hits, live, mb, n_recovered)."""
    hits, live, mb_tot, nrec = {}, 0.0, 0.0, 0
    remaining = seconds
    while remaining > 0:
        s = min(chunk_s, remaining)
        pcap = os.path.join(out, 'chunk.pcapng')
        mb, res = capture_chunk(pcap, s, max_mb)
        if stalled(res):
            nrec += 1
            log('point %s: chunk with no heartbeat (%.2f MB, live %.1f s): full configure and capture again'
                % (i, mb, res['live_s']))
            if nrec > 3:
                raise FR.Abort('read-out stalled 4 times at one point')
            shutil.copy(path, os.path.join(EXT_DIR, FR.RECOVER))
            NP.select_and_apply(FR.RECOVER)
            continue
        for c, d in res['hits'].items():
            hc = hits.setdefault(c, {})
            for ch, n in d.items():
                hc[ch] = hc.get(ch, 0) + n
        live += res['live_s']
        mb_tot += mb
        remaining -= s
        if mb >= max_mb * 0.98:       # high rate: stopped on size; the live time is what counts
            break
    return hits, live, mb_tot, nrec


def run_sweep(a, eq_ext, chips, sdt_eq, x0, k, d_eq, points, out, tag, masked, map_at, history=None):
    """history (repeats): {x: masks the main sweep had when it took x}; each repeat point starts
    from those (not from the masks at the end of the sweep, which cover most channels)."""
    left = set()
    prev = {c: None for c in chips}
    hist = {}
    os.makedirs(os.path.join(out, 'files'), exist_ok=True)
    for j, x in enumerate(points):
        i = '%s%02d' % (tag, j)
        t0 = time.time()
        if history is not None:
            masked = {c: set(v) for c, v in history.get(x, masked).items()}
            prev = {c: None for c in chips}
        hist.setdefault(x, {c: set(v) for c, v in masked.items()})
        ok, hvs = hv_gate(a.hv, a.hv_wait_s)
        if not ok:
            log('point %s X %+d skipped: HV' % (i, x))
            continue
        sdt = sdt_for(x, sdt_eq, x0, k)
        ext, used = point_file(eq_ext, sdt, chips, masked, left, sdt_eq)
        path = os.path.join(out, 'files', 'point_%s.txt' % i)
        ext.write(path, force=True)
        t_apply = FR.vmm_write(path)
        L = FR.links()
        recovered = 0
        if not FR.links_ok(L):
            log('point %s: links %s after the VMM-only write: full configure' % (i, L))
            shutil.copy(path, os.path.join(EXT_DIR, FR.RECOVER))
            NP.select_and_apply(FR.RECOVER)
            L, recovered = FR.links(), 1
            if not FR.links_ok(L):
                raise FR.Abort('links still %s after a full configure' % L)
        secs = int(getattr(a, 'fixed_seconds', 0) or seconds_for(x) * a.time_scale)
        hits, live, mb, nrec = capture_point(path, secs, a.chunk_s, a.max_mb, out, i)
        recovered += nrec
        ok2, hvs2 = hv_ok(a.hv) if a.hv == 'on' else (True, 'off')
        live = live or 1e-9
        rates = {c: {int(kk): v / live for kk, v in hits.get(str(c), {}).items()} for c in chips}
        tot = {c: sum(rates[c].values()) for c in chips}
        new = {}
        for c in chips:
            if c in left:
                continue
            nm = FR.decide(rates[c], prev[c], masked[c], a.mask_hz, a.stuck_min_hz)
            if nm:
                new[c] = nm
        dist = distances(d_eq, sdt_eq, used, k)
        rec = {'index': i, 'x_mV': x, 'time': datetime.now().isoformat(timespec='seconds'), 'hv_mode': a.hv,
               'hv_before': hvs, 'hv_after': hvs2, 'hv_ok_after': ok2, 'sweep_chips': chips,
               'sdt': {str(c): v for c, v in used.items()}, 'x0_chip': {str(c): v for c, v in x0.items()},
               'distance_mV': {str(c): {str(ch): v for ch, v in dd.items()} for c, dd in dist.items()},
               'masked': {str(c): (list(range(64)) if c in left else sorted(masked[c])) for c in chips},
               'left': sorted(left), 'new_masks': {str(c): {str(kk): v for kk, v in d.items()} for c, d in new.items()},
               'chip_total_hz': {str(c): round(tot[c], 2) for c in chips}, 'hits': hits, 'live_s': live,
               'mb': mb, 'links': L, 'recovered': recovered, 'heartbeat_hz': round(heartbeat(hits) / live, 1),
               'apply_s': round(t_apply, 1), 'seconds_asked': secs, 'ext_file': path}
        with open(os.path.join(out, 'point_%s.json' % i), 'w') as f:
            json.dump(rec, f)
        for c, d in new.items():
            masked[c].update(d)
            if len(masked[c]) >= a.leave_at:
                left.add(c)
                log('   chip %d leaves (%d channels masked)' % (c, len(masked[c])))
        for c in chips:
            if c not in left and tot[c] > a.chip_max_hz:
                left.add(c)
                log('   chip %d leaves (%.2f MHz)' % (c, tot[c] / 1e6))
        prev = rates
        busy = ' '.join('%d:%.0fk' % (c, tot[c] / 1e3) for c in chips if tot[c] >= 1e3)
        log('pt %s X %+3d  write %.1f s  live %.0f s (%.0f MB)  links %s%s  total %.3f MHz  +%d masks (sum %d)  '
            'hb %.0f Hz  %.0f s%s | %s'
            % (i, x, t_apply, live, mb, ''.join(str(v) for v in L.values()),
               ' RECOVERED x%d' % recovered if recovered else '', sum(tot.values()) / 1e6,
               sum(len(d) for d in new.values()), sum(len(masked[c]) for c in chips),
               heartbeat(hits) / live, time.time() - t0, '' if ok2 else ' HV-CHANGED', busy or 'all < 1 kHz'))
        if x in map_at:
            shutil.copy(path, os.path.join(EXT_DIR, POINT_NAME))
            FR.monitor_map(POINT_NAME, os.path.join(out, 'map_%s_x%02d.json' % (tag, x)),
                           os.path.join(out, 'mapwork'), POINT_NAME)
        if len(left) == len(chips):
            log('all chips have left')
            break
    return masked, hist


def make_eq(a, chips, map_start):
    """Equalised file at X0 from a map of START; returns its name."""
    name = a.eq_name or EQ_NAME % a.x0
    per_trim = MS._per_chip(TRIMCURVE if os.path.exists(TRIMCURVE) else None, 'mV_per_trim', 1.1)
    per_sdt = MS._per_chip(SDTCAL, 'mV_per_sdt', 0.77)
    table, sdt_new, info = MS.equalize(map_start, per_trim, per_sdt, chips, a.headroom, float(a.x0))
    ext = VT.ExtFile.load(os.path.join(EXT_DIR, a.start_file or START))
    for cs, v in sdt_new.items():
        ext.set_sdt(int(cs), int(v))
    t = ext.trims()
    for cs, d in table.items():
        t[int(cs)] = {int(ch): v for ch, v in d.items()}
    ext.set_trims(t, 'baseline_scan: all channels at %d mV above the monitor baseline (map %s)'
                  % (a.x0, map_start['meta'].get('time')))
    ext.write(os.path.join(EXT_DIR, name), force=True)
    with open(os.path.join(a.out_dir, 'equalize_info.json'), 'w') as f:
        json.dump({'trims': table, 'sdt': sdt_new, 'info': info}, f, indent=1)
    for cs in sorted(info, key=int):
        i = info[cs]
        if 'skipped' in i:
            log('  eq chip %s skipped: %s' % (cs, i['skipped']))
        else:
            log('  eq chip %2s: sdt %+d, trims %d..%d, %d clipped, expected %.1f mV rms %.1f, odd %s'
                % (cs, i['sdt_change'], i['trim_min'], i['trim_max'], len(i['clipped_at_31']),
                   i['expected_distance_after_mV'], i['expected_spread_after_rms_mV'], i['odd_baseline_channels']))
    return name


def refs(a, out, tag):
    for name in REFS:
        FR.probe(name, a.ref_seconds, os.path.join(out, 'ref_' + tag), a.max_mb)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--hv', choices=('off', 'on'), default='off')
    ap.add_argument('--chips', default='2-19')
    ap.add_argument('--points', default=DEFAULT_POINTS, help='X in mV above baseline, run in this order')
    ap.add_argument('--repeat', default='30,20,12,8', help='points repeated after the sweep ("" = none)')
    ap.add_argument('--x0', type=int, default=15, help='mV above baseline of the equalised file')
    ap.add_argument('--start-file', help='config_ext file the equalised file is made from (default START, '
                    'gain 4.5); another gain = a copy with other sg lines, mapped as --map-start')
    ap.add_argument('--eq-name', help='name of the equalised file (default %s)' % (EQ_NAME % 15))
    ap.add_argument('--fixed-seconds', type=int, default=0, help='capture time of every point (0 = by X)')
    ap.add_argument('--headroom', type=int, default=2)
    ap.add_argument('--map-start', help='map of START (default: full/map_start.json of 16 Nov)',
                    default=os.path.expanduser('~/trim_work/out/fastscan_20261116/full/map_start.json'))
    ap.add_argument('--eq-file', help='reuse this equalised file (config_ext name) ...')
    ap.add_argument('--map-eq', help='... and its map (skip making and mapping it)')
    ap.add_argument('--map-at', default='40,5', help='monitor map at these X during the sweep')
    ap.add_argument('--time-scale', type=float, default=1.0, help='multiply every capture time')
    ap.add_argument('--chunk-s', type=int, default=120)
    ap.add_argument('--max-mb', type=int, default=300)
    ap.add_argument('--ref-seconds', type=int, default=60)
    ap.add_argument('--mask-hz', type=float, default=1e4)
    ap.add_argument('--stuck-min-hz', type=float, default=1e3)
    ap.add_argument('--chip-max-hz', type=float, default=1e6)
    ap.add_argument('--leave-at', type=int, default=60)
    ap.add_argument('--hv-wait-s', type=float, default=900)
    ap.add_argument('--no-heal', action='store_true')
    ap.add_argument('--no-refs', action='store_true')
    ap.add_argument('--no-end-map', action='store_true')
    ap.add_argument('--restore', default=FR.RESTORE)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    chips = VT.parse_list(a.chips, 2, VT.N_CHIPS - 1, 'chip')
    points = [int(x) for x in a.points.split(',')]
    repeat = [int(x) for x in a.repeat.split(',')] if a.repeat else []
    map_at = {int(x) for x in a.map_at.split(',')} if a.map_at else set()
    a.out_dir = os.path.abspath(a.out_dir)
    os.makedirs(a.out_dir, exist_ok=True)
    k = {c: v for c, v in ((int(c), v) for c, v in MS._per_chip(SDTCAL if os.path.exists(SDTCAL) else None,
                                                                 'mV_per_sdt', 0.77).items())}
    if a.dry_run:
        m = json.load(open(a.map_start))
        x0, d = chip_x0(m, chips)
        sdt_eq = {c: m['sdt'][str(c)] for c in chips}
        for x in (points[0], points[-1]):
            s = sdt_for(x, sdt_eq, x0, k)
            dist = distances(d, sdt_eq, s, k)
            print('X %+d: sdt %s' % (x, ' '.join('%d:%d' % kv for kv in sorted(s.items()))))
            print('   median distance per chip: %s' % ' '.join(
                '%d:%.1f' % (c, sorted(v.values())[len(v) // 2]) for c, v in sorted(dist.items())))
        tot = sum(seconds_for(x) for x in points) + sum(seconds_for(x) for x in repeat)
        print('capture time: %.0f min for %d + %d points' % (tot * a.time_scale / 60, len(points), len(repeat)))
        return

    FR.LOGF = open(os.path.join(a.out_dir, 'scan.log'), 'a')
    if a.hv == 'on':     # FR.probe refuses with HV on; here HV on is the point (checked by hv_gate)
        FR.check_hv = lambda: {'hv': 'on, checked by baseline_scan'}
    log('baseline_scan start: HV %s, chips %s, points %s, repeat %s; args %s' % (a.hv, a.chips, points, repeat, vars(a)))
    if NP.daq_run_active() or NP.capture_active():
        raise SystemExit('a DAQ run or a capture is active: not touching anything')
    ok, s = hv_ok(a.hv)
    log('HV: %s' % s)
    if not ok:
        raise SystemExit('HV is not in the state asked for (--hv %s)' % a.hv)
    log('GUI selection before: %s' % NP.gui('/chip_config/status').get('selected'))
    status, swept, eq_name = 'ok', False, a.eq_file
    try:
        if not a.no_heal:
            heal(a.out_dir)
        if not a.no_refs:
            refs(a, a.out_dir, 'start')
        if eq_name and a.map_eq:
            map_eq_path = a.map_eq
        else:
            eq_name = make_eq(a, chips, json.load(open(a.map_start)))
            map_eq_path = os.path.join(a.out_dir, 'map_eq.json')
            FR.monitor_map(eq_name, map_eq_path, os.path.join(a.out_dir, 'mapwork'), eq_name)
        map_eq = json.load(open(map_eq_path))
        x0, d_eq = chip_x0(map_eq, chips)
        sdt_eq = {c: int(map_eq['sdt'][str(c)]) for c in chips}
        log('equalised file %s; measured X0 per chip: %s' % (eq_name, ' '.join('%d:%.1f' % kv for kv in sorted(x0.items()))))
        eq_ext = VT.ExtFile.load(os.path.join(EXT_DIR, eq_name))
        json.dump({'args': vars(a), 'eq_file': eq_name, 'map_eq': map_eq_path, 'x0': x0, 'sdt_eq': sdt_eq, 'k': k},
                  open(os.path.join(a.out_dir, 'scan_meta.json'), 'w'), indent=1)
        NP.select_and_apply(eq_name)
        swept = True
        start_masks = eq_ext.channel_bits('sm')
        masked = {c: set(start_masks.get(c, [])) for c in chips}
        masked, hist = run_sweep(a, eq_ext, chips, sdt_eq, x0, k, d_eq, points, a.out_dir, 'p', masked, map_at)
        if repeat:
            log('repeat points %s' % repeat)
            run_sweep(a, eq_ext, chips, sdt_eq, x0, k, d_eq, repeat, a.out_dir, 'r', masked, set(), history=hist)
        log('sweep done: %d channels masked' % sum(len(m) for m in masked.values()))
    except FR.Abort as e:
        status = 'aborted: %s' % e
        log('ABORT: %s' % e)
    except BaseException as e:  # noqa: BLE001 - always restore
        status = 'error: %r' % e
        log('ERROR: %s' % traceback.format_exc())
    finally:
        try:
            if swept:
                if not a.no_heal:
                    heal(a.out_dir)
                if status == 'ok' and not a.no_refs:
                    refs(a, a.out_dir, 'end')
                if status == 'ok' and not a.no_end_map:
                    FR.monitor_map(eq_name, os.path.join(a.out_dir, 'map_eq_end.json'),
                                   os.path.join(a.out_dir, 'mapwork'), a.restore)
        except BaseException as e:  # noqa: BLE001
            status += ' | end phase: %r' % e
            log('END PHASE ERROR: %s' % traceback.format_exc())
        try:
            log('restoring %s' % a.restore)
            NP.select_and_apply(a.restore)
            log('restored; links %s' % FR.links())
        except BaseException as e:  # noqa: BLE001
            status += ' | restore FAILED: %r' % e
            log('RESTORE FAILED: %s' % e)
        log('ALLDONE status: %s' % status)


if __name__ == '__main__':
    main()
