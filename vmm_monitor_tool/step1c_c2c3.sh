#!/bin/bash
# Checks of the rate-scan sigma (HV off, no DAQ run):
#  A. reproducibility: the step-1 sdt scan of chips 2,3 repeated with the same 11 files
#  B. trim scan: 8 channels of chip 3 lowered by their trims only, each point with its own monitor map
# The GUI file (Km18_i5) is configured again at the end.
set -e
cd ~/trim_work
E=/local/p2/testbench/TestBenchCERN/config_ext
T=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_step1_c2c3
Q=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_step1c_c3
O=out/step1c
CH=3,7,10,24,25,39,46,52
mkdir -p $O/maps
echo "START $(date)"
curl -s --max-time 15 localhost:5002/hv_data_live | python3 -c "
import sys,json;d=json.load(sys.stdin);v={k:a[-1] for k,a in d['voltage'].items()};print('HV',v)
assert max(v.values())<5,'HV is ON: stop'"

# A. repeat of the sdt scan
RS=()
for tag in p10 p6 p3 p0 m2 m4 m6 m8 m10 m12 m14; do RS+=(--ext ${P}_ratescan_sdt${tag}.txt); done
python3 vmm_monitor_tool/noise_probe.py "${RS[@]}" --seconds 30 --max-mb 300 --out-dir $O/repeat
echo "REPEAT DONE $(date)"

# B. trim scan
python3 vmm_monitor_tool/trimscan_files.py $E/$T --chip 3 --channels $CH --steps 0,2,4,6,8,10,12,14 --out-prefix $E/${Q}_trimscan --force
STEPS=(0 2 4 6 8 10 12 14)
for i in "${!STEPS[@]}"; do
  n=${STEPS[$i]}
  f=${Q}_trimscan_t$n.txt
  python3 vmm_monitor_tool/vmm_monitor_scan.py map --ext $E/$f --channels $CH --no-reapply \
      --bin ~/trim_work/build/vmm_monitor --workdir $O/maps --out $O/maps/${f%.txt}.map.json > $O/maps/${f%.txt}.log 2>&1
  R=""; [ $i -eq $((${#STEPS[@]}-1)) ] && R=--restore
  python3 vmm_monitor_tool/noise_probe.py --ext $f --seconds 30 --max-mb 300 --out-dir $O/trimscan $R
done
echo "ALLDONE $(date)"
