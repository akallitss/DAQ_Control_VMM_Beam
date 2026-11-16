#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short noise capture with a given chip exception file, WITHOUT starting a DAQ
run: no run number, no logbook entry and, above all, NO HIGH VOLTAGE (a normal
lab run sets the "Run HV" on the supplies before each sub-run; this does not
touch them). Meant for threshold/trim tests with the detectors unpowered.

What it does, on the DAQ machine:
  1. selects the file in the DAQ GUI and presses "Configure chip" (warm reset,
     then the P2 group's configuration script) - the normal path;
  2. acquisition on, dumpcap for --seconds (stops early at --max-mb), acquisition off;
  3. counts the valid hits per channel (memory-capped decoder) -> JSON;
  4. puts the GUI selection back to what it was (and, with --restore, configures
     the chips with it again).

    noise_probe.py --ext FILE.txt [--ext FILE2.txt ...] --out-dir DIR [--seconds 60] [--restore]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

GUI = 'http://localhost:5002'
BENCH = '/local/p2/p2testbench/TestBenchCERN'
SC = '/local/p2/deploy/bin/p2basket-sc'
BASE = 'config_base/p2b-config-cern-base.yaml'
IFACE = 'enp4s0f1'
P2_PY = '/local/p2/p2-readout/venv/bin/python'
COUNTER = '/local/p2/DAQ_Control_VMM_Beam/quick_scripts/adaptive_thr_scan.py'


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def gui(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(GUI + path, data=data, method='POST' if payload is not None else 'GET',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def daq_run_active():
    return subprocess.run(['pgrep', '-f', 'daq_control.py .*json'], capture_output=True).returncode == 0


def capture_active():
    return subprocess.run(['pgrep', '-x', 'dumpcap|tcpdump'], capture_output=True).returncode == 0


def select_and_apply(name):
    r = gui('/chip_config/select', {'file': name})
    if not r.get('success'):
        raise SystemExit('GUI refused to select %s: %s' % (name, r))
    r = gui('/chip_config/apply', {})
    if not r.get('success'):
        raise SystemExit('GUI refused to apply: %s' % r)
    time.sleep(4)
    t0 = time.time()
    while time.time() - t0 < 420:
        st = gui('/chip_config/status')
        if not st.get('running'):
            last = st.get('last') or {}
            if last.get('file') != name or last.get('rc') != 0:
                raise SystemExit('apply of %s failed: rc=%s %s' % (name, last.get('rc'), (last.get('output') or '')[-400:]))
            return
        time.sleep(4)
    raise SystemExit('apply did not finish')


def sc(action, config=BASE):
    r = subprocess.run([SC, '--config-file', config, '--' + action], cwd=BENCH, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError('p2basket-sc --%s failed: %s' % (action, (r.stdout + r.stderr)[-400:]))


def capture(path, seconds, max_mb):
    sc('acq-on')
    try:
        subprocess.run(['dumpcap', '-q', '-i', IFACE, '-a', 'duration:%d' % seconds,
                        '-a', 'filesize:%d' % (max_mb * 1000), '-w', path],
                       capture_output=True, text=True, timeout=seconds + 60)
    finally:
        sc('acq-off')
    return os.path.getsize(path) / 1e6 if os.path.exists(path) else 0.0


def count(path):
    cmd = ['systemd-run', '--user', '--scope', '-q', '-p', 'MemoryMax=2G', P2_PY, COUNTER, '_count', path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError('decode failed: ' + r.stderr[-400:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ext', action='append', required=True, help='file name in config_ext/ (repeatable)')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--max-mb', type=int, default=400, help='stop a capture early at this size')
    ap.add_argument('--restore', action='store_true', help='configure the chips with the original GUI selection at the end')
    ap.add_argument('--keep-pcap', action='store_true')
    ap.add_argument('--fec-yaml', help='after each configure, write the FEC registers (only those) from this base '
                                       'YAML, e.g. a copy with another test-pulse tp_number; the standard base FEC '
                                       'registers are written back at the end')
    a = ap.parse_args()

    if daq_run_active() or capture_active():
        raise SystemExit('a DAQ run or a capture is active: not touching anything')
    os.makedirs(a.out_dir, exist_ok=True)
    original = gui('/chip_config/status').get('selected')
    log('GUI selection before: %s' % original)
    try:
        for name in a.ext:
            name = os.path.basename(name)
            log('configure: %s' % name)
            select_and_apply(name)
            if a.fec_yaml:
                log('FEC registers from %s' % a.fec_yaml)
                sc('config-fec-regs', os.path.abspath(a.fec_yaml))
            pcap =os.path.join(a.out_dir, name[:-4] + '.pcapng')
            mb = capture(pcap, a.seconds, a.max_mb)
            log('captured %.1f MB' % mb)
            res = count(pcap) if mb > 0.01 else {'live_s': 0.0, 'hits': {}}
            res.update({'ext': name, 'time': datetime.now().isoformat(timespec='seconds'), 'mb': mb,
                        'seconds_asked': a.seconds, 'hv': 'not touched by this tool'})
            out = os.path.join(a.out_dir, name[:-4] + '.rates.json')
            with open(out, 'w') as f:
                json.dump(res, f)
            tot = sum(sum(v.values()) for v in res['hits'].values())
            log('%d hits in %.1f s live -> %s' % (tot, res['live_s'], out))
            if not a.keep_pcap and os.path.exists(pcap):
                os.remove(pcap)
    finally:
        if a.fec_yaml:
            try:
                log('FEC registers back to the standard base YAML')
                sc('config-fec-regs')
            except BaseException as e:  # noqa: BLE001
                log('could not restore the FEC registers: %s (a Configure chip will)' % e)
        if original:
            try:
                gui('/chip_config/select', {'file': original})
                if a.restore:
                    log('restoring the chips to %s' % original)
                    select_and_apply(original)
                else:
                    log('GUI selection put back to %s (chips NOT re-configured: press Configure chip before a run)' % original)
            except BaseException as e:  # noqa: BLE001 - report, never hide the first error
                log('could not restore the GUI selection: %s' % e)


if __name__ == '__main__':
    main()
