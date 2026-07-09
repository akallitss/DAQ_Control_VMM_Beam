#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LV control/monitoring server (port 2102).

Structural clone of hv_control.py for the Aim-TTi bench power supplies that
power the VMM hybrids: SCPI over raw TCP (port 9221), V<n>O? / I<n>O? readback.
Commands from daq_control:
  'Begin Monitoring' + subrun JSON -> thread writes {run_out_dir}/{subrun}/lv_monitor.csv
  'End Monitoring'                 -> stop the monitor thread
  'Check'                          -> voltages vs lv_info['expected'] tolerances
  'Finished'                       -> end of run

The monitor keeps running through PSU disconnects: rows get empty cells for the
unreachable unit (visible gap in the GUI plot) and reconnection is retried every
reconnect_interval seconds. LV never blocks the DAQ.

@author: Alexandra Kallitsopoulou (structure from Dylan Neff's hv_control)
"""

import os
import csv
import socket
import threading
import time

from Server import Server
from sim.fake_tti import FakeTTiPSU


class TTiPSU:
    """Aim-TTi bench PSU over raw TCP SCPI (default port 9221)."""

    def __init__(self, name, ip, port=9221, timeout=2):
        self.name = name
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        idn = self.query('*IDN?')
        print(f'[lv] {self.name} connected: {idn}')
        return idn

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def query(self, cmd):
        """Send one SCPI command and return the newline-terminated reply."""
        if self.sock is None:
            raise ConnectionError(f'{self.name} not connected')
        self.sock.sendall((cmd.strip() + '\n').encode())
        reply = b''
        while not reply.endswith(b'\n'):
            chunk = self.sock.recv(256)
            if not chunk:
                raise ConnectionError(f'{self.name} closed connection')
            reply += chunk
        return reply.decode(errors='replace').strip()

    def read_channel(self, channel):
        """(voltage V, current A) for one output channel.

        TTi units reply like '2.475V' / '3.189A' (some prefix with 'V1 ');
        strip everything that is not part of the number.
        """
        v = _parse_scpi_number(self.query(f'V{channel}O?'))
        i = _parse_scpi_number(self.query(f'I{channel}O?'))
        return v, i


def _parse_scpi_number(reply):
    """Extract the float from a TTi readback reply (e.g. '2.475V', 'V1 2.475')."""
    token = reply.strip().rstrip('VAva')
    token = token.split()[-1].rstrip('VAva') if token.split() else token
    return float(token)


def get_lv_unit(name, unit_info, simulate):
    """Real TTi PSU, or the simulator when the run config asks for it."""
    ip = unit_info['ip']
    port = unit_info.get('port', 9221)
    if simulate or ip == 'sim':
        return FakeTTiPSU(name, ip, port)
    return TTiPSU(name, ip, port)


def main():
    port = 2102
    monitor_stop_event, monitor_thread = threading.Event(), None
    while True:
        try:
            with Server(port=port) as server:
                server.receive()
                server.send('LV control connected')
                lv_info = server.receive_json()

                units = {name: get_lv_unit(name, unit_info, lv_info.get('simulate'))
                         for name, unit_info in lv_info['units'].items()}
                for name, unit in units.items():
                    try:
                        unit.connect()
                    except (OSError, ConnectionError) as e:
                        print(f'[lv] {name} DISCONNECTED at startup ({e}) — will retry during monitoring')
                print('LV Connected')

                res = server.receive()
                while 'Finished' not in res:
                    if 'Begin Monitoring' in res:
                        server.send('Starting LV monitor')
                        sub_run = server.receive_json()
                        monitor_stop_event.clear()
                        monitor_args = (lv_info, units, sub_run['sub_run_name'], monitor_stop_event)
                        monitor_thread = threading.Thread(target=monitor_lvs, args=monitor_args)
                        monitor_thread.start()
                        server.send(f'LV monitoring started for {sub_run["sub_run_name"]}')
                    elif 'End Monitoring' in res:
                        server.send('Stopping LV monitor')
                        if monitor_thread is not None:
                            monitor_stop_event.set()
                            monitor_thread.join()
                            monitor_thread = None
                        server.send('LV Monitor Stopped')
                    elif 'Check' in res:
                        server.send(check_lvs(lv_info, units))
                    else:
                        server.send('Unknown Command')
                    res = server.receive()

                if monitor_thread is not None:
                    monitor_stop_event.set()
                    monitor_thread.join()
                    monitor_thread = None
                for unit in units.values():
                    unit.close()
        except Exception as e:
            print(f'Error: {e}\nRestarting lv control server...')
            time.sleep(2)
    print('donzo')


def check_lvs(lv_info, units):
    """Compare each expected channel voltage against tolerance; 'LV OK' or 'LV FAIL ...'."""
    expected = lv_info.get('expected') or {}
    failures = []
    for unit_name, channels in expected.items():
        unit = units.get(unit_name)
        if unit is None:
            failures.append(f'{unit_name} not configured')
            continue
        for ch, exp in channels.items():
            try:
                if not unit.connected:
                    unit.connect()
                v, _ = unit.read_channel(int(ch))
            except (OSError, ConnectionError, ValueError) as e:
                failures.append(f'{unit_name} ch{ch} unreadable ({e})')
                continue
            v_exp, v_tol = exp['v'], exp.get('v_tol', 0.2)
            if not (v_exp - v_tol <= v <= v_exp + v_tol):
                failures.append(f'{unit_name} ch{ch} {v:.3f}V != {v_exp}+/-{v_tol}V')
    if failures:
        return 'LV FAIL: ' + '; '.join(failures)
    return 'LV OK'


def monitor_lvs(lv_info, units, sub_run_name, stop_event):
    """Log V/I of every LV channel to {run_out_dir}/{subrun}/lv_monitor.csv.

    Column layout mirrors hv_monitor.csv so the flask /lv_data route can reuse
    the /hv_data reader: timestamp, '<unit>_ch<n> v', '<unit>_ch<n> i', ...
    Disconnected units produce empty cells and are re-dialed every
    reconnect_interval seconds.
    """
    run_out_dir = lv_info['run_out_dir']
    monitor_interval = lv_info.get('monitor_interval', 2)  # seconds
    reconnect_interval = lv_info.get('reconnect_interval', 5)
    sub_run_out_dir = f'{run_out_dir}/{sub_run_name}'
    os.makedirs(sub_run_out_dir, exist_ok=True)
    log_file_path = f'{sub_run_out_dir}/lv_monitor.csv'

    tti_log_files = {}
    if lv_info.get('also_write_tti_logs'):
        ts = time.strftime('%Y%m%d-%H%M%S')
        for name in lv_info['units']:
            tti_log_files[name] = open(f'{sub_run_out_dir}/{name}_{ts}_mon.log', 'w')

    headers = ['timestamp']
    unit_channels = {name: info.get('channels', [1, 2, 3])
                     for name, info in lv_info['units'].items()}
    for name, channels in unit_channels.items():
        for ch in channels:
            headers.extend([f'{name}_ch{ch} v', f'{name}_ch{ch} i'])

    last_reconnect = {name: 0.0 for name in units}
    try:
        with open(log_file_path, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)

            while not stop_event.is_set():
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                row = [timestamp]
                print(f'Monitoring LV \n{time.strftime("%H:%M:%S")}')
                for name, channels in unit_channels.items():
                    unit = units[name]
                    if not unit.connected:
                        now = time.time()
                        if now - last_reconnect[name] >= reconnect_interval:
                            last_reconnect[name] = now
                            try:
                                unit.connect()
                                print(f'[lv] {name} reconnected')
                            except (OSError, ConnectionError) as e:
                                print(f'[lv] {name} DISCONNECTED — retrying ({e})')
                    readings = {}
                    if unit.connected:
                        try:
                            for ch in channels:
                                readings[ch] = unit.read_channel(ch)
                        except (OSError, ConnectionError, ValueError) as e:
                            print(f'[lv] {name} DISCONNECTED — read failed ({e})')
                            unit.close()
                            readings = {}
                    for ch in channels:
                        if ch in readings:
                            v, i = readings[ch]
                            row.extend([f'{v:.4f}', f'{i:.4f}'])
                            print(f'{name} Channel {ch}: v={v:.4f} V, i={i:.4f} A')
                            if name in tti_log_files:
                                ts_us = time.strftime('%Y-%m-%d %H:%M:%S') + '.000000'
                                tti_log_files[name].write(
                                    f'{ts_us}   TTI Channel {ch} {v:.4f} V ; {i:.4f} A\n')
                                tti_log_files[name].flush()
                        else:
                            row.extend(['', ''])
                writer.writerow(row)
                csvfile.flush()
                stop_event.wait(monitor_interval)
    finally:
        for fh in tti_log_files.values():
            fh.close()


if __name__ == '__main__':
    main()
