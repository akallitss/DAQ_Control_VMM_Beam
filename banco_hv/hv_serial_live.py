#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read-only HV monitor for the USB-only lab supplies on banco (Nov 2026).

WHY. The lab CAEN crate is dead; the P2 chambers are powered from a CAEN N1471
NIM module and an iseg NHR 42 60r, both USB (FTDI serial) only, both plugged
into banco. This process polls them and serves the SAME /hv_live JSON contract
as docs/dream_side/hv_live.py, so hv_dream_shim.py on the VMM box writes each
sub-run's hv_monitor.csv from it with no change on the VMM side:

  GET /hv_live?token=<secret>
  -> {"time": "YYYY-mm-dd HH:MM:SS",
      "channels": {"P2_IN_Drift": {"power": 0, "v0": 730.0, "vmon": 0.5,
                                   "imon": 0.0}, ...},
      "labels":   {"P2_IN_Drift": "N1471 ch0", ...}}

Only channels with a label in the config are served there (unused outputs stay
out of the run record). /hv_all serves every channel plus raw status words, and
/ is a small auto-refreshing table for a browser (through an ssh tunnel).

It also appends every reading to <log_dir>/hv_serial_YYYY-MM-DD.csv, so the HV
history exists whether or not a VMM run is polling.

STRICTLY READ-ONLY. _query() refuses anything that is not a monitor command
(CAEN: CMD:MON, iseg: a '?' query), so no code path here can set, ramp, switch
on or kill a channel. Control is a separate, later step.

Units: volts and microamps (CAEN IMON is already uA; iseg answers in A).
Voltages are reported as magnitudes with the module's polarity in /hv_all —
the NHR is negative, the N1471 reports magnitudes with POL '-'.

Run on banco (user banco must be in group dialout):
  python3 banco_hv/hv_serial_live.py --config banco_hv/hv_serial_live.json
Config: copy hv_serial_live.json.example and set a token.
Source of truth is this repo; the banco copy is deployed from it.
"""

import argparse
import csv
import html
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import serial   # pyserial

DEFAULTS = {
    'bind': '0.0.0.0',
    'port': 5010,
    'poll_s': 5,
    'stale_s': 60,       # a reading older than this is not served
    'log_dir': '~/hv_serial_logs',
    'log_every_s': 10,   # CSV history cadence (the HTTP cache still updates every poll)
}


# ---------------------------------------------------------------- devices

class SerialDevice:
    """One serial port, opened lazily, reopened after any error."""

    xonxoff = False

    def __init__(self, name, port, baud=9600):
        self.name, self.port, self.baud = name, port, baud
        self._s = None

    def _open(self):
        if self._s is None:
            self._s = serial.Serial(self.port, self.baud, bytesize=8, parity='N',
                                    stopbits=1, timeout=1.0, xonxoff=self.xonxoff)
            self._s.reset_input_buffer()

    def close(self):
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

    def _is_read_only(self, cmd):
        raise NotImplementedError

    def _query(self, cmd):
        if not self._is_read_only(cmd):
            raise PermissionError(f'{self.name}: refusing non-monitor command {cmd!r}')
        self._open()
        try:
            self._s.reset_input_buffer()
            self._s.write(cmd.encode('ascii') + b'\r\n')
            return self._reply(cmd)
        except Exception:
            self.close()
            raise

    def read(self):
        """-> {ch: {'vset','vmon','imon','power', ...}} plus module dict."""
        raise NotImplementedError


class CaenN1471(SerialDevice):
    """CAEN N1470/N1471 ASCII protocol: $BD:xx,CMD:MON,CH:4,PAR:VMON -> #BD:xx,CMD:OK,VAL:a;b;c;d"""

    xonxoff = True

    def __init__(self, name, port, baud=9600, board=0):
        super().__init__(name, port, baud)
        self.board = int(board)

    def _is_read_only(self, cmd):
        return ',CMD:MON,' in cmd and ',CMD:SET' not in cmd

    def _reply(self, cmd):
        line = self._readline()
        if ',CMD:OK' not in line:
            raise IOError(f'{self.name}: {cmd} -> {line!r}')
        return line.split('VAL:', 1)[1] if 'VAL:' in line else ''

    def _mon(self, par, ch=4):
        val = self._query(f'$BD:{self.board:02d},CMD:MON,CH:{ch},PAR:{par}')
        return val.split(';')

    def read(self):
        vset = [float(x) for x in self._mon('VSET')]
        vmon = [float(x) for x in self._mon('VMON')]
        imon = [float(x) for x in self._mon('IMON')]     # uA
        stat = [int(x) for x in self._mon('STAT')]
        pol = self._mon('POL')
        chans = {}
        for i in range(len(vmon)):
            chans[i] = {'v0': vset[i], 'vmon': vmon[i], 'imon': imon[i],
                        'power': stat[i] & 1, 'status': stat[i],
                        'polarity': pol[i] if i < len(pol) else ''}
        ctr = self._query(f'$BD:{self.board:02d},CMD:MON,PAR:BDCTR')
        return chans, {'control': ctr}


_ISEG_NUM = re.compile(r'^\s*([-+0-9.eE]+)')


class IsegNHR(SerialDevice):
    """iseg NHR/NHS SCPI. The module echoes every command line before answering."""

    def __init__(self, name, port, baud=9600, channels='0-3'):
        super().__init__(name, port, baud)
        self.chs = channels

    def _is_read_only(self, cmd):
        return cmd.rstrip().split(' ')[0].endswith('?')

    def _reply(self, cmd):
        line = self._readline()
        if line == cmd.strip():          # the echo
            line = self._readline()
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
        vset = [abs(self._num(x)) for x in self._list(':READ:VOLT?')]
        vmon = [abs(self._num(x)) for x in self._list(':MEAS:VOLT?')]
        imon = [self._num(x) * 1e6 for x in self._list(':MEAS:CURR?')]   # A -> uA
        on = [int(x) for x in self._list(':READ:VOLT:ON?')]
        stat = [int(x) for x in self._list(':READ:CHAN:STAT?')]
        pol = self._list(':CONF:OUTP:POL?')
        chans = {}
        for i in range(len(vmon)):
            chans[i] = {'v0': vset[i], 'vmon': vmon[i], 'imon': abs(imon[i]),
                        'power': on[i], 'status': stat[i],
                        'polarity': pol[i] if i < len(pol) else ''}
        temp = self._query(':READ:MOD:TEMP?')
        return chans, {'temperature': temp}


DRIVERS = {'caen_n1470': CaenN1471, 'iseg_nhr': IsegNHR}


# ---------------------------------------------------------------- poller

class Poller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.devices = []
        for d in cfg['devices']:
            kw = {k: v for k, v in d.items() if k not in ('type', 'name', 'port', 'labels')}
            self.devices.append((d, DRIVERS[d['type']](d['name'], d['port'], **kw)))
        self.lock = threading.Lock()
        self.snapshot = None          # {'time','t','channels':{key:...},'modules','errors'}
        self._last_log = 0
        self._log_path = None
        self._log_cols = None

    def poll_once(self):
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
        with self.lock:
            self.snapshot = snap
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


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>Lab HV monitor</title>
<style>body{{font-family:sans-serif;margin:16px}}table{{border-collapse:collapse}}
td,th{{border:1px solid #bbb;padding:4px 10px;text-align:right}}
.on{{background:#fdd}}.off{{color:#888}}.err{{color:#b00}}</style></head><body>
<h3>Lab HV monitor (read-only) &mdash; {time}</h3>{errors}
<table><tr><th>channel</th><th>label</th><th>power</th><th>Vset [V]</th>
<th>Vmon [V]</th><th>Imon [&micro;A]</th><th>status</th></tr>{rows}</table>
<p>{modules}</p></body></html>"""


def render_page(snap):
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
    return PAGE.format(time=snap['time'], errors=errs, rows=''.join(rows), modules=mods)


def make_handler(poller, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype='application/json'):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if token and q.get('token', [''])[0] != token:
                return self._send(403, json.dumps({'error': 'bad token'}))
            snap = poller.current()
            if u.path == '/hv_live':
                if snap is None:
                    return self._send(503, json.dumps({'error': 'no current reading'}))
                return self._send(200, json.dumps(live_payload(snap)))
            if u.path == '/hv_all':
                if snap is None:
                    return self._send(503, json.dumps({'error': 'no current reading'}))
                return self._send(200, json.dumps(snap))
            if u.path == '/':
                return self._send(200, render_page(snap), 'text/html; charset=utf-8')
            return self._send(404, json.dumps({'error': 'not found'}))
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--config', default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), 'hv_serial_live.json'))
    ap.add_argument('--once', action='store_true', help='poll once, print, exit')
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = {**DEFAULTS, **json.load(f)}
    poller = Poller(cfg)
    if a.once:
        print(json.dumps(poller.poll_once(), indent=1))
        return
    stop = threading.Event()
    t = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    t.start()
    srv = ThreadingHTTPServer((cfg['bind'], cfg['port']),
                              make_handler(poller, cfg.get('token', '')))
    print(f'[hv_serial] serving on {cfg["bind"]}:{cfg["port"]} '
          f'(poll {cfg["poll_s"]} s, log {cfg["log_dir"]})', flush=True)
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
