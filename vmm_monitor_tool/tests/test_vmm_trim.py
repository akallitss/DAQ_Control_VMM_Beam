"""Tests of the exception-file handling (no hardware). Run: python3 -m pytest vmm_monitor_tool/tests"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import vmm_trim as vt  # noqa: E402

BASE = """# header
Sys 0 Bec 0 Hyb 0 Vmm 0 Reg bank1spi1   Field sdt 44 #
Sys 0 Bec 0 Hyb 0 Vmm 0 Reg bank1spi2   Field sdt  6 # 300 (comment is wrong on purpose)
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg bank1spi2   Field sg  3
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg bank1spi1   Field sdt 20
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg bank1spi2   Field sdt  3 # 212
#Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[07] Field sm 1
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[45] Field sm   1
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[46] Field sm   1
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[46] Field sm   0
Sys 0 Bec 0 Hyb 0 Vmm 1 Reg channel[20] Field st   1
"""


def ext(text=BASE):
    return vt.ExtFile(text.splitlines())


def test_rev5_is_an_involution_and_matches_the_measurement():
    assert [vt.rev5(v) for v in (0, 1, 2, 4, 8, 16, 31)] == [0, 16, 8, 4, 2, 1, 31]
    assert all(vt.rev5(vt.rev5(v)) == v for v in range(32))


def test_sdt_is_computed_from_the_two_values_not_the_comment():
    assert ext().sdt() == {0: 6 * 64 + 44, 7: 3 * 64 + 20}


def test_last_line_wins_and_commented_lines_are_ignored():
    assert ext().channel_bits('sm') == {7: [45]}
    assert ext().channel_bits('st') == {1: [20]}


def test_malformed_sys_line_is_an_error():
    with pytest.raises(ValueError):
        ext(BASE + 'Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[01] Fild sd 3\n').final()


def test_set_trims_writes_reversed_and_reads_back_effective():
    e = ext()
    n = e.set_trims({7: {5: 1, 6: 12, 9: 0}})
    assert n == 2                                   # trim 0 is not written
    text = '\n'.join(e.lines)
    assert 'Reg channel[05] Field sd 16   # trim 1' in text
    assert 'Reg channel[06] Field sd  6   # trim 12' in text
    assert vt.ExtFile(e.lines).trims() == {7: {5: 1, 6: 12}}


def test_set_trims_replaces_the_old_block_and_keeps_everything_else():
    e = ext()
    e.set_trims({7: {5: 3}})
    e.set_trims({7: {8: 4}})
    again = vt.ExtFile(e.lines)
    assert again.trims() == {7: {8: 4}}
    assert '\n'.join(e.lines).count(vt.TRIM_BLOCK_BEGIN) == 1
    assert again.sdt() == ext().sdt() and again.channel_bits('sm') == ext().channel_bits('sm')
    assert e.lines[:len(BASE.splitlines())] == BASE.splitlines()


@pytest.mark.parametrize('bad', [{7: {5: 32}}, {7: {5: -1}}, {7: {64: 1}}, {20: {0: 1}}])
def test_out_of_range_is_refused(bad):
    with pytest.raises(ValueError):
        ext().set_trims(bad)


def test_set_sdt_rewrites_in_place_and_appends_when_missing():
    e = ext()
    assert e.set_sdt(7, 300) == 212
    assert vt.ExtFile(e.lines).sdt()[7] == 300
    assert len(e.lines) == len(BASE.splitlines())
    assert e.set_sdt(9, 250) is None
    assert vt.ExtFile(e.lines).sdt()[9] == 250
    with pytest.raises(ValueError):
        e.set_sdt(7, 1024)


def test_unmask_removes_only_the_asked_channels():
    e = ext()
    assert e.unmask(7, [45, 46]) == [45]
    assert vt.ExtFile(e.lines).channel_bits('sm') == {}
    assert vt.ExtFile(e.lines).channel_bits('st') == {1: [20]}


def test_commands_roundtrip(tmp_path):
    src = tmp_path / 'a.txt'
    src.write_text(BASE)
    p1, p2, p3 = (str(tmp_path / n) for n in ('p.txt', 'r.txt', 's.txt'))
    vt.main(['pedestal', str(src), '--out', p1, '--trim', '20', '--chips', '7', '--sdt-per-trim', '1.4'])
    e = vt.ExtFile.load(p1)
    assert e.sdt()[7] == 212 + 28 and set(e.trims()[7].values()) == {20} and len(e.trims()[7]) == 64
    vt.main(['raise', p1, '--out', p2, '--chip', '7', '--channels', '45', '--steps', '15', '--unmask'])
    e = vt.ExtFile.load(p2)
    assert e.trims()[7][45] == 5 and e.trims()[7][44] == 20 and e.channel_bits('sm') == {}
    with pytest.raises(SystemExit):
        vt.main(['raise', p2, '--out', p3, '--chip', '7', '--channels', '45', '--steps', '6'])
    vt.main(['raise', p2, '--out', p3, '--chip', '7', '--channels', '45', '--steps', '6', '--clip'])
    assert vt.ExtFile.load(p3).trims()[7].get(45, 0) == 0
    with pytest.raises(SystemExit):
        vt.main(['set', p2, '--out', p3, '--chip', '7', '--channels', '1', '--trim', '3'])   # exists
