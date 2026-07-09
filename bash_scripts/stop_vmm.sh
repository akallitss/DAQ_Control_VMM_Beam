#!/bin/bash
# Stop the VMM capture gracefully.
#
# Drop a .stop_vmm flag: vmm_daq_control.py owns the dumpcap/tcpdump PIDs and
# does the graceful stop itself (SIGINT so the in-progress pcapng is finalized,
# then alinx-sc --acq-off). Safety net: if capture processes are still alive
# after 20 s (hung server), SIGINT them directly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

touch "$REPO_DIR/.stop_vmm"

(
  sleep 20
  if [ -f "$REPO_DIR/.stop_vmm" ] && pgrep -f "dumpcap -i" > /dev/null 2>&1; then
    echo "stop_vmm.sh: server did not stop dumpcap within 20 s — sending SIGINT directly."
    pkill -INT -f "dumpcap -i"
  fi
) > /dev/null 2>&1 &

exit 0
