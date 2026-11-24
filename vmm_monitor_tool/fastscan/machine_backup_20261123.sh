#!/bin/bash
# Mon 23 Nov 2026: copy the folders that exist ONLY on the DAQ box (dedippce185) to EOS before the box is replaced
# (user, 22 Nov: "yes, back it up on Monday, leave config out").
# Waits until the weekend supervisor and the HV guard have ended (+10 min so backup_watcher copies the last run),
# or until 09:00 at the latest; then one tar.gz per folder -> EOS .../vmm_cosmic_run_oct26/machine_backup_20261123/,
# size check, MANIFEST, logbook note. Local tars are removed only after the size check.
#   setsid nohup bash ~/machine_backup_20261123.sh >> /local/p2/p2data/machine_backup_20261123.log 2>&1 < /dev/null &
set -u
SUP_PID=210111          # weekend_supervisor_20261119.sh
GUARD_PID=116143        # hv_lab.py guard (until 07:30, then ramp down)
LATEST=$(date -d "2026-11-23 09:00" +%s)
XRD=root://eospublic.cern.ch
DEST=/eos/experiment/ntof/data/x17/p2_sps_july/vmm_cosmic_run_oct26/machine_backup_20261123
TMP=/local/p2/p2data/machine_backup_20261123_tmp
say() { echo "[$(date '+%F %T')] $*"; }

say "machine backup armed (pid $$): waiting for supervisor $SUP_PID and guard $GUARD_PID (latest start 09:00)"
while { kill -0 $SUP_PID 2>/dev/null || kill -0 $GUARD_PID 2>/dev/null; } && [ "$(date +%s)" -lt "$LATEST" ]; do sleep 60; done
kill -0 $SUP_PID 2>/dev/null && say "WARNING: supervisor still running at the latest start time"
kill -0 $GUARD_PID 2>/dev/null && say "WARNING: HV guard still running at the latest start time"
say "supervisor/guard done: $(grep -h 'ALLDONE\|deadline' ~/trim_work/out/weekend_20261119/supervisor.log | tail -1)"
say "$(grep -h 'guard end\|ramped down' ~/trim_work/out/hv_lab/hv_lab.log | tail -2 | tr '\n' ' ')"
say "waiting 10 min for backup_watcher to copy the last run"
sleep 600

klist 2>&1 | grep -q "krbtgt/CERN.CH" || say "WARNING: no CERN ticket in the cache"
mkdir -p "$TMP" || exit 1
xrdfs $XRD mkdir -p "$DEST" || { say "cannot create $DEST"; exit 1; }
# name|folder   (config/ of the DAQ checkout is NOT included: it holds the banco HV token and password - user)
SRC="trim_work|$HOME/trim_work
config_ext|/local/p2/p2testbench/TestBenchCERN/config_ext
DAQ_logs|/local/p2/DAQ_Control_VMM_Beam/logs
p2-setups|/local/p2/p2-setups
eff_work|$HOME/eff_work"
MAN="$TMP/MANIFEST.txt"
{
  echo "Backup of the DAQ box dedippce185 (vmm_daplxa), $(date '+%F %T'), before the machine was replaced."
  echo "One tar.gz per folder (absolute source path below). NOT included: /local/p2/DAQ_Control_VMM_Beam/config (credentials)."
  echo "Run data are in ../runs (copied by backup_watcher), the logbook in ../logbook. Restore: tar xzf <name>.tar.gz -C <dir>."
  echo "Also on the box at backup time: DAQ checkout $(cd /local/p2/DAQ_Control_VMM_Beam && git log --oneline -1 2>/dev/null), git status:"
  (cd /local/p2/DAQ_Control_VMM_Beam && git status --short 2>/dev/null | sed 's/^/    /')
  echo
  echo "name | source | files | tar.gz bytes | md5"
} > "$MAN"
ok=1
while IFS='|' read -r name src; do
    [ -d "$src" ] || { say "$name: $src missing, skipped"; echo "$name | $src | MISSING" >> "$MAN"; continue; }
    t="$TMP/$name.tar.gz"
    say "$name: tar $src ($(du -sh "$src" | cut -f1))"
    nice -n 10 ionice -c3 tar -czf "$t" -C "$(dirname "$src")" "$(basename "$src")" 2> "$TMP/$name.tar.err"
    rc=$?
    [ $rc -le 1 ] || { say "$name: tar FAILED rc $rc: $(tail -2 "$TMP/$name.tar.err")"; ok=0; continue; }
    [ $rc = 1 ] && say "$name: tar rc 1 (a file changed while reading) - kept"
    n=$(tar -tzf "$t" | wc -l); sz=$(stat -c %s "$t"); md=$(md5sum "$t" | cut -c1-32)
    echo "$name | $src | $n | $sz | $md" >> "$MAN"
    if xrdcp -f -s "$t" "$XRD/$DEST/$name.tar.gz"; then
        es=$(xrdfs $XRD stat "$DEST/$name.tar.gz" | awk '/Size:/ {print $2}')
        if [ "$es" = "$sz" ]; then say "$name: on EOS, $sz bytes, $n entries"; rm -f "$t"
        else say "$name: SIZE MISMATCH local $sz EOS ${es:-?} - local tar kept"; ok=0; fi
    else
        say "$name: xrdcp FAILED - local tar kept in $TMP"; ok=0
    fi
done <<< "$SRC"
xrdcp -f -s "$MAN" "$XRD/$DEST/MANIFEST.txt" && say "MANIFEST on EOS"
cp /local/p2/p2data/machine_backup_20261123.log "$TMP/backup.log" 2>/dev/null && xrdcp -f -s "$TMP/backup.log" "$XRD/$DEST/backup.log"
if [ $ok = 1 ]; then
    say "MACHINE BACKUP DONE: $DEST"
    rm -f "$TMP"/*.tar.err "$TMP/backup.log"; rmdir "$TMP" 2>/dev/null; rm -f "$MAN" 2>/dev/null
    (cd /local/p2/DAQ_Control_VMM_Beam && .venv/bin/python lab_logbook.py note "23 Nov: box-only folders copied to EOS before the machine replacement: $DEST (trim_work, config_ext, DAQ logs, p2-setups, eff_work; one tar.gz each + MANIFEST.txt). DAQ config/ not included (credentials).") > /dev/null 2>&1
else
    say "MACHINE BACKUP INCOMPLETE - see above; local copies in $TMP"
fi
