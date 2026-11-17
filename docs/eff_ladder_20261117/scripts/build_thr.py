#!/usr/bin/env python3
"""thr.csv for channel_efficiency.py measure: per (pseudo-run, vmm, ch) the threshold - baseline distance in mV.

The distance of every channel in a run comes from the run's OWN ext file copy (runs/run_N/*.txt) and a measured
reference map + the ext file it was measured with, with the model bscan_target.py uses:

    d(ch) = d_ref(ch) + (S_run - S_ref) k_chip - (t_run - t_ref) step_chip

d_ref = threshold - baseline of the reference map, S = chip sdt, t = effective trim (0 if no sd line), k = mV per
sdt (sdtcal.json), step = mV per trim (trimcurve_ch5_40.json). References:
  bscan_* files  -> pilot/map_eq.json, measured with ..._bscan_eq_x15.txt (16 Nov 23:12, HV off)
  trimmed_Km18_* -> fastscan_20261116/full/map_start.json, measured with ..._trimmed_Km18_v1.txt (16 Nov 20:31)
For a run whose sdt/trims equal the reference's, d = the measured map distance exactly.
Usage: build_thr.py PSEUDO_DIR OUT_CSV
"""
import glob
import json
import os
import sys

TW = os.path.expanduser('~/trim_work')
sys.path.insert(0, f'{TW}/vmm_monitor_tool')
import vmm_trim as VT  # noqa: E402
import vmm_monitor_scan as MS  # noqa: E402

EXT = '/local/p2/p2testbench/TestBenchCERN/config_ext'
RUNS = '/local/p2/p2data/cosmics_3det_202610/runs'
P = 'p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_'
REF = {'bscan': (f'{TW}/out/bscan_20261116/pilot/map_eq.json', f'{EXT}/{P}bscan_eq_x15.txt'),
       'trimmed': (f'{TW}/out/fastscan_20261116/full/map_start.json', f'{EXT}/{P}trimmed_Km18_v1.txt')}
K = {int(c): v for c, v in MS._per_chip(f'{TW}/out/sdtcal.json', 'mV_per_sdt', 0.77).items()}
STEP = {int(c): v for c, v in MS._per_chip(f'{TW}/out/trimcurve_ch5_40.json', 'mV_per_trim', 1.1).items()}

refs = {}
for fam, (mp, ext) in REF.items():
    m = json.load(open(mp))
    assert os.path.basename(ext) == m['meta']['ext'], (mp, m['meta']['ext'])
    e = VT.ExtFile.load(ext)
    refs[fam] = (m, e.trims())


def distances(run):
    ext = glob.glob(f'{RUNS}/run_{run}/{P}*.txt')
    assert len(ext) == 1, ext
    name = os.path.basename(ext[0])[len(P):]
    fam = 'bscan' if name.startswith('bscan') else 'trimmed' if name.startswith('trimmed') else None
    m, t_ref = refs[fam]
    e = VT.ExtFile.load(ext[0])
    S, T, masked = e.sdt(), e.trims(), e.channel_bits('sm')
    out = {}
    for c in range(2, 20):
        bl, thr = m['baseline'][str(c)], m['threshold'][str(c)]
        dS = (S[c] - int(m['sdt'][str(c)])) * K[c]
        for ch in range(64):
            if str(ch) not in bl:
                continue
            dt = T.get(c, {}).get(ch, 0) - t_ref.get(c, {}).get(ch, 0)
            d = thr[str(ch)] - bl[str(ch)] + dS - dt * STEP[c]
            out[(c, ch)] = (bl[str(ch)], d, ch in masked.get(c, []))
    return name, fam, out


if __name__ == '__main__':
    pseudo, out_csv = sys.argv[1:3]
    chunks = json.load(open(f'{pseudo}/chunks.json'))
    cache, rows = {}, []
    for pid, c in sorted(chunks.items()):
        r = c['run']
        if r not in cache:
            cache[r] = distances(r)
            name, fam, d = cache[r]
            vals = sorted(v[1] for v in d.values() if not v[2])
            print(f'run_{r} {name} ({fam}): {len(vals)} unmasked channels, distance median '
                  f'{vals[len(vals) // 2]:.1f} mV, p10 {vals[len(vals) // 10]:.1f}, p90 {vals[9 * len(vals) // 10]:.1f}')
        for (v, ch), (bl, d, msk) in cache[r][2].items():
            rows.append(f'{pid},,{v},{ch},{bl + d:.3f},{bl:.3f},{d:.3f}')
    with open(out_csv, 'w') as f:
        f.write('run,K,vmm,ch,threshold_mV,baseline_mV,dist_mV\n' + '\n'.join(rows) + '\n')
    print(len(rows), 'rows ->', out_csv)
