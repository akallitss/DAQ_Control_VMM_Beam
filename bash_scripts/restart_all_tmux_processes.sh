#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SERVERS="$SCRIPT_DIR/../start_servers.sh"

# Restart servers in detached screen
screen -dmS restart_tmux bash -c "
  sleep 2
  $START_SERVERS
"
# Kill tmux server
tmux kill-server