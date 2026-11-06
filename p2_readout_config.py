#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Where the P2 readout packages live and how the DAQ uses them.

The packages (p2-decode, p2-mapping, p2-monitor; github.com/akallitss) are
installed in their own venv, NOT in this repo's .venv, so installing or
updating them can never change the numpy/pandas the DAQ runs on:

    /local/p2/p2-readout/venv      the packages and their dependencies
    /local/p2/p2-readout/wheels    the wheelhouse they were installed from
    /local/p2/p2-readout/src       the tagged sources (for their tests)

The box's network to GitHub/PyPI is unreliable, so updates go through the
wheelhouse: build wheels on a machine with network, copy them in, then
    venv/bin/pip install --no-index --no-deps --find-links wheels p2-monitor==X

Two users here:
  * the processor (vmm_processor_config.py): ENGINE 'p2' runs
    p2-monitor-reduce as the watcher's worker instead of vmm_qa/vmm_reduce.py,
    under the same guards (memory kill, stall/hard timeouts, nice/ionice,
    thread cap, oom_score_adj, mem_guardian);
  * the GUI's Live tab (flask_app/app.py): one `p2-monitor live` process in
    tmux session vmm_live, following the newest sub-run.

@author: Alexandra Kallitsopoulou
"""

import os

import gui_settings as _gs
from run_config_beam import BASE_DATA_DIR, SITE

P2_READOUT_DIR = '/local/p2/p2-readout'
P2_PYTHON = f'{P2_READOUT_DIR}/venv/bin/python'
P2_BIN = f'{P2_READOUT_DIR}/venv/bin'

# The p2-mapping setup -- the same file run_config_beam.py takes the stand
# from (setup_source.py): this machine's copy in /local/p2/p2-setups if there
# is one, else the copy shipped with p2-mapping. Passed as --setup to the
# processor, QA and live monitor.
from run_config_beam import SETUP_REF as P2_SETUP  # noqa: E402

# --- processor -------------------------------------------------------------
# 'legacy' = vmm_qa/vmm_reduce.py (this repo's venv); 'p2' = p2-monitor-reduce.
# 'p2' since 2026-10-28, after validation on the lab DAQ box: identical
# counts.npz and scalars to the legacy reduce on run_124 and run_123 captures,
# 9x faster on the 255 MB flood file (24 s vs 223 s). Set 'legacy' to go back
# (legacy imports vmm_decode from /local/p2/p2soft/vmm-analysis via a .pth).
PROCESSOR_ENGINE = 'p2'
# changed from the GUI Settings tab, if set there (gui_settings.py)
PROCESSOR_ENGINE = _gs.value('processor_engine', PROCESSOR_ENGINE)
# Where the p2 engine writes its stores. 'hits_store' replaces the legacy
# output; 'hits_store_p2' lets both run side by side for a comparison.
P2_STORE_INNER = 'hits_store'
# Keep the per-hit columns? They cost ~3.4x the capture on disk and triple the
# reduce time on this box (1.1 GB capture: 36 s without, 114 s with). With the
# p2 engine the Online QA plots come from the histograms (P2_QA below), so
# nothing online reads the columns; they can be regenerated from the pcapng.
# (With the old QA script they were needed: dropping them made every QA job
# fail on 2026-10-28.)
P2_KEEP_COLUMNS = False
P2_KEEP_COLUMNS = _gs.value('p2_keep_columns', P2_KEEP_COLUMNS)

# --- Online QA ---------------------------------------------------------------
# With the p2 engine, qa_watcher runs p2-monitor-qa (plots from counts_chip.npz,
# same file names and events.json as vmm_qa/vmm_pcapng_qa.py) instead of the
# per-hit script. It costs about the same for any capture size (~8 s, ~260 MB
# for a 21 MB capture), so every capture gets its plots, not only the first of
# each sub-run.
P2_QA = PROCESSOR_ENGINE == 'p2'
P2_QA_PLOT_POLICY = 'always'
P2_QA_PLOT_POLICY = _gs.value('qa_plot_policy', P2_QA_PLOT_POLICY)

# --- live monitor (GUI Live tab) -------------------------------------------
LIVE_TMUX = 'vmm_live'
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_STATUS_PATH = f'{REPO_DIR}/config/live_status.json'
LIVE_PNG_DIR = f'{REPO_DIR}/live_png'
LIVE_RUNS_DIR = f'{BASE_DATA_DIR}runs'
# None of these assume a data rate: they bound the monitor's own work and
# memory per poll, so a flood makes it sample, not fall behind or grow.
LIVE_OPTIONS = {
    'window': 60,           # s of data the rates are averaged over
    'interval': 5,          # s between polls
    'max_read_mb': 16,      # read per poll at most
    'max_behind_mb': 64,    # skip to the newest data beyond this backlog
    'rss_limit_mb': 600,    # shed the window above this own memory
    'oom_score_adj': 500,   # first to go under memory pressure, as other restartable jobs
    'plot_every': 60,       # s between PNG redraws
    'nice': 19,
}

# Where the live monitor reads from: 'file' follows the capture dumpcap writes;
# 'udp' reads the FEC's stream from the data NIC itself (dumpcap still gets
# every packet: the kernel delivers each datagram to both). The GUI's Live tab
# can pick either at start; this is the default.
LIVE_SOURCE = 'file'
LIVE_SOURCE = _gs.value('live_source', LIVE_SOURCE)
# The FEC's destination (data NIC address and port), per site. Measured on the
# lab box on 2026-10-28: 192.168.1.13:6006 -> 192.168.1.16:6006, nothing else
# bound to 6006.
LIVE_UDP_ADDRESS = {
    'lab': '192.168.1.16:6006',
}.get(SITE)
LIVE_UDP_ADDRESS = _gs.value('live_udp_address', LIVE_UDP_ADDRESS)
# A socket buffer holds only ~4 MB, so the network reader polls more often with
# a smaller read: about the same CPU budget as the file reader (~3-4 MB/s
# decoded at most), but moderate rates are read completely. What it cannot
# read, the kernel drops and counts; rates are scaled by the fraction read.
LIVE_UDP_OPTIONS = {**LIVE_OPTIONS, 'interval': 1, 'max_read_mb': 4}

# changed from the GUI Settings tab, if set there (gui_settings.py)
for _k, _key in (('window', 'live_window_s'), ('interval', 'live_interval_s'),
                 ('max_read_mb', 'live_max_read_mb'), ('max_behind_mb', 'live_max_behind_mb'),
                 ('rss_limit_mb', 'live_rss_limit_mb'), ('plot_every', 'live_plot_every_s')):
    LIVE_OPTIONS[_k] = _gs.value(_key, LIVE_OPTIONS[_k])
for _k, _key in (('interval', 'live_udp_interval_s'), ('max_read_mb', 'live_udp_max_read_mb')):
    LIVE_UDP_OPTIONS[_k] = _gs.value(_key, LIVE_UDP_OPTIONS[_k])
for _k in ('window', 'max_behind_mb', 'rss_limit_mb', 'plot_every'):
    LIVE_UDP_OPTIONS[_k] = LIVE_OPTIONS[_k]


def p2_available():
    """(ok, reason): is the p2 venv there with p2-monitor installed?"""
    if not os.path.isfile(P2_PYTHON):
        return False, f'no p2 venv at {P2_PYTHON}'
    if not os.path.isfile(f'{P2_BIN}/p2-monitor'):
        return False, f'p2-monitor not installed in {P2_BIN}'
    if not P2_SETUP:
        return False, f'no p2-mapping setup for site {SITE!r} (add it to P2_SETUPS)'
    return True, ''


def live_sources():
    """The live sources usable on this site."""
    return ['file'] + (['udp'] if LIVE_UDP_ADDRESS else [])


def live_command(source=None):
    """argv of the live monitor, run under nice in its tmux session."""
    source = source or LIVE_SOURCE
    if source not in live_sources():
        raise ValueError(f'live source {source!r} not available on site {SITE!r} '
                         f'(have {live_sources()})')
    o = LIVE_UDP_OPTIONS if source == 'udp' else LIVE_OPTIONS
    where = (['--udp', LIVE_UDP_ADDRESS] if source == 'udp'
             else [LIVE_RUNS_DIR, '--follow-newest',
                   '--max-behind-mb', str(o['max_behind_mb'])])
    return (['nice', '-n', str(o['nice']), P2_PYTHON, '-m', 'p2_monitor.cli', 'live']
            + where +
            ['--setup', P2_SETUP,
             '--out', LIVE_STATUS_PATH, '--plot-dir', LIVE_PNG_DIR,
             '--plot-every', str(o['plot_every']), '--window', str(o['window']),
             '--interval', str(o['interval']), '--max-read-mb', str(o['max_read_mb']),
             '--rss-limit-mb', str(o['rss_limit_mb']),
             '--oom-score-adj', str(o['oom_score_adj'])])
