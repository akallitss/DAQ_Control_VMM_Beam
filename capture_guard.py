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
import subprocess
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
# Gap between the two reads of Dream's trigger counter. Long enough that a real
# trigger rate moves it unmistakably (thousands of events even on a weak beam),
# short enough that a genuinely dead readout is still caught inside ~2 min.
DREAM_EVENT_SAMPLE_S = 30

# ---------------------------------------------------------------------------
# AUTO-RECOVERY. After stopping a dead run the guard can re-apply the chip
# config, warm reset until the hybrids come back ready, and start a FOLLOW-UP
# run that continues the interrupted plan from the point that died. That run
# carries continues_run/continuation_* in its config, so the data records its
# own provenance.
#
# This is the guard driving the detectors on its own, so it is fenced in:
#   - MAX_RECOVERIES caps how many times it will do this per guard lifetime. A
#     genuinely broken readout must not become a loop that burns the slot and
#     leaves a trail of empty runs.
#   - It refuses to start anything if the beam is off — there would be nothing
#     to record, and an unattended restart into no beam just ramps HV for
#     nothing.
#   - A warm reset that will not reach 0 failed hybrids aborts recovery. We do
#     not start a run on hybrids we know are not ready.
#   - Dropping a file named DISABLE_FILE turns the whole thing off without
#     touching code or restarting the service.
# ---------------------------------------------------------------------------
AUTO_RECOVER = True
MAX_RECOVERIES = 2
REPO = os.path.dirname(os.path.abspath(__file__))
DISABLE_FILE = os.path.join(REPO, 'config', 'no_auto_recovery')
CONTINUATION_PATH = os.path.join(REPO, 'config', 'continuation.json')
RECOVER_SETTLE_S = 20      # let the stopped run finish tearing down
WARM_RESET_TRIES = 3
STEP_TIMEOUT_S = 300
# Dream's teardown powers the crate off and is slow — measured at ~7 min on
# 2026-07-30. Allow well beyond that before giving up.
DREAM_TEARDOWN_TIMEOUT_S = 900
START_TRIES = 2


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
    """Which run is being written, asking the Flask first and the disk second.

    The DAQ does not need the Flask — daq_control runs in its own tmux and keeps
    recording regardless — so the guard must not need it either. On 2026-07-31
    the Flask died four times, and each time this returned None, so the guard
    silently skipped every check and protected nothing while a run was live.
    Falling back to the run directory holding the most recently written capture
    file keeps detection working through a Flask outage.
    """
    d = get_json(f'{VMM_FLASK}/live_hits') or {}
    run = d.get('run')
    if run:
        return run
    newest = None
    try:
        for r in os.listdir(RUNS_DIR):
            for dp, _, fns in os.walk(os.path.join(RUNS_DIR, r)):
                for f in fns:
                    if f.endswith('.pcapng'):
                        t = os.path.getmtime(os.path.join(dp, f))
                        if newest is None or t > newest[0]:
                            newest = (t, r)
    except Exception:
        return None
    # Only trust it if something was written recently; an old run directory is
    # not evidence that a run is in progress now.
    if newest and (time.time() - newest[0]) < CAPTURE_STALE_S:
        return newest[1]
    return None


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


def dream_events():
    """Dream's trigger counter for the current run (external TCM coincidence)."""
    d = get_json(f'{DREAM_FLASK}/status')
    if not isinstance(d, list):
        return None
    for s in d:
        if s.get('name') == 'dream_daq':
            ev = s.get('run_events')
            return int(ev) if isinstance(ev, (int, float)) else None
    return None


def dream_recording():
    """Are triggers ARRIVING RIGHT NOW? Only that separates a dead readout from
    a beam outage.

    The previous version asked two questions that feel equivalent and are not:
    does Dream have a run open, and does the beam monitor say beam_on. Both were
    true on 2026-07-31 at 16:52 while the beam to the North Area was collapsing
    — FTARGET spills fell 42 -> 8 per 10 min while the SPS went on spilling
    happily to SPS_DUMP. beam_on tracks SPS extraction with a 300 s off-gap and
    says nothing about the beam reaching H4, so the guard read "beam is on, the
    VMM is silent" and stopped and restarted a perfectly healthy run.

    Dream's cumulative data volume is no good either: it is what was recorded
    BEFORE the beam left, and comparing a running total against an instantaneous
    emptiness always favours blaming the VMM.

    So: sample Dream's own event counter twice. It is read off the same external
    TCM coincidence the VMM sees, so growing means triggers are arriving and a
    silent VMM really is the VMM's fault; flat means nothing is arriving for
    anyone and this is the beam. Unreadable counts as flat — the guard must fail
    towards NOT stopping a run, because a false trip costs good beam time.
    """
    d = get_json(f'{DREAM_FLASK}/get_current_run') or {}
    if not d.get('run_name'):
        return False, 'Dream reports no current run'
    e0 = dream_events()
    if e0 is None:
        return False, 'cannot read Dream event counter — not blaming the VMM'
    time.sleep(DREAM_EVENT_SAMPLE_S)
    e1 = dream_events()
    if e1 is None:
        return False, 'cannot re-read Dream event counter — not blaming the VMM'
    if e1 > e0:
        return True, (f'Dream triggers {e0:,} -> {e1:,} in {DREAM_EVENT_SAMPLE_S}s '
                      f'(+{e1 - e0:,}): triggers ARE arriving, so a silent VMM is '
                      f'the VMM')
    return False, (f'Dream triggers flat at {e1:,} over {DREAM_EVENT_SAMPLE_S}s — '
                   f'nothing is arriving for anyone, so this is a BEAM outage, '
                   f'not a readout fault')


def plan_of(run):
    """Which scan plan that run was following, from its own written config.
    Falls back to the retake plan rather than guessing from the live RUN_PLAN,
    which may have been edited since the run started."""
    try:
        with open(os.path.join(RUNS_DIR, run, 'run_config.json')) as f:
            return json.load(f).get('run_plan') or 'retake_run25'
    except Exception:
        return 'retake_run25'


# Dream's own idle states, same list its GUI uses to decide the run is over.
DREAM_IDLE_STATES = ('WAITING', 'Run Complete', 'ERROR')


def dream_idle():
    """Has the Dream side finished tearing the paired run down?

    Stopping the VMM run stops Dream too (daq_control teardown posts to
    /vmm_trigger/stop), but Dream's teardown is NOT instant — it powers HV off
    and can take minutes; on 2026-07-30 it took about seven. Dream refuses a new
    trigger with 409 while a run is still in progress, so restarting before it
    is idle just gets the follow-up run rejected and the recovery lost.
    """
    d = get_json(f'{DREAM_FLASK}/status')
    if not isinstance(d, list):
        return False
    for s in d:
        if s.get('name') == 'daq_control':
            return s.get('status') in DREAM_IDLE_STATES
    return False


def post(path, payload=None):
    return get_json(f'{VMM_FLASK}{path}', data=payload if payload is not None else {})


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=20).stdout
    except Exception:
        return ''


def stop_the_run():
    """Stop the run, via the Flask if it is up and directly if it is not.

    The Flask route is preferred because it records the stop in daq_events.log
    with attribution. But the guard exists to stop a run that is recording
    nothing, and the flask died four times on 2026-07-31 — a guard that can only
    act while a web server happens to be alive is not protection. stop_run.sh is
    exactly what that route runs, so falling back to it loses the attribution
    and nothing else; we write the event line ourselves to keep even that.
    """
    r = get_json(f'{VMM_FLASK}/stop_run', data={})
    if r and r.get('success'):
        return f'via flask: {r.get("message")}'

    log(f'   flask stop unavailable ({r}) — running stop_run.sh directly')
    try:
        with open(os.path.join(REPO, 'logs', 'daq_events.log'), 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} | STOP_RUN       | '
                    f'capture_guard | flask down, stop_run.sh invoked directly\n')
    except Exception as e:
        log(f'   could not write the event line: {e}')
    try:
        subprocess.Popen([os.path.join(REPO, 'bash_scripts', 'stop_run.sh')],
                         cwd=REPO, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return 'via stop_run.sh directly (flask was down)'
    except Exception as e:
        return f'FAILED to stop the run at all: {e}'


def daq_running():
    """Is the per-RUN daq_control.py alive?

    The [/] bracket does two jobs: it stops the pattern matching the shell
    running this very check, and the leading slash keeps it from matching
    vmm_daq_control.py, the persistent VMM DAQ server that runs for weeks.
    """
    return sh('pgrep -f "[/]daq_control[.]py" || true').strip() != ''


def wait_until(pred, timeout_s, what):
    """Poll pred() until true. (ok, seconds_waited)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if pred():
            return True, time.time() - t0
        time.sleep(3)
    log(f'   TIMEOUT after {timeout_s}s waiting for {what}')
    return False, time.time() - t0


def chip_status():
    return get_json(f'{VMM_FLASK}/chip_config/status') or {}


# THE ORDER IS: warm reset -> config apply -> acq-on, and it is not
# interchangeable. A standalone warm reset AFTER an apply can revert the
# registers the apply just set, so the chips run something other than what the
# data is labelled with. chip_config.run_armed() enforces it by refusing to arm
# unless the last successful apply is NEWER than the last successful warm reset,
# so the wrong order also leaves the run unarmed and /run_config_py answers 409.


def warm_reset_until_ready():
    """Step 1. 0 failed hybrids. Does NOT require 'armed' — arming cannot
    happen until the apply, which comes after this."""
    for attempt in range(1, WARM_RESET_TRIES + 1):
        log(f'   warm reset attempt {attempt}/{WARM_RESET_TRIES}...')
        post('/chip_config/warm_reset')
        wait_until(lambda: chip_status().get('warm_reset', {}).get('running') is False,
                   STEP_TIMEOUT_S, 'warm reset')
        failed = (chip_status().get('warm_reset', {}).get('last') or {}).get('failed')
        log(f'   warm reset failed={failed}')
        if failed == 0:
            return True
    return False


def apply_chip_config():
    """Step 2. Retried: the apply does its own warm reset first and aborts if
    the hybrids have drifted back to unready ('10 hybrid(s) not ready - config
    NOT applied'), which happens on this setup. Green light requires 'armed',
    which is the state machine confirming apply-after-warm-reset."""
    for attempt in range(1, WARM_RESET_TRIES + 1):
        log(f'   applying chip config, attempt {attempt}/{WARM_RESET_TRIES}...')
        post('/chip_config/apply')
        ok, _ = wait_until(lambda: chip_status().get('running') is False,
                           STEP_TIMEOUT_S, 'chip config apply')
        rc = (chip_status().get('last') or {}).get('rc')
        armed = chip_status().get('warm_reset', {}).get('armed')
        log(f'   apply rc={rc} armed={armed}')
        if ok and rc == 0 and armed:
            return True
        time.sleep(5)
    return False


def recover(dead_run, dead_subrun, plan_hint):
    """Stop-to-restart recovery. Returns True if a follow-up run was started."""
    log(f'--- AUTO-RECOVERY for {dead_run} (died at {dead_subrun}) ---')
    if os.path.exists(DISABLE_FILE):
        log(f'   {DISABLE_FILE} present — auto-recovery disabled, not restarting')
        return False

    ok, waited = wait_until(lambda: not daq_running(), STEP_TIMEOUT_S,
                            'the stopped run to exit')
    if not ok:
        log('   the run did not exit; NOT starting anything on top of it')
        return False
    log(f'   previous run exited after {waited:.0f}s; settling {RECOVER_SETTLE_S}s')
    time.sleep(RECOVER_SETTLE_S)

    # The VMM being gone is not enough — Dream must have finished too, or it
    # answers the new trigger with 409 and nothing starts.
    ok, waited = wait_until(dream_idle, DREAM_TEARDOWN_TIMEOUT_S,
                            'Dream to finish tearing down')
    if not ok:
        log('   Dream is still running; it would refuse the new trigger (409). '
            'Aborting recovery rather than leaving a half-started state.')
        return False
    log(f'   Dream idle after {waited:.0f}s')

    beam = get_json(f'{DREAM_FLASK}/beam/status') or {}
    if beam.get('beam_on') is False:
        log('   beam is OFF — not starting a follow-up run into no beam')
        return False

    # Hand the next generated config the remainder of the interrupted plan.
    cont = {'parent_run': dead_run, 'plan': plan_hint, 'start_at': dead_subrun,
            'reason': f'VMM readout produced no packets during {dead_subrun} '
                      f'in {dead_run}; auto-recovered by capture_guard'}
    try:
        with open(CONTINUATION_PATH, 'w') as f:
            json.dump(cont, f, indent=2)
        log(f'   wrote continuation: plan={plan_hint} start_at={dead_subrun}')
    except Exception as e:
        log(f'   could not write {CONTINUATION_PATH}: {e} — aborting recovery')
        return False

    # warm reset -> config apply -> (start = acq-on). See the note above.
    if not warm_reset_until_ready():
        log(f'   warm reset never reached 0 failed hybrids in {WARM_RESET_TRIES} '
            f'attempts — aborting recovery, THIS NEEDS A HUMAN')
        return False
    if not apply_chip_config():
        log('   chip config apply failed or left the run unarmed — aborting '
            'recovery rather than recording data at chip settings we cannot '
            'vouch for')
        return False

    started, r = False, None
    for attempt in range(1, START_TRIES + 1):
        post('/update_run_config_py')          # iterate to a fresh run number
        time.sleep(2)
        r = post('/run_config_py', {'dream': True})   # combined: Dream too
        started = bool(r and r.get('success'))
        log(f'   start attempt {attempt}/{START_TRIES}: {r}')
        if started:
            break
        # Most likely cause is Dream not being quite ready yet; give it a
        # moment rather than burning the recovery on one bad moment.
        time.sleep(30)
    if started:
        try:
            os.remove(CONTINUATION_PATH)   # consumed; do not affect later runs
        except FileNotFoundError:
            pass
        log(f'--- AUTO-RECOVERY OK: {r.get("run_name")} continues {dead_run} '
            f'from {dead_subrun} ---')
    else:
        log('--- AUTO-RECOVERY FAILED to start the follow-up run ---')
    return started


def main():
    log('capture_guard START — will stop the run on '
        f'{EMPTY_TRIP} consecutive closed empty pcapng files (>{EMPTY_MAX_BYTES} B = healthy)')
    log(f'watching {RUNS_DIR}')
    # This is a SERVICE started with the rest of the stack, so it must survive a
    # trip and re-arm for the next run rather than exiting. stopped_run holds the
    # run it already acted on, so it never fires twice for the same one.
    stopped_run = None
    recoveries = 0
    consecutive_errors = 0
    while True:
      # One bad iteration must not end the service. Anything unexpected — a
      # half-written file, a flask dying mid-request, a transient OSError — is
      # logged once and the loop carries on, because a guard that exits on the
      # first surprise stops protecting exactly when things are going wrong.
      try:
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
            r = stop_the_run()
            log(f'stop: {r}')
            stopped_run = run

            if not AUTO_RECOVER:
                log('auto-recovery disabled in config — stopping here')
            elif recoveries >= MAX_RECOVERIES:
                log(f'auto-recovery budget spent ({recoveries}/{MAX_RECOVERIES}) — '
                    f'NOT restarting again. The readout is failing repeatedly and '
                    f'THIS NEEDS A HUMAN.')
            else:
                recoveries += 1
                log(f'auto-recovery {recoveries}/{MAX_RECOVERIES}')
                if recover(run, sub, plan_of(run)):
                    stopped_run = None      # a new run is live; watch it too
            log('re-armed')
        time.sleep(POLL_S)
        consecutive_errors = 0
      except Exception as e:
        consecutive_errors += 1
        # Log the first few, then go quiet so a persistent fault cannot fill the
        # disk with the same line. The guard keeps running either way.
        if consecutive_errors <= 3:
            log(f'ALERT | iteration failed ({consecutive_errors}): '
                f'{type(e).__name__}: {e} — continuing to watch')
        elif consecutive_errors % 100 == 0:
            log(f'ALERT | still failing after {consecutive_errors} iterations: '
                f'{type(e).__name__}: {e}')
        time.sleep(POLL_S)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f'ALERT capture_guard CRASHED: {type(e).__name__}: {e}')
        raise
