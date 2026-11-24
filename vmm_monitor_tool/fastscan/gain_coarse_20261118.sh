#!/bin/bash
# 18 Nov: coarse noise probe per gain (10 s captures, chip sdt raised by N counts over trimmed_Km18_v1_hvm)
# to find where each gain becomes quiet, before the per-gain baseline scans. Waits for the gain maps.
cd ~/trim_work
until [ "$(grep -c GMAPSDONE gmaps_20261118.log)" -ge 2 ]; do sleep 10; done
CE=/local/p2/testbench/TestBenchCERN/config_ext
SRC=$CE/p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1_hvm.txt
OUT=~/trim_work/out/gain_20261118/coarse; mkdir -p $OUT
LIST=""
for spec in "4 6.0 10" "4 6.0 20" "4 6.0 35" "4 6.0 50" \
            "5 9.0 45" "5 9.0 65" "5 9.0 90" "5 9.0 130" \
            "6 12.0 70" "6 12.0 100" "6 12.0 150" "6 12.0 210" \
            "7 16.0 100" "7 16.0 150" "7 16.0 210" "7 16.0 300"; do
  set -- $spec
  N=p2b-config-cern-ext_gain$2_peaktime200_opt_cosmic_gtest_Km18_sdtp$3.txt
  if [ ! -f $CE/$N ]; then
    python3 vmm_monitor_tool/vmm_trim.py sdt $SRC --out $CE/$N --chips 2-19 --by $3 > /dev/null
    sed -i -E "/Hyb [1-9] Vmm [01] Reg bank1spi2 +Field sg /s/Field sg +[0-9]+/Field sg  $1/" $CE/$N
  fi
  LIST="$LIST --ext $N"
done
echo "[$(date +%T)] coarse probe: $LIST"
python3 vmm_monitor_tool/noise_probe.py $LIST --seconds 10 --max-mb 150 --out-dir $OUT --restore
echo COARSEDONE
