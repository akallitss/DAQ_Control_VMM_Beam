#!/usr/bin/env python3
"""
Hold the P2Basket slow-control session lock while we use the test bench.

p2basket-sc keeps one lock per bench in /run/p2basket/session.lock (first line:
"uid start_epoch lifetime_s"). Behaviour, from the p2basket-sc SessionLock manual and
checked on dedippce185 on 2026-11-05 (build 2026-11-05T165959Z-d2cb9ef):

  p2basket-sc --session-lock N   lock for N minutes; the process stays in the
                                 foreground with a countdown, and removes the
                                 lock on expiry or on SIGINT ("CANCELED").
                                 Works with stdout piped (no tty needed).
     -> "SUCCESSFULLY LOCKED"    we got it
     -> "ALREADY OWN"            our uid already holds it (another process of
                                 ours); returns immediately, rc 0
     -> "Session locked by X"    another user holds it; returns immediately
  p2basket-sc --session-lock     probe: "Session is FREE" / "Session OWNED by X"

While we hold the lock, our own p2basket-sc commands and the p2basket-python
scripts (warm reset, config apply) run normally and do NOT release it; any
other user's commands that touch the electronics are refused. Without a lock,
each command takes a 1-minute lock and drops it at the end, so a long run was
unprotected between commands — hence this module.

SessionLockKeeper holds the lock for as long as we want it: it keeps one
`--session-lock <chunk>` child running and re-acquires if the chunk expires.
If another of our own processes holds the lock (ALREADY OWN — e.g. chip_config
just applied a config and handed over to the run), it waits for that lock to
end and takes over within POLL_S. If our process dies hard, the orphaned child
just expires after its chunk, so the bench is never locked forever.
"""

import logging
import os
import pwd
import signal
import subprocess
import threading
import time

DEFAULT_SC = '/local/p2/deploy/bin/p2basket-sc'
LOCK_FILE = '/run/p2basket/session.lock'
POLL_S = 2.0
START_TIMEOUT_S = 20.0


def _clean(text):
    """Countdown lines are refreshed with \\r: normalise to plain lines."""
    return [ln.strip() for ln in text.replace('\r', '\n').splitlines() if ln.strip()]


def lock_file_owner():
    """(uid, remaining_s) from the lock file, or None if there is no live lock.
    Cheap enough to poll; p2basket-sc stays the authority for acquiring."""
    try:
        with open(LOCK_FILE) as f:
            uid, start, seconds = f.readline().split()[:3]
        remaining = float(start) + float(seconds) - time.time()
        return (int(uid), remaining) if remaining > 0 else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def probe(sc=DEFAULT_SC, timeout=15):
    """('free' | 'mine' | 'other' | 'unknown', message) via p2basket-sc."""
    try:
        r = subprocess.run([sc, '--session-lock'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 'unknown', str(e)
    lines = _clean(r.stdout + r.stderr)
    msg = lines[-1] if lines else ''
    if 'FREE' in msg:
        return 'free', msg
    if 'OWNED by' in msg:
        me = pwd.getpwuid(os.getuid()).pw_name
        return ('mine' if f'OWNED by {me} ' in msg else 'other'), msg
    return 'unknown', msg


class SessionLockKeeper:
    """Hold the session lock until stop() / detach(). Use start() first."""

    def __init__(self, sc=DEFAULT_SC, chunk_min=60, tag='daq', log=None):
        self.sc = sc
        self.chunk_min = max(1, int(round(chunk_min)))
        self.tag = tag
        self.log = log or logging.getLogger('session_lock')
        self.state = 'idle'      # idle | held | waiting_own | blocked | stopped
        self.message = ''
        self._want = False
        self._proc = None
        self._first = threading.Event()
        self._cv = threading.Condition()
        self._thread = None

    # ---- public -------------------------------------------------------------
    def start(self, timeout=START_TIMEOUT_S):
        """Acquire. (ok, message): ok if we hold it, or our own uid already
        does (then we take over when that lock ends). Not ok if another user
        holds it or p2basket-sc cannot run — nothing is left running then."""
        if not os.path.exists(self.sc):
            return False, f'{self.sc} not found'
        self._want = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f'session-lock-{self.tag}')
        self._thread.start()
        if not self._first.wait(timeout):
            self.stop()
            return False, f'no answer from {self.sc} --session-lock within {timeout:.0f}s'
        if self.state in ('held', 'waiting_own'):
            return True, self.message
        self.stop()
        return False, self.message

    def stop(self):
        """Release now (SIGINT -> 'Session CANCELED', lock file removed)."""
        self._want = False
        with self._cv:
            self._cv.notify_all()
        p = self._proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._thread:
            self._thread.join(timeout=10)
        if self.state == 'held':
            self.log.info(f'[session lock] {self.tag}: released')
        self.state = 'stopped'

    def detach(self):
        """Stop renewing but leave the current lock to expire on its own
        (hand-over window for the next step, e.g. apply -> run start)."""
        self._want = False
        with self._cv:
            self._cv.notify_all()
        p = self._proc
        self._proc = None
        if p and p.poll() is None and self.state == 'held':
            # The drain thread started in _acquire_once keeps reading its
            # countdown, so the child can run on to expiry unattended.
            self.log.info(f'[session lock] {self.tag}: left to expire in '
                          f'<= {self.chunk_min} min (hand-over)')
        if self._thread:
            self._thread.join(timeout=10)
        self.state = 'stopped'

    # ---- internals ----------------------------------------------------------
    def _set(self, state, message, log_level=logging.INFO):
        changed = (state, message) != (self.state, self.message)
        self.state, self.message = state, message
        if changed:
            self.log.log(log_level, f'[session lock] {self.tag}: {state} — {message}')
        self._first.set()

    def _sleep(self, s):
        with self._cv:
            if self._want:
                self._cv.wait(s)

    def _acquire_once(self):
        """Start one `--session-lock chunk` child and classify the answer."""
        p = subprocess.Popen([self.sc, '--session-lock', str(self.chunk_min)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._proc = p          # so stop() can interrupt a hung acquire
        seen = []
        deadline = time.time() + START_TIMEOUT_S
        for line in iter(p.stdout.readline, ''):     # readline splits on \r too
            line = line.strip()
            if line:
                seen.append(line)
            if 'SUCCESSFULLY LOCKED' in line:
                # Keep draining the countdown so the pipe never fills.
                threading.Thread(target=lambda: [None for _ in p.stdout],
                                 daemon=True).start()
                return 'held', f'locked for {self.chunk_min} min'
            if time.time() > deadline:
                break
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        text = ' | '.join(seen[-3:])
        if 'ALREADY OWN' in text:
            return 'waiting_own', 'our account already holds the lock — taking over when it ends'
        if 'locked by' in text.lower():
            err = next((s for s in seen if 'locked by' in s.lower()), text)
            return 'blocked', err.lstrip('* ').strip()
        return 'blocked', f'unexpected p2basket-sc answer (rc={p.returncode}): {text}'

    def _loop(self):
        while self._want:
            try:
                state, msg = self._acquire_once()
            except OSError as e:
                state, msg = 'blocked', str(e)
            if not self._want:          # stop() raced with the acquire
                p = self._proc
                if p and p.poll() is None:
                    p.send_signal(signal.SIGINT)
                break
            self._set(state, msg, logging.INFO if state != 'blocked' else logging.WARNING)
            if state == 'held':
                p = self._proc
                while self._want and p.poll() is None:
                    self._sleep(POLL_S)
                if self._want:
                    self.log.info(f'[session lock] {self.tag}: chunk ended '
                                  f'(rc={p.returncode}) — renewing')
                continue
            if state == 'waiting_own':
                while self._want and lock_file_owner() is not None:
                    self._sleep(POLL_S)
                continue
            self._sleep(10)             # blocked: retry quietly, still report it


if __name__ == '__main__':
    # Manual check: `python3 session_lock.py [minutes]` holds the lock until Ctrl-C.
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    print(probe())
    k = SessionLockKeeper(chunk_min=float(sys.argv[1]) if len(sys.argv) > 1 else 1, tag='manual')
    print(k.start())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        k.stop()
    print(probe())
