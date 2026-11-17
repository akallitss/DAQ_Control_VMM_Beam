#!/bin/bash
# Second part of the night 16/17 Nov (after the first chain ends on its STOP flag, after the HV-on scan):
#   1. HV-on points X = 28..11 again with all three detectors on (P2_OUT was off from 03:06 to ~03:50,
#      i.e. for those points of the HV-on scan)
#   2. cosmics runs, 30 min each, alternating target50_p2_rt3 / trimmed_Km18_v1, none started unless it
#      can end by 07:55 (config_scan --until). HV stays on (user: leave it on at 08:00).
set -u
cd ~/trim_work
D=out/bscan_20261116
T=vmm_monitor_tool
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
say() { echo "[$(date '+%F %T')] $*"; }
until grep -q "STOP file: chain ends\|ALLDONE chain\|chain ends" $D/chain.log; do sleep 15; done
rm -f $D/STOP
say "chain 1 ended: $(tail -1 $D/chain.log)"
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target"
python3 $T/fastscan/baseline_scan.py --out-dir $D/hvon_patch --hv on --eq-file ${P}bscan_eq_x15.txt \
    --map-eq $D/pilot/map_eq.json --points 28,25,23,21,19,17,15,14,13,12,11 --repeat "" --map-at "" \
    --no-refs --no-end-map --restore ${P}target50_p2_rt3.txt > $D/hvon_patch.stdout 2>&1
say "HV-on patch: $(grep ALLDONE $D/hvon_patch/scan.log | tail -1)"
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target before the cosmics runs"
CFGS=$(for i in 1 2 3 4 5 6; do printf "%starget50_p2_rt3.txt,%strimmed_Km18_v1.txt," $P $P; done)
CFGS=${CFGS%,}
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "Night 17 Nov: cosmics runs, HV at the operating point (IN 420/720, MID 430/730, OUT 430/730), 30 min each, alternating target50_p2_rt3 / trimmed_Km18_v1, after the HV-off and HV-on threshold-above-baseline scans (session d0ca01)." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 07:55 --minutes 30 --configs "$CFGS" > ~/trim_work/$D/cosmics.stdout 2>&1
say "cosmics runs done: $(grep 'CONFIG SCAN END' /local/p2/config_scan.log | tail -1)"
say "ALLDONE chain2"
