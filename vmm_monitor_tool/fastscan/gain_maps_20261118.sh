#!/bin/bash
# monitor maps at several gains (HV off, acquisition off), 18 Nov
cd ~/trim_work
CE=/local/p2/testbench/TestBenchCERN/config_ext
SRC=$CE/p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1_hvm.txt
OUT=~/trim_work/out/gain_20261118/maps; mkdir -p $OUT
for spec in "7 16.0 60" "5 9.0 30" "6 12.0 45" "4 6.0 15" "3 4.5 0"; do
  set -- $spec
  N=p2b-config-cern-ext_gain$2_peaktime200_opt_cosmic_gtest_Km18_sdtp$3.txt
  if [ ! -f $CE/$N ]; then
    python3 vmm_monitor_tool/vmm_trim.py sdt $SRC --out $CE/$N --chips 2-19 --by $3
    sed -i -E "/Hyb [1-9] Vmm [01] Reg bank1spi2 +Field sg /s/Field sg +[0-9]+/Field sg  $1/" $CE/$N
  fi
  [ -f STOP_GMAPS ] && { echo "stop flag"; break; }
  mkdir -p $OUT/work_g$2; echo "[$(date +%T)] map gain $2"
  python3 vmm_monitor_tool/vmm_monitor_scan.py map --ext $CE/$N --unmask --bin ~/trim_work/build/vmm_monitor \
     --workdir $OUT/work_g$2 --out $OUT/map_g$2.json > $OUT/map_g$2.log 2>&1
  echo "[$(date +%T)] rc=$? gain $2"
done
echo GMAPSDONE
