#!/bin/bash
# Daytime 17 Nov (to be STARTED BY HAND in the morning, after the user's go):
#   cosmics statistics with HV on, 60 min runs alternating bscan_hvon50_v1_hvm / trimmed_Km18_v1_hvm
#   until UNTIL (default 18:00), HV guard extended to the same time (trip -> that detector off).
#     setsid nohup bash ~/trim_work/vmm_monitor_tool/fastscan/day_cosmics_20261117.sh >> ~/trim_work/out/bscan_20261116/day.log 2>&1 < /dev/null &
#   Stop: Stop Run in the GUI (config_scan then stops at the run boundary), or kill config_scan.
set -u
UNTIL=${UNTIL:-18:00}
cd ~/trim_work
T=vmm_monitor_tool
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
say() { echo "[$(date '+%F %T')] $*"; }
until ! pgrep -f "config_scan[.]py" >/dev/null; do sleep 30; done     # the night's runs end first
say "night runs over; guard until $UNTIL"
touch out/hv_lab/GUARD_STOP
until grep -q "GUARD_STOP file found" <(tail -n 5 out/hv_lab/hv_lab.log); do sleep 5; done
rm -f out/hv_lab/GUARD_STOP
setsid nohup python3 $T/hv_lab.py guard --until "$(date +%F)T$UNTIL" --on-trip detector-off --end leave \
    >> out/hv_lab/guard.stdout 2>&1 < /dev/null &
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target"
CFGS=""
for i in $(seq 1 8); do CFGS="$CFGS${P}bscan_hvon50_v1_hvm.txt,${P}trimmed_Km18_v1_hvm.txt,"; done
CFGS=${CFGS%,}
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "17 Nov day: cosmics statistics, HV at the operating point, 60 min runs alternating bscan_hvon50_v1_hvm / trimmed_Km18_v1_hvm until $UNTIL." >/dev/null 2>&1
.venv/bin/python config_scan.py --until "$UNTIL" --minutes 60 --configs "$CFGS"
say "ALLDONE day cosmics"
