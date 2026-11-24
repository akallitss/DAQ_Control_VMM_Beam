#!/bin/bash
# 18 Nov 2026 (user's go 17 Nov ~23:40): VMM gain scan with HV on, peaking time 200 ns.
#  1. per gain g (6, 9, 12, 16 mV/fC): HV-on threshold-above-baseline scan (baseline_scan.py, equalised at
#     X0_g from the HV-off monitor map of that gain), fixed 40 s per point;
#  2. bscan_target.py: every channel where ITS HV-on rate is R = 10 and 50 Hz, at each gain;
#     4.5 mV/fC: the files of 17 Nov (bscan_hvon10_v1, bscan_hvon50_v1, from the 17 Nov HV-on scan);
#  3. cosmics_prep.py: 60 s rate check of every file (<= 70 MB/min, hot channels masked);
#  4. config_scan.py: cosmics runs until 22:55, two passes (gain up, gain down), Km18 reference first and last.
# HV guard (separate, hvup_20261118.sh) runs until 23:15 and then ramps down.
#   setsid nohup bash ~/trim_work/gain_chain_20261118.sh >> ~/trim_work/out/gain_20261118/chain.log 2>&1 < /dev/null &
# Stop between steps: touch ~/trim_work/out/gain_20261118/STOP
set -u
cd ~/trim_work
T=vmm_monitor_tool
O=out/gain_20261118
P45=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
say() { echo "[$(date '+%F %T')] $*"; }
stop() { [ -f $O/STOP ] && { say "STOP flag: chain ends here"; exit 0; }; }
GAINS=${GAINS:-"6.0 9.0 12.0 16.0"}

# spec per gain: start file (gtest copy with that gain's sg), X0 (mV above baseline), points (mV, high -> low)
declare -A START X0 PTS
# ranges from the HV-on coarse probe of 00:10-00:15 (out/gain_20261118/coarse): 6 quiet ~10 mV above 4.5;
# 9 quiet ~50 mV except chips 8, 11 (~90); 12/16 several chips at 10-100 kHz up to 150-250 mV above baseline
GT=p2b-config-cern-ext_gain%s_peaktime200_opt_cosmic_gtest_Km18_sdtp%s.txt
START[6.0]=$(printf $GT 6.0 15);  X0[6.0]=20; PTS[6.0]=48,42,37,33,29,26,23,21,19,17,15,13,11
START[9.0]=$(printf $GT 9.0 30);  X0[9.0]=35; PTS[9.0]=110,90,75,64,56,50,45,41,37,34,31,28,25,22
START[12.0]=$(printf $GT 12.0 45); X0[12.0]=50; PTS[12.0]=200,160,130,110,95,84,75,67,60,54,49,44,40,36,32
START[16.0]=$(printf $GT 16.0 60); X0[16.0]=65; PTS[16.0]=260,210,170,140,120,105,93,83,75,68,62,56,51,46,42

say "chain start; gains $GAINS"
for i in $(seq 1 60); do python3 $T/hv_lab.py check-on >/dev/null 2>&1 && break; sleep 30; done
python3 $T/hv_lab.py check-on || { say "HV not at the operating point after 30 min: chain ends"; exit 1; }

for g in $GAINS; do
    stop
    D=$O/hvon_g$g
    EQ=p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_eq.txt
    if [ -f $D/scan_meta.json ] && grep -q "ALLDONE status: ok" $D/scan.log 2>/dev/null; then
        say "gain $g: scan already done"
    else
        say "gain $g: baseline scan, X0 ${X0[$g]}, points ${PTS[$g]}"
        python3 $T/fastscan/baseline_scan.py --out-dir $D --hv on --chips 2-19 \
            --start-file ${START[$g]} --map-start $O/maps/map_g$g.json --x0 ${X0[$g]} --eq-name $EQ \
            --points ${PTS[$g]} --repeat "" --fixed-seconds 40 --map-at "" --no-refs --no-end-map \
            --restore ${P45}trimmed_Km18_v1_hvm.txt > $D.stdout 2>&1
        say "gain $g: $(grep ALLDONE $D/scan.log | tail -1)"
    fi
    for R in 10 50; do
        python3 $T/fastscan/bscan_target.py --scan $D --eq-file $EQ --map-eq $D/map_eq.json --rate $R --margin 0.5 \
            --mask-extra $BURSTS --skip-raise 150 \
            --out-ext p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_hvon${R}.txt \
            --report $O/hvon_g${g}_R${R}_report.json > $O/target_g${g}_R${R}.log 2>&1
        say "gain $g R $R: $(tail -1 $O/target_g${g}_R${R}.log)"
    done
done
stop

LIST="${P45}trimmed_Km18_v1.txt ${P45}bscan_hvon10_v1.txt ${P45}bscan_hvon50_v1.txt"
for g in $GAINS; do
    for R in 10 50; do
        f=p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_hvon${R}.txt
        [ -f /local/p2/testbench/TestBenchCERN/config_ext/$f ] && LIST="$LIST $f"
    done
done
say "rate check: $LIST"
python3 $T/fastscan/cosmics_prep.py --out-dir $O/prep --mask $BURSTS $LIST > $O/prep.log 2>&1
cat $O/prep.log
OK=$(grep '^CONFIGS ' $O/prep.log | tail -1 | cut -d' ' -f2)
say "configs under the rate limit: $OK"
[ -n "$OK" ] || { say "nothing under the rate limit: no runs"; exit 1; }
stop

# order: Km18 ref, then pass 1 gain up (R50, R10 at each gain), pass 2 gain down, Km18 ref
IFS=, read -ra C <<< "$OK"
REF=""; LAD=()
for g in 4.5 $GAINS; do for R in 50 10; do
    for c in "${C[@]}"; do
        case $c in
            p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_*hvon${R}_*) LAD+=("$c") ;;
        esac
    done
done; done
for c in "${C[@]}"; do case $c in *Km18*) REF=$c ;; esac; done
SEQ=()
[ -n "$REF" ] && SEQ+=("$REF")
SEQ+=("${LAD[@]}")
for ((i=${#LAD[@]}-1; i>=0; i--)); do SEQ+=("${LAD[$i]}"); done
[ -n "$REF" ] && SEQ+=("$REF")
CFGS=$(IFS=,; echo "${SEQ[*]}")
say "${#SEQ[@]} runs: $CFGS"
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "18 Nov: VMM gain scan, HV at the operating point (IN 420/720, MID/OUT 430/730), 200 ns. Every channel at its own HV-on noise rate R = 10 and 50 Hz at each gain 4.5/6/9/12/16 mV/fC (gscan_hvonR files from per-gain HV-on scans; 4.5 = bscan_hvonR_v1 of 17 Nov) + trimmed_Km18_v1_hvm reference; two passes until 22:55; HV ramped down at 23:15." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 22:55 --configs "$CFGS"
say "ALLDONE gain chain"
