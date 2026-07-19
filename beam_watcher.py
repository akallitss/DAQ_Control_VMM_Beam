#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on July 10 2026
Created in PyCharm
Created as nTof_x17_DAQ/beam_watcher.py (ported via DAQ_Control_Dream_Beam)

@author: Dylan Neff, dylan

Standalone SPS beam-intensity watcher — the SOLE owner of the NXCALS/Spark
session that pulls the SPS North Area intensity variable (see
beam_monitor/beam_intensity_controller.BEAM_VARIABLE; the same data Timber shows).

Runs ALONGSIDE the Vistar ON/OFF beam monitor (flask_app/beam_state.py); the two
publish to separate state files and don't interfere.

Runs in its own tmux session (started via the GUI "Start Beam Watcher" button).
Continuously:
  * queries NXCALS every ~30 s for the latest TOF-cycle intensities,
  * appends every point to the per-day CSV in beam_monitor/logs/,
  * publishes a beam on/off summary to config/beam_state.json (served by
    /beam/status and the Shift Overview card).

MUST run under the NXCALS venv (~/venvs/nxcals/bin/python — pytimber + PySpark
live there, not in the DAQ venv) with a valid Kerberos ticket in the default
cache (same `kinit akallits@CERN.CH` the EOS backup uses). See
beam_monitor/README.md.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from beam_monitor.beam_intensity_controller import BeamIntensityMonitor


def main():
    BeamIntensityMonitor().run_blocking()


if __name__ == "__main__":
    main()
