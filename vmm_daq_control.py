#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMM DAQ control server (port 2101).

Replaces dream_daq_control.py from DAQ_Control_Dream_Beam: instead of driving
the Dream RunCtrl, it captures raw UDP from the VMM front-ends into rotating
.pcapng files with dumpcap (or a tcpdump loop), with optional ALINX slow
control (alinx-sc --acq-on/--acq-off) around each sub-run.

Protocol (same as the Dream server, spoken by daq_control.py/DAQController.py):
  handshake: receive -> send 'VMM DAQ control connected' -> receive vmm_daq_info JSON
  per sub-run: 'Start' + subrun JSON -> reply 'VMM DAQ starting',
               capture for run_time minutes, reply 'VMM DAQ stopped'
  'Finished' ends the run.

Stop: bash_scripts/stop_vmm.sh touches .stop_vmm in the repo root; the run loop
sees it and stops the capture gracefully (dumpcap finalizes the in-progress
file on SIGINT).

@author: Alexandra Kallitsopoulou (structure from Dylan Neff's dream_daq_control)
"""

import os
import shutil
import signal
import subprocess
import threading
import time
import logging
from datetime import datetime

from Server import Server
from common_functions import (setup_logging, teardown_logging, create_dir_if_not_exist,
                              is_capture_file)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STOP_VMM_FLAG = os.path.join(REPO_DIR, '.stop_vmm')
CAPTURE_DONE_MARKER = '.capture_done'


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        handlers=[logging.StreamHandler()]
    )
    port = 2101
    while True:
        run_log_handler = None
        subrun_log_handler = None
        try:
            with Server(port=port) as server:
                server.receive()
                server.send('VMM DAQ control connected')
                vmm_info = server.receive_json()

                create_dir_if_not_exist(vmm_info['data_out_dir'])
                run_log_handler = setup_logging(
                    os.path.join(vmm_info['data_out_dir'], 'vmm_daq.log'))
                logging.info('Run started')

                res = server.receive()
                while 'Finished' not in res:
                    if 'Start' in res:
                        subrun = server.receive_json()
                        effective_info = {**vmm_info, **subrun}

                        sub_run_name = subrun['sub_run_name']
                        run_time = float(subrun['run_time'])
                        print(f'Sub-run name: {sub_run_name}, Run time: {run_time} minutes')

                        raw_dir = (f'{effective_info["data_out_dir"]}/{sub_run_name}/'
                                   f'{effective_info["raw_daq_inner_dir"]}/')
                        create_dir_if_not_exist(raw_dir)
                        subrun_log_handler = setup_logging(os.path.join(raw_dir, 'vmm_daq.log'))
                        logging.info(f'Subrun started: {sub_run_name}  run_time={run_time}min')

                        _remove_file(STOP_VMM_FLAG)  # clear stale stop requests
                        _remove_file(os.path.join(raw_dir, CAPTURE_DONE_MARKER))

                        # ALINX slow control: acquisition ON before capture starts.
                        alinx_ok = alinx_acq_on(effective_info)
                        if not alinx_ok:
                            logging.error('ALINX acq-on failed — aborting sub-run')
                            server.send('VMM DAQ error: alinx acq-on failed')
                            teardown_logging(subrun_log_handler)
                            subrun_log_handler = None
                            res = server.receive()
                            continue

                        captures = start_captures(raw_dir, effective_info)
                        server.send('VMM DAQ starting')

                        status_stop = threading.Event()
                        status_args = (raw_dir, sub_run_name, captures, status_stop,
                                       effective_info.get('status_interval_s', 10))
                        status_thread = threading.Thread(target=status_loop, args=status_args,
                                                         daemon=True)
                        status_thread.start()

                        # Run loop: tick until requested time elapsed or stop flag dropped.
                        start_time = time.time()
                        run_time_s = run_time * 60
                        while time.time() - start_time < run_time_s:
                            if os.path.exists(STOP_VMM_FLAG):
                                print('[vmm daq] Stop flag found — stopping capture.')
                                break
                            if all(not cap.alive() for cap in captures):
                                logging.error('All capture processes died — ending sub-run early.')
                                break
                            time.sleep(1)

                        stop_captures(captures)
                        status_stop.set()
                        status_thread.join(timeout=5)

                        alinx_acq_off(effective_info)

                        # Tell the QA watcher the last file is final.
                        with open(os.path.join(raw_dir, CAPTURE_DONE_MARKER), 'w') as f:
                            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n')

                        copy_provenance(raw_dir, effective_info)
                        _remove_file(STOP_VMM_FLAG)

                        n_files, total_mb = capture_totals(raw_dir)
                        logging.info(f'Subrun finished: {sub_run_name}  '
                                     f'files={n_files} mb={total_mb:.1f}')
                        server.send('VMM DAQ stopped')
                        teardown_logging(subrun_log_handler)
                        subrun_log_handler = None
                    else:
                        server.send('Unknown Command')
                    res = server.receive()
                logging.info('Run finished normally')
                if run_log_handler is not None:
                    teardown_logging(run_log_handler)
                    run_log_handler = None
        except Exception as e:
            logging.exception(f'Unhandled error: {e}')
            if subrun_log_handler is not None:
                teardown_logging(subrun_log_handler)
                subrun_log_handler = None
            if run_log_handler is not None:
                teardown_logging(run_log_handler)
                run_log_handler = None
            # If a client is still waiting on 'VMM DAQ stopped', unblock it.
            try:
                server.send('VMM DAQ stopped')
            except Exception:
                pass
            print(f'Error: {e}\nRestarting VMM DAQ control server...')
            time.sleep(2)
    print('donzo')


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# ALINX slow control
# ---------------------------------------------------------------------------

def _alinx_interfaces(info):
    """Interfaces with ALINX slow control and a configured alinx-sc config file."""
    if info.get('simulate'):
        return []
    return [i for i in info.get('interfaces', [])
            if i.get('slow_control') == 'alinx' and i.get('alinx_config')]


def _run_alinx_sc(alinx_config, action):
    """Run alinx-sc --config-file <cfg> --<action>; return (ok, output)."""
    cmd = ['alinx-sc', '--config-file', alinx_config, f'--{action}']
    print(f'[vmm daq] Running: {" ".join(cmd)}')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logging.error(f'alinx-sc {action} failed to run: {e}')
        return False, str(e)
    output = (result.stdout or '') + (result.stderr or '')
    for line in output.strip().splitlines():
        logging.info(f'alinx-sc: {line}')
    return result.returncode == 0, output


def alinx_acq_on(info):
    """Read link status then switch acquisition ON (with retries) for every ALINX interface."""
    for iface_info in _alinx_interfaces(info):
        cfg = iface_info['alinx_config']
        ok, _ = _run_alinx_sc(cfg, 'read-link-status')
        print(f'[vmm daq] ALINX link status {"OK" if ok else "FAIL"} ({iface_info["iface"]})')
        retries = info.get('acq_on_retries', 3)
        print(f'[vmm daq] Switching acquisition ON ({iface_info["iface"]})')
        for attempt in range(1, retries + 1):
            ok, _ = _run_alinx_sc(cfg, 'acq-on')
            if ok:
                break
            logging.warning(f'alinx-sc acq-on attempt {attempt}/{retries} failed')
            time.sleep(2)
        if not ok:
            print(f'[vmm daq] ALINX ERROR acq-on failed ({iface_info["iface"]})')
            return False
    return True


def alinx_acq_off(info):
    """Switch acquisition OFF and read back link status for every ALINX interface."""
    for iface_info in _alinx_interfaces(info):
        cfg = iface_info['alinx_config']
        print(f'[vmm daq] Switching acquisition OFF ({iface_info["iface"]})')
        ok, _ = _run_alinx_sc(cfg, 'acq-off')
        if not ok:
            print(f'[vmm daq] ALINX ERROR acq-off failed ({iface_info["iface"]})')
        _run_alinx_sc(cfg, 'read-link-status')


def copy_provenance(raw_dir, info):
    """Copy the alinx-sc config(s) used into the subrun raw dir for provenance."""
    for iface_info in _alinx_interfaces(info):
        cfg = iface_info['alinx_config']
        try:
            shutil.copy(cfg, raw_dir)
        except OSError as e:
            logging.warning(f'Could not copy alinx config {cfg} to {raw_dir}: {e}')


# ---------------------------------------------------------------------------
# Capture handles: one per interface (dumpcap process, tcpdump loop, or simulator)
# ---------------------------------------------------------------------------

class DumpcapCapture:
    """One dumpcap ring-buffer capture on one interface."""

    def __init__(self, raw_dir, iface, info):
        self.iface = iface
        duration = info.get('capture_duration_s', 44)
        cmd = ['dumpcap', '-i', iface, '-q',
               '-b', f'duration:{duration}',
               '-w', os.path.join(raw_dir, f'{iface}.pcapng')]
        bpf = info.get('bpf_filter')
        if bpf:
            cmd += ['-f', bpf]
        snaplen = info.get('snaplen', 0)
        if snaplen:
            cmd += ['-s', str(snaplen)]
        self.stderr_path = os.path.join(raw_dir, f'dumpcap_{iface}.log')
        self._stderr_fh = open(self.stderr_path, 'w')
        print(f'[vmm daq] Starting capture: {" ".join(cmd)}')
        self.process = subprocess.Popen(cmd, stdout=self._stderr_fh, stderr=self._stderr_fh)

    def alive(self):
        return self.process.poll() is None

    def returncode(self):
        return self.process.poll()

    def stop(self):
        if self.alive():
            self.process.send_signal(signal.SIGINT)  # dumpcap finalizes current file
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self._stderr_fh.close()
        # dumpcap prints 'Packets captured/received/dropped' stats on exit.
        try:
            with open(self.stderr_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        logging.info(f'dumpcap[{self.iface}]: {line}')
        except OSError:
            pass


class TcpdumpLoopCapture:
    """tcpdump fallback: one file per invocation, loop_daq-style names."""

    def __init__(self, raw_dir, iface, info):
        self.iface = iface
        self.raw_dir = raw_dir
        self.duration = info.get('capture_duration_s', 44)
        self.bpf = info.get('bpf_filter', '')
        self.snaplen = info.get('snaplen', 0)
        self._stop = threading.Event()
        self._proc = None
        self._died = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        seq = 1
        while not self._stop.is_set():
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            out_path = os.path.join(self.raw_dir, f'{self.iface}_{ts}_{seq}.pcapng')
            cmd = ['tcpdump', '-i', self.iface, '-w', out_path,
                   '-G', str(self.duration), '-W', '1']
            if self.snaplen:
                cmd += ['-s', str(self.snaplen)]
            if self.bpf:
                cmd.append(self.bpf)
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            self._proc.wait()
            if self._proc.returncode not in (0, -signal.SIGINT, -signal.SIGTERM) \
                    and not self._stop.is_set():
                logging.error(f'tcpdump[{self.iface}] exited rc={self._proc.returncode}')
                self._died = True
                return
            seq += 1

    def alive(self):
        return self.thread.is_alive()

    def returncode(self):
        return 1 if self._died else None

    def stop(self):
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
        self.thread.join(timeout=15)


class SimulatedCapture:
    """Local test mode: replay a sample pcapng via sim/fake_vmm_daq.py."""

    def __init__(self, raw_dir, iface, info):
        from sim.fake_vmm_daq import run_simulated_capture
        self.iface = iface
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=run_simulated_capture,
            args=(raw_dir, iface, info, self._stop.is_set),
            daemon=True)
        self.thread.start()

    def alive(self):
        return self.thread.is_alive()

    def returncode(self):
        return None

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=30)


def start_captures(raw_dir, info):
    """Start one capture per configured interface; return the list of handles."""
    captures = []
    for iface_info in info.get('interfaces', []):
        iface = iface_info['iface']
        if info.get('simulate'):
            captures.append(SimulatedCapture(raw_dir, iface, info))
        elif info.get('capture_tool', 'dumpcap') == 'tcpdump':
            captures.append(TcpdumpLoopCapture(raw_dir, iface, info))
        else:
            captures.append(DumpcapCapture(raw_dir, iface, info))
    return captures


def stop_captures(captures):
    for cap in captures:
        try:
            cap.stop()
        except Exception as e:
            logging.error(f'Error stopping capture on {cap.iface}: {e}')


def capture_totals(raw_dir):
    """(n_files, total_mb) over capture files currently in raw_dir."""
    n_files, total_bytes = 0, 0
    try:
        for name in os.listdir(raw_dir):
            if is_capture_file(name):
                n_files += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(raw_dir, name))
                except OSError:
                    pass
    except OSError:
        pass
    return n_files, total_bytes / 1024 / 1024


def status_loop(raw_dir, sub_run_name, captures, stop_event, interval):
    """Print a scrapeable status line every interval seconds; flag dead captures.

    The flask daq_status.py parses these lines for the vmm_daq status card.
    """
    start = time.time()
    while not stop_event.is_set():
        n_files, total_mb = capture_totals(raw_dir)
        newest, newest_mtime = '-', 0
        try:
            for name in os.listdir(raw_dir):
                if is_capture_file(name):
                    mtime = os.path.getmtime(os.path.join(raw_dir, name))
                    if mtime > newest_mtime:
                        newest, newest_mtime = name, mtime
        except OSError:
            pass
        elapsed = int(time.time() - start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        print(f'[vmm daq] status subrun={sub_run_name} elapsed={h}h {m}m {s}s '
              f'files={n_files} mb={total_mb:.1f} file={newest}')
        for cap in captures:
            rc = cap.returncode()
            if rc is not None and rc != 0:
                print(f'[vmm daq] CAPTURE ERROR iface={cap.iface} rc={rc}')
        stop_event.wait(interval)


if __name__ == '__main__':
    main()
