#!/bin/bash
# 17/18 Nov: HV ramp to the operating point (user's go 23:40, gas same), then guard until 18 Nov 23:15, ramp down at the end
cd ~/trim_work
T=vmm_monitor_tool; H=out/hv_lab
say() { echo "[$(date '+%F %T')] $*"; }
echo "user 17 Nov 23:40: gas same as last night; HV at the operating point for the 24 h gain scan; trip -> that detector off; ramp down at the end (~23:00 18 Nov)" > $H/HV_GO
echo '{"hw": [], "detectors": [], "time": "'$(date -Iseconds)'", "note": "reset for the 18 Nov gain scan"}' > $H/GIVEN_UP.json
say "ramp-up"
python3 $T/hv_lab.py ramp-up --rup 5 --hold 120 --i-max 1.0 || say "ramp-up failed"
python3 $T/hv_lab.py state
rm -f $H/GUARD_STOP
say "guard until 2026-11-18T23:15, end down"
python3 $T/hv_lab.py guard --until 2026-11-18T23:15 --on-trip detector-off --end down >> $H/guard.stdout 2>&1
say "guard ended"
