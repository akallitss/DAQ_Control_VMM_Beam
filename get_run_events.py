#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sum VMM hit counts for a run from the per-pcapng events.json summaries that
vmm_qa/vmm_pcapng_qa.py writes under the analysis tree:

    <analysis_dir>/<run>/<subrun>/<pcap_base>/events.json

The GUI's event counter shows hits (there is no trigger-built 'event' in the
self-triggered SRS stream); n_hits is summed across all analyzed capture files.
Counts trail the DAQ by up to one capture rotation + QA time, since a pcapng is
only analyzed once it is finalized.

Adapted from Dylan Neff's get_run_events (Dream RunCtrl log scraping).

@author: Alexandra Kallitsopoulou (based on Dylan Neff's original)
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_config_beam import BASE_DATA_DIR

ANALYSIS_DIR = f'{BASE_DATA_DIR}analysis'


def main():
    if len(sys.argv) != 2:
        print('Usage: python get_run_events.py <run_name>')
        sys.exit(1)
    run_name = sys.argv[1]
    total_hits, subrun_details = get_total_events_for_run(ANALYSIS_DIR, run_name)
    print(f'Total VMM hits for run {run_name}: {total_hits:,}')
    for subrun, n in subrun_details.items():
        print(f'  {subrun}: {n:,}')
    print('donzo')


def get_total_events_for_run(run_dir=None, run_name=None):
    """
    Return total analyzed VMM hits for run_name across all subruns.

    run_dir is the ANALYSIS tree (default: BASE_DATA_DIR/analysis), not the raw
    runs tree — hit counts come from the events.json files the QA writes per
    capture file. Keeps the Dream-era name/signature so flask_app imports work
    unchanged. Returns (total_hits, {subrun_name: hit_count}).
    A run with no QA output yet returns (0, {}).
    """
    if run_dir is None:
        run_dir = ANALYSIS_DIR
    run_path = os.path.join(run_dir, run_name)
    if not os.path.isdir(run_path):
        return 0, {}

    total_hits = 0
    subrun_hit_counts = {}

    for subrun in sorted(os.listdir(run_path)):
        subrun_path = os.path.join(run_path, subrun)
        if not os.path.isdir(subrun_path):
            continue

        hits = _sum_events_jsons(subrun_path)
        total_hits += hits
        subrun_hit_counts[subrun] = hits

    return total_hits, subrun_hit_counts


def _sum_events_jsons(subrun_analysis_dir):
    """Sum n_hits over <pcap_base>/events.json below one subrun's analysis dir."""
    total = 0
    for entry in os.listdir(subrun_analysis_dir):
        events_path = os.path.join(subrun_analysis_dir, entry, 'events.json')
        if not os.path.isfile(events_path):
            continue
        try:
            with open(events_path) as f:
                summary = json.load(f)
            total += int(summary.get('n_hits', 0))
        except (OSError, ValueError):
            continue
    return total


if __name__ == '__main__':
    main()
