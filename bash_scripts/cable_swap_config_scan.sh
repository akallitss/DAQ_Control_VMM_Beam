#!/bin/bash
# Config scan of 2026-08-01 — the SAMPTEC cable comparison.
#
# P2_MID c4 and c6 were rebuilt with SAMPTEC 2 m cables in place of the HITACHI
# 2 m ones; nothing else changed. This retakes four of the configs from last
# night's HITACHI scan (runs 38-48) so each one has a same-detector, same-HV,
# same-chip-config partner on the other cable. P2_MID c5 keeps its HITACHI cable
# and is the in-station control; P2_IN and P2_OUT are the reference stations.
#
# The four are the peaktime-200 opt/deflt pair at each gain, in the order asked
# for. Combined runs, so Dream records the uRWELLs alongside; all three P2
# stations stay active throughout (nothing here touches included_detectors).
#
# --until is a WALL CLOCK time and config_scan divides the window it actually
# has when it starts, so pass the end of the two hours rather than a duration.
# It refuses to START a config it cannot finish by then.
#
# Usage:  cable_swap_config_scan.sh HH:MM      # e.g. 13:20 for a 2 h window
#         cable_swap_config_scan.sh HH:MM --dry-run
set -euo pipefail

UNTIL="${1:?usage: $0 HH:MM [--dry-run]   (HH:MM = end of the scan window)}"
shift || true

CONFIGS="p2b-config-cern-ext_gain3.0_peaktime200_opt.txt,\
p2b-config-cern-ext_gain3.0_peaktime200_deflt.txt,\
p2b-config-cern-ext_gain4.5_peaktime200_opt.txt,\
p2b-config-cern-ext_gain4.5_peaktime200_deflt.txt"

# config_scan REFUSES to start on top of a live run (it marks the config failed
# rather than waiting), so wait for any run to exit first. Waiting here also
# means the per-config livetime is computed from the time the scan really
# starts, not from now.
if pgrep -f "[/]daq_control[.]py" >/dev/null; then
  echo "[armed $(date '+%H:%M:%S')] a run is active — waiting for it to finish..."
  until ! pgrep -f "[/]daq_control[.]py" >/dev/null; do sleep 20; done
  echo "[$(date '+%H:%M:%S')] DAQ idle — settling 60s before starting the scan"
  sleep 60
fi

exec /local/p2/DAQ_Control_VMM_Beam/.venv/bin/python \
  /local/p2/DAQ_Control_VMM_Beam/config_scan.py \
  --until "$UNTIL" --configs "$CONFIGS" "$@"
