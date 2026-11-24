#!/bin/bash
# 18 Nov 2026, user 00:25: focus on gains <= 9 mV/fC; at most ~2 h on 12/16 ("a clever way to operate at higher gain").
# Takes over from gain_chain_20261118.sh, which is stopped (STOP flag) after its 6 and 9 scans + targets.
#  1. HV-on scans at 12 (full) and 16 (short), bscan_target R = 50 (time-boxed: 16 is skipped if past 02:40)
#  2. gain_mix.py: each chip at the highest of 16/12 where it is healthy, else 9 -> one mixed R = 50 file
#  3. rate check of Km18 ref, 4.5/6/9 x R 10/50, mix R 50
#  4. cosmics until 22:55: ref, pass 1 up, pass 2 down, mix (1 run), pass 3 up, ref
#   setsid nohup bash ~/trim_work/gain_chain2_20261118.sh >> ~/trim_work/out/gain_20261118/chain2.log 2>&1 < /dev/null &
set -u
cd ~/trim_work
T=vmm_monitor_tool
O=out/gain_20261118
P45=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
say() { echo "[$(date '+%F %T')] $*"; }
stop() { [ -f $O/STOP2 ] && { say "STOP2 flag: chain ends here"; exit 0; }; }
GT=p2b-config-cern-ext_gain%s_peaktime200_opt_cosmic_gtest_Km18_sdtp%s.txt

until grep -q "STOP flag: chain ends here" $O/chain.log; do sleep 20; done
rm -f $O/STOP
say "chain2 start (chain 1 stopped after gains 6, 9)"

scan() {  # gain start-offset x0 points seconds
    local g=$1 D=$O/hvon_g$1 EQ=p2b-config-cern-ext_gain$1_peaktime200_opt_cosmic_gscan_eq.txt
    say "gain $g: baseline scan, X0 $3, points $4, $5 s"
    python3 $T/fastscan/baseline_scan.py --out-dir $D --hv on --chips 2-19 \
        --start-file $(printf $GT $g $2) --map-start $O/maps/map_g$g.json --x0 $3 --eq-name $EQ \
        --points $4 --repeat "" --fixed-seconds $5 --map-at "" --no-refs --no-end-map \
        --restore ${P45}trimmed_Km18_v1_hvm.txt > $D.stdout 2>&1
    say "gain $g: $(grep ALLDONE $D/scan.log | tail -1)"
    for R in 10 50; do
        python3 $T/fastscan/bscan_target.py --scan $D --eq-file $EQ --map-eq $D/map_eq.json --rate $R --margin 0.5 \
            --mask-extra $BURSTS --skip-raise 150 \
            --out-ext p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_hvon${R}.txt \
            --report $O/hvon_g${g}_R${R}_report.json > $O/target_g${g}_R${R}.log 2>&1
        say "gain $g R $R: $(tail -1 $O/target_g${g}_R${R}.log)"
    done
}
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target"
scan 12.0 45 50 200,160,130,110,95,84,75,67,60,54,49,44,40,36 30
stop
TRY=12.0
if [ $(date +%H%M) -lt 0240 ]; then
    scan 16.0 60 65 260,200,160,130,110,95,83,72,62,54,48 30
    TRY=16.0,12.0
else
    say "gain 16 skipped: past the time box"
fi
stop

MIX=p2b-config-cern-ext_gain12mix_peaktime200_opt_cosmic_gscan_mix_hvon50.txt
python3 $T/fastscan/gain_mix.py --base-gain 9.0 --try $TRY --rate 50 --out-ext $MIX --summary $O/gain_mix_R50.json \
    > $O/gain_mix.log 2>&1
cat $O/gain_mix.log
NMIX=$(grep -c -E "^  chip .* -> (12|16)" $O/gain_mix.log)
say "mixed file: $NMIX chips above 9 mV/fC"

LIST="${P45}trimmed_Km18_v1.txt ${P45}bscan_hvon10_v1.txt ${P45}bscan_hvon50_v1.txt"
for g in 6.0 9.0; do for R in 10 50; do
    f=p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_hvon${R}.txt
    [ -f /local/p2/testbench/TestBenchCERN/config_ext/$f ] && LIST="$LIST $f"
done; done
[ "$NMIX" -gt 0 ] && LIST="$LIST $MIX"
say "rate check: $LIST"
python3 $T/fastscan/cosmics_prep.py --out-dir $O/prep --mask $BURSTS $LIST > $O/prep.log 2>&1
cat $O/prep.log
OK=$(grep '^CONFIGS ' $O/prep.log | tail -1 | cut -d' ' -f2)
say "configs under the rate limit: $OK"
[ -n "$OK" ] || { say "nothing under the rate limit: no runs"; exit 1; }
stop

IFS=, read -ra C <<< "$OK"
REF=""; MIXH=""; LAD=()
for g in 4.5 6.0 9.0; do for R in 50 10; do
    for c in "${C[@]}"; do
        case $c in p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_*hvon${R}_*) LAD+=("$c") ;; esac
    done
done; done
for c in "${C[@]}"; do case $c in *Km18*) REF=$c ;; *gain12mix*) MIXH=$c ;; esac; done
SEQ=()
[ -n "$REF" ] && SEQ+=("$REF")
SEQ+=("${LAD[@]}")
for ((i=${#LAD[@]}-1; i>=0; i--)); do SEQ+=("${LAD[$i]}"); done
[ -n "$MIXH" ] && SEQ+=("$MIXH")
SEQ+=("${LAD[@]}")
[ -n "$REF" ] && SEQ+=("$REF")
CFGS=$(IFS=,; echo "${SEQ[*]}")
say "${#SEQ[@]} runs: $CFGS"
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "18 Nov: VMM gain scan, HV at the operating point (IN 420/720, MID/OUT 430/730), 200 ns. Every channel at its own HV-on noise rate R = 10 and 50 Hz at gains 4.5/6/9 mV/fC (gscan_hvonR from per-gain HV-on scans; 4.5 = bscan_hvonR_v1 of 17 Nov), three passes, + one mixed-gain run (chips healthy at 12/16 mV/fC at that gain, the rest at 9) + trimmed_Km18_v1_hvm reference; until 22:55; HV ramped down at 23:15. 12/16 mV/fC: several chips oscillate (10-100 kHz up to 150-250 mV above baseline), chip 8 baseline +180 mV at 16." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 22:55 --configs "$CFGS"
say "ALLDONE gain chain2"
