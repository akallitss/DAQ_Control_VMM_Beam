#!/bin/bash
# Restart the core DAQ sessions (the GUI's "Restart all" button runs this):
# kill vmm_daq_control, vmm_daq, vmm_hv_control, vmm_lv_control and vmm_flask,
# then start_servers.sh, which recreates them (sessions still running, such as
# vmm_mem_guardian or vmm_capture_guard, are left alone by start_tmux.sh).
#
# The work runs in its own session (setsid): this script is started by the
# Flask, inside vmm_flask, and must survive that session being killed. It used
# `screen` for that, which is not installed on the DAQ box, so the sessions
# were killed and never restarted.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="$BASE_DIR/logs/restart_daq_tmux.log"
mkdir -p "$BASE_DIR/logs"

setsid bash -c "
  echo \"[\$(date '+%F %T')] restart: killing the core sessions\"
  for s in vmm_daq_control vmm_daq vmm_hv_control vmm_lv_control vmm_flask; do
    tmux has-session -t \"\$s\" 2>/dev/null && tmux kill-session -t \"\$s\"
  done
  sleep 2
  cd '$BASE_DIR' && bash ./start_servers.sh
  echo \"[\$(date '+%F %T')] restart: done\"
" </dev/null >>"$LOG" 2>&1 &
