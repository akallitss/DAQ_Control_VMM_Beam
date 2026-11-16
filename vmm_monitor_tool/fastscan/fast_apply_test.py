#!/usr/bin/env python3
"""Test (HV off, no DAQ run): can a scan point be set by writing ONLY the VMM
registers (no warm reset, no FEC/hybrid write) and give the same rates as the
full GUI apply?  Uses the step-1 rate-scan files of chips 2,3 (sdt m4/m6/p10).

Sequence: GUI m4 | fast m6 | fast m4 | fast p10 | GUI m6 | restore (GUI Km18_i5).
Every capture 20 s; link status of hybrids 0-9 read after every configure.
"""
import json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.expanduser('~/trim_work/vmm_monitor_tool'))
import noise_probe as NP

BENCH = NP.BENCH
PY = '/local/p2/deploy/bin/p2basket-python'
FAST = os.path.expanduser('~/trim_work/fastscan/vmm_only_config.py')
P = 'p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_step1_c2c3_ratescan_sdt'
OUT = os.path.expanduser('~/trim_work/fastscan/out')
SEC = 20


def link_ready():
    r = subprocess.run([NP.SC, '--config-file', NP.BASE, '--read-link-status'], cwd=BENCH,
                       capture_output=True, text=True, timeout=60)
    st = {int(m.group(1)): int(m.group(2)) for m in
          re.finditer(r'hybrid\[(\d+)\]\.linkStatus \S+ (\d+)', r.stdout)}
    return {h: st.get(h) for h in range(10)}


def fast_apply(name):
    t = time.time()
    r = subprocess.run([PY, FAST, '-c', BENCH, '-b', NP.BASE, '-e', 'config_ext/' + name],
                       cwd=BENCH, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit('fast apply failed: ' + (r.stdout + r.stderr)[-600:])
    return time.time() - t


def gui_apply(name):
    t = time.time()
    NP.select_and_apply(name)
    return time.time() - t


def point(label, mode, name):
    dt = gui_apply(name) if mode == 'gui' else fast_apply(name)
    links = link_ready()
    pcap = os.path.join(OUT, label + '.pcapng')
    t = time.time()
    mb = NP.capture(pcap, SEC, 300)
    tcap = time.time() - t
    res = NP.count(pcap)
    os.remove(pcap)
    L = res['live_s']
    chip = {c: sum(v.values()) / L for c, v in res['hits'].items()}
    rec = {'label': label, 'mode': mode, 'ext': name, 'apply_s': round(dt, 1), 'capture_s': round(tcap, 1),
           'mb': mb, 'live_s': L, 'links': links, 'chip_rate_Hz': chip, 'hits': res['hits']}
    json.dump(rec, open(os.path.join(OUT, label + '.json'), 'w'))
    NP.log('%-9s %-4s apply %5.1f s  capture %4.1f s  links %s  chip2 %.0f Hz  chip3 %.0f Hz  total %.0f Hz' % (
        label, mode, dt, tcap, ''.join(str(v) for v in links.values()),
        chip.get('2', 0), chip.get('3', 0), sum(chip.values())))


def main():
    os.makedirs(OUT, exist_ok=True)
    if NP.daq_run_active() or NP.capture_active():
        raise SystemExit('run or capture active')
    import urllib.request
    d = json.load(urllib.request.urlopen('http://localhost:5002/hv_data_live', timeout=15))
    v = {k: a[-1] for k, a in d['voltage'].items()}
    NP.log('HV %s' % v)
    if max(v.values()) > 5:
        raise SystemExit('HV is ON: stop')
    original = NP.gui('/chip_config/status').get('selected')
    NP.log('GUI selection before: %s' % original)
    try:
        point('1_gui_m4', 'gui', P + 'm4.txt')
        point('2_fast_m6', 'fast', P + 'm6.txt')
        point('3_fast_m4', 'fast', P + 'm4.txt')
        point('4_fast_p10', 'fast', P + 'p10.txt')
        point('5_gui_m6', 'gui', P + 'm6.txt')
    finally:
        NP.log('restoring %s' % original)
        NP.gui('/chip_config/select', {'file': original})
        NP.select_and_apply(original)
        NP.log('restored; links %s' % link_ready())
        NP.log('ALLDONE')


if __name__ == '__main__':
    main()
