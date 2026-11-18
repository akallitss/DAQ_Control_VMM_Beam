#!/bin/bash
# 18 Nov 08:05: resume the gain-scan cosmics after config_scan stopped at 06:02 on its 60 GB free-disk floor
# (pruner was set to 30/50 GB: below that floor; now 70/90). Runs 248-251 done (ref, 4.5 R50, 4.5 R10, 6 R50);
# the remaining 16 of the planned sequence (mix dropped: no chip healthy at 12/16), until 22:55.
#   setsid nohup bash ~/trim_work/gain_resume_20261118.sh >> ~/trim_work/out/gain_20261118/resume.log 2>&1 < /dev/null &
set -u
say() { echo "[$(date '+%F %T')] $*"; }
P=p2b-config-cern-ext_gain
S=_peaktime200_opt_cosmic_
K=${P}4.5${S}trimmed_Km18_v1_hvm.txt
a50=${P}4.5${S}bscan_hvon50_v1_hvm.txt; a10=${P}4.5${S}bscan_hvon10_v1_hvm.txt
b50=${P}6.0${S}gscan_hvon50_hvm.txt;     b10=${P}6.0${S}gscan_hvon10_hvm.txt
c50=${P}9.0${S}gscan_hvon50_hvm.txt;     c10=${P}9.0${S}gscan_hvon10_hvm.txt
SEQ=($b10 $c50 $c10   $c10 $c50 $b10 $b50 $a10 $a50   $a50 $a10 $b50 $b10 $c50 $c10   $K)
CFGS=$(IFS=,; echo "${SEQ[*]}")
say "${#SEQ[@]} runs: $CFGS"
python3 ~/trim_work/vmm_monitor_tool/hv_lab.py check-on || say "WARNING: HV not all at target"
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "18 Nov 08:05: gain scan resumed (config_scan stopped 06:02 on its 60 GB disk floor; pruner now keeps 70-90 GB free, runs <= 244 verified on EOS deleted locally). Remaining 16 runs until 22:55. Mixed-gain run dropped: no chip healthy at 12 or 16 mV/fC." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 22:55 --configs "$CFGS"
say "ALLDONE resume"
