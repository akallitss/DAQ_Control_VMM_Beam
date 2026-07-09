#!/bin/bash
SESSION="vmm_daq_control"
CONFIG_PATH="$1"

if [ -z "$CONFIG_PATH" ]; then
  echo "Usage: $0 <config_path>"
  exit 1
fi

# Check if run output directory exists, iterate run name if so
#python iterate_run_num.py "$CONFIG_PATH"  # Not working, skip for now!

COMMAND="python daq_control.py \"$CONFIG_PATH\""

# Send command to the tmux session
tmux send-keys -t "$SESSION" "$COMMAND" C-m
