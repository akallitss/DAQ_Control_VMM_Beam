#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone backup watcher configuration for the P2 SPS VMM beam test
(ported from Dylan Neff's nTof_x17_DAQ implementation via DAQ_Control_Dream_Beam).
Edit the constants below, then run this script to regenerate config/backup_config.json.
The flask UI's Start Backup button reads that JSON to launch backup_watcher.py.

Runtime requirements on the DAQ machine:
  * xrdcp/xrdfs in PATH  (conda-forge xrootd; ~/bin symlinks are prepended by
    backup_watcher at import so tmux/cron sessions find them)
  * Kerberos for CERN.CH (KRB5_CONFIG=config/krb5_cern.conf, set by the watcher)
  * ~/.cern_pass.gpg     (GPG-encrypted CERN password for unattended re-kinit)
"""

import json
import os

from run_config_beam import BASE_DATA_DIR

SOURCE_DIR     = BASE_DATA_DIR
EOS_DIR        = '/eos/project/s/salsachip/Data/T2_tests/P2_SPS_VMM_Data/'
XROOTD_URL     = 'root://eosproject.cern.ch'  # serves /eos/project; verify on first transfer
CERN_PRINCIPAL = 'akallits@CERN.CH'
GPG_PASS_FILE  = os.path.expanduser('~/.cern_pass.gpg')  # create with gpg --encrypt

CONFIG = {
    # Local top-level data directory
    'source_dir': SOURCE_DIR,

    # EOS destination path (mirrored structure). Transfers use the native xrootd
    # protocol (xrdcp/xrdfs), NOT the FUSE mount: the legacy xrootdfs mount cannot
    # mkdir/rename/overwrite, so rsync-over-FUSE fails for any new directory.
    'eos_dir': EOS_DIR,

    # Native xrootd endpoint for the EOS instance holding eos_dir. Full URLs are
    # built as f"{xrootd_url}//{absolute_eos_path}" (note the double slash).
    'xrootd_url': XROOTD_URL,

    # Subdir of source_dir that gets smart per-subrun sync
    'runs_subdir': 'runs',

    # Subdirs of source_dir to never sync (simulation captures + derived analysis)
    'exclude_dirs': ['sim_pcapng', 'analysis'],

    # GPG-encrypted CERN password file (created with: gpg --encrypt -r KEY -o ~/.cern_pass.gpg)
    'gpg_pass_file': GPG_PASS_FILE,

    # Kerberos principal for kinit
    'cern_principal': CERN_PRINCIPAL,

    # Seconds between kinit renewal attempts (ticket lasts ~25h, renew well before expiry)
    'kinit_interval': 3600,

    # Run filtering for the runs_subdir (same semantics as qa_config.py)
    'include_runs': None,   # e.g. ['run_1', 'run_2'] — only sync these; None = all
    'exclude_runs': None,   # e.g. ['run_35']          — skip these

    # Watcher behavior
    'poll_interval':       30,     # seconds between runs-dir scans
    'stale_run_days':      10,     # runs with no new data for this many days are skipped
    'extra_sync_interval': 300,    # seconds between full syncs of non-runs subdirs
    'reconcile_interval':  86400,  # seconds between idle-only full-reconcile sweeps of
                                   # ALL runs (verify vs EOS, re-copy missing/changed
                                   # files incl. stale runs); once a day

    # Extra arguments passed verbatim to xrdcp (e.g. ['-S', '4'] for 4 parallel data
    # streams per file, or ['--retry', '3'] on flaky WAN links). '-f' (overwrite) and
    # '-p' (create parent dirs) are always applied by the watcher.
    'xrdcp_extra_args': [],
}

if __name__ == '__main__':
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'backup_config.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(CONFIG, f, indent=4)
    print(f'Written: {out_path}')
