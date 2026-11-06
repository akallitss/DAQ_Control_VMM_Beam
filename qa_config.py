#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone QA watcher configuration for the P2 VMM SPS beam test.
Edit the constants below, then run this script to regenerate config/qa_config.json.
The flask UI's Start QA Watcher button reads that JSON to launch qa_watcher.py.
"""

import json
import os

from run_config_beam import BASE_DATA_DIR, CAPTURE_DURATION_S, SITE

BASE_DATA = BASE_DATA_DIR
# The pcapng QA lives in this repo (vmm_qa/vmm_pcapng_qa.py) and runs with this
# repo's venv — no external analysis repository needed at the beam.
DAQ_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    # Top-level directory containing all run_N/ subdirectories
    'runs_dir': f'{BASE_DATA}runs/',

    # Scripts and the venv to run them with. Absolute so a tmux login shell,
    # which resets PATH and drops the venv, still resolves them.
    'qa_python':    f'{DAQ_REPO_DIR}/.venv/bin/python',
    'qa_script':    f'{DAQ_REPO_DIR}/vmm_qa/vmm_pcapng_qa.py',
    'trend_script': f'{DAQ_REPO_DIR}/vmm_qa/vmm_trend.py',

    # Subdirectory of each subrun holding the DECODED STORES. qa_watcher no
    # longer reads pcapng at all -- vmm_processor_watcher decodes each capture
    # into hits_store/<capture>/ and renames it into place atomically, so a
    # store that exists is complete and the two watchers never race.
    'store_inner_dir': 'hits_store',

    # QA outputs land in <qa_out_base>/<run>/<subrun>/<capture>/ (PNGs +
    # events.json), with the trend dashboards at <run>/_trend.png and
    # <run>/<subrun>/_trend.png. The flask Online QA gallery reads this tree.
    'qa_out_base': f'{BASE_DATA}analysis/',

    # Passed through to vmm_pcapng_qa.py
    'data_format': 'SRS',   # 'SRS' (continuous) or 'TRG' (external trigger markers)
    'calibration': None,    # vmm-sdat calibration JSON path; None = no calibration

    # How often to render the full 36-PNG QA set:
    #   'subrun' - first store of each sub-run (default). Those plots are how the
    #              mapping and offset problems were found, and a sub-run boundary
    #              is exactly where conditions change (each scan point is one),
    #              but at ~43 s each they cannot run on every capture.
    #   'always' - every store. Only with headroom to spare.
    #   'never'  - trend dashboard only.
    # Any capture can still be rendered on demand:
    #   vmm_qa/vmm_pcapng_qa.py <store_dir> --out-dir <dir>
    'plot_policy': 'subrun',

    # Trend dashboard: the scalars of every capture plotted against time. This
    # is what answers "is something drifting" -- per-file PNGs cannot.
    'do_trend': True,
    'trend_scope': 'both',  # 'subrun' | 'run' | 'both'

    # Run filtering
    'include_runs': None,  # e.g. ['run_1', 'run_2'] — only process these; None = all
    # 2026-10-22 lab campaign: run_1..run_3 are the 1.3 GB-per-2-min flood files of the
    # hot-channel tests (VMM 4/9 bursting); QA on them would be killed by mem_guardian.
    'exclude_runs': ['run_1', 'run_2', 'run_3'] if SITE == 'lab' else None,

    # Watcher behavior
    'poll_interval':   10,  # seconds between scans
    'stale_run_days':   1,  # runs with no new capture files for this many days are skipped
    'memory_kill_pct': 80,  # kill the QA process if system RAM usage exceeds this % (retried next poll)
    'max_attempts':     3,  # give up on a file after this many failed/killed QA attempts (qa_reset to retry)

    # CPU throttling — keep QA from starving the DAQ.
    'cpu_nice':         19,          # nice level (also ionice idle class); null = no niceing
    'cpu_affinity':   None,          # CPU cores QA may use (taskset); null = all cores
    'qa_threads':        4,          # numpy/BLAS thread cap; null = len(cpu_affinity)
}

# p2 engine: plots from the stored histograms (p2-monitor-qa, same file names
# and events.json), cheap enough for every capture. See p2_readout_config.py.
import p2_readout_config as p2cfg  # noqa: E402

if p2cfg.P2_QA:
    CONFIG.update({
        'qa_python': p2cfg.P2_PYTHON,
        'qa_script': f'{p2cfg.P2_BIN}/p2-monitor-qa',
        'store_inner_dir': p2cfg.P2_STORE_INNER,
        'plot_policy': p2cfg.P2_QA_PLOT_POLICY,
    })

# Guards changed from the GUI Settings tab, if set there (gui_settings.py)
for _k, _key in (('memory_kill_pct', 'qa_memory_kill_pct'), ('max_attempts', 'qa_max_attempts'),
                 ('cpu_nice', 'qa_cpu_nice'), ('qa_threads', 'qa_threads')):
    CONFIG[_k] = p2cfg._gs.value(_key, CONFIG[_k])

if __name__ == '__main__':
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'qa_config.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(CONFIG, f, indent=4)
    print(f'Written: {out_path}')
