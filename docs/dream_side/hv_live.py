#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dream-side live HV readback for LAB runs — STAGED, see README_DEPLOY.md.

WHY. In the lab the VMM DAQ takes the run and banco owns the CAEN crate, and
Dream takes no run at all. Dream's /hv_data can only serve a run's
hv_monitor.csv, so with no Dream run there is nothing to read and a VMM lab run
would keep no record of the voltage it was taken at. This adds ONE read-only
route that reports the crate's current readback, which hv_dream_shim.py on the
VMM machine polls and writes into each sub-run's own hv_monitor.csv.

  GET /hv_live?token=<secret>
  -> {"time": "YYYY-mm-dd HH:MM:SS",           # when the reading was taken
      "channels": {"8:0": {"power": 1, "v0": 750.0, "vmon": 749.8,
                           "imon": 0.12}, ...},
      "labels":   {"8:0": "P2_Drift", ...}}    # echoed from the config

STRICTLY READ-ONLY. There is no route here that sets, ramps or powers anything;
the crate is still driven only from the Dream side, by hand or by a Dream run.

HOLDING THE CAEN SESSION. Dream's own hv_control.py opens a session only while
a run is connected to it, so most of the time nobody holds one. This poller
therefore takes the session LAZILY and gives it back: it connects on the first
request, polls while requests keep coming, and disconnects after
idle_release_s of silence. A lab run polls continuously, so it holds a session
for the length of the run and releases it a couple of minutes after the run
ends. Dropping a file named `config/hv_live_pause` releases it immediately and
keeps it released — the switch to reach for if a Dream run ever has trouble
talking to the crate while this is up.

Deployment (see README_DEPLOY.md next to this file):
  1. copy this file to ~/DAQ_Control_Dream_Beam/flask_app/hv_live.py
  2. in flask_app/app.py, next to the vmm_trigger registration, add:
         import hv_live
         hv_live.register(app, BASE_DIR)
  3. create gitignored config/hv_live.json (crate, channels, token)
  4. restart the Dream flask (NOT the DAQ).

This file is the source of truth for the deployed copy: edit it here and
re-copy, so the VMM repo keeps the record of what runs on banco.
"""

import json
import os
import threading
import time

DEFAULTS = {
    'poll_s': 5,             # seconds between crate reads while active
    'idle_release_s': 120,   # no requests for this long -> drop the CAEN session
    'first_wait_s': 15,      # how long a request waits for the very first reading
    'stale_s': 60,           # a cached reading older than this is not served
}


def _load_cfg(base_dir):
    with open(os.path.join(base_dir, 'config', 'hv_live.json')) as f:
        cfg = json.load(f)
    return {**DEFAULTS, **cfg}


def _get_controller(hv):
    """Real CAEN controller, or the simulator when the config asks for it.

    Same two-line rule as Dream's hv_control.get_hv_controller, so 'sim' works
    here too and the route can be exercised on a machine with no crate.
    """
    if hv.get('simulate') or hv.get('ip') == 'sim':
        from sim.fake_caen import FakeCAENHVController
        return FakeCAENHVController(hv['ip'], hv.get('username'), hv.get('password'))
    from caen_hv_py.CAENHVController import CAENHVController
    return CAENHVController(hv['ip'], hv['username'], hv['password'])


class HVLiveReader:
    """Caches the crate's readback while somebody is asking for it."""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self._lock = threading.Lock()
        self._thread = None
        self._snapshot = None      # {'time', 'channels'}
        self._error = None
        self._last_request = 0.0

    # -- public ------------------------------------------------------------
    def snapshot(self, cfg):
        """Latest reading, or (None, why). Starts the poller if it is not up."""
        if os.path.exists(os.path.join(self.base_dir, 'config', 'hv_live_pause')):
            return None, 'paused (config/hv_live_pause exists)'
        self._last_request = time.time()
        self._ensure_thread(cfg)
        deadline = time.time() + cfg['first_wait_s']
        while True:
            with self._lock:
                snap, err = self._snapshot, self._error
            if snap and (time.time() - snap['taken_at']) <= cfg['stale_s']:
                return {k: v for k, v in snap.items() if k != 'taken_at'}, None
            if time.time() >= deadline:
                if snap:
                    age = time.time() - snap['taken_at']
                    return None, f'last reading is {age:.0f}s old (crate unreachable?)'
                return None, err or 'no reading yet'
            time.sleep(0.5)

    # -- internals ---------------------------------------------------------
    def _ensure_thread(self, cfg):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._poll, args=(cfg,), daemon=True)
            self._thread.start()

    def _poll(self, cfg):
        channels = [tuple(int(x) for x in str(c).split(':')) for c in cfg['channels']]
        try:
            with _get_controller(cfg['hv']) as caen:
                print('[hv_live] CAEN session open')
                while True:
                    if time.time() - self._last_request > cfg['idle_release_s']:
                        break
                    if os.path.exists(os.path.join(self.base_dir, 'config',
                                                   'hv_live_pause')):
                        print('[hv_live] pause file present — releasing the crate')
                        break
                    reading = {}
                    for slot, ch in channels:
                        entry = {
                            'power': caen.get_ch_power(slot, ch),
                            'vmon': caen.get_ch_vmon(slot, ch),
                            'imon': caen.get_ch_imon(slot, ch),
                        }
                        # V0Set is the SETPOINT — the one number a machine that
                        # does not drive the crate cannot know any other way.
                        # Optional: not every controller build exposes it, and a
                        # missing setpoint must not cost us the readback.
                        try:
                            entry['v0'] = caen.get_ch_param_float(slot, ch, 'V0Set')
                        except Exception:
                            entry['v0'] = None
                        reading[f'{slot}:{ch}'] = entry
                    with self._lock:
                        self._snapshot = {
                            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                            'taken_at': time.time(),
                            'channels': reading,
                        }
                        self._error = None
                    time.sleep(cfg['poll_s'])
        except Exception as e:
            # Includes the case that matters most: Dream's own hv_control has
            # the crate and this session was refused. Recorded for the route to
            # report; the next request starts a fresh attempt.
            with self._lock:
                self._error = f'{type(e).__name__}: {e}'
            print(f'[hv_live] poller stopped: {self._error}')
        finally:
            print('[hv_live] CAEN session closed')


def register(app, base_dir):
    from flask import jsonify, request

    reader = HVLiveReader(base_dir)

    @app.route('/hv_live')
    def hv_live():
        try:
            cfg = _load_cfg(base_dir)
        except FileNotFoundError:
            return jsonify({'success': False,
                            'message': 'config/hv_live.json missing'}), 503
        token = cfg.get('token')
        if token and request.args.get('token') != token:
            return jsonify({'success': False, 'message': 'bad token'}), 403
        allow = cfg.get('allow_ips')
        if allow and request.remote_addr not in allow:
            return jsonify({'success': False,
                            'message': f'{request.remote_addr} not allowed'}), 403
        snap, why = reader.snapshot(cfg)
        if snap is None:
            return jsonify({'success': False, 'message': why}), 503
        return jsonify({'success': True, 'labels': cfg.get('labels', {}), **snap})
