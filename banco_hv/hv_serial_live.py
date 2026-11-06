#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HV monitor AND control for the USB-only lab supplies on banco (Nov 2026).

WHY. The lab CAEN crate is dead; the P2 chambers are powered from a CAEN N1471
NIM module and an iseg NHR 42 60r, both USB (FTDI serial) only, both plugged
into banco. A serial port has ONE owner, so this one process does everything:
it polls both modules, serves the readings, and is the only way to change a
setting from a computer. The DAQ GUI and banco's own desktop use the same page.

MONITORING (read token, or any request from banco itself):
  GET /hv_live            same JSON contract as docs/dream_side/hv_live.py, so
                          hv_dream_shim.py writes each sub-run's hv_monitor.csv
  GET /hv_history?minutes=N   recent labelled readings, {time, voltage, current,
                          power}; the VMM flask proxies it as /hv_data_live
  GET /hv_all             every channel, raw status words, current settings
  GET /api/state          what the control page draws (readings + settings + limits)
  GET /api/actions        the last control actions
  GET /                   the control page (read-only until unlocked)
  GET /table              the old plain table

CONTROL (read token or local, AND the control password in the JSON body):
  POST /api/set           {"hw": "N1471:0", "param": "v0", "value": 700, "password": ...}
  POST /api/all_off       {"password": ...}   every labelled channel OFF (ramped)
  POST /api/unlock        {"password": ...}   check only, for the page

  params: v0 [V], power [0/1], iset [uA], rup / rdw [V/s],
          CAEN: trip [s], pdwn RAMP|KILL, imrange HIGH|LOW
          iseg: trip_action 0-4, trip_time [ms]

SAFETY. Only labelled channels (config) can be changed; unlabelled outputs are
locked. Every value is checked against config 'limits' before anything is sent:
v0 <= vmax for the channel's electrode (Drift 1000 V, Mesh 500 V as set by the
user on 2026-11-02), iset <= iset_max_uA, ramps within [ramp_min, ramp_max].
Switching ON re-checks the channel's current setpoint against the same limit.
Every attempt, accepted or refused, is appended to <log_dir>/hv_actions.jsonl
with time, source, channel, old and new value. Monitoring commands go through
_query (read-only guard); set commands only through _set, which only the
validated apply() path calls. The N1471 accepts set commands only with its
front-panel switch on REMOTE; in LOCAL it answers an error, shown on the page.

Units: volts and microamps everywhere on the wire (iseg speaks amps: converted
here). Voltages are magnitudes; both modules are negative polarity.

Run on banco (user banco must be in group dialout), see banco_hv/README.md.
"""

import argparse
import csv
import glob
import hmac
import html
import json
import os
import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import serial   # pyserial

DEFAULTS = {
    'bind': '0.0.0.0',
    'port': 5010,
    'poll_s': 5,
    'settings_every_s': 30,  # ramp/trip/current-limit readback cadence
    'stale_s': 60,           # a reading older than this is not served
    'log_dir': '~/hv_serial_logs',
    'log_every_s': 10,       # CSV history cadence
    'history_h': 24,         # how far back /hv_history can serve (in memory)
    'limits': {
        'vmax_by_electrode': {'Drift': 1000, 'Mesh': 500},
        'iset_max_uA': 100,
        'ramp_min': 1, 'ramp_max': 50,
        'trip_max_s': 1000,
        'trip_time_max_ms': 4095,
    },
}


# ---------------------------------------------------------------- devices

class DeviceError(Exception):
    pass


class SerialDevice:
    """One serial port, opened lazily (exclusively), reopened after any error.

    self.lock serialises the poller and control requests on the port.
    """

    xonxoff = False

    def __init__(self, name, port, baud=9600):
        self.name, self.port, self.baud = name, port, baud
        self._s = None
        self.lock = threading.RLock()

    def _open(self):
        if self._s is None:
            self._s = serial.Serial(self.port, self.baud, bytesize=8, parity='N',
                                    stopbits=1, timeout=1.0, xonxoff=self.xonxoff,
                                    exclusive=True)
            self._s.reset_input_buffer()

    def close(self):
        with self.lock:
            if self._s is not None:
                try:
                    self._s.close()
                finally:
                    self._s = None

    def _readline(self):
        line = self._s.readline()
        if not line.endswith(b'\n'):
            raise IOError(f'{self.name}: timeout waiting for a reply')
        return line.decode('ascii', 'replace').strip()

    def _xfer(self, cmd):
        with self.lock:
            self._open()
            try:
                self._s.reset_input_buffer()
                self._s.write(cmd.encode('ascii') + b'\r\n')
                return self._reply(cmd)
            except DeviceError:
                raise                      # a clean refusal: port is fine
            except Exception:
                self.close()
                raise

    def _query(self, cmd):
        if not self._is_read_only(cmd):
            raise PermissionError(f'{self.name}: refusing non-monitor command {cmd!r}')
        return self._xfer(cmd)

    def _set(self, cmd):
        if self._is_read_only(cmd):
            raise PermissionError(f'{self.name}: {cmd!r} is not a set command')
        return self._xfer(cmd)


# CAEN N1470/N1471 channel STAT bits (CAEN manual, as encoded in the hvps library).
CAEN_STAT_BITS = [
    (0, 'ON', 'on'), (1, 'RUP', 'ramping up'), (2, 'RDW', 'ramping down'),
    (3, 'OVC', 'over current (Imon >= Iset)'), (4, 'OVV', 'over voltage'),
    (5, 'UNV', 'under voltage'), (6, 'MAXV', 'at MAXV protection'),
    (7, 'TRIP', 'tripped (Imon >= Iset for the trip time)'),
    (8, 'OVP', 'output power above max'), (9, 'OVT', 'over temperature'),
    (10, 'DIS', "disabled: the channel's front-panel switch is OFF (REMOTE mode)"),
    (11, 'KILL', 'killed from the front panel'), (12, 'ILK', 'interlock'),
    (13, 'NOCAL', 'calibration error'),
]
CAEN_BLOCKING = {7: 'TRIP', 10: 'DIS', 11: 'KILL', 12: 'ILK'}


def caen_status_text(stat):
    names = [short for bit, short, _ in CAEN_STAT_BITS if stat >> bit & 1 and bit != 0]
    return ' '.join(names) or 'ok'


def caen_blocking_reason(stat):
    return '; '.join(f'{short} = {long}' for bit, short, long in CAEN_STAT_BITS
                     if bit in CAEN_BLOCKING and stat >> bit & 1)


class CaenN1471(SerialDevice):
    """CAEN N1470/N1471 ASCII protocol.

    $BD:00,CMD:MON,CH:4,PAR:VMON          -> #BD:00,CMD:OK,VAL:a;b;c;d
    $BD:00,CMD:SET,CH:0,PAR:VSET,VAL:700  -> #BD:00,CMD:OK
    $BD:00,CMD:SET,CH:0,PAR:ON            -> #BD:00,CMD:OK
    Errors come back as e.g. #BD:00,CMD:ERR / LOC:ERR (front panel on LOCAL).
    """

    xonxoff = True
    SETTABLE = ('v0', 'power', 'iset', 'rup', 'rdw', 'trip', 'pdwn', 'imrange')
    _PAR = {'v0': ('VSET', '{:.1f}'), 'iset': ('ISET', '{:.2f}'),
            'rup': ('RUP', '{:.0f}'), 'rdw': ('RDW', '{:.0f}'),
            'trip': ('TRIP', '{:.1f}'), 'pdwn': ('PDWN', '{}'),
            'imrange': ('IMRANGE', '{}')}

    def __init__(self, name, port, baud=9600, board=0):
        super().__init__(name, port, baud)
        self.board = int(board)

    def _is_read_only(self, cmd):
        return ',CMD:MON,' in cmd and ',CMD:SET' not in cmd

    def _reply(self, cmd):
        line = self._readline()
        if ',CMD:OK' not in line:
            if 'LOC:ERR' in line:
                raise DeviceError(f'{self.name} is in LOCAL mode: switch its front '
                                  f'panel to REMOTE to control it from here ({line})')
            raise DeviceError(f'{self.name}: {cmd} -> {line!r}')
        return line.split('VAL:', 1)[1] if 'VAL:' in line else ''

    def _mon(self, par, ch=4):
        return self._query(f'$BD:{self.board:02d},CMD:MON,CH:{ch},PAR:{par}').split(';')

    def read(self):
        with self.lock:
            vset = [float(x) for x in self._mon('VSET')]
            vmon = [float(x) for x in self._mon('VMON')]
            imon = [float(x) for x in self._mon('IMON')]     # uA
            stat = [int(x) for x in self._mon('STAT')]
            pol = self._mon('POL')
            ctr = self._query(f'$BD:{self.board:02d},CMD:MON,PAR:BDCTR')
        chans = {}
        for i in range(len(vmon)):
            chans[i] = {'v0': vset[i], 'vmon': vmon[i], 'imon': imon[i],
                        'power': stat[i] & 1, 'status': stat[i],
                        'status_text': caen_status_text(stat[i]),
                        'polarity': pol[i] if i < len(pol) else ''}
        return chans, {'control': ctr}

    def read_settings(self):
        with self.lock:
            iset = [float(x) for x in self._mon('ISET')]
            rup = [float(x) for x in self._mon('RUP')]
            rdw = [float(x) for x in self._mon('RDW')]
            trip = [float(x) for x in self._mon('TRIP')]
            pdwn = self._mon('PDWN')
            imr = self._mon('IMRANGE')
        return {i: {'iset': iset[i], 'rup': rup[i], 'rdw': rdw[i], 'trip': trip[i],
                    'pdwn': pdwn[i], 'imrange': imr[i]} for i in range(len(iset))}

    def set(self, ch, param, value):
        head = f'$BD:{self.board:02d},CMD:SET,CH:{int(ch)}'
        if param == 'power':
            return self._set(f'{head},PAR:{"ON" if value else "OFF"}')
        par, fmt = self._PAR[param]
        return self._set(f'{head},PAR:{par},VAL:{fmt.format(value)}')


_ISEG_NUM = re.compile(r'^\s*([-+0-9.eE]+)')


class IsegNHR(SerialDevice):
    """iseg NHR/NHS SCPI. The module echoes every command line before answering.

    :MEAS:VOLT? (@0-3)          -> echo, then -0.000299E3V,...
    :VOLT 700.000,(@0);*OPC?    -> echo, then 1      (magnitude; the channel's
                                   polarity gives the sign, as hvps does it)
    :VOLT ON,(@0);*OPC?         -> echo, then 1
    """

    SETTABLE = ('v0', 'power', 'iset', 'rup', 'rdw', 'trip_action', 'trip_time')
    _CMD = {'v0': (':VOLT', '{:.3f}'),
            'iset': (':CURR', '{:.4E}'),            # value given in A
            'rup': (':CONF:RAMP:VOLT:UP', '{:.3f}'),
            'rdw': (':CONF:RAMP:VOLT:DOWN', '{:.3f}'),
            'trip_action': (':CONF:TRIP:ACTION', '{:d}'),
            'trip_time': (':CONF:TRIP:TIME', '{:d}')}

    def __init__(self, name, port, baud=9600, channels='0-3'):
        super().__init__(name, port, baud)
        self.chs = channels

    def _is_read_only(self, cmd):
        head = cmd.rstrip()
        if head.endswith(';*OPC?'):
            return False
        return head.split(' ')[0].endswith('?')

    def _reply(self, cmd):
        line = self._readline()
        if line == cmd.strip():          # the echo
            line = self._readline()
        if cmd.endswith(';*OPC?') and line != '1':
            raise DeviceError(f'{self.name}: {cmd} -> {line!r}')
        return line

    def _list(self, q):
        return [x.strip() for x in self._query(f'{q} (@{self.chs})').split(',')]

    @staticmethod
    def _num(s):
        m = _ISEG_NUM.match(s)
        if not m:
            raise ValueError(f'cannot parse {s!r}')
        return float(m.group(1))

    def read(self):
        with self.lock:
            vset = [abs(self._num(x)) for x in self._list(':READ:VOLT?')]
            vmon = [abs(self._num(x)) for x in self._list(':MEAS:VOLT?')]
            imon = [self._num(x) * 1e6 for x in self._list(':MEAS:CURR?')]   # A -> uA
            on = [int(x) for x in self._list(':READ:VOLT:ON?')]
            stat = [int(x) for x in self._list(':READ:CHAN:STAT?')]
            pol = self._list(':CONF:OUTP:POL?')
            temp = self._query(':READ:MOD:TEMP?')
        chans = {}
        for i in range(len(vmon)):
            chans[i] = {'v0': vset[i], 'vmon': vmon[i], 'imon': abs(imon[i]),
                        'power': on[i], 'status': stat[i],
                        'polarity': pol[i] if i < len(pol) else ''}
        return chans, {'temperature': temp, 'control': 'REMOTE'}

    def read_settings(self):
        with self.lock:
            iset = [abs(self._num(x)) * 1e6 for x in self._list(':READ:CURR?')]
            rup = [self._num(x) for x in self._list(':CONF:RAMP:VOLT:UP?')]
            rdw = [self._num(x) for x in self._list(':CONF:RAMP:VOLT:DOWN?')]
            act = [int(self._num(x)) for x in self._list(':CONF:TRIP:ACTION?')]
            tim = [int(self._num(x)) for x in self._list(':CONF:TRIP:TIME?')]
        return {i: {'iset': iset[i], 'rup': rup[i], 'rdw': rdw[i],
                    'trip_action': act[i], 'trip_time': tim[i]} for i in range(len(iset))}

    def set(self, ch, param, value):
        if param == 'power':
            return self._set(f':VOLT {"ON" if value else "OFF"},(@{int(ch)});*OPC?')
        cmd, fmt = self._CMD[param]
        if param == 'iset':
            value = value * 1e-6                   # uA -> A
        return self._set(f'{cmd} {fmt.format(value)},(@{int(ch)});*OPC?')


DRIVERS = {'caen_n1470': CaenN1471, 'iseg_nhr': IsegNHR}


# ---------------------------------------------------------------- validation

class Refused(Exception):
    pass


def _electrode(label):
    return label.rsplit('_', 1)[-1] if label else ''


def validate(param, value, label, limits, dev, ch_state):
    """-> the value to send (typed), or raise Refused with a reason a person can read."""
    if param not in dev.SETTABLE:
        raise Refused(f'{dev.name} has no setting {param!r}')
    vmax = limits['vmax_by_electrode'].get(_electrode(label))
    try:
        if param in ('pdwn', 'imrange'):
            v = str(value).upper()
            ok = ('RAMP', 'KILL') if param == 'pdwn' else ('HIGH', 'LOW')
            if v not in ok:
                raise Refused(f'{param} must be one of {ok}')
            return v
        if param == 'power':
            v = 1 if value in (1, '1', True, 'on', 'ON', 'true') else 0
            if v and vmax is None:
                raise Refused(f'{label}: no voltage limit configured for electrode '
                              f'{_electrode(label)!r}; refusing to switch on')
            if v and ch_state.get('v0', 0) > vmax:
                raise Refused(f'{label}: setpoint {ch_state.get("v0")} V is above the '
                              f'{vmax} V limit; lower it before switching on')
            return v
        if param in ('trip_action', 'trip_time'):
            v = int(float(value))
            hi = 4 if param == 'trip_action' else limits['trip_time_max_ms']
            if not 0 <= v <= hi:
                raise Refused(f'{param} must be within 0..{hi}')
            return v
        v = float(value)
    except (TypeError, ValueError):
        raise Refused(f'{param}: {value!r} is not a valid value')
    if v != v or v < 0:
        raise Refused(f'{param}: {value!r} must be a non-negative number')
    if param == 'v0':
        if vmax is None:
            raise Refused(f'{label}: no voltage limit configured for electrode '
                          f'{_electrode(label)!r}')
        if v > vmax:
            raise Refused(f'{label}: {v} V is above the {vmax} V limit for '
                          f'{_electrode(label)} electrodes')
    elif param == 'iset':
        if not 0 < v <= limits['iset_max_uA']:
            raise Refused(f'current limit must be within 0..{limits["iset_max_uA"]} uA')
    elif param in ('rup', 'rdw'):
        if not limits['ramp_min'] <= v <= limits['ramp_max']:
            raise Refused(f'ramp must be within {limits["ramp_min"]}..{limits["ramp_max"]} V/s')
    elif param == 'trip':
        if v > limits['trip_max_s']:
            raise Refused(f'trip time must be within 0..{limits["trip_max_s"]} s')
    return v


# ---------------------------------------------------------------- poller

class Poller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.devices = []
        for d in cfg['devices']:
            kw = {k: v for k, v in d.items() if k not in ('type', 'name', 'port', 'labels')}
            self.devices.append((d, DRIVERS[d['type']](d['name'], d['port'], **kw)))
        self.lock = threading.Lock()
        self.snapshot = None          # {'time','t','channels':{hw:...},'modules','errors'}
        self.settings = {}            # hw -> {iset, rup, ...}
        self._settings_t = 0
        self._last_log = 0
        self._log_path = None
        self._log_cols = None
        self.control_lock = threading.Lock()
        n = int(cfg['history_h'] * 3600 / cfg['poll_s']) + 1
        self.history = deque(maxlen=n)
        self._seed_history()

    # -- history ---------------------------------------------------------
    def _seed_history(self):
        d = os.path.expanduser(self.cfg['log_dir'])
        cutoff = time.time() - self.cfg['history_h'] * 3600
        days = sorted({datetime.fromtimestamp(t).strftime('%Y-%m-%d')
                       for t in (cutoff, time.time())})
        rows = []
        for day in days:
            for path in sorted(glob.glob(os.path.join(d, f'hv_serial_{day}*.csv'))):
                try:
                    with open(path, newline='') as f:
                        for r in csv.DictReader(f):
                            t = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S').timestamp()
                            if t < cutoff:
                                continue
                            chans = {}
                            for k, v in r.items():
                                if k.endswith(' label') and v:
                                    hw = k[:-6]
                                    chans[v] = (float(r[f'{hw} vmon']), float(r[f'{hw} imon']),
                                                int(r[f'{hw} power']))
                            if chans:
                                rows.append((t, r['timestamp'], chans))
                except Exception as e:
                    print(f'[hv_serial] history seed skipped {path}: {e}', flush=True)
        for row in sorted(rows):
            self.history.append(row)
        print(f'[hv_serial] history seeded with {len(self.history)} readings', flush=True)

    def history_payload(self, minutes):
        cutoff = time.time() - minutes * 60
        with self.lock:
            rows = [r for r in self.history if r[0] >= cutoff]
        labels = sorted({lab for r in rows for lab in r[2]})
        out = {'success': bool(rows), 'time': [r[1] for r in rows],
               'voltage': {lab: [r[2][lab][0] if lab in r[2] else None for r in rows]
                           for lab in labels},
               'current': {lab: [r[2][lab][1] if lab in r[2] else None for r in rows]
                           for lab in labels},
               'power': {lab: rows[-1][2][lab][2] for lab in labels if lab in rows[-1][2]}
               if rows else {}}
        snap = self.current()
        out['latest'] = snap['time'] if snap else None
        out['errors'] = snap['errors'] if snap else {'service': 'no current reading'}
        return out

    # -- polling ---------------------------------------------------------
    def _refresh_settings(self, only=None):
        for d, dev in self.devices:
            if only is not None and dev is not only:
                continue
            try:
                for i, s in dev.read_settings().items():
                    with self.lock:
                        self.settings[f"{d['name']}:{i}"] = s
            except Exception as e:
                print(f'[hv_serial] settings read failed on {d["name"]}: {e}', flush=True)
        self._settings_t = time.time()

    def poll_once(self):
        if time.time() - self._settings_t >= self.cfg['settings_every_s']:
            self._refresh_settings()
        chans, modules, errors = {}, {}, {}
        for d, dev in self.devices:
            try:
                cs, mod = dev.read()
                modules[d['name']] = mod
                labels = d.get('labels', {})
                for i, c in cs.items():
                    hw = f"{d['name']}:{i}"
                    chans[hw] = dict(c, hw=hw, label=labels.get(str(i), ''))
            except Exception as e:
                errors[d['name']] = f'{type(e).__name__}: {e}'
        now = time.time()
        snap = {'time': datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
                't': now, 'channels': chans, 'modules': modules, 'errors': errors}
        labelled = {c['label']: (c['vmon'], c['imon'], c['power'])
                    for c in chans.values() if c['label']}
        with self.lock:
            self.snapshot = snap
            if labelled:
                self.history.append((now, snap['time'], labelled))
        # A poll where every device failed has no columns; logging it would
        # start a new CSV with an empty header. The error is printed instead.
        if chans and now - self._last_log >= self.cfg['log_every_s']:
            self._last_log = now
            self._log(snap)
        return snap

    def _log(self, snap):
        try:
            day = snap['time'][:10]
            d = os.path.expanduser(self.cfg['log_dir'])
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f'hv_serial_{day}.csv')
            keys = sorted(snap['channels'])
            cols = ['timestamp'] + [f'{k} {f}' for k in keys
                                    for f in ('label', 'power', 'v0', 'vmon', 'imon', 'status')]
            new = not os.path.exists(path)
            if not new and (path != self._log_path or cols != self._log_cols):
                with open(path) as f:
                    header = f.readline().rstrip('\r\n').split(',')
                if header != cols:   # channel set changed: start a new file
                    path = path[:-4] + time.strftime('_%H%M%S') + '.csv'
                    new = True
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(cols)
                row = [snap['time']]
                for k in keys:
                    c = snap['channels'][k]
                    row += [c['label'], c['power'], c['v0'], c['vmon'], c['imon'], c['status']]
                w.writerow(row)
            self._log_path, self._log_cols = path, cols
        except Exception as e:
            print(f'[hv_serial] log write failed: {e}', flush=True)

    def run(self, stop):
        fails = 0
        while not stop.is_set():
            snap = self.poll_once()
            if snap['errors']:
                fails += 1
                if fails in (1, 10) or fails % 100 == 0:
                    print(f'[hv_serial] {snap["time"]} read errors ({fails} polls): '
                          f'{snap["errors"]}', flush=True)
            else:
                if fails:
                    print(f'[hv_serial] {snap["time"]} recovered', flush=True)
                fails = 0
            stop.wait(self.cfg['poll_s'])

    def current(self):
        with self.lock:
            snap = self.snapshot
        if snap is None or time.time() - snap['t'] > self.cfg['stale_s']:
            return None
        return snap

    # -- control ---------------------------------------------------------
    def _find(self, hw):
        name, _, idx = str(hw).partition(':')
        for d, dev in self.devices:
            if d['name'] == name and idx.isdigit():
                return d, dev, int(idx), d.get('labels', {}).get(idx, '')
        raise Refused(f'unknown channel {hw!r}')

    def _action_log(self, entry):
        try:
            d = os.path.expanduser(self.cfg['log_dir'])
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'hv_actions.jsonl'), 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            print(f'[hv_serial] action log write failed: {e}', flush=True)
        print(f'[hv_serial] ACTION {json.dumps(entry)}', flush=True)

    def actions(self, n=30):
        path = os.path.join(os.path.expanduser(self.cfg['log_dir']), 'hv_actions.jsonl')
        try:
            with open(path) as f:
                lines = deque(f, maxlen=n)
            return [json.loads(x) for x in reversed(lines)]
        except FileNotFoundError:
            return []

    def _state_of(self, hw):
        with self.lock:
            snap = self.snapshot or {'channels': {}}
            return dict(snap['channels'].get(hw, {}), **self.settings.get(hw, {}))

    def apply(self, hw, param, value, source):
        """Validate, send, read back. Always logs. -> (ok, message, new_state)."""
        entry = {'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'source': source,
                 'hw': hw, 'param': param, 'value': value}
        try:
            d, dev, idx, label = self._find(hw)
            entry['label'] = label
            if not label:
                raise Refused(f'{hw} is not a configured detector channel (locked)')
            with self.control_lock:
                state = self._state_of(hw)
                entry['old'] = state.get(param)
                v = validate(param, value, label, self.cfg['limits'], dev, state)
                entry['sent'] = v
                if param == 'power' and v and isinstance(dev, CaenN1471):
                    why = caen_blocking_reason(int(state.get('status', 0)))
                    if why:   # the module would answer OK and stay off
                        raise Refused(f'{label} cannot be switched on: {why}')
                dev.set(idx, param, v)
                # read back now, so the page and the log show the module's own value
                self._refresh_settings(only=dev)
                self.poll_once()
                new = self._state_of(hw)
                if param == 'power' and new.get('power') != v:
                    # an ON can take a moment to show; give it a few polls
                    for _ in range(3):
                        time.sleep(1.0)
                        self.poll_once()
                        new = self._state_of(hw)
                        if new.get('power') == v:
                            break
                entry['new'] = new.get(param)
            if param == 'power' and entry['new'] != v:
                st = new.get('status')
                why = caen_blocking_reason(int(st)) if isinstance(dev, CaenN1471) and st is not None else ''
                entry['result'] = (f'NOT APPLIED: module accepted the command but the channel is '
                                   f'still {"off" if v else "on"} (status {st}{": " + why if why else ""})')
                self._action_log(entry)
                return False, f'{label}: {entry["result"]}', entry
            entry['result'] = 'ok'
            self._action_log(entry)
            return True, f'{label}: {param} set to {entry["new"]}', entry
        except (Refused, DeviceError, PermissionError) as e:
            entry['result'] = f'refused: {e}'
            self._action_log(entry)
            return False, str(e), entry
        except Exception as e:
            entry['result'] = f'error: {type(e).__name__}: {e}'
            self._action_log(entry)
            return False, f'{type(e).__name__}: {e}', entry

    def all_off(self, source):
        results = []
        for d, dev in self.devices:
            for idx, label in sorted(d.get('labels', {}).items()):
                ok, msg, _ = self.apply(f"{d['name']}:{idx}", 'power', 0, source)
                results.append({'hw': f"{d['name']}:{idx}", 'label': label, 'ok': ok, 'message': msg})
        return results

    def state_payload(self):
        snap = self.current()
        out = {'time': snap['time'] if snap else None, 'errors': snap['errors'] if snap else
               {'service': 'no current reading'}, 'modules': snap['modules'] if snap else {},
               'limits': self.cfg['limits'], 'channels': []}
        if not snap:
            return out
        devs = {d['name']: (d, dev) for d, dev in self.devices}
        for hw in sorted(snap['channels']):
            c = dict(snap['channels'][hw])
            with self.lock:
                c.update(self.settings.get(hw, {}))
            d, dev = devs[hw.split(':')[0]]
            c['module'] = d['name']
            c['type'] = d['type']
            c['settable'] = list(dev.SETTABLE) if c['label'] else []
            c['vmax'] = self.cfg['limits']['vmax_by_electrode'].get(_electrode(c['label']))
            out['channels'].append(c)
        return out


# ---------------------------------------------------------------- http

def live_payload(snap):
    """The /hv_live contract: labelled channels only, keyed by label."""
    out, labels = {}, {}
    for hw, c in snap['channels'].items():
        if not c['label']:
            continue
        out[c['label']] = {k: c[k] for k in ('power', 'v0', 'vmon', 'imon')}
        labels[c['label']] = hw
    return {'time': snap['time'], 'channels': out, 'labels': labels,
            'errors': snap['errors']}


TABLE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>Lab HV monitor</title>
<style>body{{font-family:sans-serif;margin:16px}}table{{border-collapse:collapse}}
td,th{{border:1px solid #bbb;padding:4px 10px;text-align:right}}
.on{{background:#fdd}}.off{{color:#888}}.err{{color:#b00}}</style></head><body>
<h3>Lab HV monitor (read-only) &mdash; {time}</h3>{errors}
<table><tr><th>channel</th><th>label</th><th>power</th><th>Vset [V]</th>
<th>Vmon [V]</th><th>Imon [&micro;A]</th><th>status</th></tr>{rows}</table>
<p>{modules}</p></body></html>"""


def render_table(snap):
    if snap is None:
        return '<p class="err">No current reading (service starting or devices not answering).</p>'
    rows = []
    for hw in sorted(snap['channels']):
        c = snap['channels'][hw]
        rows.append(f'<tr class="{"on" if c["power"] else "off"}"><td>{html.escape(hw)}</td>'
                    f'<td>{html.escape(c["label"] or "(unused)")}</td>'
                    f'<td>{"ON" if c["power"] else "off"}</td><td>{c["v0"]:.1f}</td>'
                    f'<td>{c["vmon"]:.1f}</td><td>{c["imon"]:.4f}</td><td>{c["status"]}</td></tr>')
    errs = ''.join(f'<p class="err">{html.escape(k)}: {html.escape(v)}</p>'
                   for k, v in snap['errors'].items())
    mods = '; '.join(f'{html.escape(k)}: {html.escape(json.dumps(v))}'
                     for k, v in snap['modules'].items())
    return TABLE.format(time=snap['time'], errors=errs, rows=''.join(rows), modules=mods)


PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hv_control_page.html')


def make_handler(poller, cfg):
    token = cfg.get('token', '')
    password = cfg.get('control_password', '')

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype='application/json'):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b)

        def _json(self, code, obj):
            return self._send(code, json.dumps(obj))

        def _local(self):
            return self.client_address[0] in ('127.0.0.1', '::1')

        def _authorised(self, q):
            if self._local():
                return True          # banco's own desktop
            return bool(token) and hmac.compare_digest(q.get('token', [''])[0], token)

        def _source(self):
            via = self.headers.get('X-HV-Client', '')
            return f'{self.client_address[0]}' + (f' ({via})' if via else ' (banco page)'
                                                    if self._local() else '')

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authorised(q):
                return self._json(403, {'error': 'bad token'})
            snap = poller.current()
            if u.path == '/hv_live':
                if snap is None:
                    return self._json(503, {'error': 'no current reading'})
                return self._json(200, live_payload(snap))
            if u.path == '/hv_history':
                try:
                    minutes = max(1.0, min(float(q.get('minutes', ['60'])[0]),
                                           poller.cfg['history_h'] * 60))
                except ValueError:
                    minutes = 60.0
                return self._json(200, poller.history_payload(minutes))
            if u.path == '/hv_all':
                if snap is None:
                    return self._json(503, {'error': 'no current reading'})
                s = dict(snap, settings=poller.settings)
                return self._json(200, s)
            if u.path == '/api/state':
                return self._json(200, poller.state_payload())
            if u.path == '/api/actions':
                return self._json(200, poller.actions())
            if u.path == '/table':
                return self._send(200, render_table(snap), 'text/html; charset=utf-8')
            if u.path in ('/', '/control'):
                with open(PAGE_PATH, 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            return self._json(404, {'error': 'not found'})

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authorised(q):
                return self._json(403, {'ok': False, 'message': 'bad token'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(min(n, 65536)) or b'{}')
            except Exception:
                return self._json(400, {'ok': False, 'message': 'bad JSON'})
            if not password:
                return self._json(403, {'ok': False, 'message':
                                        'control disabled: no control_password in the config'})
            if not hmac.compare_digest(str(body.get('password', '')), password):
                time.sleep(1.0)
                poller._action_log({'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'source': self._source(), 'path': u.path,
                                    'result': 'refused: wrong password'})
                return self._json(403, {'ok': False, 'message': 'wrong control password'})
            if u.path == '/api/unlock':
                return self._json(200, {'ok': True})
            if u.path == '/api/set':
                ok, msg, entry = poller.apply(body.get('hw'), body.get('param'),
                                              body.get('value'), self._source())
                return self._json(200 if ok else 409, {'ok': ok, 'message': msg, 'entry': entry})
            if u.path == '/api/all_off':
                res = poller.all_off(self._source())
                return self._json(200, {'ok': all(r['ok'] for r in res), 'results': res})
            return self._json(404, {'ok': False, 'message': 'not found'})
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--config', default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), 'hv_serial_live.json'))
    ap.add_argument('--once', action='store_true', help='poll once, print, exit')
    a = ap.parse_args()
    with open(a.config) as f:
        user = json.load(f)
    cfg = {**DEFAULTS, **user}
    cfg['limits'] = {**DEFAULTS['limits'], **user.get('limits', {})}
    poller = Poller(cfg)
    if a.once:
        poller._refresh_settings()
        print(json.dumps(poller.state_payload() if poller.poll_once() else {}, indent=1))
        return
    stop = threading.Event()
    t = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    t.start()
    srv = ThreadingHTTPServer((cfg['bind'], cfg['port']), make_handler(poller, cfg))
    print(f'[hv_serial] serving on {cfg["bind"]}:{cfg["port"]} (poll {cfg["poll_s"]} s, '
          f'log {cfg["log_dir"]}, control {"ENABLED" if cfg.get("control_password") else "disabled"}, '
          f'limits {cfg["limits"]})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        for _, dev in poller.devices:
            dev.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
