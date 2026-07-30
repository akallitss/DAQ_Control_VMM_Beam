#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote-HV shim for combined VMM + Dream runs at the SPS.

Speaks the exact hv_control.py wire protocol on port 2100, so daq_control
needs no changes — but drives no CAEN hardware. At this site the crate hangs
off banco's private LAN and ALL HV (uRWELL + P2) is ramped, monitored and
logged by the Dream DAQ. This shim's only real job is the per-subrun gate for
HV scans: on each 'Start' it waits until Dream's actual voltage readback
(read-only GET of Dream's /hv_data) reaches this subrun's targets — the
targets arrive in sub_run['hvs'] from our own run config — and only then
answers 'HV Set', so the VMM capture never starts at the wrong voltage.

Modes (decided per run by config/dream_bridge_state.json, written by the GUI):
  - no combined run active -> every 'Start' answers 'HV Set' immediately
    (VMM-only runs work with no HV hardware at all);
  - combined run active   -> readback gate as above, with a timeout that
    answers WITHOUT 'HV Set' so daq_control skips the subrun instead of
    capturing bad data.

Monitoring commands are acknowledged but do nothing: hv_monitor.csv lives in
Dream's run tree (same run name), the VMM GUI proxies Dream's /hv_data for
display. 'Power Off' is acknowledged but never forwarded — Dream owns the
crate; powering off is done from the Dream side.

Wiring in gitignored config/dream_bridge.json (see .example):
  dream_flask_url, hv_gate.channel_labels ("slot:ch" -> Dream trace label),
  hv_gate.tolerance_v / poll_s / timeout_s.
"""

import json
import os
import time
import urllib.request
import urllib.parse

from Server import Server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'dream_bridge.json')
BRIDGE_STATE_PATH = os.path.join(BASE_DIR, 'config', 'dream_bridge_state.json')
PORT = 2100

GATE_DEFAULTS = {'tolerance_v': 3.0, 'poll_s': 5, 'timeout_s': 900}

# How long the FIRST gate of a connection re-checks for the combined-run state
# file before concluding the run is VMM-only. See _combined_state().
STATE_GRACE_S = 30

# Reset for every client connection; only the first gate of a run pays the grace.
_first_gate = True


def _combined_state():
    """Combined-run state, tolerating a race at the start of a run.

    dream_bridge_state.json is written by the VMM flask immediately BEFORE it
    launches daq_control, so it should already be on disk by the first gate.
    On run_24 (2026-07-30) it was not: sub-run nominal_00 read it as absent,
    took the 'no combined run active' fast path, and acquisition started 1 s
    after Dream began ramping — ~100 s of beam recorded from 0 V up. Every
    later sub-run gated correctly, so the state file was there by then.

    Reading it once and failing OPEN is what made that a silent data-quality
    bug, so the first gate of a connection now re-checks for STATE_GRACE_S
    before deciding a run is VMM-only. A genuinely VMM-only run pays this once
    per run, not once per sub-run.
    """
    state = _load_json(BRIDGE_STATE_PATH)
    if state.get('combined') or not _first_gate:
        return state
    waited = 0.0
    while waited < STATE_GRACE_S:
        time.sleep(1)
        waited += 1
        state = _load_json(BRIDGE_STATE_PATH)
        if state.get('combined'):
            print(f'[shim] combined-run state appeared after {waited:.0f}s — '
                  f'gating after all (would have started ungated before)')
            return state
    return state


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _subrun_targets(sub_run):
    """{'slot:ch': v0} for every channel this subrun actually sets."""
    targets = {}
    for slot, channels in (sub_run.get('hvs') or {}).items():
        for channel, v0 in channels.items():
            if v0 is not None and v0 != 0:
                targets[f'{slot}:{channel}'] = float(v0)
    return targets


def _dream_readback_ok(bridge, state, sub_run, targets):
    """(ok, detail): compare Dream's /hv_data last readback to targets."""
    gate = {**GATE_DEFAULTS, **bridge.get('hv_gate', {})}
    labels = gate.get('channel_labels') or {}
    base = bridge.get('dream_flask_url', '').rstrip('/')
    run_param = state.get('dream_run_param') or state.get('run_name', '')
    q = urllib.parse.urlencode({'run': run_param,
                                'subrun': sub_run.get('sub_run_name', '')})
    try:
        data = _get_json(f'{base}/hv_data?{q}')
    except Exception as e:
        return False, f'hv_data unreachable: {e}'
    if not data or not data.get('success'):
        return False, 'no hv_data yet for this subrun'
    voltages = data.get('voltage', {})
    for chan, v0 in targets.items():
        label = labels.get(chan)
        if label is None:
            # Channel not mapped to a Dream trace -> not gated (e.g. a channel
            # only the VMM config knows about). Configure channel_labels fully.
            continue
        trace = voltages.get(label) or []
        if not trace:
            return False, f'no readback trace {label!r} yet'
        vmon = float(trace[-1])
        if abs(vmon - v0) > gate['tolerance_v']:
            return False, f'{label}: {vmon:.1f} V -> {v0:.1f} V'
    return True, 'all channels at target'


def gate_start(sub_run):
    """Blocking per-subrun gate. Returns the reply for daq_control — contains
    'HV Set' ONLY when the subrun may start."""
    global _first_gate
    name = sub_run.get('sub_run_name', '?')
    state = _combined_state()
    first = _first_gate
    _first_gate = False
    if not state.get('combined'):
        # Loud on purpose: for a COMBINED run this line means the sub-run began
        # while the crate may still have been ramping. Grep it when a sub-run
        # looks like it recorded at the wrong voltage.
        print(f'[shim] {name}: NOT GATED — no combined run active after '
              f'{STATE_GRACE_S if first else 0}s'
              f' (expected for a VMM-only run; a BUG for a combined one)')
        return f'HV Set {name}'
    bridge = _load_json(BRIDGE_CONFIG_PATH)
    targets = _subrun_targets(sub_run)
    if not bridge.get('dream_flask_url') or not targets:
        print(f'[shim] {name}: combined but nothing to gate — instant HV Set')
        return f'HV Set {name}'
    gate = {**GATE_DEFAULTS, **bridge.get('hv_gate', {})}
    print(f'[shim] {name}: gating on Dream readback, targets {targets}')
    deadline = time.time() + gate['timeout_s']
    while time.time() < deadline:
        ok, detail = _dream_readback_ok(bridge, state, sub_run, targets)
        print(f'[shim] {name}: {detail}')
        if ok:
            return f'HV Set {name}'
        time.sleep(gate['poll_s'])
    print(f'[shim] {name}: GATE TIMEOUT after {gate["timeout_s"]}s — subrun will be skipped')
    return f'HV GATE TIMEOUT {name} — Dream readback never reached targets'


def main():
    while True:
        try:
            with Server(port=PORT) as server:
                server.receive()
                server.send('HV control connected')
                hv_info = server.receive_json()  # kept for protocol parity; unused
                print('[shim] client connected (Dream owns the real HV)')
                globals()['_first_gate'] = True   # new run: re-arm the grace
                res = server.receive()
                while 'Finished' not in res:
                    if 'Start' in res:
                        server.send('HV ready to start')
                        sub_run = server.receive_json()
                        server.send(gate_start(sub_run))
                    elif 'Power Off' in res:
                        server.send('HV ready to power off')
                        print('[shim] Power Off acknowledged — NOT forwarded; '
                              'the crate is powered off from the Dream side.')
                        server.send('HV Powered Off')
                    elif 'Begin Monitoring' in res:
                        server.send('Starting HV monitor')
                        sub_run = server.receive_json()
                        server.send(f'HV monitoring started for {sub_run["sub_run_name"]}')
                    elif 'End Monitoring' in res:
                        server.send('Stopping HV monitor')
                        server.send('HV Monitor Stopped')
                    else:
                        server.send('Unknown Command')
                    res = server.receive()
        except Exception as e:
            print(f'Error: {e}\nRestarting hv shim server...')
    print('donzo')


if __name__ == '__main__':
    main()
