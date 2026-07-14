#!/bin/bash

# Absolute venv flask — the tmux pane's interactive shell may have a
# different python (pyenv/conda) on PATH; see start_servers.sh.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export FLASK_APP="$BASE_DIR/flask_app/app.py"
exec "$BASE_DIR/.venv/bin/flask" run --host=0.0.0.0 --port=5002
