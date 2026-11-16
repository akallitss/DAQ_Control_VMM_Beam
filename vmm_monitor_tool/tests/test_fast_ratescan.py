"""Tests of the fast rate scan logic and its fit (no hardware)."""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fastscan'))
import fast_ratescan as FR  # noqa: E402
import noise_ratefit as NR  # noqa: E402
import vmm_trim as vt  # noqa: E402

START = """# start
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi1   Field sdt 53
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi2   Field sdt  3 # 245
Sys 0 Bec 0 Hyb 1 Vmm 1 Reg bank1spi1   Field sdt 34
Sys 0 Bec 0 Hyb 1 Vmm 1 Reg bank1spi2   Field sdt  3 # 226
Sys 0 Bec 0 Hyb 1 Vmm 1 Reg channel[33] Field sm   1
""".splitlines()


def test_decide_rules():
    new = FR.decide({1: 2e4, 2: 50.0, 3: 30.0}, {2: 40.0, 3: 5e3}, masked={4})
    assert set(new) == {1, 3}
    assert new[1].startswith('rate') and new[3].startswith('stuck')
    assert FR.decide({4: 1e6}, None, masked={4}) == {}
    # a fall from a low rate is Poisson noise, not a stuck comparator
    assert FR.decide({5: 1.0}, {5: 500.0}, masked=set()) == {}


def test_point_file_sdt_and_masks():
    ext = vt.ExtFile(START)
    ref = ext.sdt()
    masked = {2: {5}, 3: {33}}
    pe, sdt = FR.point_file(ext, ref, -3, [2, 3], masked, left=set())
    assert sdt == {2: 242, 3: 223} and pe.sdt() == {2: 242, 3: 223}
    assert pe.channel_bits('sm') == {2: [5], 3: [33]}
    pe, sdt = FR.point_file(ext, ref, -3, [2, 3], masked, left={3})
    assert sdt[3] == 266 and len(pe.channel_bits('sm')[3]) == 64
    assert ext.sdt() == ref          # the start file is not modified


def test_fitpoints_recovers_sigma(tmp_path):
    sigma, f0, k = 3.5, 8e5, 0.8
    m = {'sdt': {'2': 245}, 'baseline': {'2': {'0': 150.0, '1': 150.0}},
         'threshold': {'2': {'0': 170.0, '1': 170.0}}}
    json.dump(m, open(tmp_path / 'map.json', 'w'))
    json.dump({'fit': {'2': {'mV_per_sdt': k}}}, open(tmp_path / 'cal.json', 'w'))
    pts = tmp_path / 'pts'
    pts.mkdir()
    for i, off in enumerate(range(4, -14, -1)):
        d = 20.0 + off * k
        r = f0 * math.exp(-d * d / (2 * sigma ** 2))
        live = 15.0
        masked = [1] if off <= -5 else []          # channel 1 masked from offset -5 on
        hits = {'0': int(r * live), '1': 0 if masked else int(r * live)}
        json.dump({'offset': off, 'sweep_chips': [2], 'sdt': {'2': 245 + off}, 'masked': {'2': masked},
                   'left': [], 'chip_total_hz': {'2': 2 * r}, 'hits': {'2': hits}, 'live_s': live},
                  open(pts / ('point_%02d.json' % i), 'w'))
    out = tmp_path / 'fit.json'
    NR.main(['fitpoints', '--map', str(tmp_path / 'map.json'), '--sdtcal', str(tmp_path / 'cal.json'),
             '--points', str(pts), '--out', str(out)])
    ch = json.load(open(out))['chips']['2']['channels']
    assert abs(ch['0']['fixed']['sigma_mV'] - sigma) < 0.1 and ch['0']['quality'] == 'ok'
    assert ch['1']['masked_from_offset'] == -5
    assert all(d >= 20.0 + -4 * k - 1e-6 for d, _ in ch['1']['points'])
