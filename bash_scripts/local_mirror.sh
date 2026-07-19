#!/bin/bash
# Local backup mirror for the VMM data tree.
#
# A SECOND, on-site copy of the run data on an external disk — completely
# independent of the EOS/xrootd backup (backup_watcher.py). Plain rsync to a
# locally-mounted ext4 disk (no Kerberos, no xrootd), meant to run from cron.
#
# Destination is read from config/local_mirror.txt (one line: the mount path,
# e.g. /mnt/p2backup) so it stays machine-specific and out of git. The source
# is BASE_DATA_DIR from run_config_beam.py, so it follows the active site.
#
# Safety:
#   * refuses to run unless the destination is a MOUNTED filesystem — so an
#     unplugged/unmounted disk never gets silently filled on the root disk;
#   * APPEND-ONLY by default (no --delete): deleting a run locally does NOT
#     remove it from the backup. Set MIRROR_DELETE=1 for a strict mirror;
#   * a flock lock prevents overlapping runs when a sync outlasts the interval.
#
# Cron (every 15 min):
#   */15 * * * * /path/to/DAQ_Control_VMM_Beam/bash_scripts/local_mirror.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$BASE_DIR/.venv/bin/python"
DEST_FILE="$BASE_DIR/config/local_mirror.txt"
LOG="$BASE_DIR/logs/local_mirror.log"
LOCK="$BASE_DIR/logs/.local_mirror.lock"

mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%F %T') [mirror] $*" >> "$LOG"; }

# Single-instance: bail if another mirror run is still going.
exec 9>"$LOCK"
if ! flock -n 9; then
    log "another mirror run is in progress — skipping"
    exit 0
fi

if [ ! -f "$DEST_FILE" ]; then
    log "no $DEST_FILE — write the external-disk mount path there (e.g. /mnt/p2backup)"
    exit 1
fi
DEST="$(head -n1 "$DEST_FILE" | tr -d '[:space:]')"
[ -n "$DEST" ] || { log "$DEST_FILE is empty"; exit 1; }

SRC="$("$PY" -c 'from run_config_beam import BASE_DATA_DIR; print(BASE_DATA_DIR)')"
SRC="${SRC%/}"                       # strip trailing slash for basename
SUBDIR="$(basename "$SRC")"          # e.g. vmm_daq_bench
DEST_TREE="$DEST/$SUBDIR"

# Never write to a destination that isn't a real mount — protects the root disk.
if ! mountpoint -q "$DEST"; then
    log "destination $DEST is not mounted — skipping (plug in / mount the disk)"
    exit 1
fi

DELETE_ARG=()
[ "${MIRROR_DELETE:-0}" = "1" ] && DELETE_ARG=(--delete)

mkdir -p "$DEST_TREE"
log "start: $SRC/ -> $DEST_TREE/ (delete=${MIRROR_DELETE:-0})"
# -a archive, exclude simulation captures + derived analysis (as the EOS backup does).
if rsync -a "${DELETE_ARG[@]}" \
        --exclude 'sim_pcapng/' --exclude 'analysis/' \
        "$SRC/" "$DEST_TREE/" >> "$LOG" 2>&1; then
    log "done OK"
else
    rc=$?
    log "rsync FAILED (rc=$rc)"
    exit "$rc"
fi
