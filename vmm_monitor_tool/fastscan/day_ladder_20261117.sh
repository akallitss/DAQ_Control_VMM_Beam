#!/bin/bash
# 17 Nov day (user's go 07:55): cosmics threshold scan with HV on, every channel at the threshold where ITS
# HV-on noise rate is R (from the night's baseline_scan curves), R = 2, 5, 10, 20, 50, 100 Hz, plus
# trimmed_Km18_v1_hvm as the reference. Two passes (R up, then down), runs as long as fit by 19:55;
# HV guard until 20:00, then the HV is RAMPED DOWN (user).
#   setsid nohup bash ~/trim_work/vmm_monitor_tool/fastscan/day_ladder_20261117.sh >> ~/trim_work/out/bscan_20261116/day_ladder.log 2>&1 < /dev/null &
set -u
cd ~/trim_work
D=out/bscan_20261116
T=vmm_monitor_tool
H=out/hv_lab
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
RATES="2 5 10 20 50 100"
say() { echo "[$(date '+%F %T')] $*"; }

# guard until 20:00, then ramp down (replaces the night guard)
if pgrep -f "hv_lab.py guard" >/dev/null; then
    touch $H/GUARD_STOP
    until ! pgrep -f "hv_lab.py guard" >/dev/null; do sleep 3; done
fi
rm -f $H/GUARD_STOP
setsid nohup python3 $T/hv_lab.py guard --until 2026-11-17T20:00 --on-trip detector-off --end down \
    >> $H/guard.stdout 2>&1 < /dev/null &
say "guard until 20:00, ramp-down at the end"
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target"

LIST=""
for R in $RATES; do
    python3 $T/fastscan/bscan_target.py --scan $D/hvon --scan2 $D/hvon_patch --chips2 14-19 --index2 p04-p15 \
        --eq-file ${P}bscan_eq_x15.txt --map-eq $D/pilot/map_eq.json --rate $R --margin 0.5 --mask-extra $BURSTS \
        --out-ext ${P}bscan_hvon${R}_v1.txt --report $D/hvon${R}_v1_report.json | tail -1
    LIST="$LIST ${P}bscan_hvon${R}_v1.txt"
done
python3 $T/fastscan/cosmics_prep.py --out-dir $D/ladder_prep --mask $BURSTS $LIST ${P}trimmed_Km18_v1.txt \
    | tee $D/ladder_prep.log
OK=$(grep '^CONFIGS ' $D/ladder_prep.log | tail -1 | cut -d' ' -f2)
say "configs under the rate limit: $OK"
[ -n "$OK" ] || { say "nothing under the rate limit: no runs"; exit 1; }
IFS=, read -ra C <<< "$OK"
REF=""; LAD=()
for c in "${C[@]}"; do case $c in *Km18*) REF=$c ;; *) LAD+=("$c") ;; esac; done
# pass 1: ref, R up; pass 2: R down, ref  (LAD is in R order)
SEQ=()
[ -n "$REF" ] && SEQ+=("$REF")
SEQ+=("${LAD[@]}")
for ((i=${#LAD[@]}-1; i>=0; i--)); do SEQ+=("${LAD[$i]}"); done
[ -n "$REF" ] && SEQ+=("$REF")
CFGS=$(IFS=,; echo "${SEQ[*]}")
N=${#SEQ[@]}
NOW=$(date +%s); END=$(date -d "2026-11-17 19:55" +%s)
MIN=$(( (END - NOW) / 60 / N - 3 ))      # ~3 min overhead per run (warm reset, apply, HV gate)
say "$N runs of $MIN min: $CFGS"
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "17 Nov day: cosmics threshold scan, HV at the operating point; every channel at its own HV-on noise rate R (bscan_hvonR_v1_hvm, R = $RATES Hz, from the night's threshold-above-baseline scan) + trimmed_Km18_v1_hvm reference; two passes, $MIN min each, until 19:55; HV ramped down at 20:00." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 19:58 --minutes $MIN --configs "$CFGS"
say "ALLDONE day ladder"
