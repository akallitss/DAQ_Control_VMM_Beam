#!/bin/bash
# One-off launcher for the overnight config scan of 2026-07-31/08-01.
# run_38 (gain3.0_peaktime100) is still taking data; config_scan REFUSES to
# start on top of a live run (it marks the config failed rather than waiting),
# so wait for daq_control.py to exit first. Waiting here also means config_scan
# computes its per-config livetime from the time it actually starts, not from
# now, so the 11 configs divide the real remaining window.
echo "[armed $(date '+%H:%M:%S')] waiting for run_38 to finish..."
until ! pgrep -f "[/]daq_control[.]py" >/dev/null; do sleep 20; done
echo "[$(date '+%H:%M:%S')] DAQ idle — settling 60s before starting the scan"
sleep 60
exec /local/p2/DAQ_Control_VMM_Beam/.venv/bin/python \
  /local/p2/DAQ_Control_VMM_Beam/config_scan.py \
  --until 08:00 --configs "p2b-config-cern-ext_gain3.0_peaktime25.txt,p2b-config-cern-ext_gain3.0_peaktime50.txt,p2b-config-cern-ext_gain3.0_peaktime200.txt,p2b-config-cern-ext_gain3.0_peaktime200_deflt.txt,p2b-config-cern-ext_gain3.0_peaktime200_opt.txt,p2b-config-cern-ext_gain4.5_peaktime25.txt,p2b-config-cern-ext_gain4.5_peaktime50.txt,p2b-config-cern-ext_gain4.5_peaktime100.txt,p2b-config-cern-ext_gain4.5_peaktime200.txt,p2b-config-cern-ext_gain4.5_peaktime200_deflt.txt,p2b-config-cern-ext_gain4.5_peaktime200_opt.txt"
