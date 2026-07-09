#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fake CAEN HV controller for local testing without a crate.

Implements the subset of caen_hv_py.CAENHVController that hv_control.py uses
(set_ch_v0, set_ch_pw, get_ch_power, get_ch_vmon, get_ch_imon, context manager).
Channels ramp toward their setpoint at RAMP_RATE V/s; vmon carries a small
gaussian noise (well inside the 1.5 V ramp-check tolerance of set_hvs) and imon
returns a voltage-dependent current with noise, so the monitor CSV and the flask
HV plot look realistic.

Selected by hv_control.py when hv_info['simulate'] is true or hv_info['ip'] == 'sim'.
"""

import random
import time


class FakeCAENHVController:
    RAMP_RATE = 25.0   # V/s, both up and down
    V_NOISE = 0.2      # V, gaussian sigma on vmon
    I_NOISE = 0.005    # uA, gaussian sigma on imon

    def __init__(self, ip_address, username=None, password=None):
        self.ip_address = ip_address
        # Per (slot, channel): setpoint, power state, last known voltage and
        # the wall time it was recorded (voltage is advanced lazily on read).
        self._v0 = {}
        self._power = {}
        self._vlast = {}
        self._tlast = {}
        print(f'FakeCAENHVController: simulating CAEN crate at "{ip_address}" '
              f'(ramp {self.RAMP_RATE} V/s)')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def _key(self, slot, channel):
        return int(slot), int(channel)

    def _advance(self, key):
        """Move the channel voltage toward its target for the elapsed time."""
        now = time.time()
        v = self._vlast.get(key, 0.0)
        t = self._tlast.get(key, now)
        target = self._v0.get(key, 0.0) if self._power.get(key, False) else 0.0
        dv_max = self.RAMP_RATE * (now - t)
        if v < target:
            v = min(v + dv_max, target)
        elif v > target:
            v = max(v - dv_max, target)
        self._vlast[key] = v
        self._tlast[key] = now
        return v

    # --- Interface used by hv_control.py ---

    def set_ch_v0(self, slot, channel, v0):
        key = self._key(slot, channel)
        self._advance(key)  # freeze current voltage before changing target
        self._v0[key] = float(v0)

    def set_ch_pw(self, slot, channel, power):
        key = self._key(slot, channel)
        self._advance(key)
        self._power[key] = bool(power)

    def get_ch_power(self, slot, channel):
        return self._power.get(self._key(slot, channel), False)

    def get_ch_vmon(self, slot, channel):
        v = self._advance(self._key(slot, channel))
        return v + random.gauss(0.0, self.V_NOISE) if v > 0 else max(v, 0.0)

    def get_ch_imon(self, slot, channel):
        v = self._advance(self._key(slot, channel))
        # ~0.5 nA/V leakage-like scale plus noise, in uA
        return max(v * 5e-4 + random.gauss(0.0, self.I_NOISE), 0.0)
