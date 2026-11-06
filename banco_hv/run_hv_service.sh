#!/bin/bash
# Keep banco_hv/hv_serial_live.py running: restart it 10 s after any exit.
#
# Started at boot and re-checked every 10 min by banco's crontab (see README),
# always inside the tmux server `-L lab_hv`, session `lab_hv`:
#   @reboot      sleep 30; ~/lab_hv_monitor/run_hv_service.sh --tmux
#   */10 * * * * ~/lab_hv_monitor/run_hv_service.sh --tmux
# --tmux starts the session only if it is not there, so the cron line is a
# no-op while the service is up.
#
# To stop it for good (and stop cron bringing it back):
#   touch ~/lab_hv_monitor/DISABLED; tmux -L lab_hv kill-server
# and `rm ~/lab_hv_monitor/DISABLED` to allow it again.
#
# Memory cap: systemd-run --user needs a user session, which does not exist at
# boot before anyone logs in, so the cap falls back to ulimit -v there.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

if [ -e "$DIR/DISABLED" ]; then
    exit 0
fi

if [ "$1" = "--tmux" ]; then
    if /usr/bin/tmux -L lab_hv has-session -t lab_hv 2>/dev/null; then
        exit 0
    fi
    echo "[$(date '+%F %T')] starting tmux -L lab_hv session lab_hv" >> "$DIR/service.log"
    exec /usr/bin/tmux -L lab_hv new-session -d -s lab_hv "$DIR/run_hv_service.sh"
fi

# One loop per machine: a second copy would fight over the serial ports.
exec 9>"$DIR/.service.lock"
if ! flock -n 9; then
    echo "[hv_serial] another run_hv_service.sh is already running; not starting"
    sleep 5
    exit 0
fi

while true; do
    [ -e "$DIR/DISABLED" ] && { echo "[hv_serial] DISABLED file present; stopping"; exit 0; }
    echo "=== hv_serial start $(date '+%F %T') ===" >> "$DIR/service.log"
    if systemd-run --user --scope -q true 2>/dev/null; then
        systemd-run --user --scope -q -p MemoryMax=300M \
            /usr/bin/python3 hv_serial_live.py --config hv_serial_live.json 2>&1 | tee -a service.log
    else
        ( ulimit -v 1000000; exec /usr/bin/python3 hv_serial_live.py --config hv_serial_live.json ) \
            2>&1 | tee -a service.log
    fi
    echo "[hv_serial] exited, restarting in 10 s (Ctrl-C again to stop the loop)"
    sleep 10
done
