#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SERVERS="$SCRIPT_DIR/../start_servers.sh"

# Restart servers in detached screen
screen -dmS restart_tmux bash -c "
  sleep 2
  $START_SERVERS
"
# Kill tmux server
sessions=(
  vmm_daq_control
  vmm_daq
  vmm_hv_control
  vmm_lv_control
  vmm_flask
)

for s in "${sessions[@]}"; do
  if tmux has-session -t "$s" 2>/dev/null; then
    tmux kill-session -t "$s"
  fi
done
