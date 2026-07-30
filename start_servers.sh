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
# Capture guard: the VMM readout has gone silent mid-run twice (2026-07-30),
# writing packet-less 272-byte pcapng files while every health indicator kept
# saying fine — and daq_control marks such sub-runs .subrun_complete anyway, so
# run_25 banked five empty scan points and lost its whole drift scan. This
# stops the run on two consecutive empty CLOSED capture files and records the
# point to restart from. It does NOT trip on a beam outage (no beam => no
# triggers => empty files are correct); it blames the VMM only when Dream, on
# the same external trigger, is still recording. Idle and near-free when no run
# is active, and it re-arms after acting.
bash_scripts/start_tmux.sh vmm_capture_guard "$PY $BASE_DIR/capture_guard.py" 2000
