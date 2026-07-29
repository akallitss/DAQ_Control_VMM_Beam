#!/bin/bash

# Repo root + the venv interpreter, ABSOLUTE. tmux panes run interactive
# shells whose rc files (pyenv, conda, ...) can override PATH — a bare
# `python` may not be the venv (bit us on the Saclay bench, where .bashrc
# activates a pyenv without caen_hv_py). Never rely on PATH for python here.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE_DIR/.venv/bin/python"

# Start sessions. 3rd arg = tmux scrollback cap in LINES (memory-saving).
# hv_control / lv_control are very chatty (monitor rows every couple of
# seconds), so keep them short. The others keep a longer buffer for debugging.
# HV server: at 'sps' the CAEN crate hangs off banco's private LAN and the
# Dream DAQ owns ALL HV — run the remote-HV shim (per-subrun scan gate against
# Dream's readback) instead of the CAEN-driving hv_control.
SITE="$(cat "$BASE_DIR/config/site.txt" 2>/dev/null || echo local)"
if [ "$SITE" = "sps" ]; then
  HV_SERVER="$BASE_DIR/hv_dream_shim.py"
else
  HV_SERVER="$BASE_DIR/hv_control.py"
fi
bash_scripts/start_tmux.sh vmm_hv_control "$PY $HV_SERVER" 500
bash_scripts/start_tmux.sh vmm_lv_control "$PY $BASE_DIR/lv_control.py" 500
bash_scripts/start_tmux.sh vmm_daq "$PY $BASE_DIR/vmm_daq_control.py" 20000
bash_scripts/start_tmux.sh vmm_daq_control "echo 'Daq control session started'" 20000
bash_scripts/start_tmux.sh vmm_flask "$BASE_DIR/flask_app/start_flask.sh" 5000
# Memory guardian: on this ~8 GB box a runaway QA job can exhaust RAM and freeze
# the machine, taking the live DAQ with it. This kills the biggest QA/compute
# process before that happens — never the DAQ. Tunable via config/mem_guardian.json.
bash_scripts/start_tmux.sh vmm_mem_guardian "$PY $BASE_DIR/mem_guardian.py" 2000
