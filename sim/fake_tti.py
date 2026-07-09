#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fake Aim-TTi bench power supply for local testing (SITE='local').

In-process stand-in for lv_control.TTiPSU (same pattern as sim/fake_caen.py):
implements the small SCPI surface lv_control uses — connect/close, *IDN?,
V<n>O? / I<n>O? readback — with gaussian noise on voltage and current and an
occasional slow response to exercise the caller's timeout handling.

@author: Alexandra Kallitsopoulou
"""

import random
import time

# Nominal per-channel operating points (V, A) — typical hybrid LV rails.
DEFAULT_CHANNELS = {
    1: (2.5, 3.2),
    2: (4.0, 1.1),
    3: (1.5, 0.4),
}
V_NOISE = 0.005   # V rms readback noise
I_NOISE = 0.02    # A rms readback noise
SLOW_RESPONSE_PROB = 1 / 500  # occasional slow reply (~0.5 s) to exercise timeouts


class FakeTTiPSU:
    """Drop-in fake for lv_control.TTiPSU ('sim' ip or simulate flag)."""

    def __init__(self, name, ip, port=9221, timeout=2):
        self.name = name
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.channels = dict(DEFAULT_CHANNELS)

    def connect(self):
        self.connected = True
        return f'THURLBY THANDAR, FAKE-MX100TP, 0, 1.0 ({self.name})'

    def close(self):
        self.connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _maybe_slow(self):
        if random.random() < SLOW_RESPONSE_PROB:
            time.sleep(0.5)

    def query(self, cmd):
        """Answer the SCPI queries lv_control uses; raise if 'disconnected'."""
        if not self.connected:
            raise ConnectionError(f'FakeTTiPSU {self.name} not connected')
        self._maybe_slow()
        cmd = cmd.strip()
        if cmd == '*IDN?':
            return f'THURLBY THANDAR, FAKE-MX100TP, 0, 1.0 ({self.name})'
        if cmd.startswith('V') and cmd.endswith('O?'):
            ch = int(cmd[1:-2])
            v_nom, _ = self.channels.get(ch, (0.0, 0.0))
            return f'{v_nom + random.gauss(0, V_NOISE):.4f}V'
        if cmd.startswith('I') and cmd.endswith('O?'):
            ch = int(cmd[1:-2])
            _, i_nom = self.channels.get(ch, (0.0, 0.0))
            return f'{max(0.0, i_nom + random.gauss(0, I_NOISE)):.4f}A'
        return 'ERR'

    def read_channel(self, channel):
        """(voltage V, current A) for one output channel."""
        v = float(self.query(f'V{channel}O?').rstrip('V'))
        i = float(self.query(f'I{channel}O?').rstrip('A'))
        return v, i
