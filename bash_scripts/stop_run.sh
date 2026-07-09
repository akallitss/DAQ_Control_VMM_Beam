#!/bin/bash
# Stop the WHOLE run cleanly.
#
# Drop a .stop_run flag, then stop the DAQ. The capture stops and the current
# sub-run ends; daq_control sees the flag, skips the rest of the sub-runs, and
# powers off HV via its normal shutdown — no orphaned dumpcap and no Ctrl-C
# races. The cut-short sub-run is left unmarked so resume re-runs it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_FILE="$REPO_DIR/logs/daq_events.log"
mkdir -p "$REPO_DIR/logs"
echo "$(date '+%Y-%m-%d %H:%M:%S') | STOP_RUN       | bash_script  |" >> "$LOG_FILE"

touch "$REPO_DIR/.stop_run"
"$SCRIPT_DIR/stop_vmm.sh"
