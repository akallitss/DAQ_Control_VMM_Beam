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
exec "$BASE_DIR/.venv/bin/flask" run --host=0.0.0.0 --port=5002
