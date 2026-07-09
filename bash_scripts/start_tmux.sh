#!/bin/bash

name=$1
cmd=$2
# Scrollback cap for this session, in LINES. tmux's only history knob is a
# per-pane line count (no byte-size or time-based limit), so "cap by size"
# means capping lines. Each retained line costs memory, so keep chatty
# sessions short to avoid the OOM killer on this RAM-limited machine.
hist=${3:-5000}

# Check if tmux session already exists
if tmux has-session -t "$name" 2>/dev/null; then
    echo "❌ Tmux session '$name' already exists!"
    return 1
fi

# A pane captures history-limit at the moment it is created, so set the
# server-wide default right before creating this session's first pane. We also
# set it on the session itself so any later windows inherit the same cap.
tmux start-server 2>/dev/null
tmux set-option -g history-limit "$hist" 2>/dev/null

if [ -z "$cmd" ]; then
    # Start an empty interactive tmux session
    tmux new-session -d -s "$name"
    tmux set-option -t "$name" history-limit "$hist" 2>/dev/null
    echo "✅ Started empty tmux session: $name (scrollback ${hist} lines)"
else
    # Start tmux session and run command
    tmux new-session -d -s "$name"
    tmux set-option -t "$name" history-limit "$hist" 2>/dev/null
    tmux send-keys -t "$name" "$cmd" Enter
    echo "✅ Started $name running: $cmd (scrollback ${hist} lines)"
fi