#!/bin/bash

# Absolute venv flask — the tmux pane's interactive shell may have a
# different python (pyenv/conda) on PATH; see start_servers.sh.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# cd into BASE_DIR regardless of the caller's cwd: app.py Popen's subprocesses
# (e.g. run_config_beam.py to regenerate run_config_beam.json) with no cwd=
# override, so they resolve run_config_beam.py's relative
# 'config/json_run_configs/' against the FLASK PROCESS'S cwd. Launching this
# script from the wrong directory (e.g. a stray tmux respawn) silently makes
# Start Run write the fresh JSON somewhere else while the route keeps reading
# a stale one at the real (absolute) path — confirmed 2026-07-31.
cd "$BASE_DIR"
export FLASK_APP="$BASE_DIR/flask_app/app.py"

# Keep the server output in a file, not only in the tmux pane. The flask died
# four times on 2026-07-31; twice it was the kernel OOM killer, but at 17:53 it
# went with memory healthy and NOTHING was left to diagnose it with, because
# when the pane dies its scrollback dies too. A traceback that only exists in a
# dead tmux session may as well not have happened.
mkdir -p "$BASE_DIR/logs"
echo "=== flask start $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$BASE_DIR/logs/flask.log"
exec "$BASE_DIR/.venv/bin/flask" run --host=0.0.0.0 --port=5002 \
    2>&1 | tee -a "$BASE_DIR/logs/flask.log"
