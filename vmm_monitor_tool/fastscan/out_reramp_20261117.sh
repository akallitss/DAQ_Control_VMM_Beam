#!/bin/bash
# 17 Nov 03:4x: P2_OUT was switched off at 03:06 by the guard on a 10 s UNV sag of its mesh (404 V, 4.8 uA,
# a discharge, not a supply trip; the user's rule is "trip -> detector off"). Re-ramp P2_OUT only, then
# restart the guard with the fixed logic (acts on trips at once, on other bad states only after 30 s).
set -u
cd ~/trim_work
H=out/hv_lab
T=vmm_monitor_tool
say() { echo "[$(date '+%F %T')] $*"; }
say "stopping the old guard"
touch $H/GUARD_STOP
until grep -q "GUARD_STOP file found" $H/hv_lab.log; do sleep 2; done
rm -f $H/GUARD_STOP
say "old guard ended; P2_OUT no longer given up"
echo '{"hw": [], "detectors": [], "time": "'$(date -Iseconds)'", "note": "P2_OUT re-ramped after a discharge (not a trip)"}' > $H/GIVEN_UP.json
python3 $T/hv_lab.py ramp-up --only P2_OUT --rup 5 --hold 60 --stages 0.5,0.8,0.95,1.0 --i-max 1.0
rc=$?
say "P2_OUT re-ramp rc=$rc"
tmux new-window -d -t bscan_night -n hvguard2 "cd ~/trim_work; python3 $T/hv_lab.py guard --until 2026-11-17T08:00 --on-trip detector-off --end leave 2>&1 | tee -a $H/guard.stdout"
say "new guard started (window hvguard2)"
