"""Pulser phase files and the pedestal/noise bookkeeping, without hardware."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pulser_files as pf  # noqa: E402
import pulser_pedestal as pp  # noqa: E402
import vmm_trim as vt  # noqa: E402


def base_file(tmp_path):
    lines = ['# base']
    for chip in range(20):
        h, v = chip // 2, chip % 2
        lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg bank1spi2   Field sng  0' % (h, v))
    lines.append('Sys 0 Bec 0 Hyb 1 Vmm 0 Reg channel[09] Field sm   1')
    lines.append('Sys 0 Bec 0 Hyb 1 Vmm 0 Reg channel[10] Field st   1')    # an old test-pulse line
    p = tmp_path / 'base.txt'
    p.write_text('\n'.join(lines) + '\n')
    return str(p)


def test_three_phases_cover_every_channel_as_L_and_U():
    seen = {ch: set() for ch in range(64)}
    for phase in (0, 1, 2):
        for ch, role in pp.roles(pf.pulsed_channels(phase)).items():
            seen[ch].add(role)
    for ch in range(64):
        assert 'P' in seen[ch]
        if ch > 0:
            assert 'U' in seen[ch], ch
        if ch < 63:
            assert 'L' in seen[ch], ch


def test_phase_file(tmp_path):
    ext, rep = pf.make(base_file(tmp_path), [2, 3], 0, 80, unmask=True)
    fin = ext.final()
    for c in (2, 3):
        assert fin[(c, 'bank1spi2', 'sng')] == 1
        assert fin[(c, 'bank1spi1', 'sdp10')] == 80
        assert sorted(ext.channel_bits('st')[c]) == list(range(0, 64, 3))
    assert fin[(4, 'bank1spi2', 'sng')] == 0                 # other chips untouched
    assert 9 not in ext.channel_bits('sm').get(2, [])        # unmasked
    assert rep[2]['unmasked'] == [9]
    assert 10 not in ext.channel_bits('st')[2]               # the old test-pulse line is gone


def test_hist_stats_gaussian():
    rng = np.random.default_rng(1)
    h = np.bincount(np.clip(np.round(rng.normal(45, 4.3, 200000)), 0, 1023).astype(int), minlength=1024)
    s = pp.hist_stats(h)
    assert abs(s['mean'] - 45) < 0.05 and abs(s['std'] - 4.33) < 0.1    # rounding adds 1/12
    assert s['median'] == 45


def test_merge_combines_L_and_U(tmp_path):
    def res(name, role, mean, std, n):
        return {'pcap': name, 'chips': {'2': {'channels': {'5': {'role': role, 'nb': {'n': n, 'mean': mean, 'std': std}}}}}}
    import json
    a, b = tmp_path / 'a.json', tmp_path / 'b.json'
    a.write_text(json.dumps(res('a', 'L', 40.0, 4.0, 1000)))
    b.write_text(json.dumps(res('b', 'U', 44.0, 4.0, 1000)))
    m = pp.merge([str(a), str(b)])
    c = m['chips']['2']['5']['combined']
    assert c['n'] == 2000 and c['mean'] == 42.0 and abs(c['std'] - (16 + 4) ** 0.5) < 0.01


def test_rate_fit_recovers_sigma():
    import math
    import noise_ratefit as nr
    sigma, f0, d0 = 3.2, 1.5e6, 1.0
    pts = []
    for d in [x * 1.5 for x in range(2, 16)]:
        r = f0 * math.exp(-(d - d0) ** 2 / (2 * sigma ** 2))
        pts.append((d, r, int(r * 30)))
    fit = nr.fit_channel(pts, 0.3, 30000)
    assert abs(fit['free']['sigma_mV'] - sigma) < 0.01
    assert abs(fit['free']['d0_mV'] - d0) < 0.05
    assert abs(math.log(fit['free']['f0_Hz'] / f0)) < 0.01
