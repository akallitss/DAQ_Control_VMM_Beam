#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-channel threshold trims for the VMM3a chips: read, write and edit the
trim lines of a chip exception file (config_ext/*.txt).

Standard library only, so it runs with any python3 on the DAQ machine or on a
laptop. It never talks to the hardware: it reads and writes text files.

WHAT A TRIM IS
    threshold of a channel = threshold of its chip (sdt)  -  trim x ~1.1 mV
    trim = 0..31. A trim can only LOWER a channel. To be able to RAISE single
    channels later, give every channel a common trim first (the "pedestal
    trim", e.g. 20) and compensate with a higher chip sdt; a noisy channel is
    then raised by giving it a SMALLER trim.

THE BIT ORDER (measured 2026-11-13, p2basket-sc build 2026-11-05 d2cb9ef)
    The value written to field `sd` reaches the chip with its 5 bits in
    reverse order: writing 1 acts as trim 16, writing 16 acts as trim 1.
    Everything here speaks in EFFECTIVE trim (what the chip does) and converts
    when reading or writing the file:  written = bit_reverse_5(effective).
    Every line we write says so in its comment. If a future slow-control build
    fixes the order, set SD_BIT_REVERSED = False (vmm_monitor_scan.py
    `trimcheck` measures which one is true).

chip number = 2 x hybrid + vmm (0..19), as in the analysis.

Commands (see --help of each):
    summary  EXT                       what a file sets, chip by chip
    trims    EXT [--json OUT]          the trims in a file as a table
    set      EXT --out NEW --chip C --channels 3,7-9 --trim T
    shift    EXT --out NEW --chip C --channels ... --by +3     (relative)
    raise    EXT --out NEW --chip C --channels ... --steps N [--unmask]
                                       raise single channels instead of masking them
    pedestal EXT --out NEW --trim T [--sdt-per-trim 1.38] [--chips ...]
                                       same trim on all channels of the chips
                                       and sdt raised to compensate
    apply-table EXT --out NEW --table trims.json
"""

import argparse
import json
import os
import re
import sys

SD_BIT_REVERSED = True
N_CHANNELS = 64
N_CHIPS = 20
TRIM_MAX = 31
SDT_MAX = 1023
TRIM_BLOCK_BEGIN = '# >>> per-channel trims (written by vmm_trim.py; edit with that tool) >>>'
TRIM_BLOCK_END = '# <<< per-channel trims <<<'

LINE_RE = re.compile(
    r'^\s*Sys\s+(?P<sys>\d+)\s+Bec\s+(?P<bec>\d+)\s+Hyb\s+(?P<hyb>\d+)\s+Vmm\s+(?P<vmm>\d+)\s+'
    r'Reg\s+(?P<reg>\S+)\s+Field\s+(?P<field>\S+)\s+(?P<val>-?\d+)(?P<tail>.*)$')
CHAN_RE = re.compile(r'^channel\[(\d+)\]$')


def rev5(v):
    """Reverse the 5 bits of v (0..31)."""
    return int('{:05b}'.format(v)[::-1], 2)


def to_written(effective):
    return rev5(effective) if SD_BIT_REVERSED else effective


def to_effective(written):
    return rev5(written) if SD_BIT_REVERSED else written


def chip_of(hyb, vmm):
    return 2 * hyb + vmm


def hyb_vmm(chip):
    return chip // 2, chip % 2


def parse_list(text, lo, hi, what):
    out = []
    for tok in str(text).split(','):
        tok = tok.strip()
        if not tok:
            continue
        if tok == 'all':
            out.extend(range(lo, hi + 1))
        elif '-' in tok:
            a, b = tok.split('-', 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(tok))
    for v in out:
        if not lo <= v <= hi:
            raise ValueError('%s %d outside %d..%d' % (what, v, lo, hi))
    return sorted(set(out))


class ExtFile:
    """An exception file, kept line by line so that rewriting it changes only
    what was asked. Same positional grammar as p2basket_sc_config.py: a line
    is a setting only if its first word is `Sys`; the last line for a given
    (chip, register, field) wins."""

    def __init__(self, lines, path=None):
        self.lines = [ln.rstrip('\n') for ln in lines]
        self.path = path

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(f.readlines(), path)

    def settings(self):
        """Yield (line_index, chip, register, field, value) for every active line."""
        for i, ln in enumerate(self.lines):
            m = LINE_RE.match(ln)
            if not m:
                if ln.split()[:1] == ['Sys']:
                    raise ValueError('%s line %d starts with Sys but is not a valid setting: %s'
                                     % (self.path or '<ext>', i + 1, ln))
                continue
            if m['sys'] != '0' or m['bec'] != '0':
                raise ValueError('line %d: only Sys 0 Bec 0 is supported: %s' % (i + 1, ln))
            yield i, chip_of(int(m['hyb']), int(m['vmm'])), m['reg'], m['field'], int(m['val'])

    def final(self):
        """{(chip, register, field): value} after applying every line in order."""
        out = {}
        for _, chip, reg, field, val in self.settings():
            out[(chip, reg, field)] = val
        return out

    # ---- reading ---------------------------------------------------------
    def sdt(self):
        """{chip: sdt} for chips where at least one half is set (the other
        half falls back to the base YAML default of sdt 300: 44 and 4)."""
        fin = self.final()
        out = {}
        for chip in range(N_CHIPS):
            lo, hi = fin.get((chip, 'bank1spi1', 'sdt')), fin.get((chip, 'bank1spi2', 'sdt'))
            if lo is None and hi is None:
                continue
            out[chip] = 64 * (4 if hi is None else hi) + (44 if lo is None else lo)
        return out

    def trims(self):
        """{chip: {channel: EFFECTIVE trim}}, only channels with a `sd` line."""
        out = {}
        for (chip, reg, field), val in self.final().items():
            m = CHAN_RE.match(reg)
            if m and field == 'sd':
                out.setdefault(chip, {})[int(m[1])] = to_effective(val)
        return out

    def channel_bits(self, field):
        """{chip: sorted channels} where a per-channel bit (sm, st, smx...) ends up 1."""
        out = {}
        for (chip, reg, f), val in self.final().items():
            m = CHAN_RE.match(reg)
            if m and f == field and val:
                out.setdefault(chip, []).append(int(m[1]))
        return {c: sorted(v) for c, v in out.items()}

    def summary(self):
        fin = self.final()
        sdt, trims, masks = self.sdt(), self.trims(), self.channel_bits('sm')
        pulsed = self.channel_bits('st')
        rows = []
        for chip in range(N_CHIPS):
            t = trims.get(chip, {})
            full = [t.get(ch, 0) for ch in range(N_CHANNELS)]
            rows.append({
                'chip': chip, 'hyb': chip // 2, 'vmm': chip % 2,
                'sdt': sdt.get(chip),
                'sg': fin.get((chip, 'bank1spi2', 'sg')),
                'st': fin.get((chip, 'bank1spi2', 'st')),
                'masked': masks.get(chip, []),
                'pulsed': pulsed.get(chip, []),
                'n_trim_lines': len(t),
                'trim_min': min(full), 'trim_max': max(full),
                'trim_mode': max(set(full), key=full.count),
                'trims': {str(ch): v for ch, v in sorted(t.items())},
            })
        return rows

    # ---- writing ---------------------------------------------------------
    def _strip_trim_lines(self):
        """Remove every active `sd` line and our block markers; return what
        the trims were (effective)."""
        old = self.trims()
        keep = []
        for ln in self.lines:
            m = LINE_RE.match(ln)
            if m and CHAN_RE.match(m['reg']) and m['field'] == 'sd':
                continue
            if ln.strip() in (TRIM_BLOCK_BEGIN, TRIM_BLOCK_END):
                continue
            keep.append(ln)
        while keep and not keep[-1].strip():
            keep.pop()
        self.lines = keep
        return old

    def set_trims(self, trims, note=''):
        """Replace ALL trims of the file by `trims` = {chip: {channel: effective}}.
        Trims of 0 are not written (0 is the base default)."""
        for chip, chans in trims.items():
            if not 0 <= int(chip) < N_CHIPS:
                raise ValueError('chip %s outside 0..%d' % (chip, N_CHIPS - 1))
            for ch, v in chans.items():
                if not 0 <= int(ch) < N_CHANNELS:
                    raise ValueError('chip %s: channel %s outside 0..63' % (chip, ch))
                if not 0 <= int(v) <= TRIM_MAX:
                    raise ValueError('chip %s channel %s: trim %s outside 0..31' % (chip, ch, v))
        self._strip_trim_lines()
        block = ['', TRIM_BLOCK_BEGIN,
                 '# trim = 0..31, lowers the channel threshold by about 1.1 mV per step.',
                 '# The value in the line is the trim with its 5 bits reversed (needed by this'
                 if SD_BIT_REVERSED else '# The value in the line is the trim itself.',
                 '# slow-control build); the comment gives the real trim.'
                 if SD_BIT_REVERSED else '#']
        if note:
            block.append('# ' + note)
        n = 0
        for chip in sorted(trims, key=int):
            hyb, vmm = hyb_vmm(int(chip))
            for ch in sorted(trims[chip], key=int):
                v = int(trims[chip][ch])
                if v == 0:
                    continue
                block.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field sd %2d   # trim %d'
                             % (hyb, vmm, int(ch), to_written(v), v))
                n += 1
        block.append(TRIM_BLOCK_END)
        self.lines.extend(block)
        return n

    def set_sdt(self, chip, sdt):
        """Set a chip's sdt by rewriting its existing two lines in place (or
        appending them). Returns the previous value (None if it was not set)."""
        if not 0 <= sdt <= SDT_MAX:
            raise ValueError('chip %d: sdt %d outside 0..1023' % (chip, sdt))
        old = self.sdt().get(chip)
        want = {'bank1spi1': sdt % 64, 'bank1spi2': sdt // 64}
        last = {}
        for i, c, reg, field, _ in self.settings():
            if c == chip and field == 'sdt' and reg in want:
                last[reg] = i
        hyb, vmm = hyb_vmm(chip)
        for reg, val in want.items():
            text = 'Sys 0 Bec 0 Hyb %d Vmm %d Reg %s   Field sdt %2d' % (hyb, vmm, reg, val)
            text += '  # sdt %d' % sdt if reg == 'bank1spi2' else ''
            if reg in last:
                self.lines[last[reg]] = text
            else:
                self.lines.append(text)
        return old

    def unmask(self, chip, channels):
        """Remove the mask (`sm 1` lines) of the given channels of a chip.
        Returns the channels that were masked."""
        was = [ch for ch in channels if ch in self.channel_bits('sm').get(chip, [])]
        hyb, vmm = hyb_vmm(chip)
        keep = []
        for ln in self.lines:
            m = LINE_RE.match(ln)
            if (m and m['field'] == 'sm' and int(m['hyb']) == hyb and int(m['vmm']) == vmm
                    and CHAN_RE.match(m['reg']) and int(CHAN_RE.match(m['reg'])[1]) in channels):
                continue
            keep.append(ln)
        self.lines = keep
        return was

    def write(self, path, force=False):
        if os.path.exists(path) and not force:
            raise FileExistsError('%s exists (use --force to overwrite)' % path)
        ExtFile(self.lines).final()          # must still parse
        with open(path, 'w') as f:
            f.write('\n'.join(self.lines) + '\n')


def full_trims(ext, chips):
    """{chip: {channel: effective}} for ALL 64 channels of the given chips."""
    t = ext.trims()
    return {c: {ch: t.get(c, {}).get(ch, 0) for ch in range(N_CHANNELS)} for c in chips}


# ---------------------------------------------------------------- commands --
def cmd_summary(a):
    rows = ExtFile.load(a.ext).summary()
    if a.json:
        json.dump(rows, sys.stdout, indent=1)
        print()
        return
    print('%s' % a.ext)
    print('chip hyb vmm   sdt gain peak  masked  trims: lines  min  max  most-common')
    for r in rows:
        print('%4d %3d %3d %5s %4s %4s %7d %13d %4d %4d %6d   %s' % (
            r['chip'], r['hyb'], r['vmm'], r['sdt'] if r['sdt'] is not None else '-',
            r['sg'] if r['sg'] is not None else '-', r['st'] if r['st'] is not None else '-',
            len(r['masked']), r['n_trim_lines'], r['trim_min'], r['trim_max'], r['trim_mode'],
            ('masked: ' + ','.join(map(str, r['masked']))) if r['masked'] else ''))


def cmd_trims(a):
    ext = ExtFile.load(a.ext)
    t = ext.trims()
    if a.json:
        with open(a.json, 'w') as f:
            json.dump({'source': os.path.basename(a.ext), 'effective_trim': True,
                       'trims': {str(c): {str(ch): v for ch, v in sorted(d.items())} for c, d in sorted(t.items())}},
                      f, indent=1)
        print('wrote', a.json)
        return
    chips = parse_list(a.chips, 0, N_CHIPS - 1, 'chip') if a.chips else sorted(t)
    for c in chips:
        row = [t.get(c, {}).get(ch, 0) for ch in range(N_CHANNELS)]
        print('chip %2d (hyb %d vmm %d)' % (c, c // 2, c % 2))
        for k in range(0, N_CHANNELS, 16):
            print('   ch %2d-%2d: ' % (k, k + 15) + ' '.join('%2d' % v for v in row[k:k + 16]))


def _edit(a, fn, note):
    ext = ExtFile.load(a.ext)
    chips = parse_list(a.chip, 0, N_CHIPS - 1, 'chip')
    channels = parse_list(a.channels, 0, N_CHANNELS - 1, 'channel')
    t = ext.trims()
    for c in chips:
        for ch in channels:
            old = t.get(c, {}).get(ch, 0)
            new = fn(old)
            if not 0 <= new <= TRIM_MAX:
                raise SystemExit('chip %d channel %d: trim %d -> %d is outside 0..31 '
                                 '(a channel cannot be raised above trim 0: raise the chip sdt '
                                 'or use a larger pedestal trim)' % (c, ch, old, new))
            t.setdefault(c, {})[ch] = new
            print('chip %2d channel %2d: trim %2d -> %2d' % (c, ch, old, new))
    ext.set_trims(t, note)
    ext.write(a.out, a.force)
    print('wrote', a.out)


def cmd_set(a):
    _edit(a, lambda old: a.trim, 'set: chip %s channels %s trim %d' % (a.chip, a.channels, a.trim))


def cmd_shift(a):
    _edit(a, lambda old: old + a.by, 'shift: chip %s channels %s by %+d' % (a.chip, a.channels, a.by))


def cmd_raise(a):
    """Raise the threshold of single channels by lowering their trim, and
    optionally take their mask away: the alternative to masking a noisy channel."""
    ext = ExtFile.load(a.ext)
    chips = parse_list(a.chip, 0, N_CHIPS - 1, 'chip')
    channels = parse_list(a.channels, 0, N_CHANNELS - 1, 'channel')
    t = ext.trims()
    for c in chips:
        for ch in channels:
            old = t.get(c, {}).get(ch, 0)
            new = old - a.steps
            if new < 0:
                if not a.clip:
                    raise SystemExit('chip %d channel %d has trim %d: cannot be raised by %d steps. Give the chip '
                                     'more room first (command "pedestal"), or use --clip to stop at trim 0.'
                                     % (c, ch, old, a.steps))
                new = 0
            t.setdefault(c, {})[ch] = new
            print('chip %2d channel %2d: trim %2d -> %2d  (threshold about %+.0f mV)'
                  % (c, ch, old, new, (old - new) * 1.1))
        if a.unmask:
            was = ext.unmask(c, channels)
            if was:
                print('chip %2d: mask removed from channels %s' % (c, ','.join(map(str, was))))
    ext.set_trims(t, 'raise: chip %s channels %s by %d steps' % (a.chip, a.channels, a.steps))
    ext.write(a.out, a.force)
    print('wrote', a.out)


def cmd_pedestal(a):
    """Give every channel of the chips the same trim on top of what it has,
    and raise sdt so that the thresholds stay where they were."""
    ext = ExtFile.load(a.ext)
    chips = parse_list(a.chips, 0, N_CHIPS - 1, 'chip')
    sdt = ext.sdt()
    t = ext.trims()
    for c in chips:
        if c not in sdt:
            raise SystemExit('chip %d has no sdt line in %s' % (c, a.ext))
        cur = [t.get(c, {}).get(ch, 0) for ch in range(N_CHANNELS)]
        if max(cur) + a.trim > TRIM_MAX:
            raise SystemExit('chip %d: a channel already has trim %d, +%d exceeds 31' % (c, max(cur), a.trim))
        t[c] = {ch: cur[ch] + a.trim for ch in range(N_CHANNELS)}
        new_sdt = sdt[c] + int(round(a.trim * a.sdt_per_trim))
        ext.set_sdt(c, new_sdt)
        print('chip %2d: all trims +%d, sdt %d -> %d' % (c, a.trim, sdt[c], new_sdt))
    ext.set_trims(t, 'pedestal trim +%d on chips %s, sdt raised by %.2f per trim step'
                  % (a.trim, a.chips, a.sdt_per_trim))
    ext.write(a.out, a.force)
    print('wrote', a.out)


CHANNEL_BITS = ('sm', 'sl', 'st', 'sth', 'sc', 'smx')


def cmd_bit(a):
    """Set one of the one-bit channel settings (mask sm, leakage sl, test pulse st, ...) on some channels."""
    ext = ExtFile.load(a.ext)
    chips = parse_list(a.chip, 0, N_CHIPS - 1, 'chip')
    channels = parse_list(a.channels, 0, N_CHANNELS - 1, 'channel')
    for c in chips:
        hyb, vmm = hyb_vmm(c)
        keep = []
        for ln in ext.lines:                    # drop earlier lines for the same channels and field
            m = LINE_RE.match(ln)
            if (m and m['field'] == a.field and int(m['hyb']) == hyb and int(m['vmm']) == vmm
                    and CHAN_RE.match(m['reg']) and int(CHAN_RE.match(m['reg'])[1]) in channels):
                continue
            keep.append(ln)
        ext.lines = keep
        if a.value:
            ext.lines.append('# %s 1 on chip %d channels %s%s' % (a.field, c, a.channels, (': ' + a.why) if a.why else ''))
            for ch in channels:
                ext.lines.append('Sys 0 Bec 0 Hyb %d Vmm %d Reg channel[%02d] Field %-4s 1' % (hyb, vmm, ch, a.field))
        print('chip %2d: %s = %d on %d channels' % (c, a.field, a.value, len(channels)))
    ext.write(a.out, a.force)
    print('wrote', a.out)


def cmd_sdt(a):
    """Move the chip threshold (sdt) of some chips by a number of DAC counts."""
    ext = ExtFile.load(a.ext)
    sdt = ext.sdt()
    for c in parse_list(a.chips, 0, N_CHIPS - 1, 'chip'):
        if c not in sdt:
            raise SystemExit('chip %d has no sdt line in %s' % (c, a.ext))
        ext.set_sdt(c, sdt[c] + a.by)
        print('chip %2d: sdt %d -> %d' % (c, sdt[c], sdt[c] + a.by))
    ext.write(a.out, a.force)
    print('wrote', a.out)


def cmd_apply_table(a):
    ext = ExtFile.load(a.ext)
    with open(a.table) as f:
        tab = json.load(f)
    trims = {int(c): {int(ch): int(v) for ch, v in d.items()} for c, d in tab['trims'].items()}
    t = ext.trims() if a.merge else {}
    for c, d in trims.items():
        t.setdefault(c, {}).update(d)
    for c, s in (tab.get('sdt') or {}).items():
        old = ext.set_sdt(int(c), int(s))
        print('chip %2d: sdt %s -> %d' % (int(c), old, int(s)))
    n = ext.set_trims(t, 'from table %s' % os.path.basename(a.table))
    ext.write(a.out, a.force)
    print('wrote %s (%d trim lines)' % (a.out, n))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('summary', help='what a file sets, chip by chip')
    s.add_argument('ext'); s.add_argument('--json', action='store_true'); s.set_defaults(fn=cmd_summary)

    s = sub.add_parser('trims', help='the trims of a file (real trims, 0..31)')
    s.add_argument('ext'); s.add_argument('--chips'); s.add_argument('--json'); s.set_defaults(fn=cmd_trims)

    for name, fn, hlp in (('set', cmd_set, 'set the trim of some channels'),
                          ('shift', cmd_shift, 'change the trim of some channels by a number of steps')):
        s = sub.add_parser(name, help=hlp)
        s.add_argument('ext'); s.add_argument('--out', required=True)
        s.add_argument('--chip', required=True, help='chip number(s) 0..19, e.g. 7 or 2,3')
        s.add_argument('--channels', required=True, help='e.g. 5 or 3,7-9 or all')
        if name == 'set':
            s.add_argument('--trim', type=int, required=True, help='0..31; larger = lower threshold')
        else:
            s.add_argument('--by', type=int, required=True,
                           help='steps; NEGATIVE raises the threshold (about 1.1 mV per step)')
        s.add_argument('--force', action='store_true'); s.set_defaults(fn=fn)

    s = sub.add_parser('raise', help='raise the threshold of single channels (instead of masking them)')
    s.add_argument('ext'); s.add_argument('--out', required=True)
    s.add_argument('--chip', required=True); s.add_argument('--channels', required=True)
    s.add_argument('--steps', type=int, required=True, help='trim steps, about 1.1 mV each')
    s.add_argument('--unmask', action='store_true', help='also remove the mask of these channels')
    s.add_argument('--clip', action='store_true', help='stop at trim 0 instead of refusing')
    s.add_argument('--force', action='store_true'); s.set_defaults(fn=cmd_raise)

    s = sub.add_parser('pedestal', help='same extra trim on all channels + sdt raised to compensate')
    s.add_argument('ext'); s.add_argument('--out', required=True)
    s.add_argument('--trim', type=int, required=True)
    s.add_argument('--chips', default='2-19', help='default 2-19 (hybrids 1-9; H0 is the trigger hybrid)')
    s.add_argument('--sdt-per-trim', type=float, default=1.38,
                   help='sdt counts equivalent to one trim step (measured; see the scan tool)')
    s.add_argument('--force', action='store_true'); s.set_defaults(fn=cmd_pedestal)

    s = sub.add_parser('bit', help='set a one-bit channel setting: sm (mask), sl (leakage), st (test pulse), ...')
    s.add_argument('ext'); s.add_argument('--out', required=True)
    s.add_argument('--chip', required=True); s.add_argument('--channels', required=True)
    s.add_argument('--field', required=True, choices=CHANNEL_BITS)
    s.add_argument('--value', type=int, choices=(0, 1), required=True, help='0 removes the lines (0 is the default)')
    s.add_argument('--why', default='', help='a few words written as a comment in the file')
    s.add_argument('--force', action='store_true'); s.set_defaults(fn=cmd_bit)

    s = sub.add_parser('sdt', help='move the chip threshold of some chips by a number of DAC counts')
    s.add_argument('ext'); s.add_argument('--out', required=True)
    s.add_argument('--chips', default='2-19'); s.add_argument('--by', type=int, required=True,
                                                               help='DAC counts, about 0.77 mV each; negative lowers')
    s.add_argument('--force', action='store_true'); s.set_defaults(fn=cmd_sdt)

    s = sub.add_parser('apply-table', help='write the trims (and sdt) of a JSON table into a file')
    s.add_argument('ext'); s.add_argument('--out', required=True); s.add_argument('--table', required=True)
    s.add_argument('--merge', action='store_true', help='keep trims of the file that the table does not mention')
    s.add_argument('--force', action='store_true'); s.set_defaults(fn=cmd_apply_table)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, FileExistsError) as e:
        raise SystemExit('vmm_trim: ' + str(e))


if __name__ == '__main__':
    main()
