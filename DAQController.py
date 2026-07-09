#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-subrun DAQ driver: sends 'Start' to the VMM DAQ server and blocks until it
reports the capture stopped. Adapted from Cosmic_Bench_DAQ_Control/DAQController.py
(Dylan Neff) — same protocol, Dream RunCtrl replaced by the VMM capture server.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's DAQController)
"""

import os
import subprocess
from time import time


class DAQController:
    def __init__(self, subrun=None, out_dir=None, vmm_daq_client=None):
        self.subrun = subrun or {}
        self.out_directory = out_dir
        self.out_name = self.subrun.get('sub_run_name')
        self.run_time = self.subrun.get('run_time', 10)  # minutes
        self.vmm_daq_client = vmm_daq_client
        self.original_working_directory = os.getcwd()

        self.run_start_time = None
        self.measured_run_time = None

        self.stop_vmm_sh_path = './bash_scripts/stop_vmm.sh'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.chdir(self.original_working_directory)

    def run(self):
        run_successful = True

        try:
            self.vmm_daq_client.send('Start')
            self.vmm_daq_client.send_json(self.subrun)
            res = self.vmm_daq_client.receive()
            if res == '':
                raise ConnectionError('VMM DAQ server closed connection unexpectedly')
            if res != 'VMM DAQ starting':
                print(f'Error starting VMM DAQ: received "{res}"')
                return False
            self.run_start_time = time()

            res = self.vmm_daq_client.receive()  # Wait for vmm daq to finish
            if res != 'VMM DAQ stopped':
                print('Error stopping DAQ')
                return False

            self.measured_run_time = time() - self.run_start_time

        except KeyboardInterrupt:
            print('Keyboard interrupt. Stopping DAQ process.')
            # Drop the .stop_vmm flag so the server stops the capture gracefully
            ret = subprocess.call([self.stop_vmm_sh_path])
            if ret != 0:
                print('Error stopping VMM DAQ via stop_vmm.sh script.')
            run_successful = False

            if self.run_start_time is not None:
                self.measured_run_time = time() - self.run_start_time
                run_successful = True  # Low bar for a successful run, but maybe ok?
            else:
                self.measured_run_time = 0
            res = self.vmm_daq_client.receive()
            if res != 'VMM DAQ stopped':
                print('Error stopping VMM DAQ')
        finally:
            print('VMM Subrun complete.')
            if self.measured_run_time is None:
                if self.run_start_time is None:
                    self.measured_run_time = 0
                else:
                    self.measured_run_time = time() - self.run_start_time

            if run_successful:
                self.write_run_time()

        return run_successful

    def write_run_time(self):
        with open(f'{self.out_directory}/run_time.txt', 'w') as file:
            out_str = ''
            if self.measured_run_time is not None:
                out_str += f'Run Time: {self.measured_run_time:.2f} seconds'
            if self.run_start_time is not None:
                out_str += f'\nRun Start Time: {self.run_start_time}'
            if out_str != '':
                file.write(out_str)
            else:
                file.write('None')
