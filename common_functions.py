#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Common helpers for the VMM SPS DAQ control system.

Adapted from Cosmic_Bench_DAQ_Control/common_functions.py (Dylan Neff): kept the
generic logging/dir/file-stability helpers, replaced the Dream fdf name parsers
with a pcapng capture-file name parser shared by the DAQ server, the QA watcher,
the simulator and get_run_events.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's helpers)
"""

import os
import re
import logging
from datetime import datetime
import time


def setup_logging(log_path):
    """Attach a FileHandler for log_path to the root logger. Returns the handler."""
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    logging.getLogger().addHandler(handler)
    return handler


def teardown_logging(handler):
    """Remove and close a logging FileHandler."""
    logging.getLogger().removeHandler(handler)
    handler.close()


def create_dir_if_not_exist(dir_path):
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path)
        os.chmod(dir_path, 0o777)


# Capture-file naming. Two producers, one parser:
#   dumpcap ring buffer:  <iface>_<seq:05d>_<YYYYMMDDHHMMSS>.pcapng   (e.g. enp4s0f1_00001_20260709120000.pcapng)
#   loop_daq / tcpdump:   <iface>_<YYYYMMDD-HHMMSS>_<seq>.pcapng      (e.g. enp4s0f1_20251115-205857_1.pcapng)
_DUMPCAP_RE = re.compile(r'^(?P<iface>.+)_(?P<seq>\d{5})_(?P<ts>\d{14})\.pcapn?g$')
_LOOPDAQ_RE = re.compile(r'^(?P<iface>.+)_(?P<ts>\d{8}-\d{6})_(?P<seq>\d+)\.pcapn?g$')


def parse_pcapng_name(file_name):
    """Parse a capture file name into (iface, seq, timestamp) or None if not a capture file.

    Accepts both dumpcap ring-buffer names and loop_daq/tcpdump-style names (see
    regexes above). seq is returned as int; timestamp as a datetime.
    """
    base = os.path.basename(file_name)
    m = _DUMPCAP_RE.match(base)
    if m:
        ts = datetime.strptime(m.group('ts'), '%Y%m%d%H%M%S')
        return m.group('iface'), int(m.group('seq')), ts
    m = _LOOPDAQ_RE.match(base)
    if m:
        ts = datetime.strptime(m.group('ts'), '%Y%m%d-%H%M%S')
        return m.group('iface'), int(m.group('seq')), ts
    return None


def is_capture_file(file_name):
    """True if file_name looks like a pcapng capture file from either naming scheme."""
    return parse_pcapng_name(file_name) is not None


def wait_for_copy_complete(filepath, check_interval=1.0, stable_time=3.0, wait_for_creation=False):
    """
    Wait until file size stops changing for 'stable_time' seconds.

    Args:
        filepath (str): Path to file to check.
        check_interval (float): Seconds between size checks.
        stable_time (float): Time file size must remain constant to be considered complete.
        wait_for_creation (bool):
            - If True, keep waiting until the file appears.
            - If False, return False immediately if file does not exist.

    Returns:
        bool: True if file appears and stabilizes, False otherwise.
    """
    last_size = -1
    stable_start = None

    while True:
        if not os.path.exists(filepath):
            if not wait_for_creation:
                return False
            stable_start = None
            time.sleep(check_interval)
            continue

        current_size = os.path.getsize(filepath)

        if current_size == last_size:
            if stable_start is None:
                stable_start = time.time()
            elif time.time() - stable_start >= stable_time:
                return True  # File size stable long enough
        else:
            stable_start = None
            last_size = current_size

        time.sleep(check_interval)
