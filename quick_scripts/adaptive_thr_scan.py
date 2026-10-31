#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adaptive cosmic threshold scan: lower the threshold, mask the channels that turn noisy, retry.

The scan moves every detector chip (VMM 2-19) by the same offset K (DAC) from a base config (the run_130
"safe" config: each chip at its own noise onset + 8). H0, the trigger hybrid, and every line other than the
detector sdt lines are never touched. At each scan point K:

  1. probe   a short run (config_scan minimum, 2 min) with the masks needed so far at this K;
  2. decide  decode the probe, rate per channel; every detector channel above --ch-limit-hz is masked
             (sm 1). A probe the rate guard stopped is decoded the same way (it is the same question, louder);
  3. retry   until a probe is clean (no channel over the limit and the volume under --max-mb-min), at most
             --max-iter probes per point;
  4. floor   a chip that would need more than --max-masks-per-chip masked channels stops following the scan:
             it is held at its last clean K (with the masks it had there) for this and every lower K;
  5. measure the long run (--minutes) with the clean config. If the rate guard stops it, the masks are
             updated from the stopped run and the point is resumed for the remaining time.

Masks are per point: each point starts from the base masks (plus those found at the same K in an earlier
pass) and its probes add what it needs. Noise is not monotonic in threshold on every chip (the VMM 4/7/9
bursts), so masks are not carried from one K to another. Everything is written to
a state file (one JSON per scan, --state) and a logbook note per decision, so the analysis can use the pad
set that was live at every point.

Runs on the DAQ machine, in tmux. Decoding runs in a memory-capped child (systemd-run MemoryMax).

    adaptive_thr_scan.py --points 0,-4,-8,-12 --minutes 180 --tag wk1          # the scan
    adaptive_thr_scan.py --points 0,-8 --dry-run --tag test                    # configs + plan only
    adaptive_thr_scan.py --replay run_130 --tag test                           # decision on an old run
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

REPO = '/local/p2/DAQ_Control_VMM_Beam'
CFG_DIR = '/local/p2/p2testbench/TestBenchCERN/config_ext'
DATA = '/local/p2/p2data/cosmics_3det_202610/runs'
BASE = 'p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_safe_m9c16to21_m4c36to41_h0open.txt'
SCAN_LOG = '/local/p2/config_scan.log'
TRIP = os.path.join(REPO, 'config', 'rate_guard_tripped.json')
P2_PY = '/local/p2/p2-readout/venv/bin/python'
DAQ_PY = os.path.join(REPO, '.venv', 'bin', 'python')
CHIPS = range(2, 20)                       # detector chips; H0 = chips 0, 1, never touched
PROBE_MIN = 2                              # config_scan refuses shorter runs at the lab

SDT_RE = re.compile(
    r'^(?P<head>Sys\s+0\s+Bec\s+0\s+Hyb\s+(?P<hyb>\d+)\s+Vmm\s+(?P<vmm>\d+)\s+Reg\s+'
    r'bank1spi(?P<spi>[12])\s+Field\s+sdt\s+)(?P<val>\d+)(?P<tail>.*)$')
MASK_RE = re.compile(r'^\s*Sys\s+0\s+Bec\s+0\s+Hyb\s+(\d+)\s+Vmm\s+(\d+)\s+Reg\s+channel\[(\d+)\]\s+Field\s+sm\s+1\b')

LOGF = None


def log(msg):
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}'
    print(line, flush=True)
    if LOGF:
        with open(LOGF, 'a') as f:
            f.write(line + '\n')


def note(text, run=None):
    """Logbook note (best effort: the scan never stops because the logbook failed)."""
    cmd = [DAQ_PY, os.path.join(REPO, 'lab_logbook.py'), 'note', text] + (['--run', run] if run else [])
    try:
        subprocess.run(cmd, cwd=REPO, capture_output=True, timeout=60)
    except Exception as e:
        log(f'   logbook note failed: {e}')


# ---------------------------------------------------------------- configs

def read_base(path):
    lines = open(path).readlines()
    idx, vals = {}, {}
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith('#'):
            continue
        m = SDT_RE.match(ln.rstrip('\n'))
        if m:
            chip = 2 * int(m['hyb']) + int(m['vmm'])
            idx.setdefault(chip, {})[int(m['spi'])] = i
            vals.setdefault(chip, {})[int(m['spi'])] = int(m['val'])
    sdt = {c: v[2] * 64 + v[1] for c, v in vals.items() if set(v) == {1, 2}}
    missing = [c for c in CHIPS if c not in sdt]
    if missing:
        raise SystemExit(f'base config: no complete sdt lines for chips {missing}')
    base_masks = {}
    for ln in lines:
        m = MASK_RE.match(ln)
        if m:
            base_masks.setdefault(2 * int(m[1]) + int(m[2]), set()).add(int(m[3]))
    return lines, idx, sdt, base_masks


def render(lines, idx, new_sdt, masks, header):
    out = list(lines)
    for chip, s in new_sdt.items():
        if not 0 <= s <= 1023:
            raise SystemExit(f'chip {chip}: sdt {s} outside 0..1023')
        for spi, v in ((1, s % 64), (2, s // 64)):
            i = idx[chip][spi]
            m = SDT_RE.match(out[i].rstrip('\n'))
            tail = re.sub(r'#.*$', f'# {s}' if spi == 2 else '#', m['tail'])
            out[i] = f'{m["head"].rstrip()} {v:>2} {tail.strip()}\n'.replace(' \n', '\n')
    if out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.append('\n# ' + header + '\n')
    n = 0
    for chip in sorted(masks):
        for ch in sorted(masks[chip]):
            out.append(f'Sys 0 Bec 0 Hyb {chip // 2} Vmm {chip % 2} Reg channel[{ch:02d}] Field sm   1\n')
            n += 1
    # safety: nothing but detector sdt lines changed, nothing but masks appended
    for a, b in zip(lines, out[:len(lines)]):
        if a != b:
            m = SDT_RE.match(a.rstrip('\n'))
            if not m or 2 * int(m['hyb']) + int(m['vmm']) not in CHIPS:
                raise SystemExit(f'refusing: a non-detector-sdt line would change:\n{a}{b}')
    return out, n


def k_label(k):
    return f'p{k}' if k > 0 else (f'm{-k}' if k < 0 else '0')


# ---------------------------------------------------------------- state

class State:
    """masks_found[K][chip] = channels found noisy at K; hold[chip] = {'k': floor K, 'masks': [...]}"""

    def __init__(self, path):
        self.path = path
        d = json.load(open(path)) if os.path.exists(path) else {}
        self.found = {int(k): {int(c): set(v) for c, v in m.items()} for k, m in d.get('masks_found', {}).items()}
        self.hold = {int(c): v for c, v in d.get('hold', {}).items()}
        self.points = d.get('points', [])
        self.meta = d.get('meta', {})

    def save(self):
        d = {'meta': self.meta,
             'masks_found': {str(k): {str(c): sorted(v) for c, v in m.items()} for k, m in sorted(self.found.items())},
             'hold': {str(c): v for c, v in sorted(self.hold.items())},
             'points': self.points}
        tmp = self.path + '.tmp'
        json.dump(d, open(tmp, 'w'), indent=1)
        os.replace(tmp, self.path)

    def masks_at(self, k, base_masks):
        """Masks at K: the base ones plus those found at this same K (a point revisited in a later pass
        starts from them). Masks found at other K are NOT carried over: the VMM 4/7/9 bursts are not monotonic
        in threshold (VMM 9 bursts at K=+64 and is quiet at K=0, run_133 vs run_130), so each point's probes
        find what that point needs. Held chips keep the masks they had at their floor."""
        m = {c: set(v) for c, v in base_masks.items()}
        for c, chs in self.found.get(k, {}).items():
            m.setdefault(c, set()).update(chs)
        for c, h in self.hold.items():
            if k < h['k']:
                m[c] = set(base_masks.get(c, set())) | set(h['masks'])
        return m

    def sdt_at(self, k, base_sdt):
        s = {}
        for c in CHIPS:
            kk = self.hold[c]['k'] if (c in self.hold and k < self.hold[c]['k']) else k
            s[c] = base_sdt[c] + kk
        return s


# ---------------------------------------------------------------- runs

def run_config_scan(cfg_name, minutes, dry):
    """One config_scan invocation for one config. -> (run_name or None, log lines of this call)."""
    start = os.path.getsize(SCAN_LOG) if os.path.exists(SCAN_LOG) else 0
    until = (datetime.now() + timedelta(minutes=minutes + 15)).strftime('%H:%M')
    cmd = [DAQ_PY, 'config_scan.py', '--until', until, '--minutes', str(minutes), '--configs', cfg_name]
    if dry:
        cmd.append('--dry-run')
    log(f'   config_scan {minutes} min: {cfg_name}')
    subprocess.run(cmd, cwd=REPO)
    with open(SCAN_LOG) as f:
        f.seek(start)
        out = f.read().splitlines()
    run = None
    for ln in out:
        m = re.search(r'started (run_\d+)', ln)
        if m:
            run = m.group(1)
    return run, out


def tripped(run, since):
    try:
        t = json.load(open(TRIP))
    except Exception:
        return None
    if t.get('run') != run:
        return None
    if datetime.strptime(t['time'], '%Y-%m-%d %H:%M:%S').timestamp() < since - 5:
        return None
    return t


def run_files(run):
    return sorted(glob.glob(os.path.join(DATA, run, '*', 'raw_daq_data', '*.pcapng')), key=os.path.getmtime)


def channel_rates(files, mem='2G'):
    """Decode in a memory-capped child; -> {'live_s', 'hits': {chip: {ch: n}}}."""
    files = [f for f in files if os.path.getsize(f) > 10_000]
    if not files:
        return {'live_s': 0.0, 'hits': {}}
    cmd = ['systemd-run', '--user', '--scope', '-q', '-p', f'MemoryMax={mem}',
           P2_PY, os.path.abspath(__file__), '_count'] + files
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f'decode failed ({r.returncode}): {r.stderr[-500:]}')
    d = json.loads(r.stdout.strip().splitlines()[-1])
    d['hits'] = {int(c): {int(ch): n for ch, n in v.items()} for c, v in d['hits'].items()}
    return d


def _count(files):
    """Child: per-channel valid-hit counts and the summed capture live time (decoder metadata)."""
    import numpy as np
    import p2_decode
    hits, live = {}, 0.0
    for f in files:
        # streamed chunk by chunk: a loud probe is one 300+ MB file, and a whole-file decode of it was
        # killed at the 2 GB cap (run_136, 31 Oct 03:53)
        meta = {}
        n = np.zeros(64 * 20, dtype=np.int64)
        for h in p2_decode.iter_chunks(f, meta=meta):
            # a chunk is a dict of the raw column arrays; hit_valid is derived (as decode() does)
            p2_decode.derive(h)
            good = np.asarray(h['hit_valid'], dtype=bool)
            if good.any():
                v = np.asarray(h['vmm'])[good].astype(np.int64)
                c = np.asarray(h['ch'])[good].astype(np.int64)
                ok = (v >= 0) & (v < 20) & (c >= 0) & (c < 64)
                n += np.bincount(v[ok] * 64 + c[ok], minlength=64 * 20)
            del h
        live += float((meta.get('capture') or {}).get('live_time_s') or 0.0)
        for k in np.nonzero(n)[0]:
            d = hits.setdefault(str(k // 64), {})
            d[str(k % 64)] = d.get(str(k % 64), 0) + int(n[k])
    print(json.dumps({'live_s': live, 'hits': hits}))


def decide(rates, masks, a):
    """-> new masks {chip: set} (channels over the limit, not yet masked), per-chip summary."""
    live = max(rates['live_s'], 1.0)
    new, summary = {}, {}
    for c in CHIPS:
        chans = rates['hits'].get(c, {})
        tot = sum(chans.values()) / live
        hot = sorted((ch for ch, n in chans.items() if n / live > a.ch_limit_hz and ch not in masks.get(c, set())),
                     key=lambda ch: -chans[ch])
        if hot:
            new[c] = set(hot)
        summary[c] = {'rate_hz': round(tot, 1), 'new_hot': {ch: round(chans[ch] / live, 1) for ch in hot}}
    return new, summary


# ---------------------------------------------------------------- the scan

def make_cfg(st, k, it, base, a):
    lines, idx, base_sdt, base_masks = base
    sdt = st.sdt_at(k, base_sdt)
    masks = st.masks_at(k, base_masks)
    held = {c: st.hold[c]['k'] for c in st.hold if k < st.hold[c]['k']}
    n_extra = sum(len(masks.get(c, set()) - base_masks.get(c, set())) for c in CHIPS)
    hdr = (f'adaptive threshold scan {a.tag}: K = {k:+d} DAC on every detector chip from {BASE}; '
           f'held chips {held or "none"}; {n_extra} channels masked by the scan (plus the base masks)')
    out, _ = render(lines, idx, sdt, {c: masks[c] - base_masks.get(c, set()) for c in masks}, hdr)
    stem = BASE[:-4] + f'_ad{a.tag}_K{k_label(k)}_i{it}'
    name = stem + '.txt'
    with open(os.path.join(CFG_DIR, name), 'w') as f:
        f.writelines(out)
    return name, sdt, masks, held, n_extra


def apply_new_masks(st, k, new, base, a, prev_clean_k):
    """Record new masks at K; chips over the cap are held at their last clean K."""
    lines, idx, base_sdt, base_masks = base
    held_now = []
    for c, chs in new.items():
        if c in st.hold and k < st.hold[c]['k']:
            continue                                   # held chip: its setting does not change any more
        cur = st.masks_at(k, base_masks).get(c, set()) - base_masks.get(c, set())
        if len(cur | chs) > a.max_masks_per_chip:
            if prev_clean_k is None:
                # nothing cleaner to fall back to: mask anyway, the scan will stop at this point
                st.found.setdefault(k, {}).setdefault(c, set()).update(chs)
                continue
            floor_masks = sorted(st.masks_at(prev_clean_k, base_masks).get(c, set()) - base_masks.get(c, set()))
            st.hold[c] = {'k': prev_clean_k, 'masks': floor_masks,
                          'why': f'at K={k:+d} would need {len(cur | chs)} masked channels (> {a.max_masks_per_chip})'}
            held_now.append(c)
        else:
            st.found.setdefault(k, {}).setdefault(c, set()).update(chs)
    return held_now


def do_point(st, k, base, a, prev_clean_k):
    rec = {'k': k, 'start': f'{datetime.now():%Y-%m-%d %H:%M:%S}', 'probes': [], 'long': []}
    st.points.append(rec)
    st.save()
    # ---- probes
    clean = None
    for it in range(1, a.max_iter + 1):
        name, sdt, masks, held, n_extra = make_cfg(st, k, it, base, a)
        t0 = time.time()
        run, out = run_config_scan(name, PROBE_MIN, a.dry_run)
        if a.dry_run:
            rec['probes'].append({'cfg': name, 'dry_run': True, 'held': held, 'masked_by_scan': n_extra})
            clean = name
            break
        if not run:
            log(f'   probe did not start (see config_scan.log); point K={k:+d} abandoned')
            rec['status'] = 'probe failed to start'
            st.save()
            return None
        trip = tripped(run, t0)
        mb = sum(os.path.getsize(f) for f in run_files(run)) / 1e6
        try:
            rates = channel_rates(run_files(run))
        except Exception as e:
            log(f'   probe {run}: could not decode ({e}); point K={k:+d} skipped')
            rec['status'] = 'probe decode failed'
            st.save()
            return None
        mbmin = mb / max(rates['live_s'] / 60, 0.5)
        new, summary = decide(rates, masks, a)
        n_new = sum(len(v) for v in new.values())
        p = {'cfg': name, 'run': run, 'tripped': bool(trip), 'mb': round(mb), 'mb_min': round(mbmin, 1),
             'live_s': round(rates['live_s']), 'held': held, 'masked_by_scan': n_extra,
             'new_hot': {str(c): s['new_hot'] for c, s in summary.items() if s['new_hot']},
             'chip_rate_hz': {str(c): s['rate_hz'] for c, s in summary.items()}}
        rec['probes'].append(p)
        hot_txt = '; '.join(f'VMM {c}: ' + ', '.join(f'ch{ch} {r:g} Hz' for ch, r in s['new_hot'].items())
                            for c, s in summary.items() if s['new_hot']) or 'none'
        log(f'   probe {run} K={k:+d} i{it}: {mbmin:.0f} MB/min{" (RATE GUARD STOPPED IT)" if trip else ""}, '
            f'new hot channels: {hot_txt}')
        if not new and not trip and mbmin <= a.max_mb_min:
            clean = name
            note(f'Adaptive scan {a.tag}, K={k:+d}: probe clean ({mbmin:.0f} MB/min, {n_extra} channels masked by '
                 f'the scan, held chips {held or "none"}). Long run next.', run)
            break
        if not new:
            # loud but no single channel over the limit: chip-wide noise. Hold the loudest chips.
            loud = sorted(((r, c) for c, r in ((c, s['rate_hz']) for c, s in summary.items())
                           if not (c in st.hold and k < st.hold[c]['k'])), reverse=True)[:2]
            if prev_clean_k is None or not loud:
                log(f'   K={k:+d}: too loud ({mbmin:.0f} MB/min) with no channel to mask and nothing to fall back to')
                rec['status'] = 'too loud'
                st.save()
                return None
            for r, c in loud:
                fm = sorted(st.masks_at(prev_clean_k, base[3]).get(c, set()) - base[3].get(c, set()))
                st.hold[c] = {'k': prev_clean_k, 'masks': fm, 'why': f'chip-wide noise at K={k:+d} ({r:.0f} Hz)'}
            held_now = [c for _, c in loud]
        else:
            held_now = apply_new_masks(st, k, new, base, a, prev_clean_k)
        note(f'Adaptive scan {a.tag}, K={k:+d}: probe {it} {mbmin:.0f} MB/min'
             f'{" (rate guard stopped it)" if trip else ""}; masking {hot_txt}'
             + (f'; chips {held_now} held at K={prev_clean_k:+d} (too many channels)' if held_now else ''), run)
        st.save()
    if not clean:
        log(f'   K={k:+d}: no clean probe after {a.max_iter} tries; point skipped')
        rec['status'] = 'no clean probe'
        st.save()
        return None
    # ---- long run (resumed after a rate-guard stop, with updated masks)
    left = a.minutes
    it = len(rec['probes'])
    cfg = clean
    while left >= max(10, PROBE_MIN):
        t0 = time.time()
        run, out = run_config_scan(cfg, left, a.dry_run)
        if a.dry_run or not run:
            rec['long'].append({'cfg': cfg, 'run': run, 'minutes': left, 'dry_run': a.dry_run})
            break
        ran = (time.time() - t0) / 60 - 2
        trip = tripped(run, t0)
        rec['long'].append({'cfg': cfg, 'run': run, 'minutes_requested': left, 'tripped': bool(trip),
                            'mb': round(sum(os.path.getsize(f) for f in run_files(run)) / 1e6)})
        st.save()
        if not trip:
            break
        left -= max(int(ran), 1)
        log(f'   long run {run} stopped by the rate guard; {left} min left at K={k:+d}')
        files = run_files(run)[-3:]
        try:
            rates = channel_rates(files)
        except Exception as e:
            log(f'   could not decode the stopped run ({e}); not resuming this point')
            break
        new, summary = decide(rates, st.masks_at(k, base[3]), a)
        if not new:
            log('   no channel over the limit in the last captures; not resuming this point')
            break
        held_now = apply_new_masks(st, k, new, base, a, prev_clean_k)
        it += 1
        cfg, *_ = make_cfg(st, k, it, base, a)
        note(f'Adaptive scan {a.tag}, K={k:+d}: long run stopped by the rate guard; masked '
             + '; '.join(f'VMM {c}: {sorted(v)}' for c, v in new.items())
             + (f'; held {held_now}' if held_now else '') + f'; resuming for {left} min', run)
    rec['status'] = 'done'
    rec['end'] = f'{datetime.now():%Y-%m-%d %H:%M:%S}'
    st.save()
    return k


def main():
    global LOGF, BASE
    if len(sys.argv) > 1 and sys.argv[1] == '_count':
        return _count(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--points', help='comma-separated K values (DAC from the base config), run in this order')
    ap.add_argument('--minutes', type=int, default=180, help='long-run length per point')
    ap.add_argument('--tag', required=True, help='scan name: in config names, state file and notes')
    ap.add_argument('--ch-limit-hz', type=float, default=50.0, help='mask a detector channel above this rate')
    ap.add_argument('--max-mb-min', type=float, default=30.0, help='a probe above this volume is not clean')
    ap.add_argument('--max-masks-per-chip', type=int, default=20,
                    help='a chip needing more masked channels is held at its last clean K')
    ap.add_argument('--max-iter', type=int, default=5, help='probes per point')
    ap.add_argument('--base', default=BASE)
    ap.add_argument('--dry-run', action='store_true', help='write configs, config_scan --dry-run, no runs')
    ap.add_argument('--replay', help='only print the mask decision for an existing run')
    a = ap.parse_args()

    outdir = os.path.join(REPO, 'logs', 'adaptive')
    os.makedirs(outdir, exist_ok=True)
    LOGF = os.path.join(outdir, f'scan_{a.tag}.log')

    if a.replay:
        rates = channel_rates(run_files(a.replay))
        _, _, _, base_masks = read_base(os.path.join(CFG_DIR, a.base))
        new, summary = decide(rates, base_masks, a)
        print(f'{a.replay}: live {rates["live_s"]:.0f} s; channels over {a.ch_limit_hz:g} Hz:')
        for c, s in summary.items():
            print(f'  VMM {c:2d}: chip {s["rate_hz"]:8.1f} Hz  ' + (', '.join(f'ch{ch} {r:g} Hz' for ch, r in s['new_hot'].items()) or '-'))
        return

    BASE = a.base
    base = read_base(os.path.join(CFG_DIR, a.base))
    st = State(os.path.join(outdir, f'state_{a.tag}.json'))
    st.meta.update({'base': a.base, 'base_sdt': {str(c): base[2][c] for c in CHIPS},
                    'ch_limit_hz': a.ch_limit_hz, 'max_mb_min': a.max_mb_min,
                    'max_masks_per_chip': a.max_masks_per_chip, 'minutes': a.minutes})
    st.save()
    ks = [int(x) for x in a.points.split(',')]
    log(f'ADAPTIVE SCAN {a.tag}: points {ks}, {a.minutes} min each, mask channels > {a.ch_limit_hz:g} Hz, '
        f'clean below {a.max_mb_min:g} MB/min, <= {a.max_masks_per_chip} masks per chip, dry_run={a.dry_run}')
    if not a.dry_run:
        note(f'Adaptive threshold scan {a.tag} started: K = {ks} (DAC from {a.base}), {a.minutes} min per point; '
             f'channels above {a.ch_limit_hz:g} Hz are masked, chips needing more than {a.max_masks_per_chip} masks '
             f'are held at their last clean K. State: logs/adaptive/state_{a.tag}.json')
    for k in ks:
        # the last clean K above this one: where a chip that fails here is held
        done_above = [p['k'] for p in st.points if p.get('status') == 'done' and p['k'] > k]
        prev = min(done_above) if done_above else None
        log(f'=== K = {k:+d} (fallback for chips that fail: {prev if prev is not None else "none"}) ===')
        do_point(st, k, base, a, prev)
        if os.path.exists(os.path.join(outdir, f'STOP_{a.tag}')):
            log(f'   STOP_{a.tag} found: stopping after this point')
            break
    log(f'ADAPTIVE SCAN {a.tag} END')
    if not a.dry_run:
        note(f'Adaptive threshold scan {a.tag} ended. State: logs/adaptive/state_{a.tag}.json')


if __name__ == '__main__':
    main()
