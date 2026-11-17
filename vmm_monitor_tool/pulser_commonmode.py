#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pulse-by-pulse look at the neighbour read-outs of a pulser capture: is the
extra spread of the neighbour ADC common to the channels (common mode), and
is it tied to the pulse?

With the chip threshold raised, every pulse gives exactly one hit per channel
(pulsed channels with the over-threshold flag, the others as neighbours), read
out in channel order 0..63. A new pulse starts when the channel number does not
increase. For each chip this builds an events x 64 table of ADC values (only
complete events are kept) and computes, for the neighbour channels:

    dev(e, ch)  = adc - mean of that channel
    CM(e)       = mean of dev over the neighbour channels of the event
    raw sigma   = std of dev per channel
    cm sigma    = std of (dev - beta_ch * CM) per channel, beta_ch fitted per channel
    PULSE(e)    = mean deviation of the pulsed channels' ADC in the event
    correlations: mean correlation of neighbour pairs at distance 1..3 and >= 10,
                  corr(CM, PULSE)

Writes a result in the pulser_pedestal format (`nb` = cm-corrected statistics,
`nb_raw` = raw) so that `noise_ratefit.py compare` can use it, plus a summary.

    pulser_commonmode.py CAPTURE.pcapng --ext PHASE_FILE.txt --chips 2,3 --out RESULT.cm.json
"""

import argparse
import json
import os
import sys

import numpy as np

np.seterr(all='ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pulser_pedestal as pp  # noqa: E402
import vmm_trim  # noqa: E402

NCH = 64


def events_matrix(ch, adc, expected):
    """Hits of one chip in stream order -> (events x 64) float array, NaN where missing; only the rows
    where every expected channel (pulsed or neighbour in this phase) is present."""
    ch = ch.astype(np.int64)
    new = np.concatenate([[True], np.diff(ch) <= 0])
    ev = np.cumsum(new) - 1
    n_ev = int(ev[-1]) + 1 if ev.size else 0
    M = np.full((n_ev, NCH), np.nan)
    M[ev, ch] = adc
    full = ~np.isnan(M[:, sorted(expected)]).any(axis=1)
    return M[full], n_ev


def analyse_chip(M, roles):
    # neighbour channels with a spread (a channel stuck at one ADC value, e.g. chip 2 ch 59, is left out)
    nb = [k for k in range(NCH) if roles.get(k) in ('L', 'U') and np.nanstd(M[:, k]) > 0]
    pu = [k for k in range(NCH) if roles.get(k) == 'P']
    dev = M - M.mean(axis=0)
    D = dev[:, nb]
    cm = D.mean(axis=1)
    pulse = dev[:, pu].mean(axis=1) if pu else np.zeros(len(M))
    var_cm = cm.var()
    out = {}
    for j, k in enumerate(nb):
        d = D[:, j]
        beta = float(np.cov(d, cm, bias=True)[0, 1] / var_cm) if var_cm > 0 else 0.0
        r = d - beta * cm
        out[k] = {'raw_std': float(d.std()), 'cm_std': float(r.std()), 'beta': beta,
                  'mean': float(M[:, k].mean()), 'corr_cm': float(np.corrcoef(d, cm)[0, 1])}
    C = np.corrcoef(D.T)
    near, far = [], []
    for a in range(len(nb)):
        for b in range(a + 1, len(nb)):
            dist = abs(nb[a] - nb[b])
            if dist <= 3:
                near.append(C[a, b])
            elif dist >= 10:
                far.append(C[a, b])
    summ = {'events': int(len(M)), 'cm_std_adc': float(cm.std()),
            'cm_std_if_independent_adc': float(np.sqrt((D.var(axis=0)).mean() / len(nb))),
            'corr_near_mean': float(np.mean(near)), 'corr_far_mean': float(np.mean(far)),
            'corr_cm_pulse': float(np.corrcoef(cm, pulse)[0, 1]) if pu else None,
            'pulse_std_adc': float(pulse.std()) if pu else None}
    return out, summ


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pcap')
    ap.add_argument('--ext', required=True)
    ap.add_argument('--chips', required=True)
    ap.add_argument('--mb', type=float, default=0)
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    chips = vmm_trim.parse_list(a.chips, 0, vmm_trim.N_CHIPS - 1, 'chip')
    (chip, ch, adc, over), span = pp.decode(a.pcap, a.mb)
    st = vmm_trim.ExtFile.load(a.ext).channel_bits('st')
    res = {'pcap': os.path.basename(a.pcap), 'ext': os.path.basename(a.ext), 'live_s': span, 'chips': {}}
    for c in chips:
        s = chip == c
        roles = pp.roles(sorted(st.get(c, [])))
        M, n_ev = events_matrix(ch[s], adc[s], roles.keys())
        per, summ = analyse_chip(M, roles)
        summ['events_seen'] = n_ev
        chans = {}
        for k in range(NCH):
            role = roles.get(k, '-')
            if k in per:
                x = per[k]
                chans[str(k)] = {'role': role,
                                 'nb': {'n': summ['events'], 'mean': round(x['mean'], 3), 'std': round(x['cm_std'], 4)},
                                 'nb_raw': {'n': summ['events'], 'mean': round(x['mean'], 3), 'std': round(x['raw_std'], 4)},
                                 'beta': round(x['beta'], 3), 'corr_cm': round(x['corr_cm'], 3)}
            else:
                chans[str(k)] = {'role': role, 'nb': {'n': 0}}
        res['chips'][str(c)] = {'summary': summ, 'channels': chans}
        mid = [per[k] for k in per if 8 <= k < 56]
        end = [per[k] for k in per if k < 8 or k >= 56]
        print('chip %d: %d complete pulses of %d | CM std %.2f ADC (%.2f if independent) | corr near %.3f far %.3f | '
              'corr(CM, pulse) %s' % (c, summ['events'], n_ev, summ['cm_std_adc'], summ['cm_std_if_independent_adc'],
                                      summ['corr_near_mean'], summ['corr_far_mean'],
                                      '%.3f' % summ['corr_cm_pulse'] if summ['corr_cm_pulse'] is not None else '-'))
        for name, grp in (('ends ', end), ('middle', mid)):
            if grp:
                print('   %s: raw sigma %.2f -> cm-corrected %.2f ADC, beta median %.2f'
                      % (name, np.median([g['raw_std'] for g in grp]), np.median([g['cm_std'] for g in grp]),
                         np.median([g['beta'] for g in grp])))
    with open(a.out, 'w') as f:
        json.dump(res, f, indent=1)
    print('wrote', a.out)


if __name__ == '__main__':
    main()
