#!/bin/bash

# Source the venv
source .venv/bin/activate

# Start sessions. 3rd arg = tmux scrollback cap in LINES (memory-saving).
# hv_control / lv_control are very chatty (monitor rows every couple of
# seconds), so keep them short. The others keep a longer buffer for debugging.
bash_scripts/start_tmux.sh vmm_hv_control "python hv_control.py" 500
bash_scripts/start_tmux.sh vmm_lv_control "python lv_control.py" 500
bash_scripts/start_tmux.sh vmm_daq "python vmm_daq_control.py" 20000
bash_scripts/start_tmux.sh vmm_daq_control "echo 'Daq control session started'" 20000
bash_scripts/start_tmux.sh vmm_flask "flask_app/start_flask.sh" 5000
