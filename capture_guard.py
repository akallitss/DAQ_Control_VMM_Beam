#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop the run as soon as the VMM readout goes silent, and record where to restart.

WHY THIS EXISTS (2026-07-30). Twice in one day the VMM readout stopped producing
packets mid-run: dumpcap kept rotating a new file every CAPTURE_DURATION_S, the
FEC still reported AcqON and the hybrid link statuses were byte-identical to a
healthy run, but every pcapng was exactly its 272-byte header. Nothing noticed.
daq_control marks a sub-run .subrun_complete on normal completion whether or not
a single packet was recorded, so on run_25 five consecutive scan points
(meshscan_m100V, driftscan_gap150V/200V/250V/300V) were banked as complete
holding no data, and the whole drift scan was lost. The Dream/uRWELL side
recorded perfectly throughout, so the run looked healthy from that GUI.

This guard watches the CURRENT sub-run's capture files and, on seeing
EMPTY_TRIP consecutive CLOSED files with no packets, stops the run through the
Flask /stop_run route (so the stop is attributed in daq_events.log) and writes
RESTART_FROM.txt naming the sub-run to resume from.

IT DELIBERATELY DOES NOT TRIP ON A BEAM OUTAGE. With no beam there are no
triggers, so empty files are correct and stopping would be wrong — you want to
sit and wait for the beam. The discriminator is the Dream DAQ: it reads the
uRWELLs off the same external trigger, so if Dream is still writing data while
the VMM writes nothing, the fault is the VMM's. If neither is recording, that is
a beam problem and the guard stays quiet.

WHY FILE SIZE ALONE IS NOT ENOUGH TO MAKE THAT CALL. It is tempting to separate
the two cases by size — a dead VMM gives exactly 272 bytes, a low-beam file a
few hundred more. That works for LOW beam, and EMPTY_MAX_BYTES is set so a file
holding even one packet is not called empty. But it cannot work for a FULL beam
stop: in external-trigger mode the VMM reads out only on a trigger, so no beam
means no packets means exactly 272 bytes — byte-identical to the failure. Hence
the size test says "no packets" and the Dream cross-check says "and that is the
VMM's fault"; neither replaces the other.

Run it alongside a run:
    tmux new-session -d -s vmm_capture_guard \\
        "/local/p2/DAQ_Control_VMM_Beam/.venv/bin/python capture_guard.py"
"""

import json
import os
import time
import urllib.request

VMM_FLASK = 'http://localhost:5002'
DREAM_FLASK = 'http://128.141.21.144:5001'
LOG = '/local/p2/capture_guard.log'

# Data tree from the run config, so this follows the SITE switch instead of
# hardcoding the sps path. Importing run_config_beam is side-effect free — it
# only defines constants and the Config class outside its __main__ guard.
try:
    from run_config_beam import BASE_DATA_DIR
    RUNS_DIR = os.path.join(BASE_DATA_DIR, 'runs')
except Exception:
    RUNS_DIR = '/local/p2/p2data/TB_July26_H4/runs'

POLL_S = 20
EMPTY_TRIP = 2          # consecutive CLOSED empty files before stopping
# A packet-less pcapng is just its header: exactly 272 bytes here. Measured over
# run_24 + run_25 (819 files): 85 files at exactly 272 B, and NOT ONE file
# between 300 B and 100 kB. So "empty" is sharply defined and the threshold only
# has to clear the header plus a little slack for a dumpcap/interface change
# that lengthens it. It must stay BELOW the one-packet floor (~272 + a ~100-byte
# enhanced-packet block ~= 370) so that a file holding even a single packet is
# never called empty — that is the low-beam case, which must not trip.
EMPTY_MAX_BYTES = 320
# Capture must be ACTIVE (a file written this recently) before we judge anything.
# /live_hits keeps reporting the last run name long after it ended, so without
# this the guard would evaluate a finished run's leftover empty files the moment
# it starts and "stop" a run that is not running. It also keeps the guard quiet
# during the per-subrun HV gate, when no files are written and there is nothing
# to judge. Comfortably longer than the 44.4 s dumpcap rotation.
CAPTURE_STALE_S = 180


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} | {msg}'
    print(line, flush=True)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def get_json(url, timeout=8, data=None):
    try:
        req = urllib.request.Request(url)
        if data is not None:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode(),
                headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def current_run():
    d = get_json(f'{VMM_FLASK}/live_hits') or {}
    return d.get('run')


def current_subrun(run):
    """The sub-run holding the most recently written capture file.

    Two rejected alternatives:
      - alphabetical: 'meshscan_m100V' sorts before 'meshscan_m10V', so a name
        sort silently reports the wrong point once a scan passes 99 V.
      - newest sub-run DIRECTORY by mtime: any touch of a directory's contents
        (even renaming a .subrun_complete marker afterwards) reorders it.
    The newest .pcapng is tied directly to where dumpcap is writing now.
    """
    base = os.path.join(RUNS_DIR, run)
    best = None
    try:
        for s in os.listdir(base):
            raw = os.path.join(base, s, 'raw_daq_data')
            if not os.path.isdir(raw):
                continue
            for f in os.listdir(raw):
                if f.endswith('.pcapng'):
                    t = os.path.getmtime(os.path.join(raw, f))
                    if best is None or t > best[0]:
                        best = (t, s)
    except Exception:
        return None
    return best[1] if best else None


def capture_active(run, subrun):
    """Has a capture file been written within CAPTURE_STALE_S? If not, either no
    run is in progress or we are mid HV-gate — nothing to judge either way."""
    d = os.path.join(RUNS_DIR, run, subrun, 'raw_daq_data')
    try:
        newest = max(os.path.getmtime(os.path.join(d, f))
                     for f in os.listdir(d) if f.endswith('.pcapng'))
    except Exception:
        return False
    return (time.time() - newest) < CAPTURE_STALE_S


def closed_capture_sizes(run, subrun):
    """Sizes of CLOSED pcapng files, oldest first. The newest file is still
    being written, so it is excluded — judging it would trip on every rotation."""
    d = os.path.join(RUNS_DIR, run, subrun, 'raw_daq_data')
    try:
        files = [(os.path.getmtime(os.path.join(d, f)), os.path.join(d, f))
                 for f in os.listdir(d) if f.endswith('.pcapng')]
    except Exception:
        return []
    files.sort()
    return [os.path.getsize(p) for _, p in files[:-1]]


def dream_recording():
    """Is the Dream side still writing data? If so, a silent VMM is the VMM's
    fault rather than a beam outage."""
    d = get_json(f'{DREAM_FLASK}/get_current_run') or {}
    if not d.get('run_name'):
        return False, 'Dream reports no current run'
    b = get_json(f'{DREAM_FLASK}/beam/status') or {}
    # Dream's own event counter is not exposed; use the beam monitor as the
    # proxy for "triggers should be arriving".
    if b.get('beam_on') is False:
        return False, 'beam is OFF — empty capture is expected, not a fault'
    return True, f'Dream running {d.get("run_name")}, beam_on={b.get("beam_on")}'


def main():
    log('capture_guard START — will stop the run on '
        f'{EMPTY_TRIP} consecutive closed empty pcapng files (>{EMPTY_MAX_BYTES} B = healthy)')
    log(f'watching {RUNS_DIR}')
    # This is a SERVICE started with the rest of the stack, so it must survive a
    # trip and re-arm for the next run rather than exiting. stopped_run holds the
    # run it already acted on, so it never fires twice for the same one.
    stopped_run = None
    while True:
        run = current_run()
        if not run:
            time.sleep(POLL_S)
            continue
        if run == stopped_run:      # already acted on this one; wait for the next
            time.sleep(POLL_S)
            continue
        sub = current_subrun(run)
        if not sub or not capture_active(run, sub):
            time.sleep(POLL_S)
            continue
        sizes = closed_capture_sizes(run, sub)
        tail = sizes[-EMPTY_TRIP:]
        if len(tail) == EMPTY_TRIP and all(s < EMPTY_MAX_BYTES for s in tail):
            blame_vmm, why = dream_recording()
            if not blame_vmm:
                log(f'{run}/{sub}: {EMPTY_TRIP} empty files but {why} — not stopping')
                time.sleep(POLL_S)
                continue
            log(f'*** TRIP *** {run}/{sub}: last {EMPTY_TRIP} closed capture files '
                f'are empty ({tail} bytes) while {why}. The VMM readout is dead. '
                f'Stopping the run.')
            marker = os.path.join(RUNS_DIR, run, 'RESTART_FROM.txt')
            try:
                with open(marker, 'w') as f:
                    f.write(
                        f'{time.strftime("%Y-%m-%d %H:%M:%S")}\n'
                        f'run={run}\n'
                        f'restart_from_subrun={sub}\n'
                        f'reason=VMM readout produced no packets '
                        f'({EMPTY_TRIP} consecutive empty pcapng files)\n'
                        f'context={why}\n\n'
                        f'This sub-run and every LATER one in the scan still need '
                        f'taking.\nBefore restarting: apply the chip config, warm '
                        f'reset to 0 failed hybrids,\nand VERIFY packets actually '
                        f'flow before committing to a long run.\n')
                log(f'wrote {marker}')
            except Exception as e:
                log(f'ALERT could not write {marker}: {e}')
            r = get_json(f'{VMM_FLASK}/stop_run', data={})
            log(f'stop_run response: {r}')
            stopped_run = run
            log(f're-arming for the next run (will not fire again for {run})')
        time.sleep(POLL_S)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f'ALERT capture_guard CRASHED: {type(e).__name__}: {e}')
        raise
