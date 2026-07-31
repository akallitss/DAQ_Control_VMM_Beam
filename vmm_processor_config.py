#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone processor configuration for the P2 VMM beam test.
Edit the constants below, then run this script to regenerate
config/processor_config.json. The flask UI's Start Processor button reads that
JSON to launch vmm_processor_watcher.py.

The processor is the decode half of the pipeline:

    raw_daq_data/<capture>.pcapng  ->  hits_store/<capture>/

qa_watcher then reads hits_store and never touches a capture. See
vmm_processor_watcher.py for the handoff contract (atomic rename).
"""

import json
import os

from run_config_beam import BASE_DATA_DIR, CAPTURE_DURATION_S

BASE_DATA = BASE_DATA_DIR
DAQ_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    # Top-level directory containing all run_N/ subdirectories
    'runs_dir': f'{BASE_DATA}runs/',

    # Subdirectory of each subrun holding the capture files
    'raw_inner_dir': 'raw_daq_data',

    # Subdirectory the decoded column stores are written into
    'store_inner_dir': 'hits_store',

    # Scripts and the venv to run them with. Absolute so a tmux login shell,
    # which resets PATH and drops the venv, still resolves them.
    'python': f'{DAQ_REPO_DIR}/.venv/bin/python',
    'worker': f'{DAQ_REPO_DIR}/vmm_qa/vmm_reduce.py',

    # dumpcap rotation interval; a capture with no higher-seq sibling and no
    # .capture_done marker finalizes after 2x this.
    'capture_duration_s': CAPTURE_DURATION_S,

    'data_format': 'SRS',   # 'SRS' (continuous) or 'TRG' (external trigger markers)
    'calibration': None,    # vmm-sdat calibration JSON path; None = no calibration

    # Fold trigger-referenced efficiency into scalars.json. ~3.3 s per file and
    # it is the most useful single number on the trend dashboard, so it is on by
    # default; the stage is wrapped so a failure never loses the decode.
    'do_efficiency': True,
    'eff_window': 1000.0,   # ns; matches vmm_pcapng_qa.py's --eff-window default

    # --- what is kept on disk -------------------------------------------------
    # The pcapng is PRIMARY DATA and is never removed by default. The option
    # exists (the DREAM processor has it) but stays off.
    'save_pcapng': True,
    # The per-hit columns are DERIVED data -- regenerable from the capture in
    # ~0.5 s. counts.npz + scalars.json are the permanent record at ~0.1 MB per
    # file; the columns are ~3.4x the pcapng. Set False when disk gets tight and
    # you no longer need per-file drill-down.
    'keep_columns': True,

    # Run filtering
    'include_runs': None,  # e.g. ['run_32'] — only process these; None = all
    'exclude_runs': None,  # e.g. ['run_0']  — skip these

    # Watcher behavior
    'poll_interval':   10,  # seconds between scans
    'stale_run_days':   1,  # runs with no new captures for this long are skipped
    'memory_kill_pct': 80,  # kill the decode if system RAM exceeds this %
    'max_attempts':     3,  # give up on a file after this many failures

    # A decode that hangs would otherwise block the pipeline forever (the DREAM
    # decoder did exactly that on certain files). These bound one invocation.
    'stall_timeout_s':  300,   # kill if the store stops growing for this long
    'hard_timeout_s':  3600,   # absolute cap on a single decode

    # CPU throttling — keep decoding from starving the DAQ.
    'cpu_nice':        19,   # nice level (also ionice idle class); null = neither
    'cpu_affinity':  None,   # cores the decode may use (taskset); null = all
    'threads':          4,   # numpy/BLAS thread cap
}

if __name__ == '__main__':
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'config', 'processor_config.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(CONFIG, f, indent=4)
    print(f'Written: {out_path}')
