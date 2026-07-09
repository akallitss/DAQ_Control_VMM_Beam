#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on November 20 15:16 2025
Created in PyCharm
Created as Cosmic_Bench_DAQ_Control/get_run_events

@author: Dylan Neff, dn277127
"""

import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_config_beam import BASE_DATA_DIR

RUN_DIR = f'{BASE_DATA_DIR}runs'


def main():
    if len(sys.argv) != 2:
        print('Usage: python get_run_events.py <run_name>')
        sys.exit(1)
    run_name = sys.argv[1]
    total_events, subrun_details = get_total_events_for_run(RUN_DIR, run_name)
    print(f'Total Dream events for run {run_name}: {total_events}')
    for subrun, n in subrun_details.items():
        print(f'  {subrun}: {n}')
    print('donzo')


def get_total_events_for_run(run_dir, run_name, raw_inner_dir='raw_daq_data'):
    """
    Return total DREAM event count for run_name across all subruns.

    Reads the per-FEU event count from the DREAM RunCtrl log files that
    dream_daq_control.py copies into each subrun's raw_daq_data/ directory at the
    end of the subrun. Each event is read out by every FEU, so the per-FEU count
    (not the FEU-summed total) is the number of physics events.
    Returns (total_events, {subrun_name: event_count}). Note: an in-progress
    subrun has no RunCtrl log yet, so it contributes 0 until it completes.
    """
    run_path = os.path.join(run_dir, run_name)
    if not os.path.isdir(run_path):
        raise FileNotFoundError(f"Run directory does not exist: {run_path}")

    total_events = 0
    subrun_event_counts = {}

    for subrun in sorted(os.listdir(run_path)):
        subrun_path = os.path.join(run_path, subrun)
        if not os.path.isdir(subrun_path):
            continue

        raw_dir = os.path.join(subrun_path, raw_inner_dir)
        if not os.path.isdir(raw_dir):
            continue

        events = _read_events_from_logs(raw_dir)
        if events is not None:
            total_events += events
            subrun_event_counts[subrun] = events

    return total_events, subrun_event_counts


def _read_events_from_logs(raw_dir):
    """
    Search *.log files in raw_dir for the DREAM RunCtrl data-taking summary, e.g.
        FeuCtrl_StopDataTaking OK after total 2216 events in 8 FEUs (277/FEU) ...
    and return the per-FEU count (277 here) as the physics event count.
    Returns the highest value found, or None if not found.
    """
    best = None
    for fname in os.listdir(raw_dir):
        if not fname.endswith('.log') or fname == 'dream_daq.log':
            continue
        try:
            with open(os.path.join(raw_dir, fname), 'r', errors='replace') as f:
                for line in f:
                    m = re.search(r'\((\d+)\s*/\s*FEU\)', line)
                    if m:
                        val = int(m.group(1))
                        if best is None or val > best:
                            best = val
        except OSError:
            continue
    return best


if __name__ == '__main__':
    main()
