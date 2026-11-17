#!/bin/bash
# Third part of the night 16/17 Nov (replaces the cosmics part of chain 2: target50_p2_rt3 is ~1 GB/min
# with HV on, above the rate guard's 100 MB/min):
#   1. wait for the HV-on re-take of X 28..11 (hvon_patch)
#   2. bscan_target.py: every channel where its HV-on rate is 50 Hz (+0.5 mV), from hvon (+ hvon_patch for
#      P2_OUT = chips 14-19), 8 known burst channels masked -> ..._bscan_hvon50_v1.txt
#   3. cosmics_prep.py: _hvm variants of it and of trimmed_Km18_v1 (same burst masks), 60 s each with HV on,
#      extra masks on channels > 5 kHz until <= 70 MB/min
#   4. cosmics runs, 30 min each, alternating, none started unless it can end by 07:55; HV left on.
set -u
cd ~/trim_work
D=out/bscan_20261116
T=vmm_monitor_tool
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
say() { echo "[$(date '+%F %T')] $*"; }
until grep -q "ALLDONE" $D/hvon_patch/scan.log 2>/dev/null; do sleep 15; done
say "HV-on patch: $(grep ALLDONE $D/hvon_patch/scan.log | tail -1)"
python3 $T/fastscan/bscan_target.py --scan $D/hvon --scan2 $D/hvon_patch --chips2 14-19 --index2 p04-p15 \
    --eq-file ${P}bscan_eq_x15.txt --map-eq $D/pilot/map_eq.json --rate 50 --margin 0.5 --mask-extra $BURSTS \
    --out-ext ${P}bscan_hvon50_v1.txt --report $D/hvon50_v1_report.json
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target"
python3 $T/fastscan/cosmics_prep.py --out-dir $D/cosmics_prep --mask $BURSTS \
    ${P}bscan_hvon50_v1.txt ${P}trimmed_Km18_v1.txt | tee $D/cosmics_prep.log
OK=$(grep '^CONFIGS ' $D/cosmics_prep.log | tail -1 | cut -d' ' -f2)
say "configs for the cosmics runs: $OK"
[ -n "$OK" ] || { say "no config under the rate limit: no cosmics runs"; exit 1; }
IFS=, read -ra C <<< "$OK"
CFGS=""
for i in 1 2 3 4 5 6; do for c in "${C[@]}"; do CFGS="$CFGS$c,"; done; done
CFGS=${CFGS%,}
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "Night 17 Nov: cosmics runs, HV at the operating point (IN 420/720, MID 430/730, OUT 430/730), 30 min each, alternating $OK (bscan_hvon50_v1 = every channel at its own HV-on 50 Hz point from the threshold-above-baseline scan; 8 burst channels masked; session d0ca01)." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 07:55 --minutes 30 --configs "$CFGS" > ~/trim_work/$D/cosmics.stdout 2>&1
say "cosmics runs done: $(grep 'CONFIG SCAN END' /local/p2/config_scan.log | tail -1)"
say "ALLDONE chain3"
