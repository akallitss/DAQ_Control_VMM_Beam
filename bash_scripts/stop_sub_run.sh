#!/bin/bash
# Stop the CURRENT sub-run but let the run continue to the next sub-run.
#
# Drop a .stop_subrun flag (so daq_control does NOT mark this cut-short sub-run
# complete — resume should re-run it), then stop the DAQ. The capture stops, the
# vmm server reports the sub-run done, and daq_control advances. No Ctrl-C.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_FILE="$REPO_DIR/logs/daq_events.log"
mkdir -p "$REPO_DIR/logs"
echo "$(date '+%Y-%m-%d %H:%M:%S') | STOP_SUB_RUN   | bash_script  |" >> "$LOG_FILE"

touch "$REPO_DIR/.stop_subrun"
"$SCRIPT_DIR/stop_vmm.sh"
