#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write the per-threshold chip configs for the cosmic-muon sdt scan.

See docs/HANDOFF_COSMIC_THRESHOLD_SCAN.md, section 5.3. Takes the July beam
config (the run_52 `_opt` file) and changes ONLY the sdt lines of hybrids 1-9
(and, optionally, appends `sm 1` mask lines), so the chips are byte-for-byte the
beam ones apart from the threshold. H0, the trigger hybrid, is never touched.

    sdt of a chip = (value of bank1spi2 sdt) * 64 + (value of bank1spi1 sdt)
    chip v -> Hyb v//2 Vmm v%2
The '#' comments in the template are NOT trusted: the sdt is recomputed from the
two register values, and a template whose comments disagree is reported.

Each output is named <template stem>_cosmic_d<d>.txt. The run is named after
the file applied, so the name is the label.

    d is the threshold ABOVE each chip's own baseline, in DAC:  sdt = baseline + d

Usage:
    make_cosmic_sdt_configs.py --d 50,40,34,30,27,24,22,20,18,16 [--out DIR]
        [--baselines baselines.json] [--limits limits.json] [--masks masks.json]
        [--hold 4=400,9=400]
        [--dry-run]

  baselines.json  {"2": 208, ...}  chip -> baseline sdt; overrides the defaults
                  below (replace them with the vmmsc pedestals of handoff 5.0).
  limits.json     {"5": 24, ...}   chip -> smallest d it may be taken at (its
                  noise limit from the 5.1 scan). A chip asked for a smaller d
                  is held at its limit, and the file says so.
  --hold          chip=sdt pairs: those chips stay at that ABSOLUTE sdt at every d
                  (a chip that already floods the link at the lowest July
                  threshold, e.g. VMM 4 and 9 at sdt 300, must not follow the
                  scan down). Reported in the output.
  masks.json      {"40": {"19": [20], "14": [34]}, "default": {...}}
                  d -> chip -> channels to write `sm 1` for. "default" applies
                  to every d that has no entry of its own.
"""

import argparse
import json
import os
import re
import sys

TEMPLATE_DIR = '/local/p2/p2testbench/TestBenchCERN/config_ext'
TEMPLATE = 'p2b-config-cern-ext_gain4.5_peaktime200_opt.txt'

# Chip baselines (sdt DAC), from the July beam ADC edges (handoff 5.2).
# 4, 7 and 9 are extrapolated from sdt 285-300 runs. VMM 6 had an ADC edge ~10
# and its baseline is UNKNOWN: it is held at an ABSOLUTE sdt of 230 (the July
# setting) whatever d is, until the vmmsc pedestals replace this table.
BASELINES = {2: 208, 3: 182, 4: 199, 5: 212, 7: 200, 8: 197, 9: 191, 10: 210,
             11: 204, 12: 210, 13: 188, 14: 201, 15: 180, 16: 199, 17: 187,
             18: 182, 19: 204}
UNKNOWN_BASELINE_ABS = {6: 230}

CHIPS = range(2, 20)          # hybrids 1-9; H0 (chips 0, 1) is never touched

SDT_RE = re.compile(
    r'^(?P<head>Sys\s+0\s+Bec\s+0\s+Hyb\s+(?P<hyb>\d+)\s+Vmm\s+(?P<vmm>\d+)\s+Reg\s+'
    r'bank1spi(?P<spi>[12])\s+Field\s+sdt\s+)(?P<val>\d+)(?P<tail>.*)$')


def chip_of(hyb, vmm):
    return 2 * hyb + vmm


def parse_template(lines):
    """-> {chip: {1: line_idx, 2: line_idx}}, {chip: sdt}, [comment problems]"""
    idx, vals, problems = {}, {}, []
    for i, ln in enumerate(lines):
        m = SDT_RE.match(ln.rstrip('\n'))
        if not m or ln.lstrip().startswith('#'):
            continue
        chip = chip_of(int(m['hyb']), int(m['vmm']))
        idx.setdefault(chip, {})[int(m['spi'])] = i
        vals.setdefault(chip, {})[int(m['spi'])] = int(m['val'])
    sdt = {}
    for chip, v in vals.items():
        if set(v) != {1, 2}:
            raise SystemExit(f'template: chip {chip} has sdt spi lines {sorted(v)}, need both')
        sdt[chip] = v[2] * 64 + v[1]
        c = re.search(r'#\s*(\d+)\s*$', lines[idx[chip][2]])
        if c and int(c.group(1)) != sdt[chip]:
            problems.append(f'chip {chip}: register value says {sdt[chip]}, '
                            f'comment says {c.group(1)}')
    return idx, sdt, problems


def render(lines, idx, new_sdt, masks, sng=False):
    out = list(lines)
    for chip, sdt in new_sdt.items():
        if not 0 <= sdt <= 1023:
            raise SystemExit(f'chip {chip}: sdt {sdt} outside 0..1023')
        for spi, v in ((1, sdt % 64), (2, sdt // 64)):
            i = idx[chip][spi]
            m = SDT_RE.match(out[i].rstrip('\n'))
            tail = m['tail']
            if spi == 2 and re.search(r'#\s*\d+\s*$', tail):
                tail = re.sub(r'(#\s*)\d+(\s*)$', lambda t: f'{t.group(1)}{sdt}{t.group(2)}', tail)
            # head's trailing whitespace already includes the template's pad
            # space for 1-digit values: normalise to one space, then right-align.
            out[i] = f'{m["head"].rstrip()} {v:>2}{tail}\n'
    if masks:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('\n')
        out.append('# Masked channels for this scan point (sm 1 = masked)\n')
        for chip in sorted(masks):
            for ch in sorted(masks[chip]):
                out.append(f'Sys 0 Bec 0 Hyb {chip // 2} Vmm {chip % 2} '
                           f'Reg channel[{ch:02d}] Field sm   1\n')
    if sng:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('\n')
        out.append('# Neighbour read-out on (sng 1) on every detector chip: a channel is read out, with its\n')
        out.append('# ADC value, when a neighbour fires, so below-threshold channels give a measured baseline\n')
        for chip in CHIPS:
            out.append(f'Sys 0 Bec 0 Hyb {chip // 2} Vmm {chip % 2} Reg bank1spi2   Field sng  1\n')
    return out


def check_only_sdt_and_masks_differ(lines, out, n_masks, n_sng=0):
    base = len(lines)
    for i, (a, b) in enumerate(zip(lines, out[:base])):
        if a != b and not SDT_RE.match(a.rstrip('\n')):
            raise SystemExit(f'line {i + 1} changed but is not an sdt line:\n{a}{b}')
    extra = out[base:]
    bad = [l for l in extra if l.strip() and not l.startswith('#')
           and 'Field sm   1' not in l and 'Field sng  1' not in l]
    if (bad or sum('Field sm   1' in l for l in extra) != n_masks
            or sum('Field sng  1' in l for l in extra) != n_sng):
        raise SystemExit('appended lines are not exactly the requested sm 1 masks / sng 1 lines')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--d', required=True, help='comma-separated d values (DAC above baseline)')
    ap.add_argument('--template', default=os.path.join(TEMPLATE_DIR, TEMPLATE))
    ap.add_argument('--out', default=TEMPLATE_DIR)
    ap.add_argument('--baselines')
    ap.add_argument('--limits')
    ap.add_argument('--masks')
    ap.add_argument('--tag', default='', help='inserted in the file name: ..._cosmic<tag>_d<d>.txt '
                                          '(a different mask set or hold is a different label)')
    ap.add_argument('--hold', default='', help='chip=sdt,... held at an absolute sdt')
    ap.add_argument('--chip-d', default='', help='chip:d,... a different d for these chips (instead of --d)')
    ap.add_argument('--sng', action='store_true', help='append sng 1 (neighbour read-out) for every detector chip')
    ap.add_argument('--label', default='', help='file name suffix instead of _d<d> (use with a single --d)')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    base = dict(BASELINES)
    if a.baselines:
        base.update({int(k): int(v) for k, v in json.load(open(a.baselines)).items()})
    limits = {int(k): int(v) for k, v in json.load(open(a.limits)).items()} if a.limits else {}
    maskspec = json.load(open(a.masks)) if a.masks else {}
    ds = [int(x) for x in a.d.split(',')]
    hold = {int(k): int(v) for k, v in (x.split('=') for x in a.hold.split(',') if x)}
    chip_d = {int(k): int(v) for k, v in (x.split(':') for x in a.chip_d.split(',') if x)}

    lines = open(a.template).readlines()
    idx, template_sdt, problems = parse_template(lines)
    missing = [c for c in CHIPS if c not in idx]
    if missing:
        raise SystemExit(f'template has no sdt lines for chips {missing}')
    for p in problems:
        print(f'NOTE (template comment disagrees with its registers): {p}')
    if any(c in template_sdt and c < 2 for c in (0, 1)):
        print('NOTE: H0 (trigger hybrid, chips 0/1) is left exactly as in the template: '
              f'registers give sdt {template_sdt[0]}/{template_sdt[1]}.')
    print('template sdt (July settings):',
          ' '.join(f'{c}:{template_sdt[c]}' for c in CHIPS))

    stem = os.path.basename(a.template)[:-4]
    for d in ds:
        new, held = {}, []
        for c in CHIPS:
            if c in hold:
                new[c] = hold[c]
            elif c in base:
                dd = max(chip_d.get(c, d), limits.get(c, 0))
                if dd != d:
                    held.append(f'{c}(d={dd})')
                new[c] = base[c] + dd
            else:
                new[c] = UNKNOWN_BASELINE_ABS[c]
        m = maskspec.get(str(d), maskspec.get('default', {}))
        masks = {int(c): [int(x) for x in chs] for c, chs in m.items()}
        out = render(lines, idx, new, masks, a.sng)
        check_only_sdt_and_masks_differ(lines, out, sum(len(v) for v in masks.values()), len(CHIPS) if a.sng else 0)
        name = f'{stem}_cosmic{a.tag}{a.label or f"_d{d}"}.txt'
        path = os.path.join(a.out, name)
        print(f'd={d:>3}: {name}  sdt ' + ' '.join(f'{c}:{new[c]}' for c in CHIPS)
              + (f'  | held at their limit: {" ".join(held)}' if held else '')
              + (f'  | {sum(len(v) for v in masks.values())} masks' if masks else ''))
        if not a.dry_run:
            if os.path.exists(path):
                raise SystemExit(f'{path} exists; refusing to overwrite (move it first)')
            with open(path, 'w') as f:
                f.writelines(out)
    print('VMM 6 is held at absolute sdt 230 (baseline unknown) at every d.')
    if hold:
        print('held at an absolute sdt at every d: ' + ' '.join(f'{c}:{v}' for c, v in hold.items()))
    if a.dry_run:
        print('(dry run: nothing written)')


if __name__ == '__main__':
    sys.exit(main())
