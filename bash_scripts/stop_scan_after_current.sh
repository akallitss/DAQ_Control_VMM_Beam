#!/bin/bash
# Stop a config_scan sequence AFTER the run in progress, without touching it.
#
# Killing config_scan mid-run leaves that run unmanaged: the BEAM_QUALITY.json
# holding its Dream trigger count -- THE metric for "did this config get beam" --
# is written by the sequencer only after the run exits, so an early kill throws
# it away. So wait for that file to appear, then interrupt.
#
# SIGINT rather than SIGTERM, deliberately: config_scan's finally block removes
# config/no_auto_recovery and config/hv_hold, and only an exception runs it.
# SIGTERM would leave the capture guard's auto-recovery disabled and an HV hold
# in force that nobody set.
#
# Usage: stop_scan_after_current.sh <run_name>   e.g. stop_scan_after_current.sh run_52
set -uo pipefail

RUN="${1:?usage: $0 <run_name>}"
RUNS_DIR=/local/p2/p2data/TB_July26_H4/runs
FLAG="$RUNS_DIR/$RUN/BEAM_QUALITY.json"
LOG=/local/p2/config_scan.log

PID=$(pgrep -f "[/]config_scan[.]py" | head -1)
if [ -z "$PID" ]; then
  echo "no config_scan running — nothing to stop"
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') | stop-after-current armed: waiting for $RUN stats (config_scan pid $PID)" | tee -a "$LOG"

while [ ! -f "$FLAG" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | config_scan exited on its own before $RUN stats appeared" | tee -a "$LOG"
    exit 0
  fi
  sleep 1
done

kill -INT "$PID"
echo "$(date '+%Y-%m-%d %H:%M:%S') | $RUN stats written — SIGINT sent to config_scan $PID; remaining configs NOT started" | tee -a "$LOG"

# Confirm the cleanup its finally block is responsible for.
sleep 5
for f in config/no_auto_recovery config/hv_hold; do
  p="/local/p2/DAQ_Control_VMM_Beam/$f"
  [ -e "$p" ] && echo "  WARNING: $f still present — remove it by hand" | tee -a "$LOG"
done
echo "$(date '+%Y-%m-%d %H:%M:%S') | stop-after-current done" | tee -a "$LOG"
