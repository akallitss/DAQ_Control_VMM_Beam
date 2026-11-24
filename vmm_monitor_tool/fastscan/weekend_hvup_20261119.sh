#!/bin/bash
# 19 Nov: HV ramp to the operating point for the weekend runs (user's go 19 Nov ~00:45), after the gain-3 HV-off
# map; then guard until Mon 23 Nov 07:30: trip -> re-ramp once, next trip -> that detector off; ramp down at the end.
#   setsid nohup bash ~/trim_work/weekend_hvup_20261119.sh >> ~/trim_work/out/weekend_20261119/hvup.log 2>&1 < /dev/null &
cd ~/trim_work
T=vmm_monitor_tool; H=out/hv_lab
say() { echo "[$(date '+%F %T')] $*"; }
until grep -q GMAP3DONE gmap3_20261119.log 2>/dev/null; do sleep 10; done
say "gain-3 map done: $(tail -2 gmap3_20261119.log | head -1)"
echo "user 19 Nov ~00:45: weekend runs; gas OK until Mon 07:00; yes, ramp up tonight (IN 420/720, MID 430/730, OUT 430/730) after the gain-3 HV-off map, guard until Mon 07:30; trip -> retry once, then that detector off; ramp down at the end" > $H/HV_GO
echo '{"hw": [], "detectors": [], "time": "'$(date -Iseconds)'", "note": "reset for the weekend runs 19-23 Nov"}' > $H/GIVEN_UP.json
say "ramp-up"
python3 $T/hv_lab.py ramp-up --rup 5 --hold 120 --i-max 1.0 || say "ramp-up failed"
python3 $T/hv_lab.py state
rm -f $H/GUARD_STOP
say "guard until 2026-11-23T07:30, retry once then detector off, end down"
python3 $T/hv_lab.py guard --until 2026-11-23T07:30 --on-trip retry-detector-off --retries 1 --end down >> $H/guard.stdout 2>&1
say "guard ended"
