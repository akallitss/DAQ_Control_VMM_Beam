#!/bin/bash
# Stop the current run at a wall-clock time, cleanly, and confirm the crate is off.
#
# For a PLANNED stop at a beam stop -- not a fault. stop_run.sh drops
# .stop_run, which daq_control checks before writing the .subrun_complete
# marker, so the sub-run in progress is deliberately left UNMARKED and a later
# resume=True run re-takes it in full. That matters for a scan: half a point is
# not a comparable point, and silently keeping it would put a short, low-
# statistics entry in the middle of the curve.
#
# The crate powers off because dream_power_off_hv_at_end is true whenever
# config/hv_hold is absent (run_config_beam.py). This script does NOT create
# that file, so the normal end-of-run power-off applies. It verifies afterwards
# rather than assuming.
#
# Usage: stop_run_at.sh HH:MM [reason]     e.g. stop_run_at.sh 16:00 "beam stop"
set -uo pipefail

TARGET="${1:?usage: $0 HH:MM [reason]}"
REASON="${2:-scheduled stop}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="$REPO_DIR/logs/scheduled_stop.log"
DREAM_FLASK='http://128.141.21.144:5001'

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$LOG"; }

now=$(date +%s)
when=$(date -d "today $TARGET" +%s 2>/dev/null) || { echo "bad time: $TARGET"; exit 2; }
[ "$when" -le "$now" ] && when=$(date -d "tomorrow $TARGET" +%s)
wait_s=$(( when - now ))

log "armed: will stop the run at $TARGET (in $((wait_s/60)) min) — $REASON"

# Wait for the target time and NOTHING else. An earlier version exited as soon
# as daq_control disappeared, on the theory that the run had ended -- but
# capture_guard's auto-recovery makes daq_control vanish and come back under a
# new pid, and on 2026-08-01 14:55 that killed this timer 65 min early. The
# 16:00 beam-stop power-off would silently never have happened.
#
# Whatever is running AT the target is what gets stopped, including a recovery
# run started after this timer was armed. That is the intent: the beam stops at
# a wall-clock time regardless of which pid is holding the run.
while [ "$(date +%s)" -lt "$when" ]; do
  sleep 20
done

if ! pgrep -f "[/]daq_control[.]py" >/dev/null; then
  log "no run active at $TARGET — nothing to stop (crate should already be off)"
  exit 0
fi

log "$TARGET reached — stopping the run (in-progress sub-run left unmarked for resume)"
"$SCRIPT_DIR/stop_run.sh"

# daq_control tears down HV/LV monitoring and Dream powers the crate off; that
# teardown was measured at ~7 min on 2026-07-30, so allow well beyond it.
for _ in $(seq 1 60); do
  pgrep -f "[/]daq_control[.]py" >/dev/null || break
  sleep 15
done

if pgrep -f "[/]daq_control[.]py" >/dev/null; then
  log "WARNING: daq_control still alive 15 min after stop — NEEDS A HUMAN"
else
  log "run stopped cleanly"
fi

hv=$(curl -s --max-time 10 "$DREAM_FLASK/status" 2>/dev/null \
     | grep -o '"name": *"hv_control"[^}]*"status": *"[^"]*"' | tail -1)
log "Dream hv_control after stop: ${hv:-<unreadable — check the crate by hand>}"
log "scheduled stop done"
