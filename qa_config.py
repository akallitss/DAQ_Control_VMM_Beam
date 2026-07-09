#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone QA watcher configuration for the P2 VMM SPS beam test.
Edit the constants below, then run this script to regenerate config/qa_config.json.
The flask UI's Start QA Watcher button reads that JSON to launch qa_watcher.py.
"""

import json
import os

from run_config_beam import BASE_DATA_DIR, CAPTURE_DURATION_S

BASE_DATA = BASE_DATA_DIR
# The pcapng QA lives in this repo (vmm_qa/vmm_pcapng_qa.py) and runs with this
# repo's venv — no external analysis repository needed at the beam.
DAQ_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    # Top-level directory containing all run_N/ subdirectories
    'runs_dir': f'{BASE_DATA}runs/',

    # Repository containing the QA script and the venv to run it with.
    # qa_script_rel_path / qa_python_rel_path are relative to analysis_dir.
    'analysis_dir': DAQ_REPO_DIR,
    'qa_script_rel_path': 'vmm_qa/vmm_pcapng_qa.py',
    'qa_python_rel_path': '.venv/bin/python',

    # Subdirectory of each subrun holding the capture files
    'raw_inner_dir': 'raw_daq_data',

    # QA outputs land in <qa_out_base>/<run>/<subrun>/<pcap_basename>/
    # (PNGs + events.json — the flask Online QA gallery and the events
    # counter both read this tree).
    'qa_out_base': f'{BASE_DATA}analysis/',

    # dumpcap rotation interval; files with no higher-seq sibling and no
    # .capture_done marker finalize after 2x this (see qa_watcher docstring).
    'capture_duration_s': CAPTURE_DURATION_S,

    # Passed through to vmm_pcapng_qa.py
    'data_format': 'SRS',   # 'SRS' (continuous) or 'TRG' (external trigger markers)
    'calibration': None,    # vmm-sdat calibration JSON path; None = no calibration
    'max_packets': None,    # optional packet cap per file; None = read whole file

    # Run filtering
    'include_runs': None,  # e.g. ['run_1', 'run_2'] — only process these; None = all
    'exclude_runs': None,  # e.g. ['run_0']          — skip these

    # Watcher behavior
    'poll_interval':   10,  # seconds between scans
    'stale_run_days':   1,  # runs with no new capture files for this many days are skipped
    'memory_kill_pct': 80,  # kill the QA process if system RAM usage exceeds this % (retried next poll)

    # CPU throttling — keep QA from starving the DAQ.
    'cpu_nice':         19,          # nice level (also ionice idle class); null = no niceing
    'cpu_affinity':   None,          # CPU cores QA may use (taskset); null = all cores
    'qa_threads':        4,          # numpy/BLAS thread cap; null = len(cpu_affinity)
}

if __name__ == '__main__':
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'qa_config.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(CONFIG, f, indent=4)
    print(f'Written: {out_path}')
