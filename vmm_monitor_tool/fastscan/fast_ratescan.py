#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast noise rate scan of all detector chips together (HV off, no DAQ run).

Design: docs/HANDOFF_FAST_RATESCAN.md section 4. One full configure (GUI apply) at the
start, then every scan point is set by writing ONLY the VMM registers
(fastscan/vmm_only_config.py: base YAML + a per-point exception file, `config-vmm-regs`),
which takes ~4 s instead of ~12 s and leaves the links at 4.

    start : reference captures (GUI apply, 30 s each) of
              REF1 = ...step1_c2c3_ratescan_sdtm6.txt   (ties to the step-1 data of 16 Nov)
              REF2 = ...fastscan_trimmed_v1_sdtm6.txt   (trimmed_v1, chips 2-19 at sdt -6)
            GUI apply of trimmed_Km18_v1 + monitor map of all chips (--unmask) = the baselines
    sweep : offset +4 .. -24 sdt counts (1-count steps) relative to each chip's trimmed_v1 sdt;
            per point: write sdt + masks -> config-vmm-regs -> link status -> acq-on, capture,
            acq-off -> count -> point_NN.json -> new masks (never unmasked):
              * rate > --mask-hz (10 kHz)
              * comparator stuck: rate was >= --stuck-min-hz and fell by > x10 while the
                threshold went down
            a chip with >= --leave-at masked channels leaves the sweep (all masked, sdt +40)
    end   : the two reference captures again, monitor map again, GUI apply of --restore.

Never starts a DAQ run (a lab run sets the Run HV on the supplies); acquisition on/off and
dumpcap are done here, as in noise_probe.py. Stops (and restores the chips) if the HV
reads above 5 V.

    fast_ratescan.py --out-dir ~/trim_work/out/fastscan_20261116 [--chips 2-19] [--top 4 --bottom -24]
    fast_ratescan.py --dry-run --out-dir /tmp/x        # no hardware: print what two points change
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import noise_probe as NP  # noqa: E402
import vmm_trim as VT  # noqa: E402

EXT_DIR = os.path.join(NP.BENCH, 'config_ext')
PY = '/local/p2/deploy/bin/p2basket-python'
VMM_ONLY = os.path.join(HERE, 'vmm_only_config.py')
SCAN = os.path.join(TOOL, 'vmm_monitor_scan.py')
MONITOR_BIN = os.path.expanduser('~/trim_work/build/vmm_monitor')
PFX = 'p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_'
START = PFX + 'trimmed_Km18_v1.txt'
REF1 = PFX + 'step1_c2c3_ratescan_sdtm6.txt'
REF2 = PFX + 'fastscan_trimmed_v1_sdtm6.txt'
RECOVER = PFX + 'fastscan_recover.txt'
RESTORE = PFX + 'safe_m9c16to21_m4c36to41_h0open_adwk2_Km18_i5.txt'
HV_MAX_V = 5.0
N_HYB = 10

LOGF = None


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + '\n')
        LOGF.flush()


class Abort(Exception):
    pass


# ------------------------------------------------------------------ pure logic (tested) --
def decide(rates, prev, masked, mask_hz=1e4, stuck_min_hz=1e3, stuck_factor=10.0):
    """New masks for one chip. rates/prev: {channel: Hz} at this and the previous point
    (prev None at the first point); masked: channels already masked. Returns {ch: reason}."""
    new = {}
    for ch in range(VT.N_CHANNELS):
        if ch in masked:
            continue
        r = rates.get(ch, 0.0)
        if r > mask_hz:
            new[ch] = 'rate %.0f Hz' % r
        elif prev is not None and prev.get(ch, 0.0) >= stuck_min_hz and r < prev[ch] / stuck_factor:
            new[ch] = 'stuck %.0f -> %.0f Hz' % (prev[ch], r)
    return new


def point_file(start_ext, ref_sdt, offset, chips, masked, left, leave_by=40):
    """ExtFile of one point: start file, sweep chips at ref_sdt + offset (left chips at
    ref + leave_by), and `sm 1` on every masked channel. Returns (ExtFile, {chip: sdt})."""
    ext = VT.ExtFile(list(start_ext.lines))
    sdt = {}
    for c in chips:
        sdt[c] = ref_sdt[c] + (leave_by if c in left else offset)
        ext.set_sdt(c, sdt[c])
    ext.lines.append('# fast_ratescan: offset %+d; masks below (cumulative)' % offset)
    for c in chips:
        hyb, vmm = VT.hyb_vmm(c)
        chans = range(VT.N_CHANNELS) if c in left else sorted(masked[c])
        for ch in chans:
            ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sm   1' % (hyb, vmm, ch))
    return ext, sdt


# ------------------------------------------------------------------------ hardware --
def hv_now():
    with urllib.request.urlopen(NP.GUI + '/hv_data_live', timeout=15) as r:
        d = json.loads(r.read().decode())
    return {k: a[-1] for k, a in d['voltage'].items()}


def check_hv():
    v = hv_now()
    if max(v.values()) > HV_MAX_V:
        raise Abort('HV is ON (%s): stopping' % v)
    return v


def links():
    r = subprocess.run([NP.SC, '--config-file', NP.BASE, '--read-link-status'], cwd=NP.BENCH,
                       capture_output=True, text=True, timeout=90)
    st = {int(m.group(1)): int(m.group(2)) for m in
          re.finditer(r'hybrid\[(\d+)\]\.linkStatus \S+ (\d+)', r.stdout)}
    return {h: st.get(h) for h in range(N_HYB)}


def links_ok(L):
    return all(L.get(h) == 4 for h in range(N_HYB))


def vmm_write(path):
    t = time.time()
    r = subprocess.run([PY, VMM_ONLY, '-c', NP.BENCH, '-b', NP.BASE, '-e', path],
                       cwd=NP.BENCH, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError('VMM-only write failed: ' + (r.stdout + r.stderr)[-600:])
    return time.time() - t


def probe(name, seconds, out_dir, max_mb):
    """GUI apply + capture + count, like noise_probe.py; writes NAME.rates.json."""
    os.makedirs(out_dir, exist_ok=True)
    check_hv()
    NP.select_and_apply(name)
    L = links()
    pcap = os.path.join(out_dir, name[:-4] + '.pcapng')
    mb = NP.capture(pcap, seconds, max_mb)
    res = NP.count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
    res.update({'ext': name, 'time': datetime.now().isoformat(timespec='seconds'), 'mb': mb,
                'seconds_asked': seconds, 'links': L, 'hv': 'not touched by this tool'})
    with open(os.path.join(out_dir, name[:-4] + '.rates.json'), 'w') as f:
        json.dump(res, f)
    if os.path.exists(pcap):
        os.remove(pcap)
    tot = sum(sum(v.values()) for v in res['hits'].values())
    log('reference %s: %.0f MB, %d hits in %.1f s, links %s' % (name[len(PFX):], mb, tot, res['live_s'],
                                                                ''.join(str(v) for v in L.values())))
    return res


def monitor_map(ext_name, out, workdir, select_first):
    """Monitor map of all chips, all channels unmasked. The tool re-applies the GUI selection at
    the end, so select_first is what the chips are configured with afterwards."""
    NP.gui('/chip_config/select', {'file': select_first})
    os.makedirs(workdir, exist_ok=True)
    t = time.time()
    with open(out[:-5] + '.log', 'w') as lf:
        r = subprocess.run(['python3', SCAN, 'map', '--ext', os.path.join(EXT_DIR, ext_name), '--unmask',
                            '--bin', MONITOR_BIN, '--workdir', workdir, '--out', out],
                           stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError('monitor map failed (rc %s), see %s' % (r.returncode, out[:-5] + '.log'))
    log('monitor map %s in %.0f s' % (os.path.basename(out), time.time() - t))


def make_ref2(start_ext, chips, force=False):
    path = os.path.join(EXT_DIR, REF2)
    if os.path.exists(path) and not force:
        return
    ext = VT.ExtFile(list(start_ext.lines))
    s = ext.sdt()
    for c in chips:
        ext.set_sdt(c, s[c] - 6)
    ext.lines.insert(0, '# fast_ratescan reference: %s with chips %s at sdt -6 (%s)'
                     % (START, ','.join(map(str, chips)), datetime.now().isoformat(timespec='seconds')))
    ext.write(path, force=True)
    log('wrote %s' % path)


# ----------------------------------------------------------------------------- main --
def sweep(a, start_ext, chips, out):
    ref_sdt = start_ext.sdt()
    start_masks = start_ext.channel_bits('sm')
    masked = {c: set(start_masks.get(c, [])) for c in chips}
    left, prev, kept = set(), {c: None for c in chips}, []
    os.makedirs(os.path.join(out, 'files'), exist_ok=True)
    offsets = list(range(a.top, a.bottom - 1, -1))
    i0 = 0
    if a.resume:
        # continue after a good point: its masks plus the ones it decided, its rates as 'previous'
        r = json.load(open(a.resume))
        for c in chips:
            masked[c] = set(r['masked'].get(str(c), [])) | {int(k) for k in r['new_masks'].get(str(c), {})}
        left = {c for c in chips if len(masked[c]) >= a.leave_at} | set(r.get('left', []))
        live = r['live_s']
        prev = {c: {int(k): v / live for k, v in r['hits'].get(str(c), {}).items()} for c in chips}
        offsets = list(range(r['offset'] - 1, a.bottom - 1, -1))
        i0 = r['index'] + 1
        log('resume after point %d (offset %+d): %d channels masked, chips left %s'
            % (r['index'], r['offset'], sum(len(m) for m in masked.values()), sorted(left)))
    for i, off in enumerate(offsets, start=i0):
        t0 = time.time()
        check_hv()
        ext, sdt = point_file(start_ext, ref_sdt, off, chips, masked, left)
        path = os.path.join(out, 'files', 'point_%02d.txt' % i)
        ext.write(path, force=True)
        t_apply = vmm_write(path)
        L = links()
        recovered = False
        if not links_ok(L):
            log('point %d: links %s after the VMM-only write: full configure of this point' % (i, L))
            shutil.copy(path, os.path.join(EXT_DIR, RECOVER))
            NP.select_and_apply(RECOVER)
            L, recovered = links(), True
            if not links_ok(L):
                raise Abort('links still %s after a full configure' % L)
        pcap = os.path.join(out, 'point_%02d.pcapng' % i)
        t1 = time.time()
        mb = NP.capture(pcap, a.seconds, a.max_mb)
        t_cap = time.time() - t1
        res = NP.count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
        empty_recovered = False
        if mb < a.empty_mb or not res['live_s']:
            # 16 Nov: after a 2 MHz point the read-out sent nothing at all (links 4) until a warm reset
            log('point %d: EMPTY capture (%.2f MB, live %.1f s): full configure of this point and capture again'
                % (i, mb, res['live_s']))
            shutil.copy(path, os.path.join(EXT_DIR, RECOVER))
            NP.select_and_apply(RECOVER)
            L, recovered, empty_recovered = links(), True, True
            t1 = time.time()
            mb = NP.capture(pcap, a.seconds, a.max_mb)
            t_cap = time.time() - t1
            res = NP.count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
            if mb < a.empty_mb or not res['live_s']:
                raise Abort('capture still empty (%.2f MB) after a full configure' % mb)
        live = res['live_s'] or 1e-9
        rates = {c: {int(k): v / live for k, v in res['hits'].get(str(c), {}).items()} for c in chips}
        tot = {c: sum(rates[c].values()) for c in chips}
        saturated = [c for c in chips if tot[c] > a.chip_max_hz]
        new = {}
        for c in chips:
            if c in left:
                continue
            nm = decide(rates[c], prev[c], masked[c], a.mask_hz, a.stuck_min_hz)
            if nm:
                new[c] = nm
        rec = {'index': i, 'offset': off, 'time': datetime.now().isoformat(timespec='seconds'),
               'sweep_chips': chips, 'sdt': {str(c): sdt[c] for c in chips},
               'masked': {str(c): (list(range(64)) if c in left else sorted(masked[c])) for c in chips},
               'left': sorted(left), 'new_masks': {str(c): {str(k): v for k, v in d.items()} for c, d in new.items()},
               'chip_total_hz': {str(c): round(tot[c], 1) for c in chips}, 'saturated': saturated,
               'hits': res['hits'], 'live_s': res['live_s'], 'mb': mb, 'links': L, 'recovered': recovered,
               'empty_recovered': empty_recovered,
               'apply_s': round(t_apply, 1), 'capture_s': round(t_cap, 1), 'ext_file': path}
        with open(os.path.join(out, 'point_%02d.json' % i), 'w') as f:
            json.dump(rec, f)
        kept.append(pcap)
        while len(kept) > 2:                 # keep the two deepest captures only
            p = kept.pop(0)
            if os.path.exists(p):
                os.remove(p)
        for c, d in new.items():
            masked[c].update(d)
            if len(masked[c]) >= a.leave_at:
                left.add(c)
                log('   chip %d leaves the sweep (%d channels masked)' % (c, len(masked[c])))
        prev = rates
        busy = ' '.join('%d:%s%.0fk' % (c, '*' if c in saturated else '', tot[c] / 1e3)
                        for c in chips if tot[c] >= 1e3)
        log('pt %2d off %+3d  write %.1f s  cap %.1f s (%.0f MB, live %.1f s)  links %s%s  total %.2f MHz  '
            '+%d masks (sum %d)  %.0f s | %s'
            % (i, off, t_apply, t_cap, mb, res['live_s'], ''.join(str(v) for v in L.values()),
               ' RECOVERED' if recovered else '', sum(tot.values()) / 1e6, sum(len(d) for d in new.values()),
               sum(len(masked[c]) for c in chips), time.time() - t0, busy or 'all chips < 1 kHz'))
        if len(left) == len(chips):
            log('all chips have left the sweep')
            break
    return masked, left


def dry_run(a, start_ext, chips):
    ref = start_ext.sdt()
    base = start_ext.final()
    masked = {c: set(start_ext.channel_bits('sm').get(c, [])) for c in chips}
    for i, off in enumerate(range(a.top, a.top - 2, -1)):
        if i == 1:      # pretend point 0 masked two channels of the first chip and one channel stuck
            masked[chips[0]].update(decide({5: 2e4, 9: 50.0}, None, masked[chips[0]]))
            masked[chips[-1]].update(decide({7: 30.0}, {7: 3e3}, masked[chips[-1]]))
        ext, sdt = point_file(start_ext, ref, off, chips, masked, set())
        fin = ext.final()
        diff = {k: (base.get(k), v) for k, v in fin.items() if base.get(k) != v}
        print('point %d offset %+d: %d fields change vs %s' % (i, off, len(diff), START))
        for k in sorted(diff, key=lambda k: (k[0], k[1], k[2]))[:12]:
            print('   chip %2d %-12s %-4s %s -> %s' % (k[0], k[1], k[2], diff[k][0], diff[k][1]))
        if len(diff) > 12:
            print('   ... (%d more)' % (len(diff) - 12))
        print('   sdt: %s' % ' '.join('%d:%d' % (c, sdt[c]) for c in chips))
        print('   masks: %s' % {c: sorted(m) for c, m in masked.items() if m})
        ext.write(os.path.join(a.out_dir, 'dry_point_%d.txt' % i), force=True)


def main():
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--chips', default='2-19')
    ap.add_argument('--top', type=int, default=4)
    ap.add_argument('--bottom', type=int, default=-24)
    ap.add_argument('--seconds', type=int, default=15, help='capture per point (stops early at --max-mb)')
    ap.add_argument('--max-mb', type=int, default=300)
    ap.add_argument('--ref-seconds', type=int, default=30)
    ap.add_argument('--mask-hz', type=float, default=1e4)
    ap.add_argument('--stuck-min-hz', type=float, default=1e3)
    ap.add_argument('--chip-max-hz', type=float, default=1e6, help='point flagged saturated for that chip')
    ap.add_argument('--leave-at', type=int, default=60)
    ap.add_argument('--empty-mb', type=float, default=0.3,
                    help='a capture below this is "no data": full configure and capture again (once)')
    ap.add_argument('--resume', help='point_NN.json of a good point: continue the sweep after it '
                                     '(use with --no-refs --no-maps, or with a new map)')
    ap.add_argument('--no-maps', action='store_true', help='skip both monitor maps (test runs)')
    ap.add_argument('--no-refs', action='store_true', help='skip the reference captures (test runs)')
    ap.add_argument('--restore', default=RESTORE)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    chips = VT.parse_list(a.chips, 2, VT.N_CHIPS - 1, 'chip')
    a.out_dir = os.path.abspath(a.out_dir)      # the slow-control scripts run in TestBenchCERN/
    os.makedirs(a.out_dir, exist_ok=True)
    start_ext = VT.ExtFile.load(os.path.join(EXT_DIR, START) if not a.dry_run or os.path.exists(
        os.path.join(EXT_DIR, START)) else START)
    if a.dry_run:
        return dry_run(a, start_ext, chips)

    LOGF = open(os.path.join(a.out_dir, 'scan.log'), 'a')
    log('fast_ratescan start: chips %s, offsets %+d..%+d, %d s per point; args %s' % (a.chips, a.top, a.bottom,
                                                                                     a.seconds, vars(a)))
    if NP.daq_run_active() or NP.capture_active():
        raise SystemExit('a DAQ run or a capture is active: not touching anything')
    log('HV %s' % check_hv())
    log('GUI selection before: %s' % NP.gui('/chip_config/status').get('selected'))
    log('links before: %s' % links())
    json.dump({'args': vars(a), 'start_file': START, 'ref_sdt': {str(c): v for c, v in start_ext.sdt().items()},
               'start_masks': {str(c): v for c, v in start_ext.channel_bits('sm').items()}},
              open(os.path.join(a.out_dir, 'scan_meta.json'), 'w'), indent=1)
    out = a.out_dir
    status, swept = 'ok', False
    try:
        if not a.no_refs:
            make_ref2(start_ext, chips)
            for name in (REF1, REF2):
                probe(name, a.ref_seconds, os.path.join(out, 'ref_start'), a.max_mb)
        if not a.no_maps:
            check_hv()
            monitor_map(START, os.path.join(out, 'map_start.json'), os.path.join(out, 'mapwork'), START)
        else:
            NP.select_and_apply(START)
        log('links at the start of the sweep: %s' % links())
        swept = True
        masked, left = sweep(a, start_ext, chips, out)
        log('sweep done: %d channels masked in total, chips left: %s'
            % (sum(len(m) for m in masked.values()), sorted(left)))
    except Abort as e:
        status = 'aborted: %s' % e
        log('ABORT: %s' % e)
    except BaseException as e:  # noqa: BLE001 - always restore
        status = 'error: %r' % e
        log('ERROR: %s' % traceback.format_exc())
    finally:
        try:
            if swept and status == 'ok':
                if not a.no_refs:
                    for name in (REF1, REF2):
                        probe(name, a.ref_seconds, os.path.join(out, 'ref_end'), a.max_mb)
                if not a.no_maps:
                    check_hv()
                    monitor_map(START, os.path.join(out, 'map_end.json'), os.path.join(out, 'mapwork'), a.restore)
        except BaseException as e:  # noqa: BLE001
            status += ' | end phase: %r' % e
            log('END PHASE ERROR: %s' % traceback.format_exc())
        try:
            log('restoring %s' % a.restore)
            NP.select_and_apply(a.restore)
            log('restored; links %s' % links())
        except BaseException as e:  # noqa: BLE001
            status += ' | restore FAILED: %r' % e
            log('RESTORE FAILED: %s -- press Configure chip in the GUI' % e)
        log('ALLDONE status: %s' % status)


if __name__ == '__main__':
    main()
