#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lab HV (P2 chambers on the banco USB supplies) for unattended tests: slow ramp, guard, ramp down.

Everything goes through banco's validated API (banco_hv/hv_serial_live.py: limits, action log),
exactly like hv_dream_shim.py does for runs: URL, token and control password from the deployed
/local/p2/DAQ_Control_VMM_Beam/config/dream_bridge.json. Voltages are magnitudes (both modules
are negative polarity).

    hv_lab.py state                          # one line per channel
    hv_lab.py ramp-up   [--rup 5] [--hold 120] [--stages 0.5,0.8,0.9,0.95,1.0]
    hv_lab.py check-on                       # exit 0 if all six are ON and at their setpoint
    hv_lab.py guard --until 2026-11-17T08:00 [--on-trip detector-off|retry|retry-detector-off|all-off]
                    [--retries 1] [--end leave|down]
        retry-detector-off (user, 19 Nov): a tripped channel is re-ramped --retries times; the next
        trip powers off its whole detector (mesh and drift), as detector-off does
    hv_lab.py ramp-down [--rdw 10]

Targets: the operating point of /local/p2/p2-setups/lab_saclay_2026.yaml (run on 3-5 Nov 2026),
the low end of what was run. ramp-up stops and ramps everything down if a channel trips or a
current stays above --i-max uA (median over the hold) at any stage.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BRIDGE = '/local/p2/DAQ_Control_VMM_Beam/config/dream_bridge.json'
LOGDIR = os.path.expanduser('~/trim_work/out/hv_lab')
# banco hw name -> (label, operating V)  [setup lab_saclay_2026.yaml, hv.operating]
TARGETS = {
    'N1471:0': ('P2_IN_Drift', 720.0),
    'N1471:1': ('P2_IN_Mesh', 420.0),
    'N1471:2': ('P2_OUT_Drift', 730.0),
    'N1471:3': ('P2_OUT_Mesh', 430.0),
    'NHR:0': ('P2_MID_Mesh', 430.0),
    'NHR:1': ('P2_MID_Drift', 730.0),
}
TOL_V = 3.0
BAD_WORDS = ('trip', 'ovc', 'ovv', 'unv', 'ilim', 'kill', 'maxv', 'disabled', 'error', 'fail')

LOGF = None


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + '\n')
        LOGF.flush()


def act(entry):
    os.makedirs(LOGDIR, exist_ok=True)
    with open(os.path.join(LOGDIR, 'hv_lab_actions.jsonl'), 'a') as f:
        f.write(json.dumps(dict(entry, time=datetime.now().isoformat(timespec='seconds'))) + '\n')


# --------------------------------------------------------------------------- banco API --
def _bridge():
    with open(BRIDGE) as f:
        return json.load(f)


def banco(path, body=None, timeout=30):
    b = _bridge()
    url = b['hv_live_url'].rstrip('/')
    assert url.endswith('/hv_live'), url
    url = url[:-len('/hv_live')] + '/' + path
    if b.get('hv_live_token'):
        url += '?' + urllib.parse.urlencode({'token': b['hv_live_token']})
    data, headers = None, {'X-HV-Client': 'vmm-daq hv_lab.py (overnight tests)'}
    if body is not None:
        data = json.dumps(dict(body, password=b['hv_control'].get('password', ''))).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {'ok': False, 'message': 'HTTP %s' % e.code}


def state():
    """{hw: channel dict} for the six P2 channels; raises if banco cannot be read (3 tries)."""
    for attempt in range(3):
        try:
            st = banco('api/state')
            if st.get('channels'):
                break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
        time.sleep(5)
    ch = {c['hw']: c for c in st.get('channels', [])}
    missing = [hw for hw in TARGETS if hw not in ch]
    if missing:
        raise RuntimeError('banco does not list %s (%s)' % (missing, st.get('errors') or st.get('message')))
    for hw, (label, _) in TARGETS.items():
        if ch[hw].get('label') != label:
            raise RuntimeError('channel map changed: %s is %r, expected %r' % (hw, ch[hw].get('label'), label))
    return {hw: ch[hw] for hw in TARGETS}, st.get('errors') or {}


def setp(hw, param, value):
    r = banco('api/set', {'hw': hw, 'param': param, 'value': value})
    act({'hw': hw, 'label': TARGETS[hw][0], 'param': param, 'value': value, 'ok': r.get('ok'),
         'message': r.get('message')})
    if not r.get('ok'):
        raise RuntimeError('%s %s=%s refused: %s' % (TARGETS[hw][0], param, value, r.get('message')))
    return r


def power_off(hw):
    """Send power=0 to one channel. Never raises: True if the supply took the command.

    banco checks the channel right after the command, so a CAEN N1471 channel that is ramping down
    answers 'NOT APPLIED ... still on (status 5)' (5 = ON + ramping down). That is the command
    working, not a refusal: the 17 Nov 20:00 ramp-down stopped on it and left 5 channels at HV."""
    try:
        setp(hw, 'power', 0)
        return True
    except Exception as e:  # noqa: BLE001 - one channel must never stop the others going off
        if hw.startswith('N1471') and off_accepted(str(e)):
            log('%s: ramping down (supply replied: still on, status bit RDW set)' % TARGETS[hw][0])
            return True
        log('%s power=0 NOT taken: %s' % (TARGETS[hw][0], e))
        return False


# ------------------------------------------------------------------------- pure logic --
CAEN_RDW = 4   # CAEN channel status bit 2: ramping down


def off_accepted(message):
    """A CAEN power=0 reply that banco flags as not applied, but whose status says ramping down
    (the iseg NHR has other status bits: not used for it)."""
    m = re.search(r'still on \(status (\d+)\)', message or '')
    return bool(m) and bool(int(m.group(1)) & CAEN_RDW)


def bad_status(c):
    """A channel that is supposed to be ON: why it is not fine (None if fine)."""
    s = str(c.get('status_text', '')).lower()
    if not int(c.get('power', 0)):
        return 'OFF (%s)' % (c.get('status_text') or c.get('status'))
    if any(w in s for w in BAD_WORDS):
        return 'status %s' % c.get('status_text')
    return None


def detector(hw):
    """'P2_IN' for 'N1471:0' (label P2_IN_Drift)."""
    return TARGETS[hw][0].rsplit('_', 1)[0]


GIVEN_UP = os.path.join(LOGDIR, 'GIVEN_UP.json')


def write_given_up(hws):
    os.makedirs(LOGDIR, exist_ok=True)
    with open(GIVEN_UP, 'w') as f:
        json.dump({'hw': sorted(hws), 'detectors': sorted({detector(h) for h in hws}),
                   'time': datetime.now().isoformat(timespec='seconds')}, f)


def read_given_up():
    try:
        with open(GIVEN_UP) as f:
            return set(json.load(f)['hw'])
    except (OSError, ValueError, KeyError):
        return set()


def all_at_target(ch, skip=()):
    """Every channel not in skip ON at its setpoint, and at least one detector left."""
    live = [hw for hw in TARGETS if hw not in skip]
    return bool(live) and all(at_target(ch[hw], TARGETS[hw][1]) for hw in live)


def at_target(c, v, tol=TOL_V):
    return int(c.get('power', 0)) == 1 and abs(float(c.get('vmon', -1e9)) - v) <= tol


def stage_values(frac, targets=TARGETS):
    return {hw: round(frac * v, 1) for hw, (_, v) in targets.items()}


def summary(chans):
    return ' | '.join('%s %s %.1f/%.0f V %.3f uA %s' % (TARGETS[hw][0], 'ON ' if int(c.get('power', 0)) else 'off',
                                                        float(c.get('vmon', 0)), float(c.get('v0', 0)),
                                                        float(c.get('imon', 0)), c.get('status_text', ''))
                      for hw, c in chans.items())


# ----------------------------------------------------------------------------- actions --
def persistent(chans, since, persist_s=30.0):
    """{hw: why} for channels that tripped, or have been in a bad status for >= persist_s
    (a discharge gives a short UNV); since: {hw: first time seen bad}, updated in place."""
    out = {}
    for hw, c in chans.items():
        if not bad_status(c):
            since.pop(hw, None)
            continue
        t = since.setdefault(hw, time.time())
        if is_trip(c) or time.time() - t >= persist_s:
            out[hw] = bad_status(c)
        elif t == since[hw] and time.time() - t < 1:
            log('transient: %s %s (%.0f V, %.2f uA)' % (TARGETS[hw][0], bad_status(c),
                                                       float(c.get('vmon', 0)), float(c.get('imon', 0))))
    return out


class HVProblem(RuntimeError):
    def __init__(self, msg, hws):
        super().__init__(msg)
        self.hws = set(hws)


def wait_at(values, timeout_s, poll=5):
    """Wait until every channel of values reads back ON within TOL_V; raises HVProblem(hws)."""
    t0, last, since = time.time(), 0, {}
    while time.time() - t0 < timeout_s:
        ch, err = state()
        bad = persistent({hw: ch[hw] for hw in values}, since)
        # a channel just switched on may need a few s to read ON
        if bad and time.time() - t0 > 20:
            raise HVProblem('not ON while ramping: %s' % {TARGETS[h][0]: v for h, v in bad.items()}, bad)
        if all(at_target(ch[hw], v) for hw, v in values.items()):
            return ch
        if time.time() - last > 30:
            log('ramping: ' + summary(ch))
            last = time.time()
        time.sleep(poll)
    away = [hw for hw, v in values.items() if not at_target(ch[hw], v)]
    raise HVProblem('not at target after %.0f s: %s' % (timeout_s, [TARGETS[h][0] for h in away]), away)


def hold(seconds, i_max, active, poll=5):
    """Hold, sampling currents of the active channels; HVProblem on a trip or a median above i_max."""
    t0, samples, since = time.time(), {hw: [] for hw in active}, {}
    while time.time() - t0 < seconds:
        ch, _ = state()
        bad = persistent({hw: ch[hw] for hw in active}, since)
        if bad:
            raise HVProblem('%s during the hold' % {TARGETS[h][0]: v for h, v in bad.items()}, bad)
        for hw in active:
            samples[hw].append(float(ch[hw].get('imon', 0)))
        time.sleep(poll)
    med = {hw: sorted(v)[len(v) // 2] for hw, v in samples.items() if v}
    mx = {hw: max(v) for hw, v in samples.items() if v}
    log('hold %.0f s: current median/max uA ' % seconds +
        ', '.join('%s %.3f/%.3f' % (TARGETS[hw][0], med[hw], mx[hw]) for hw in med))
    high = [hw for hw in med if med[hw] > i_max]
    if high:
        raise HVProblem('current above %.1f uA (median over the hold): %s'
                        % (i_max, {TARGETS[h][0]: med[h] for h in high}), high)
    return med, mx


def detector_off(hws, given_up):
    """Power off every electrode of the detectors of hws; add them to given_up (and the file)."""
    for det in sorted({detector(hw) for hw in hws}):
        log('%s: powered OFF (mesh and drift), left off; the others carry on' % det)
        for hw in TARGETS:
            if detector(hw) == det:
                power_off(hw)
                given_up.add(hw)
    write_given_up(given_up)


def ramp_down(rdw=10.0, timeout_s=300, poll=5):
    """Every channel off at rdw V/s. Never stops on one channel: each gets the off command, a
    channel still powered whose vmon is not falling gets it again, and the result is checked on vmon.
    Returns True if every channel is below 20 V at the end, else logs RAMP-DOWN INCOMPLETE."""
    ch, _ = state()
    for hw, c in ch.items():
        if 'rdw' in c.get('settable', []) and abs(float(c.get('rdw', 0)) - rdw) > 0.01:
            try:
                setp(hw, 'rdw', rdw)
            except Exception as e:  # noqa: BLE001
                log('rdw not set: %s' % e)
    for hw, c in ch.items():
        if int(c.get('power', 0)):
            power_off(hw)
    t0 = time.time()
    last_v = {}
    while True:
        try:
            ch, _ = state()
        except Exception as e:  # noqa: BLE001 - banco unreadable for a moment: keep waiting
            log('banco not readable during ramp-down: %s' % e)
            ch = None
        if ch is not None:
            if all(float(c.get('vmon', 0)) < 20 for c in ch.values()):
                log('ramped down: ' + summary(ch))
                return True
            if time.time() - t0 >= timeout_s:
                break
            for hw, c in ch.items():
                v = float(c.get('vmon', 0))
                if int(c.get('power', 0)) and v >= 20 and hw in last_v and v > last_v[hw] - 1.0:
                    log('%s still on at %.0f V and not falling: off command again' % (TARGETS[hw][0], v))
                    power_off(hw)
                last_v[hw] = v
        elif time.time() - t0 >= timeout_s:
            break
        time.sleep(poll)
    log('RAMP-DOWN INCOMPLETE after %.0f s: %s' % (timeout_s, summary(ch) if ch else 'banco unreadable'))
    return False


def is_trip(c):
    """The supply itself switched the channel off, or reports a trip/kill."""
    st = str(c.get('status_text', '')).lower()
    return not int(c.get('power', 0)) or 'trip' in st or 'kill' in st


def ramp_up(stages, rup, hold_s, i_max, timeout_s=900, only=None):
    """Staged ramp of all six channels. A channel that trips, does not reach its stage or draws
    more than i_max: its detector is powered off and the ramp goes on with the others (user rule,
    16 Nov). Raises only if no detector is left."""
    ch, err = state()
    log('before: ' + summary(ch))
    if err:
        raise RuntimeError('banco reports errors: %s' % err)
    given_up = read_given_up()
    for hw, c in ch.items():
        if only and detector(hw) not in only:
            continue
        if 'rup' in c.get('settable', []) and abs(float(c.get('rup', 0)) - rup) > 0.01:
            setp(hw, 'rup', rup)
    for frac in stages:
        while True:
            active = [hw for hw in TARGETS if hw not in given_up and (not only or detector(hw) in only)]
            if not active:
                raise RuntimeError('every detector is off')
            vals = {hw: v for hw, v in stage_values(frac).items() if hw in active}
            log('stage %.0f %%: %s' % (100 * frac, {TARGETS[hw][0]: v for hw, v in vals.items()}))
            ch, _ = state()
            for hw, v in vals.items():
                if abs(float(ch[hw].get('v0', -1)) - v) > 0.05:
                    setp(hw, 'v0', v)
            for hw in vals:
                if not int(ch[hw].get('power', 0)):
                    setp(hw, 'power', 1)
            try:
                wait_at(vals, timeout_s)
                hold(hold_s, i_max, active)
                break
            except HVProblem as e:
                log('PROBLEM at stage %.0f %%: %s' % (100 * frac, e))
                detector_off(e.hws, given_up)
    ch, _ = state()
    log('at the operating point: ' + summary(ch) +
        (' | OFF after a problem: %s' % sorted({detector(h) for h in given_up}) if given_up else ''))


def guard(until, on_trip, retries, end, poll=10, i_warn=3.0, persist_s=30.0):
    """Watch the six channels until `until`; handle trips; then leave the HV or ramp it down."""
    os.makedirs(LOGDIR, exist_ok=True)
    csvp = os.path.join(LOGDIR, 'guard_%s.csv' % datetime.now().strftime('%Y%m%d_%H%M'))
    used = {hw: 0 for hw in TARGETS}
    given_up = read_given_up()      # detectors switched off during the ramp stay off
    flag = os.path.join(LOGDIR, 'GUARD_STATUS')
    last_ok_log, high_since, bad_since = 0, {}, {}
    regrace = {}     # hw -> end of the grace after a re-ramp (only a real trip counts while it climbs)
    with open(csvp, 'a') as f:
        f.write('time,' + ','.join('%s_vmon,%s_imon,%s_power' % ((TARGETS[hw][0],) * 3) for hw in TARGETS) + '\n')
        while datetime.now() < until:
            if os.path.exists(os.path.join(LOGDIR, 'GUARD_STOP')):
                log('GUARD_STOP file found: guard ends, HV left as it is')
                return
            try:
                ch, err = state()
            except Exception as e:  # noqa: BLE001 - banco unreachable: log, keep trying
                log('banco not readable: %s' % e)
                time.sleep(poll)
                continue
            f.write(datetime.now().isoformat(timespec='seconds') + ',' + ','.join(
                '%s,%s,%s' % (c.get('vmon'), c.get('imon'), c.get('power')) for c in ch.values()) + '\n')
            f.flush()
            # act on a trip at once; on any other bad status (UNV during a discharge, OVC ...) only if
            # it lasts >= persist_s (16/17 Nov: a 10 s UNV sag of P2_OUT_Mesh, 4.8 uA, is a discharge)
            problems = {}
            for hw, c in ch.items():
                if hw in given_up or not bad_status(c):
                    bad_since.pop(hw, None)
                    continue
                if time.time() < regrace.get(hw, 0) and not is_trip(c):
                    continue
                t_bad = bad_since.setdefault(hw, time.time())
                if is_trip(c) or time.time() - t_bad >= persist_s:
                    problems[hw] = bad_status(c)
                elif time.time() - t_bad < poll:
                    log('transient: %s %s (%.0f V, %.2f uA) -- acting only if it lasts %.0f s'
                        % (TARGETS[hw][0], bad_status(c), float(c.get('vmon', 0)), float(c.get('imon', 0)), persist_s))
            for hw, c in ch.items():
                if float(c.get('imon', 0)) > i_warn:
                    high_since.setdefault(hw, time.time())
                    if time.time() - high_since[hw] > 120 and int(time.time()) % 300 < poll:
                        log('WARNING %s current %.2f uA for %.0f s' % (TARGETS[hw][0], float(c['imon']),
                                                                    time.time() - high_since[hw]))
                else:
                    high_since.pop(hw, None)
            if problems:
                log('PROBLEM: %s | %s' % ({TARGETS[hw][0]: p for hw, p in problems.items()}, summary(ch)))
                if on_trip == 'all-off':
                    ramp_down()
                    with open(flag, 'w') as g:
                        g.write('ALL_OFF after a trip %s\n' % datetime.now().isoformat(timespec='seconds'))
                    return
                if on_trip == 'detector-off':
                    # user, 16 Nov: a trip -> power off that detector (mesh and drift), carry on with the rest
                    detector_off(problems, given_up)
                    continue
                for hw in problems:
                    if hw in given_up:
                        continue
                    if used[hw] >= retries:
                        if on_trip == 'retry-detector-off':
                            log('%s: tripped again (retries used: %d) -> its detector OFF' % (TARGETS[hw][0], used[hw]))
                            detector_off({hw}, given_up)
                            continue
                        log('%s: tripped again, left OFF (retries used: %d)' % (TARGETS[hw][0], used[hw]))
                        if int(ch[hw].get('power', 0)):
                            power_off(hw)
                        given_up.add(hw)
                        continue
                    used[hw] += 1
                    log('%s: re-ramp %d of %d after 60 s' % (TARGETS[hw][0], used[hw], retries))
                    time.sleep(60)
                    try:
                        setp(hw, 'v0', TARGETS[hw][1])
                        setp(hw, 'power', 1)
                        regrace[hw] = time.time() + 300
                        bad_since.pop(hw, None)
                    except RuntimeError as e:
                        log('re-ramp refused: %s' % e)
                with open(flag, 'w') as g:
                    g.write('given_up=%s used=%s %s\n' % (sorted(TARGETS[h][0] for h in given_up),
                                                          {TARGETS[h][0]: n for h, n in used.items() if n},
                                                          datetime.now().isoformat(timespec='seconds')))
            elif time.time() - last_ok_log > 1800:
                log('ok: ' + summary(ch))
                last_ok_log = time.time()
            time.sleep(poll)
    log('guard end time reached (%s): %s' % (until, 'ramping down' if end == 'down' else 'HV left on'))
    if end == 'down':
        ramp_down()


def main():
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('state')
    sub.add_parser('check-on')
    s = sub.add_parser('ramp-up')
    s.add_argument('--rup', type=float, default=5.0)
    s.add_argument('--hold', type=float, default=120.0)
    s.add_argument('--stages', default='0.5,0.8,0.9,0.95,1.0')
    s.add_argument('--i-max', type=float, default=1.0, help='uA, median over each hold')
    s.add_argument('--only', help='comma list of detectors (P2_IN,P2_MID,P2_OUT) to ramp; the others are not touched')
    s = sub.add_parser('ramp-down')
    s.add_argument('--rdw', type=float, default=10.0)
    s = sub.add_parser('guard')
    s.add_argument('--until', required=True, help='ISO time, e.g. 2026-11-17T08:00')
    s.add_argument('--on-trip', choices=('detector-off', 'retry', 'retry-detector-off', 'all-off'), default='detector-off')
    s.add_argument('--retries', type=int, default=1)
    s.add_argument('--end', choices=('leave', 'down'), default='leave')
    a = ap.parse_args()
    os.makedirs(LOGDIR, exist_ok=True)
    LOGF = open(os.path.join(LOGDIR, 'hv_lab.log'), 'a')
    if a.cmd == 'state':
        ch, err = state()
        for hw, c in ch.items():
            print('%-8s %-13s power %s vmon %7.1f v0 %6.1f imon %6.3f uA iset %s rup %s rdw %s trip %s %s'
                  % (hw, TARGETS[hw][0], c.get('power'), float(c.get('vmon', 0)), float(c.get('v0', 0)),
                     float(c.get('imon', 0)), c.get('iset'), c.get('rup'), c.get('rdw'), c.get('trip'),
                     c.get('status_text')))
        if err:
            print('errors:', err)
    elif a.cmd == 'check-on':
        ch, _ = state()
        skip = read_given_up()
        ok = all_at_target(ch, skip)
        print(('ON at target' + (' (given up after a trip: %s)' % sorted(skip) if skip else ''))
              if ok else 'NOT at target: ' + summary(ch))
        sys.exit(0 if ok else 1)
    elif a.cmd == 'ramp-up':
        log('ramp-up: stages %s, rup %.1f V/s, hold %.0f s, i-max %.1f uA' % (a.stages, a.rup, a.hold, a.i_max))
        try:
            ramp_up([float(x) for x in a.stages.split(',')], a.rup, a.hold, a.i_max,
                    only=set(a.only.split(',')) if a.only else None)
        except Exception as e:  # noqa: BLE001
            if a.only:
                log('RAMP-UP (only %s) STOPPED: %s -- those detectors off, the others untouched' % (a.only, e))
                ch, _ = state()
                for hw in TARGETS:
                    if detector(hw) in a.only.split(',') and int(ch[hw].get('power', 0)):
                        power_off(hw)
            else:
                log('RAMP-UP STOPPED: %s -- ramping everything down' % e)
                ramp_down()
            sys.exit(2)
    elif a.cmd == 'ramp-down':
        log('ramp-down requested')
        sys.exit(0 if ramp_down(a.rdw) else 1)
    elif a.cmd == 'guard':
        until = datetime.fromisoformat(a.until)
        log('guard until %s, on trip: %s (retries %d), at the end: %s' % (until, a.on_trip, a.retries, a.end))
        guard(until, a.on_trip, a.retries, a.end)


if __name__ == '__main__':
    main()
