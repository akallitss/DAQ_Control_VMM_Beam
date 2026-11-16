"""Tests of the rate-feedback trim decisions (no hardware)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import rate_tune as RT  # noqa: E402


def curve(sigma=3.0, f0=1e6):
    return [(x * 0.8, f0 * math.exp(-(x * 0.8) ** 2 / (2 * sigma ** 2))) for x in range(40)]


def test_slope_matches_gaussian():
    d = 13.0
    s = RT.local_slope(curve(), d, 1.0)
    assert abs(s - d / 9.0) < 0.15          # -dlnR/dd = d / sigma^2


def test_decide_raise_lower_burst():
    fitc = {str(ch): {'points': curve()} for ch in range(4)}
    rates = {0: 500.0, 1: 50.0, 2: 1.0, 3: 5e5}
    trims = {0: 10, 1: 10, 2: 10, 3: 10}
    d_est = {0: 12.0, 1: 13.0, 2: 16.0, 3: 5.0}
    new, act = RT.decide(rates, 60.0, trims, d_est, fitc, 1.0, 50.0, 2.0, 4.0, 1e4, 6.0)
    assert new[0] < 10 and new[1] == 10 and new[2] == 12 and new[3] == 10
    assert act[3].startswith('burst')
    # never lowered closer than min_d to the baseline
    new, _ = RT.decide({2: 0.0}, 60.0, {2: 10}, {2: 6.5}, fitc, 1.0, 50.0, 2.0, 4.0, 1e4, 6.0)
    assert new[2] == 10
