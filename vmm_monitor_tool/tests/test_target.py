"""Tests of the target mode (no hardware)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import vmm_monitor_scan as VS  # noqa: E402


def curve(sigma, f0, d0=0.0, ds=None):
    ds = ds or [x * 0.8 for x in range(0, 40)]
    return [(d, f0 * math.exp(-(d - d0) ** 2 / (2 * sigma ** 2))) for d in ds]


def test_rate_crossing_and_rate_at():
    pts = curve(3.0, 1e6)
    d = VS.rate_crossing(pts, 50.0)
    assert abs(d - 3.0 * math.sqrt(2 * math.log(1e6 / 50))) < 0.05
    assert abs(VS.rate_at(pts, d) - 50.0) < 1.0
    # the stuck side below the maximum is ignored
    stuck = [(-2.0, 0.0), (-1.0, 10.0)] + pts
    assert abs(VS.rate_crossing(stuck, 50.0) - d) < 1e-9
    assert VS.rate_crossing(pts[:3], 50.0) is None


def make(chans, step=1.0, k=0.8, sdt=250, d_map=20.0):
    m = {'baseline': {'2': {}}, 'threshold': {'2': {}}, 'sdt': {'2': sdt}, 'trims': {'2': {}}}
    fit = {'chips': {'2': {'channels': {}}}}
    for ch, (sigma, f0, d0, trim) in chans.items():
        m['baseline']['2'][ch] = 150.0
        m['threshold']['2'][ch] = 150.0 + d_map
        m['trims']['2'][ch] = trim
        fit['chips']['2']['channels'][ch] = {'quality': 'ok', 'fixed': {'sigma_mV': sigma, 'f0_Hz': f0},
                                            'points': curve(sigma, f0, d0)}
    return m, fit, {'2': step}, {'2': k}


def test_target_puts_channels_at_their_rate():
    chans = {str(c): (2.5 + 0.1 * c, 1e6, 0.0, 10) for c in range(12)}
    chans['12'] = (3.0, 1e6, 5.0, 10)           # noise centre 5 mV above the monitor baseline
    m, fit, st, sd = make(chans)
    table, sdt, masks, info = VS.target(m, fit, st, sd, [2], rate=50.0, headroom=2)
    i = info['2']
    assert i['trim_min'] == 2 and not masks
    for ch, x in i['channels'].items():
        assert 0 <= x['expected_mV'] - x['target_mV'] < 1.0 + 1e-6      # floor: at most one step above
        assert x['predicted_Hz'] <= 50.0 + 1e-6
    assert i['channels']['12']['target_mV'] > i['channels']['5']['target_mV'] + 4   # same sigma, centre +5 mV
    assert i['channels']['12']['from'] == 'measured'


def test_min_target_floor():
    chans = {str(c): (1.0, 1e3, 0.0, 10) for c in range(10)}          # a crossing at ~2.8 mV
    m, fit, st, sd = make(chans)
    _, _, _, info = VS.target(m, fit, st, sd, [2], rate=50.0, min_target=6.0)
    assert all(x['target_mV'] >= 6.0 for x in info['2']['channels'].values())
    assert info['2']['channels']['0']['from'] == 'measured+floor'
