"""Tests of the threshold-above-baseline scan logic and the lab HV helpers (no hardware)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fastscan'))
import baseline_scan as BS  # noqa: E402
import hv_lab as HV  # noqa: E402
import vmm_trim as vt  # noqa: E402

EQ = """# eq
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi1   Field sdt 53
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi2   Field sdt  3 # 245
Sys 0 Bec 0 Hyb 1 Vmm 1 Reg bank1spi1   Field sdt 34
Sys 0 Bec 0 Hyb 1 Vmm 1 Reg bank1spi2   Field sdt  3 # 226
""".splitlines()


def _map():
    return {'baseline': {'2': {'0': 160, '1': 170, '2': 165, '3': 40}, '3': {'0': 150, '1': 152}},
            'threshold': {'2': {'0': 175, '1': 186, '2': 180, '3': 60}, '3': {'0': 165, '1': 168}},
            'sdt': {'2': 245, '3': 226}}


def test_chip_x0_ignores_odd_baselines():
    x0, d = BS.chip_x0(_map(), [2, 3])
    assert x0[2] == 15 and d[2][3] == 20          # ch3 (baseline 40 mV) listed, not in the median
    assert x0[3] == 16


def test_sdt_for_and_distances():
    m = _map()
    x0, d = BS.chip_x0(m, [2, 3])
    k = {2: 0.8, 3: 0.75}
    sdt_eq = {2: 245, 3: 226}
    s = BS.sdt_for(40, sdt_eq, x0, k)
    assert s == {2: 245 + round(25 / 0.8), 3: 226 + round(24 / 0.75)}
    dist = BS.distances(d, sdt_eq, s, k)
    assert abs(dist[2][0] - (15 + (s[2] - 245) * 0.8)) < 1e-6
    assert abs(sorted(dist[2].values())[1] - 40) < 0.5   # chip median lands at X within half a step
    s3 = BS.sdt_for(3, sdt_eq, x0, k)
    assert s3[2] < 245 and s3[3] < 226


def test_point_file_sets_sdt_masks_and_left():
    ext = vt.ExtFile(list(EQ))
    out, used = BS.point_file(ext, {2: 270, 3: 250}, [2, 3], {2: {5}, 3: set()}, {3}, {2: 245, 3: 226})
    assert used == {2: 270, 3: 266}
    assert out.sdt()[2] == 270 and out.sdt()[3] == 266
    sm = out.channel_bits('sm')
    assert sm[2] == [5] and len(sm[3]) == 64


def test_stall_and_heartbeat():
    assert BS.stalled({'live_s': 0.0, 'hits': {}})
    assert BS.stalled({'live_s': 30.0, 'hits': {'2': {'1': 5}}})
    assert not BS.stalled({'live_s': 30.0, 'hits': {'0': {'41': 10000}}})
    assert not BS.stalled({'live_s': 30.0, 'hits': {'5': {'1': 5000}}})   # heartbeat quiet but data flow


def test_seconds_grow_with_x():
    assert BS.seconds_for(40) == 600 and BS.seconds_for(20) == 180 and BS.seconds_for(12) == 60
    assert BS.seconds_for(3) == 30


def test_hv_status_logic():
    on = {'power': 1, 'vmon': 719.0, 'v0': 720.0, 'status_text': 'ok'}
    assert HV.bad_status(on) is None and HV.at_target(on, 720.0)
    assert HV.bad_status({'power': 0, 'status_text': 'trip'}).startswith('OFF')
    assert HV.bad_status({'power': 1, 'status_text': 'OVC'}) == 'status OVC'
    assert not HV.at_target({'power': 1, 'vmon': 700.0}, 720.0)
    st = HV.stage_values(0.5)
    assert st['N1471:0'] == 360.0 and st['NHR:0'] == 215.0
    assert max(HV.stage_values(1.0).values()) == 730.0


def test_detector_and_given_up():
    assert HV.detector('N1471:0') == 'P2_IN' and HV.detector('NHR:1') == 'P2_MID'
    ch = {hw: {'power': 1, 'vmon': v} for hw, (_, v) in HV.TARGETS.items()}
    assert HV.all_at_target(ch)
    ch['N1471:0'] = {'power': 0, 'vmon': 0.0}
    assert not HV.all_at_target(ch)
    assert HV.all_at_target(ch, skip={'N1471:0', 'N1471:1'})
    assert not HV.all_at_target(ch, skip=set(HV.TARGETS))     # nothing left is not "ok"


def test_discharge_is_not_a_trip(monkeypatch):
    import time as _t
    monkeypatch.setattr(HV, 'log', lambda m: None)
    unv = {'power': 1, 'vmon': 404.0, 'imon': 4.8, 'status_text': 'UNV'}
    since = {}
    assert HV.persistent({'N1471:3': unv}, since) == {}          # first sight of a sag: wait
    since['N1471:3'] = _t.time() - 31
    assert 'N1471:3' in HV.persistent({'N1471:3': unv}, since)   # lasts 30 s: act
    assert HV.persistent({'N1471:3': dict(unv, status_text='ok', vmon=430.0)}, since) == {}
    assert 'N1471:3' not in since                                # recovered: forgotten
    tripped = {'power': 0, 'vmon': 0.0, 'imon': 0.0, 'status_text': 'ok'}
    assert 'N1471:3' in HV.persistent({'N1471:3': tripped}, {})  # supply off by itself: at once
    assert HV.is_trip({'power': 1, 'status_text': 'TRIP'}) and not HV.is_trip(unv)


def test_capture_point_recovers_from_a_stall(monkeypatch, tmp_path):
    seq = [(0.0, {'live_s': 0.0, 'hits': {}}),                         # stalled chunk
           (5.0, {'live_s': 60.0, 'hits': {'0': {'41': 20000}, '2': {'3': 10}}}),
           (5.0, {'live_s': 60.0, 'hits': {'0': {'41': 20000}, '2': {'3': 5}}})]
    calls = {'apply': 0}
    monkeypatch.setattr(BS, 'capture_chunk', lambda p, s, m: seq.pop(0))
    monkeypatch.setattr(BS.NP, 'select_and_apply', lambda name: calls.__setitem__('apply', calls['apply'] + 1))
    monkeypatch.setattr(BS.shutil, 'copy', lambda a, b: None)
    hits, live, mb, nrec = BS.capture_point('x.txt', 120, 60, 300, str(tmp_path), 'p00')
    assert nrec == 1 and calls['apply'] == 1
    assert live == 120.0 and hits['2']['3'] == 15 and mb == 10.0


def test_repeat_points_use_the_masks_of_the_sweep_at_that_x(monkeypatch, tmp_path):
    import types
    monkeypatch.setattr(BS, 'hv_gate', lambda mode, w: (True, 'off'))
    monkeypatch.setattr(BS.FR, 'vmm_write', lambda p: 3.8)
    monkeypatch.setattr(BS.FR, 'links', lambda: {h: 4 for h in range(10)})
    monkeypatch.setattr(BS, 'log', lambda m: None)
    seen = []

    def cap(path, secs, chunk, mb, out, i):
        ext = vt.ExtFile.load(path)
        seen.append((i, sorted(ext.channel_bits('sm').get(2, []))))
        # channel 7 of chip 2 is above 10 kHz at every point -> masked after its first point
        return {'0': {'41': 1000}, '2': {'7': 200000, '1': 10}}, 10.0, 1.0, 0
    monkeypatch.setattr(BS, 'capture_point', cap)
    a = types.SimpleNamespace(hv='off', hv_wait_s=0, time_scale=0.01, chunk_s=60, max_mb=300, mask_hz=1e4,
                              stuck_min_hz=1e3, leave_at=60, chip_max_hz=1e6)
    eq = vt.ExtFile(list(EQ))
    d_eq = {2: {ch: 15.0 for ch in range(64)}, 3: {ch: 15.0 for ch in range(64)}}
    masked, hist = BS.run_sweep(a, eq, [2, 3], {2: 245, 3: 226}, {2: 15.0, 3: 15.0}, {2: 0.8, 3: 0.8},
                                d_eq, [30, 20], str(tmp_path), 'p', {2: set(), 3: set()}, set())
    assert masked[2] == {7} and hist[30][2] == set() and hist[20][2] == {7}
    seen.clear()
    BS.run_sweep(a, eq, [2, 3], {2: 245, 3: 226}, {2: 15.0, 3: 15.0}, {2: 0.8, 3: 0.8},
                 d_eq, [30, 20], str(tmp_path), 'r', masked, set(), history=hist)
    assert seen == [('r00', []), ('r01', [7])]


def test_off_accepted_reads_caen_rdw_bit():
    msg = 'P2_IN_Drift: NOT APPLIED: module accepted the command but the channel is still on (status 5)'
    assert HV.off_accepted(msg)                                       # 5 = ON + ramping down
    assert not HV.off_accepted(msg.replace('status 5', 'status 1'))   # on, not ramping: a real refusal
    assert not HV.off_accepted('P2_IN_Drift: permission denied')
    assert not HV.off_accepted(None)


class _FakeSupplies:
    """Six channels at HV. A CAEN power=0 is answered like banco did on 17 Nov 20:00 (not ok,
    'still on (status 5)'); each state() call then lowers vmon by 200 V."""

    def __init__(self, refuse=()):
        self.ch = {hw: {'power': 1, 'vmon': v, 'rdw': 10.0, 'settable': ['rdw']}
                   for hw, (_, v) in HV.TARGETS.items()}
        self.ramping, self.refuse, self.sent = set(), set(refuse), []

    def banco(self, path, body=None, timeout=30):
        hw = body['hw']
        if body['param'] != 'power':
            return {'ok': True}
        self.sent.append(hw)
        if hw in self.refuse:
            return {'ok': False, 'message': 'interlock'}
        self.ramping.add(hw)
        if hw.startswith('N1471'):
            return {'ok': False, 'message': '%s: NOT APPLIED: module accepted the command but the '
                    'channel is still on (status 5)' % HV.TARGETS[hw][0]}
        return {'ok': True}

    def state(self):
        for hw in self.ramping:
            c = self.ch[hw]
            c['vmon'] = max(0.0, c['vmon'] - 200.0)
            if c['vmon'] == 0.0:
                c['power'] = 0
        return {hw: dict(c) for hw, c in self.ch.items()}, {}


def _patch(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(HV, 'LOGDIR', str(tmp_path))
    monkeypatch.setattr(HV, 'log', lambda m: None)
    monkeypatch.setattr(HV, 'banco', fake.banco)
    monkeypatch.setattr(HV, 'state', fake.state)
    monkeypatch.setattr(HV.time, 'sleep', lambda s: None)


def test_ramp_down_goes_on_after_status5_reply(monkeypatch, tmp_path):
    fake = _FakeSupplies()
    _patch(monkeypatch, tmp_path, fake)
    assert HV.ramp_down() is True
    assert set(fake.sent) == set(HV.TARGETS)                 # every channel got the off command
    assert all(c['vmon'] < 20 for c in fake.ch.values())


def test_ramp_down_reports_a_channel_that_stays_on(monkeypatch, tmp_path):
    fake = _FakeSupplies(refuse={'N1471:3'})
    _patch(monkeypatch, tmp_path, fake)
    clock = iter(range(0, 10000, 10))
    monkeypatch.setattr(HV.time, 'time', lambda: next(clock))
    assert HV.ramp_down(timeout_s=60) is False
    assert set(fake.sent) == set(HV.TARGETS)                 # the refusal did not stop the others
    assert fake.sent.count('N1471:3') > 1                    # not falling -> off command again
    assert fake.ch['N1471:3']['vmon'] == 430.0
    assert all(c['vmon'] < 20 for hw, c in fake.ch.items() if hw != 'N1471:3')
