#!/bin/bash
# 19 Nov 2026, weekend preparation with HV on (docs/HANDOFF_WEEKEND_RUNS_20261119.md section 5, steps 0, 5b, 6):
#  0. end-to-end test of the new DAQ code: ONE 20 min REF run through config_scan (run_268), checked by hand afterwards
#  1. gain 3.0 mV/fC: coarse HV-on probe (10 s, sdt offset -10 / 0 over Km18), then the HV-on threshold-above-baseline
#     scan (baseline_scan.py, equalised at X0 = 12 mV from the HV-off map map_g3.0.json), 40 s per point
#  2. bscan_target.py R = 50, 10, 2 Hz -> gain3.0 gscan_hvon{50,10,2}.txt
#  3. cosmics_prep.py 60 s rate check (hot channels > 5 kHz masked) of B2, C2, D50, D10, D2 -> the _hvm files
#  4. noise_probe.py 60 s on the four pulser files (no masking): pulsed channels must show ~10.85 kHz
#   setsid nohup bash ~/trim_work/weekend_prep_20261119.sh >> ~/trim_work/out/weekend_20261119/prep.log 2>&1 < /dev/null &
# Stop between steps: touch ~/trim_work/out/weekend_20261119/STOP_PREP
set -u
cd ~/trim_work
T=vmm_monitor_tool
O=out/gain_20261118
W=out/weekend_20261119
CE=/local/p2/p2testbench/TestBenchCERN/config_ext
P=p2b-config-cern-ext_gain
S=_peaktime200_opt_cosmic_
REF=${P}4.5${S}trimmed_Km18_v1_hvm.txt
BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
say() { echo "[$(date '+%F %T')] $*"; }
stop() { [ -f $W/STOP_PREP ] && { say "STOP_PREP flag: prep ends here"; exit 0; }; }
STEPS=${STEPS:-"e2e g3 target prep pulser"}
has() { case " $STEPS " in *" $1 "*) return 0;; esac; return 1; }

say "prep start, steps: $STEPS"
for i in $(seq 1 60); do python3 $T/hv_lab.py check-on >/dev/null 2>&1 && break; sleep 30; done
python3 $T/hv_lab.py check-on || { say "HV not at the operating point after 30 min: prep ends"; exit 1; }

if has e2e; then
    stop
    U=$(date -d "+36 min" +%H:%M)
    say "e2e: config_scan --until $U --minutes 20 --configs $REF"
    (cd /local/p2/DAQ_Control_VMM_Beam && .venv/bin/python config_scan.py --until $U --minutes 20 --configs $REF) > $W/e2e_config_scan.log 2>&1
    say "e2e: $(grep -E 'started run_|finished|FATAL|REFUS|ABORT' $W/e2e_config_scan.log | tr '\n' ' ' | cut -c1-400)"
fi

G3S=${P}3.0${S}gtest_Km18_sdtp0.txt
G3M=${P}3.0${S}gtest_Km18_sdtm10.txt
EQ3=${P}3.0${S}gscan_eq.txt
if has g3; then
    stop
    if [ ! -f $CE/$G3M ]; then
        python3 $T/vmm_trim.py sdt $CE/$REF --out $CE/$G3M --chips 2-19 --by -10 > /dev/null
        sed -i -E "/Hyb [1-9] Vmm [01] Reg bank1spi2 +Field sg /s/Field sg +[0-9]+/Field sg  2/" $CE/$G3M
    fi
    mkdir -p $O/coarse_g3
    say "g3 coarse probe"
    python3 $T/noise_probe.py --ext $G3M --ext $G3S --seconds 10 --max-mb 150 --out-dir $O/coarse_g3 --restore > $O/coarse_g3.log 2>&1
    tail -4 $O/coarse_g3.log
    stop
    D=$O/hvon_g3.0
    say "g3 baseline scan, X0 12"
    python3 $T/fastscan/baseline_scan.py --out-dir $D --hv on --chips 2-19 \
        --start-file $G3S --map-start $O/maps/map_g3.0.json --x0 12 --eq-name $EQ3 \
        --points 40,34,29,25,22,19,17,15,13,11,9,8,7,6 --repeat "" --fixed-seconds 40 --map-at "" --no-refs --no-end-map \
        --restore $REF > $D.stdout 2>&1
    say "g3 scan: $(grep ALLDONE $D/scan.log | tail -1)"
fi

if has target; then
    stop
    D=$O/hvon_g3.0
    for R in 50 10 2; do
        python3 $T/fastscan/bscan_target.py --scan $D --eq-file $EQ3 --map-eq $D/map_eq.json --rate $R --margin 0.5 \
            --mask-extra $BURSTS --skip-raise 150 \
            --out-ext ${P}3.0${S}gscan_hvon${R}.txt --report $O/hvon_g3.0_R${R}_report.json > $O/target_g3.0_R${R}.log 2>&1
        say "g3 R $R: $(tail -1 $O/target_g3.0_R${R}.log)"
    done
fi

if has prep; then
    stop
    LIST=""
    for f in ${P}6.0${S}gscan_hvon2.txt ${P}9.0${S}gscan_hvon2.txt ${P}3.0${S}gscan_hvon50.txt ${P}3.0${S}gscan_hvon10.txt ${P}3.0${S}gscan_hvon2.txt; do
        [ -f $CE/$f ] && LIST="$LIST $f" || say "prep: $f missing"
    done
    say "rate check:$LIST"
    python3 $T/fastscan/cosmics_prep.py --out-dir $W/prep --mask $BURSTS $LIST > $W/prep_rates.log 2>&1
    cat $W/prep_rates.log | grep -v "^\s*$" | tail -20
fi

if has pulser; then
    stop
    mkdir -p $W/pulser_rates
    L=""
    for f in ${P}4.5${S}trimmed_Km18_v1_hvm_tpA.txt ${P}4.5${S}trimmed_Km18_v1_hvm_tpB.txt ${P}9.0${S}gscan_hvon10_hvm_tpA.txt ${P}9.0${S}gscan_hvon10_hvm_tpB.txt; do
        L="$L --ext $f"
    done
    say "pulser rate check"
    python3 $T/noise_probe.py $L --seconds 60 --max-mb 300 --out-dir $W/pulser_rates --restore > $W/pulser_rates.log 2>&1
    tail -6 $W/pulser_rates.log
fi
say "PREPDONE"
