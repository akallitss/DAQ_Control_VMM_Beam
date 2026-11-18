#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vmm_stations.py — telescope geometry, trigger, channel masking, and the
pcapng -> hits / (vmm, ch) -> pad steps for the offline efficiency/map analyses
of the SPS H4 2026 beam test.

The decoding and the mapping are the P2 readout packages', not copies:
    parse_pcapng      p2_decode.decode          (was a scapy parser lifted from
                                                 the old vmm_pcapng_qa.py)
    STATIONS,         p2-mapping setup          (was a copy of
    build_pad_table   SETUP_NAME, at CABLING_AT  run_config_beam.P2_VMM_CABLING
                                                 and P2_BASKET_mapping.csv)
so the analyses, the processor and the QA decode and map the same way. Both
packages are installed by bash_scripts/p2_readout.sh (in the DAQ .venv too);
elsewhere (lxplus/EOS) `pip install` them at the tags in p2_readout_versions.txt.

One difference to the old parser: it skipped the last 6-byte word of every
packet (p2-decode fixed that in 0.2), so captures now give ~0.07% more hits.
parse_pcapng(..., legacy_word_count=True) gives the old hits exactly.

@author: ak271430 Alexandra Kallitsopoulou
"""

import os

import numpy as np

from p2_mapping import routing
from p2_mapping.setups import load_setup
from p2_mapping.table import build_table

# --------------------------------------------------------------------------
# Telescope
# --------------------------------------------------------------------------
# The beam-test setup and the cabling these analyses use. Both SPS cabling
# entries (2026-07-29, 2026-08-01) have the same hybrids/VMMs and orders; they
# differ only in P2_MID's cables.
SETUP_NAME = os.environ.get('VMM_STATIONS_SETUP', 'sps_h4_2026')
CABLING_AT = os.environ.get('VMM_STATIONS_WHEN', '2026-07-30T12:00:00')
SETUP = load_setup(SETUP_NAME)
_CABLING = SETUP.cabling_at(CABLING_AT)

# station -> {'z_mm', 'connectors': {'c<N>': (hybrid, bottom VMM, top VMM)}}
STATIONS = {
    det: {'z_mm': float(place['z_mm']),
          'connectors': {f"c{r['connector']}": (int(r['hybrid']), int(r['bottom_vmm']),
                                                 int(r['top_vmm']))
                         for r in sorted(_CABLING.hybrids, key=lambda r: r['connector'])
                         if r['detector'] == det}}
    for det, place in SETUP.at(CABLING_AT).detectors.items()
}

STATION_VMMS = {
    name: sorted(v for _, b, t in cfg['connectors'].values() for v in (b, t))
    for name, cfg in STATIONS.items()
}
VMM_TO_STATION = {v: name for name, vs in STATION_VMMS.items() for v in vs}
DETECTOR_VMMS = sorted(VMM_TO_STATION)

# --------------------------------------------------------------------------
# Trigger
# --------------------------------------------------------------------------
# MEASURED 2026-07-30 from run_24/nominal_05: VMM 0 channel 44 carries 100.0%
# of that VMM's hits (94,223/94,223 in a 200-packet sample) and VMM 1 never
# appears in the data at all.
#
# run_config_beam.TRIGGER_VMM records {'vmm': 1, 'channel': 60} from colleagues'
# July notes, which predates the 2026-07-29 cabling. The data disagrees; the
# data wins. Every trigger-referenced efficiency number depends on this, so if
# the hybrid-0 cabling is ever changed, re-run:
#     python3 vmm_efficiency.py <file.pcapng> --find-trigger
TRIGGER = {'vmm': 0, 'channel': 44}

# --------------------------------------------------------------------------
# Channel masking
# --------------------------------------------------------------------------
# Known-hot channels, confirmed 2026-07-30 and known to the group beforehand.
# They are recorded in the data on purpose and masked here, in analysis, rather
# than being suppressed at the DAQ.
#
#   VMM 7 ch 62  — P2_IN c6 (top), 63.3% of that VMM's hits
#   VMM 4 ch 58  — P2_IN c5 (bottom), 27.4% (ch 59/61 also elevated)
#
# Every healthy VMM's busiest channel sits at 3-6%. Left unmasked these drag
# P2_IN cluster centroids, which is what poisoned the cosmic-bench det3
# analysis (pad 510 took 55% of signal hits).
NOISY_CHANNELS = {
    7: [62],
    4: [58],
}

# A channel is auto-flagged when it holds more than this many times its own
# VMM's median channel occupancy. 8x is comfortably above the ~2-3x structure
# of a real beam spot and catches ch 61/59 on VMM 4.
HOT_RATIO = 8.0

CLOCK_PERIOD_NS = 22.5   # ns per BCID count (44.44 MHz)


def auto_hot_channels(hits, ratio=HOT_RATIO, min_hits=200):
    """Return {vmm: [ch, ...]} for channels far above their VMM's median.

    Purely data-driven, so it also catches channels that go hot mid-campaign.
    VMMs with fewer than min_hits are skipped (median is meaningless there).
    """
    found = {}
    for vmm, grp in hits.groupby('vmm', observed=True):
        if len(grp) < min_hits:
            continue
        counts = np.bincount(grp['ch'].to_numpy(), minlength=64).astype(float)
        med = np.median(counts[counts > 0]) if np.any(counts > 0) else 0.0
        if med <= 0:
            continue
        hot = np.flatnonzero(counts > ratio * med)
        if hot.size:
            found[int(vmm)] = [int(c) for c in hot]
    return found


def merge_masks(*masks):
    """Union of several {vmm: [ch,...]} dicts."""
    out = {}
    for m in masks:
        for vmm, chans in (m or {}).items():
            out.setdefault(int(vmm), set()).update(int(c) for c in chans)
    return {v: sorted(cs) for v, cs in sorted(out.items())}


def apply_channel_mask(hits, mask):
    """Drop masked (vmm, ch) pairs. Trigger hits are never masked."""
    if not mask:
        return hits, 0
    t_vmm, t_ch = TRIGGER['vmm'], TRIGGER['channel']
    drop = np.zeros(len(hits), dtype=bool)
    vmm = hits['vmm'].to_numpy()
    ch = hits['ch'].to_numpy()
    for v, chans in mask.items():
        if v == t_vmm:
            chans = [c for c in chans if c != t_ch]
        if chans:
            drop |= (vmm == v) & np.isin(ch, chans)
    return hits.loc[~drop], int(drop.sum())


def trigger_times(hits, trigger=None):
    """Sorted, de-duplicated trigger timestamps in ns."""
    trg = trigger or TRIGGER
    sel = (hits['vmm'] == trg['vmm']) & (hits['ch'] == trg['channel'])
    t = np.unique(hits.loc[sel, 'abs_time_ns'].to_numpy())
    return np.sort(t)


def find_trigger_channel(hits, min_share=0.80):
    """Identify the trigger channel empirically: the (vmm, ch) that holds
    >= min_share of its VMM's hits. Returns (vmm, ch, share) or None.

    Use this after any hybrid-0 recabling rather than trusting TRIGGER.
    """
    best = None
    for vmm, grp in hits.groupby('vmm', observed=True):
        counts = np.bincount(grp['ch'].to_numpy(), minlength=64)
        ch = int(np.argmax(counts))
        share = counts[ch] / max(len(grp), 1)
        if share >= min_share and (best is None or share > best[2]):
            best = (int(vmm), ch, float(share))
    return best


# --------------------------------------------------------------------------
# pcapng parsing  (p2_decode)
# --------------------------------------------------------------------------
HIT_COLUMNS = ('vmm', 'ch', 'adc', 'over_threshold', 'offset', 'bcid', 'tdc',
               'timestamp_ns', 'srs_timestamp', 'abs_time_ns')


def parse_pcapng(path, max_packets=None, data_format='SRS', src_ips=None,
                 progress=False, legacy_word_count=False):
    """Decode a VMM pcapng into a hits DataFrame, with p2_decode.decode.

    Returns (hits, meta). hits columns: vmm, ch, adc, over_threshold, offset,
    bcid, tdc, timestamp_ns, srs_timestamp, abs_time_ns.

    meta carries n_packets, n_vm3_packets, bad_vmm_ids (counts of VMM ids that
    are neither a station VMM nor the trigger — corrupt words) and
    marker_gap_ns_p50/p99, the SRS marker cadence per VMM. The cadence matters:
    `offset` is stuck at a constant in this firmware (see the plan doc), so the
    BCID rollover counter is unavailable and chip time is only unambiguous
    within 4096 * 22.5 ns = 92.16 us of a marker.
    `progress` is accepted for compatibility (the decode takes seconds now).
    """
    from p2_decode import decode
    dec, m = decode(path, data_format=data_format, src_ips=src_ips,
                    max_packets=max_packets, legacy_word_count=legacy_word_count)
    hits = dec[list(HIT_COLUMNS)].reset_index(drop=True)

    vmm = hits['vmm'].to_numpy()
    stray = vmm[~np.isin(vmm, DETECTOR_VMMS + [TRIGGER['vmm']])]
    ids, n = np.unique(stray, return_counts=True)
    offset = hits['offset'].to_numpy()
    meta = {
        'n_packets': int(m.get('n_packets', 0)),
        'n_vm3_packets': int(m.get('n_vm3_packets', 0)),
        'n_hits': len(hits),
        'fec_ips': sorted(m.get('fec_ips', [])),
        'bad_vmm_ids': {int(k): int(v) for k, v in zip(ids, n)},
        'offset_constant': bool(len(np.unique(offset)) == 1),
    }
    meta.update(_marker_cadence(hits))
    return hits, meta


def _marker_cadence(hits):
    """Median/99th-percentile spacing between distinct SRS marker values, in ns.

    Compared against the 92.16 us BCID rollover: if p99 exceeds it, chip time
    can alias and abs_time_ns is not safe for coincidences.
    """
    gaps = []
    for _, grp in hits.groupby('vmm', observed=True):
        ts = np.unique(grp['srs_timestamp'].to_numpy())
        if ts.size > 2:
            gaps.append(np.diff(ts.astype(np.float64)) * 25.0)
    if not gaps:
        return {'marker_gap_ns_p50': None, 'marker_gap_ns_p99': None}
    allg = np.concatenate(gaps)
    return {'marker_gap_ns_p50': float(np.percentile(allg, 50)),
            'marker_gap_ns_p99': float(np.percentile(allg, 99)),
            'bcid_rollover_ns': 4096 * CLOCK_PERIOD_NS}


# --------------------------------------------------------------------------
# (vmm, ch) -> pad
# --------------------------------------------------------------------------
# Chain, as p2_mapping.table.build_table does it (first written in
# cosmic_bench_analysis/p2_mapping.py, M3-validated for the DREAM/K59V readout):
#
#   (vmm, ch)
#     -> station, connector N, half          (the setup's cabling: bottom/top VMM)
#     -> within = ch (0..63)
#     -> strip   = half_base + f(within)     (WITHIN_STRATEGY, see below)
#     -> sector  = N - 1
#     -> channel_id = sector*128 + (strip-1)
#     -> pad_cx, pad_cy                      (p2-mapping geometry/p2_basket_pads.csv)
#
# All three stations are identical P2 detectors, so the same 1280-pad map
# applies to each; they differ only in which connectors are instrumented
# (c4-c6) and in z.
#
# TWO LINKS ARE NOT YET VALIDATED FOR THE VMM HYBRID and are therefore
# swappable, exactly as p2_mapping.py keeps the DREAM one swappable:
#
#   WITHIN_STRATEGY  'reverse' is validated for DREAM/K59V, where the ordering
#                    is a property of that adapter. The VMM hybrid plugs in
#                    differently, so it must be re-validated here.
#   BOTTOM_VMM_HALF  whether the hybrid's bottom VMM reads the 'bot' half
#                    (strips 1-64) or the 'top' half (65-128).
#
# Validate with:  python3 vmm_map_scan.py <file.pcapng>
# which renders the beam spot under every combination and ranks them by
# compactness -- the same argument that settled the run-67 orientation and the
# det1 mapping. A wrong ordering scatters the spot across the fan.
_DESIGN = routing.design(SETUP.design)
STRIPS_PER_CONN = _DESIGN['strips_per_connector']
CH_PER_HALF = _DESIGN['channels_per_half']
N_CONNECTORS = _DESIGN['n_connectors']

WITHIN_STRATEGY = _DESIGN['default_order']        # 'reverse'
BOTTOM_VMM_HALF = _DESIGN['bottom_vmm_half']      # 'bot'

# Per-(station, connector_N, half) exceptions to WITHIN_STRATEGY, for ribbons
# cabled differently from the rest of the detector. The DREAM readout needed
# exactly this on P2_IN (flipped c_5_top ribbon, commit 659dfbb), and the VMM
# beam-spot scan finds P2_IN anomalous too. Filled in by vmm_map_scan.py
# --per-half; empty means every half uses WITHIN_STRATEGY.
#
# MEASURED 2026-07-30 (run_24/nominal_05, trigger-coincident hits): every half
# is 'reverse' except P2_IN c5 top, which is 'linear' — a flipped ribbon. This
# is INDEPENDENT confirmation of the DREAM-side finding (commit 659dfbb,
# "P2_IN mapping fix (flipped c_5_top ribbon)"): the same ribbon, recovered
# through a completely different readout chain. Median distance from the beam
# centroid for that half: linear 59.7 mm vs reverse 103.8 mm.
# Found by vmm_map_rough.py, which minimises SPECKLE (dead pads next to bright
# ones) rather than beam-spot compactness. Compactness is nearly blind to a
# permutation inside a 64-strip half -- it was the speckled texture on the hit
# maps that gave these away.
#   P2_IN c5 top  — also found by the compactness scan, and independently by
#                   the Dream side (commit 659dfbb, "flipped c_5_top ribbon")
#   P2_IN c6 top  — only the speckle metric sees this one
# Roughness after: P2_OUT 0.057, P2_MID 0.105, P2_IN 0.239 -> 0.184.
# P2_MID and P2_OUT are not improved by any ordering, i.e. they are already
# correct; P2_IN stays roughest even after the fix (see the plan doc).
# The overrides are recorded in the setup's cabling rows (top_order/bottom_order).
STRATEGY_OVERRIDES = {
    (r['detector'], int(r['connector']), half): r[key]
    for r in _CABLING.hybrids
    for key, half in (('bottom_order', BOTTOM_VMM_HALF),
                      ('top_order', 'top' if BOTTOM_VMM_HALF == 'bot' else 'bot'))
    if r.get(key)
}

PAD_TABLE_COLUMNS = ['vmm', 'ch', 'station', 'connector_N', 'half', 'sector', 'strip',
                     'channel_id', 'pad_cx', 'pad_cy', 'radius', 'phi', 'pad_w', 'pad_h',
                     'pad_angle', 'delta_phi', 'mapped']


def vmm_half_map(bottom_half=BOTTOM_VMM_HALF):
    """{vmm_id: (station, connector_N, half)} from P2_VMM_CABLING."""
    top_half = 'top' if bottom_half == 'bot' else 'bot'
    out = {}
    for station, cfg in STATIONS.items():
        for cname, (_hyb, bot_vmm, top_vmm) in cfg['connectors'].items():
            n = int(cname.lstrip('c'))
            out[int(bot_vmm)] = (station, n, bottom_half)
            out[int(top_vmm)] = (station, n, top_half)
    return out


def build_pad_table(strategy=WITHIN_STRATEGY, bottom_half=BOTTOM_VMM_HALF,
                    overrides=None):
    """One row per instrumented (vmm, ch), joined to pad geometry
    (p2_mapping.table.build_table of SETUP_NAME at CABLING_AT).

    Every half is read with `strategy`, except the (station, connector_N, half)
    keys of `overrides` (default STRATEGY_OVERRIDES; {} for none).
    Columns: vmm, ch, station, connector_N, half, sector, strip, channel_id,
             pad_cx, pad_cy, radius, phi, pad_w, pad_h, pad_angle, delta_phi, mapped.
    """
    over = dict(STRATEGY_OVERRIDES if overrides is None else overrides)
    tab = build_table(SETUP, CABLING_AT, order=strategy, orders=over,
                      bottom_half=bottom_half)
    tab = tab[[c for c in PAD_TABLE_COLUMNS if c in tab.columns]].reset_index(drop=True)
    tab.attrs.clear()
    tab.attrs.update(strategy=strategy, bottom_half=bottom_half, overrides=over)
    return tab


def attach_pads(hits, tab):
    """Left-join hits to the pad table on (vmm, ch)."""
    return hits.merge(tab, on=['vmm', 'ch'], how='left', copy=False)


def beam_spread(hits, tab, station=None):
    """RMS radius (mm) of the hit distribution in pad space, and the hit count.

    The mapping-quality metric: the beam is a compact spot, so the correct
    (strategy, bottom_half) combination minimises this. A wrong within-half
    ordering smears hits across the fan and inflates it.
    """
    h = attach_pads(hits, tab)
    h = h[h['mapped'].astype(bool)]
    if station is not None:
        h = h[h['station'] == station]
    if len(h) < 100:
        return float('nan'), len(h)
    x = h['pad_cx'].to_numpy(); y = h['pad_cy'].to_numpy()
    return float(np.sqrt(np.var(x) + np.var(y))), len(h)


def _neighbours(cx, cy, k=6):
    """Indices of the k nearest pads for each pad (brute force; ~400 pads)."""
    d = np.hypot(cx[:, None] - cx[None, :], cy[:, None] - cy[None, :])
    np.fill_diagonal(d, np.inf)
    return np.argsort(d, axis=1)[:, :k]


def map_roughness(hits, tab, station, min_frac=0.05):
    """How SPECKLED the occupancy is — the metric that actually finds a
    scrambled channel ordering.

    beam_spread() (compactness) is nearly blind to this: permuting channels
    inside a 64-strip half leaves the hits in the same part of the fan, so the
    RMS hardly moves. What a wrong ordering DOES produce is high-frequency
    texture -- dead pads sitting next to bright ones, and stripes -- which is
    what the eye picks up immediately on a hit map.

    So: for every pad in the beam region, compare its count to the median of
    its 6 nearest neighbours. A correct map is locally smooth (small values);
    a scrambled one is speckled (large). Returned value is the median over
    pads of |n - med| / (n + med + 1), in [0, 1].
    """
    h = attach_pads(hits, tab)
    h = h[h['mapped'].astype(bool) & (h['station'] == station)]
    sub = tab[(tab['station'] == station) & tab['mapped'].astype(bool)].reset_index(drop=True)
    if len(h) < 500 or len(sub) < 20:
        return float('nan')

    key = sub['vmm'].to_numpy().astype(np.int64) * 64 + sub['ch'].to_numpy()
    lut = {int(k): i for i, k in enumerate(key)}
    n = np.zeros(len(sub))
    hk = h['vmm'].to_numpy().astype(np.int64) * 64 + h['ch'].to_numpy()
    uk, uc = np.unique(hk, return_counts=True)
    for k, c in zip(uk, uc):
        i = lut.get(int(k))
        if i is not None:
            n[i] = c

    cx = sub['pad_cx'].to_numpy(float)
    cy = sub['pad_cy'].to_numpy(float)
    nb = _neighbours(cx, cy)
    med = np.median(n[nb], axis=1)
    # Only judge the illuminated region: empty fringes are genuinely empty.
    live = med > min_frac * np.max(med) if np.max(med) > 0 else np.zeros(len(n), bool)
    if live.sum() < 10:
        return float('nan')
    return float(np.median(np.abs(n[live] - med[live]) / (n[live] + med[live] + 1.0)))


# A pad is flagged hot when it holds more than this many times the median of
# its 6 spatial neighbours. Calibrated on run_25: the worst pad on the healthy
# P2_OUT is 4.7x, so 6x flags nothing there while catching the genuine outliers
# on IN (13-30x) and MID (8-986x).
HOT_PAD_RATIO = 6.0


def auto_hot_pads(hits, tab, ratio=HOT_PAD_RATIO, min_hits=50):
    """Flag pads far above their SPATIAL neighbours -> {vmm: [ch, ...]}.

    Strictly better than auto_hot_channels() on this detector: occupancy varies
    a lot across the fan, so a per-VMM median hides a channel that is hot
    relative to the pads actually next to it. Needs the pad map, so it only
    became possible once the mapping was validated.

    Pads whose neighbours are all dead give a large ratio by construction. They
    are flagged too -- a lone firing pad in a dead neighbourhood is not usable
    for tracking either way -- but the caller should log what was masked.
    """
    # Occupancy per (vmm, ch), counted once on the raw arrays. The pad geometry
    # is only ever needed per PAD, so joining it onto every hit is waste: on a
    # 5.5 M hit capture attach_pads() materialised ~1.5 GB (17 pad columns x
    # every hit) and was the single largest allocation in the whole reduce.
    # There are at most 32*64 keys, so the per-station work below indexes a
    # small histogram instead. The station/mapped filters that used to be
    # applied to the hits are redundant — `sub` already contains only mapped
    # pads of one station, so a key absent from it is simply never looked up.
    hk = hits['vmm'].to_numpy().astype(np.int64) * 64 + hits['ch'].to_numpy()
    uk, uc = np.unique(hk, return_counts=True)
    occ = dict(zip(uk.tolist(), uc.tolist()))

    found = {}
    for st in STATIONS:
        sub = tab[(tab['station'] == st) & tab['mapped'].astype(bool)].reset_index(drop=True)
        if len(sub) < 20:
            continue
        key = sub['vmm'].to_numpy().astype(np.int64) * 64 + sub['ch'].to_numpy()
        n = np.array([occ.get(int(k), 0) for k in key], dtype=float)
        if not n.any():
            continue
        nb = _neighbours(sub['pad_cx'].to_numpy(float), sub['pad_cy'].to_numpy(float))
        med = np.median(n[nb], axis=1)
        hot = np.flatnonzero((n >= min_hits) & (n > ratio * np.maximum(med, 1.0)))
        for i in hot:
            found.setdefault(int(sub['vmm'][i]), []).append(int(sub['ch'][i]))
    return {v: sorted(set(c)) for v, c in found.items()}
