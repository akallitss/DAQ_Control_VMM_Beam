#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone backup watcher configuration for the P2 SPS beam test.
Edit the constants below, then run this script to regenerate config/backup_config.json.
The flask UI's Start Backup button reads that JSON to launch backup_watcher.py.
"""

import json
import os

from run_config_beam import BASE_DATA_DIR

SOURCE_DIR     = BASE_DATA_DIR
EOS_DIR        = '/eos/TODO_SPS/p2_sps_beam/'      # TODO-SPS: EOS destination
CERN_PRINCIPAL = 'TODO@CERN.CH'                    # TODO-SPS: your CERN principal
GPG_PASS_FILE  = os.path.expanduser('~/.cern_pass.gpg')  # TODO-SPS: create with gpg --encrypt

CONFIG = {
    # Local top-level data directory
    'source_dir': SOURCE_DIR,

    # EOS destination (locally FUSE-mounted, mirrored structure)
    'eos_dir': EOS_DIR,

    # Subdir of source_dir that gets smart per-subrun sync
    'runs_subdir': 'runs',

    # Subdirs of source_dir to never sync (e.g. ['sim_pcapng'])
    'exclude_dirs': [],

    # GPG-encrypted CERN password file (created with: gpg --encrypt -r KEY -o ~/.cern_pass.gpg)
    'gpg_pass_file': GPG_PASS_FILE,

    # Kerberos principal for kinit
    'cern_principal': CERN_PRINCIPAL,

    # Seconds between kinit renewal attempts (ticket lasts ~25h, renew well before expiry)
    'kinit_interval': 3600,

    # Run filtering for the runs_subdir
    'include_runs': None,   # e.g. ['run_1', 'run_2'] — only sync these; None = all
    'exclude_runs': None,   # e.g. ['run_35']          — skip these

    # Watcher behavior
    'poll_interval':       30,   # seconds between runs-dir scans
    'stale_run_days':      10,   # runs with no new data for this many days are skipped
    'extra_sync_interval': 300,  # seconds between full syncs of non-runs subdirs

    # Extra arguments passed verbatim to rsync (e.g. ['--bwlimit=50000'] to cap at 50 MB/s)
    'rsync_extra_args': [],
}

if __name__ == '__main__':
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'backup_config.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(CONFIG, f, indent=4)
    print(f'Written: {out_path}')
