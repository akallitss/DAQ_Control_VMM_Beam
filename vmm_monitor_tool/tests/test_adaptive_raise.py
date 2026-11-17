"""The raise-instead-of-mask mode of quick_scripts/adaptive_thr_scan.py, without hardware or runs."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'quick_scripts'))
import adaptive_thr_scan as ad  # noqa: E402
import vmm_trim as vt  # noqa: E402


def make_base(tmp_path):
    lines = ['# base']
    for chip in range(20):
        h, v = chip // 2, chip % 2
        lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg bank1spi1   Field sdt 10' % (h, v))
        lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg bank1spi2   Field sdt  4 # 266' % (h, v))
    lines.append('Sys 0 Bec 0 Hyb 1 Vmm 0 Reg channel[09] Field sm   1')       # a base mask on chip 2
    e = vt.ExtFile(lines)
    trims = {c: {ch: 12 for ch in range(64)} for c in range(2, 20)}
    trims[2][5] = 2                                                            # little room
    trims[2][6] = 0                                                            # no room
    trims[0] = {3: 7}                                                          # H0: not the scan's business
    e.set_trims(trims)
    name = 'base_trim.txt'
    e.write(str(tmp_path / name))
    return name


def setup(tmp_path, monkeypatch, steps=4):
    name = make_base(tmp_path)
    monkeypatch.setattr(ad, 'CFG_DIR', str(tmp_path))
    monkeypatch.setattr(ad, 'BASE', name)
    ad.BASE_TRIMS.clear()
    ad.BASE_TRIMS.update(vt.full_trims(vt.ExtFile.load(str(tmp_path / name)), ad.CHIPS))
    a = types.SimpleNamespace(tag='t', raise_steps=steps, max_masks_per_chip=20)
    base = ad.read_base(str(tmp_path / name))
    st = ad.State(str(tmp_path / 'state.json'))
    return a, base, st


def test_hot_channels_are_raised_and_only_masked_at_trim_zero(tmp_path, monkeypatch):
    a, base, st = setup(tmp_path, monkeypatch)
    k = -8
    held = ad.apply_new_masks(st, k, {2: {4, 5, 6}, 7: {1}}, base, a, None)
    assert held == []
    assert st.raised[k] == {2: {4: 4, 5: 2}, 7: {1: 4}}          # ch5 had only 2 steps left
    assert st.found[k] == {2: {6}}                               # ch6 was at trim 0: masked
    name, sdt, masks, _, n_extra = ad.make_cfg(st, k, 1, base, a)
    e = vt.ExtFile.load(str(tmp_path / name))
    t = e.trims()
    assert t[2][4] == 8 and t[2].get(5, 0) == 0 and t[7][1] == 8 and t[3][0] == 12
    assert t[0] == {3: 7}                                        # H0 trims untouched
    assert e.channel_bits('sm') == {2: [6, 9]}                   # base mask + the one at trim 0
    assert e.sdt()[2] == 266 - 8 and e.sdt()[0] == 266           # detector chips moved, H0 not
    assert n_extra == 1

    # still hot at the next probe: raised again, then at 0 -> masked
    ad.apply_new_masks(st, k, {2: {4}}, base, a, None)
    ad.apply_new_masks(st, k, {2: {4}}, base, a, None)
    assert st.raised[k][2][4] == 12 and 4 not in st.found[k][2]
    ad.apply_new_masks(st, k, {2: {4}}, base, a, None)
    assert 4 in st.found[k][2]


def test_raises_are_per_point_and_survive_a_restart(tmp_path, monkeypatch):
    a, base, st = setup(tmp_path, monkeypatch)
    ad.apply_new_masks(st, -8, {3: {10}}, base, a, None)
    st.save()
    st2 = ad.State(str(tmp_path / 'state.json'))
    assert st2.raised == {-8: {3: {10: 4}}}
    assert st2.trims_at(-8)[3][10] == 8 and st2.trims_at(-4)[3][10] == 12


def test_without_the_option_nothing_changes(tmp_path, monkeypatch):
    a, base, st = setup(tmp_path, monkeypatch, steps=0)
    ad.apply_new_masks(st, -8, {2: {4}}, base, a, None)
    assert st.raised == {} and st.found[-8] == {2: {4}}
    name, *_ = ad.make_cfg(st, -8, 1, base, a)
    assert vt.ExtFile.load(str(tmp_path / name)).trims()[2][4] == 12   # base trims pass through unchanged
