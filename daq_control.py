#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run orchestrator for the VMM SPS DAQ.

Adapted from Cosmic_Bench_DAQ_Control/daq_control.py (Dylan Neff): loops over
the configured sub-runs, ramping HV, monitoring HV + LV, and driving the VMM
capture server (vmm_daq_control.py) for each sub-run. Dream-specific pieces
(processor on-the-fly, Wiener LV check, fdf helpers) removed; Aim-TTi LV
monitoring added, mirroring the HV monitoring wiring.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's daq_control)
"""

import sys
from time import sleep
from contextlib import nullcontext

from Client import Client
from DAQController import DAQController

from run_config_base import RunConfigBase
from common_functions import *

RUNCONFIG_REL_PATH = "config/json_run_configs/"

# Stop-request flags dropped by bash_scripts/stop_run.sh and stop_sub_run.sh.
# Using flag files (instead of racing Ctrl-C into the tmux pane) makes stopping
# deterministic: daq_control checks them between/after sub-runs and stops the DAQ
# via stop_vmm.sh. Paths must match those scripts (repo root = this file's dir).
STOP_RUN_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.stop_run')
STOP_SUBRUN_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.stop_subrun')
# Post-sub-run pause flag (set/cleared by the flask "Pause after subrun" button).
# When present, daq_control waits at the next sub-run boundary until it's cleared
# (Resume). One-shot: clearing it lets the run continue without re-pausing.
PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pause_run')


def _remove_flag(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _sleep_unless_stop(seconds):
    """Sleep in 1 s steps, returning early if a stop-run is requested so Stop Run
    stays responsive through a configured post-sub-run pause."""
    waited = 0
    while waited < seconds and not os.path.exists(STOP_RUN_FLAG):
        sleep(1)
        waited += 1


def main():
    print("Starting DAQ Control")

    config = RunConfigBase()  # Initially just load run_config_beam.py
    if len(sys.argv) == 2:
        config_path = os.path.join(RUNCONFIG_REL_PATH, sys.argv[1]) if not os.path.isabs(sys.argv[1]) else sys.argv[1]
        print(f'Using run config file: {config_path}')
        if not os.path.isfile(config_path):
            print(f'File {config_path} does not exist, exiting')
            return
        if config_path.endswith('.json'):
            config.load_from_file(config_path)  # If a config file is given, load it
        elif config_path.endswith('.py'):
            pass
    config.start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    hv_ip, hv_port = config.hv_control_info['ip'], config.hv_control_info['port']
    vmm_daq_ip, vmm_daq_port = config.vmm_daq_info['ip'], config.vmm_daq_info['port']

    # LV monitoring is optional: only connect if the config carries lv_info.
    lv_info = getattr(config, 'lv_info', None)
    lv_monitoring = bool(lv_info and lv_info.get('lv_monitoring'))
    if lv_monitoring:
        lv_ip, lv_port = config.lv_control_info['ip'], config.lv_control_info['port']
        lv_client = Client(lv_ip, lv_port)
    else:
        lv_client = nullcontext()

    hv_client = Client(hv_ip, hv_port)
    vmm_daq_client = Client(vmm_daq_ip, vmm_daq_port)

    with hv_client as hv, \
            lv_client as lv, \
            vmm_daq_client as vmm_daq:

        hv.send('Connected to daq_control')
        hv.receive()
        hv.send_json(config.hv_info)

        create_dir_if_not_exist(config.run_out_dir)
        config.write_to_file(f'{config.run_out_dir}run_config.json')

        vmm_daq.send('Connected to daq_control')
        vmm_daq.receive()
        vmm_daq.send_json(config.vmm_daq_info)

        if lv_monitoring:
            lv.send('Connected to daq_control')
            lv.receive()
            lv.send_json(config.lv_info)

        sleep(2)  # Wait for all clients to do what they need to do (specifically, create directories)
        _remove_flag(STOP_RUN_FLAG)  # clear any stale stop requests from a previous run
        _remove_flag(STOP_SUBRUN_FLAG)
        _remove_flag(PAUSE_FLAG)     # never start a run already paused
        try:
            for sub_run in config.sub_runs:
                if os.path.exists(STOP_RUN_FLAG):
                    print('[stop] Stop-run requested — ending run before next sub-run.')
                    break
                # Post-sub-run pause: if armed, wait here before ramping the next
                # sub-run. HV stays at its current setpoint. Interruptible by Stop Run;
                # clearing the flag (Resume) continues the run (one-shot).
                if os.path.exists(PAUSE_FLAG):
                    print('[pause] Paused after sub-run — waiting for Resume...')
                    while os.path.exists(PAUSE_FLAG) and not os.path.exists(STOP_RUN_FLAG):
                        sleep(1)
                    if os.path.exists(STOP_RUN_FLAG):
                        print('[stop] Stop-run requested during pause — ending run.')
                        break
                    print('[pause] Resumed.')
                sub_run_name = sub_run['sub_run_name']
                sub_top_out_dir = f'{config.run_out_dir}{sub_run_name}/'
                complete_marker = f'{sub_top_out_dir}.subrun_complete'
                if getattr(config, 'resume', False) and os.path.exists(complete_marker):
                    print(f'[resume] Skipping already-completed sub run {sub_run_name}')
                    continue
                create_dir_if_not_exist(sub_top_out_dir)
                sub_out_dir = f'{sub_top_out_dir}{config.raw_daq_inner_dir}/'
                create_dir_if_not_exist(sub_out_dir)

                # Optional pre-sub-run LV gate: skip the sub-run if the bench
                # supplies are off/out of tolerance (lv_info['check_before_subrun']).
                if lv_monitoring and lv_info.get('check_before_subrun'):
                    lv.send('Check')
                    lv_res = lv.receive()
                    if 'LV OK' not in lv_res:
                        print(f'LV check failed ({lv_res}), skipping sub run {sub_run_name}')
                        continue

                # Emit the status line before ramping so the flask daq_control card shows the
                # current run/subrun immediately — otherwise it displays the previous run's name
                # (from the last [status] line still in the tmux buffer) throughout the HV ramp.
                print(f'[status] run={config.run_name}  subrun={sub_run_name}  run_time={sub_run.get("run_time", "?")}min')

                print(f'Ramping HVs for {sub_run_name}')
                if config.hv_info['hv_monitoring']:  # Monitor hv and write to file
                    hv.send('Begin Monitoring')
                    hv.receive()  # Starting monitoring
                    hv.send_json(sub_run)
                    hv.receive()  # Monitoring started

                if lv_monitoring:
                    lv.send('Begin Monitoring')
                    lv.receive()  # Starting monitoring
                    lv.send_json(sub_run)
                    lv.receive()  # Monitoring started

                hv.send('Start')
                hv.receive()
                hv.send_json(sub_run)
                res = hv.receive()
                if 'HV Set' in res:
                    settle_time = sub_run.get('settle_time', 0)  # Seconds; 0 for most runs
                    if settle_time and not os.path.exists(STOP_RUN_FLAG):
                        print(f'HV ramp complete, settling for {settle_time} seconds before starting DAQ')
                        sleep(settle_time)

                    print(f'Prepping DAQs for {sub_run_name}')

                    print(f'Starting run for sub run {sub_run_name}')
                    run_daq_controller(sub_run, sub_out_dir, vmm_daq)

                    if config.hv_info['hv_monitoring']:
                        hv.send('End Monitoring')
                        hv.receive()  # Stopping monitoring
                        hv.receive()  # Finished monitoring

                    if lv_monitoring:
                        lv.send('End Monitoring')
                        lv.receive()  # Stopping monitoring
                        lv.receive()  # Finished monitoring

                    # A manual stop (stop_run/stop_sub_run) cuts the sub-run short, so don't mark it
                    # complete — resume should re-run it. Otherwise mark it so a resume run skips it.
                    stop_run_req = os.path.exists(STOP_RUN_FLAG)
                    stop_subrun_req = os.path.exists(STOP_SUBRUN_FLAG)
                    if stop_subrun_req:
                        _remove_flag(STOP_SUBRUN_FLAG)
                    if stop_run_req or stop_subrun_req:
                        print(f'[stop] Sub run {sub_run_name} stopped manually — not marking complete.')
                    else:
                        with open(complete_marker, 'w') as f:
                            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n')

                    print(f'Finished with sub run {sub_run_name}, waiting 10 seconds before next run')
                    sleep(10)

                    # Optional configured post-sub-run pause (seconds, from the run
                    # config). HV stays at its current setpoint; Stop Run interrupts it.
                    post_pause_s = sub_run.get('post_pause_s', 0) or 0
                    if post_pause_s > 0 and not os.path.exists(STOP_RUN_FLAG):
                        print(f'[pause] Post-sub-run pause: waiting {post_pause_s}s after {sub_run_name}...')
                        _sleep_unless_stop(post_pause_s)
                        print('[pause] Post-sub-run pause: done')
        except KeyboardInterrupt:
            print(f'Run stoppping.')

            if config.hv_info['hv_monitoring']:
                hv.send('End Monitoring')
                hv.receive()  # Stopping monitoring
                hv.receive()  # Finished monitoring

            if lv_monitoring:
                lv.send('End Monitoring')
                lv.receive()  # Stopping monitoring
                lv.receive()  # Finished monitoring

        finally:
            _remove_flag(STOP_RUN_FLAG)
            _remove_flag(STOP_SUBRUN_FLAG)
        print('Run complete, closing down subsystems')
        if config.power_off_hv_at_end:
            hv.send('Power Off')
            hv.receive()  # Starting power off
            hv.receive()  # Finished power off
        hv.send('Finished')
        vmm_daq.send('Finished')
        if lv_monitoring:
            lv.send('Finished')
    print('donzo')


def run_daq_controller(sub_run, sub_out_dir, vmm_daq_client):
    daq_controller = DAQController(subrun=sub_run, out_dir=sub_out_dir, vmm_daq_client=vmm_daq_client)

    daq_success = False
    while not daq_success:  # Rerun if failure
        if os.path.exists(STOP_RUN_FLAG) or os.path.exists(STOP_SUBRUN_FLAG):
            print('[stop] Stop requested — not (re)starting DAQ controller.')
            break
        print('Starting DAQ Controller')
        daq_success = daq_controller.run()


if __name__ == '__main__':
    main()
