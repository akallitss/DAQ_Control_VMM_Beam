#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_from_store.py -- the QA plots vmm_reduce.py deliberately did not draw.

The online watcher reduces each 45 s capture to counts.npz + scalars.json and
draws nothing, for the reasons written at the top of vmm_reduce.py: 36 PNGs per
capture is ~2900 images an hour that nobody opens, and the quiet VMMs get under
one entry per ADC bin in a single file. The counts were kept precisely so the
plots could be made later, once, per sub_run.

This is "later". It sums every capture's counts.npz over a sub_run and renders
that -- which is not merely a cheaper way to get the same pictures, it is a
better measurement:

  * a VMM at 382 hits/file becomes ~18k hits over a sub_run, so its ADC
    spectrum is a spectrum instead of noise;
  * the pad-geometry hit map only means anything with a whole sub_run behind
    it, and the online plots never had one;
  * the per-station ADC spectra show the discriminator wall (the low-edge
    marker) that the run_46 autopsy identified as the efficiency limit.

Not a replacement for qa_catchup.py, which solves the other problem: that one
backfills the DAQ box from ONE capture per sub-run through the normal pcapng
plot path, and every design decision in it is about surviving 7 GB of RAM with
800 MB captures. This one never opens a pcapng, uses EVERY capture, and is
meant to be run off the box -- so the two do not overlap and neither makes the
other unnecessary.

Reads a store tree, writes PNGs plus an index.html per sub_run. Nothing here
touches a pcapng, so it runs anywhere the counts are readable -- including
lxplus against the EOS mount, which is where the campaign lives:

    python3 render_from_store.py /eos/experiment/ntof/data/x17/p2_sps_july/vmm/runs \\
            --out ~/vmm_qa_plots --jobs 8

    python3 render_from_store.py .../vmm/runs --run run_46 --out ./qa    # one run
    python3 render_from_store.py .../run_46/cfg_gain4.5_peaktime200 --out ./qa

@author: ak271430 Alexandra Kallitsopoulou
"""

import argparse
import glob
import html
import json
import os
import sys
import traceback

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from matplotlib.colors import LogNorm
from mpl_toolkits.axes_grid1 import make_axes_locatable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmm_stations as vs
from vmm_reduce import merge_counts, NV, ADC_BINS, CH_BINS, ADC_CH_ADC_BINS

# Station colours, identical to the pipeline and talk figures so the whole set
# reads as one system.
SC = {"P2_IN": "#1f77b4", "P2_MID": "#ff7f0e", "P2_OUT": "#2ca02c"}
TRIGGER_C = "#8c564b"
UNKNOWN_C = "#999999"
DPI = 130

# Hybrid 0 carries VMMs 0 and 1: the external trigger digitizer, not a
# detector. They have no station and no pads, and their hits are the trigger,
# so they must not be reported as uncabled ids the way real corruption is.
TRIGGER_VMMS = {0, 1}


# --------------------------------------------------------------------------
# store discovery and loading
# --------------------------------------------------------------------------

def find_subruns(root, runs=None):
    """Every sub_run directory under `root`, as (run_name, sub_run_name, path).

    Accepts the runs root, a single run directory, or a single sub_run
    directory, so the same argument works at all three levels.
    """
    root = os.path.abspath(root)
    if os.path.isdir(os.path.join(root, "hits_store")):
        return [(os.path.basename(os.path.dirname(root)),
                 os.path.basename(root), root)]

    out = []
    for store in sorted(glob.glob(os.path.join(root, "*", "*", "hits_store"))
                        + glob.glob(os.path.join(root, "*", "hits_store"))):
        sub = os.path.dirname(store)
        run = os.path.basename(os.path.dirname(sub))
        if runs and run not in runs:
            continue
        out.append((run, os.path.basename(sub), sub))
    # Both depths give the right run name for free: the store's grandparent is
    # the run directory whether `root` was the runs root (first glob) or a
    # single run (second glob), so neither case needs special handling.
    return sorted(set(out))


def load_counts(sub_dir):
    """Sum every capture's counts.npz. Returns (counts, n_captures).

    Streams one file at a time -- the merged total is ~2 MB and never grows,
    so a 48-capture sub_run costs the same memory as a single one.

    The stored arrays are uint32. A hot channel over a long sub_run gets within
    a decade of overflowing that (VMM 4 carries 1.1e9 hits in run_46), so the
    accumulator is promoted to int64 before anything is added to it.
    """
    files = sorted(glob.glob(os.path.join(sub_dir, "hits_store", "*",
                                          "counts.npz")))
    if not files:
        return None, 0

    def stream():
        for i, f in enumerate(files):
            with np.load(f) as z:
                d = {k: z[k] for k in z.files}
            if i == 0:      # promote the accumulator, not every summand
                d = {k: v.astype(np.int64) if v.dtype != np.int64 else v
                     for k, v in d.items()}
            yield d

    return merge_counts(stream()), len(files)


def load_scalars(sub_dir):
    """Every capture's scalars.json, in capture order."""
    out = []
    for f in sorted(glob.glob(os.path.join(sub_dir, "hits_store", "*",
                                           "scalars.json"))):
        try:
            with open(f) as fh:
                out.append(json.load(fh))
        except Exception:
            pass
    return out


def run_conditions(sub_dir):
    """{'gas':…, 'start':…, 'hv': {station: (mesh, drift)}} from run_config.json.

    The HV lives as {card: {channel: volts}} per sub_run and the card/channel
    for each station's mesh and drift lives in `detectors`, so the two have to
    be joined to get a voltage anyone can read.
    """
    cfg_path = os.path.join(os.path.dirname(sub_dir), "run_config.json")
    info = {"gas": None, "start": None, "hv": {}}
    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
    except Exception:
        return info

    info["gas"] = cfg.get("gas")
    info["start"] = cfg.get("start_time")

    name = os.path.basename(sub_dir)
    hvs = {}
    for sr in cfg.get("sub_runs") or []:
        if sr.get("sub_run_name") == name:
            hvs = sr.get("hvs") or {}
            break
    for det in cfg.get("detectors") or []:
        ch = det.get("hv_channels") or {}
        got = {}
        for line in ("mesh", "drift"):
            card_ch = ch.get(line)
            if not card_ch:
                continue
            v = (hvs.get(str(card_ch[0])) or {}).get(str(card_ch[1]))
            if v is not None:
                got[line] = float(v)
        if got:
            info["hv"][det.get("name")] = got
    return info


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def active_vmms(counts):
    return [int(v) for v in np.flatnonzero(np.asarray(counts["hits_per_vmm"]))]


def rebin(h, factor):
    """Sum adjacent bins of a 1D histogram. Nothing is dropped: the stored
    binning is finer than any of these plots can show."""
    n = (len(h) // factor) * factor
    return h[:n].reshape(-1, factor).sum(axis=1)


def grid(vmm_ids, per_row=4, w=4.4, h=3.3):
    n = len(vmm_ids)
    ncols = min(per_row, max(n, 1))
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(w * ncols, h * nrows),
                             squeeze=False)
    for i in range(n, nrows * ncols):
        axes[i // ncols][i % ncols].set_visible(False)
    return fig, [axes[i // ncols][i % ncols] for i in range(n)]


def vmm_label(v):
    st = vs.VMM_TO_STATION.get(v)
    if st:
        return f"VMM {v} — {st}"
    return f"VMM {v} — trigger" if v in TRIGGER_VMMS else f"VMM {v} — not cabled"


def vmm_colour(v):
    st = vs.VMM_TO_STATION.get(v)
    if st:
        return SC[st]
    return TRIGGER_C if v in TRIGGER_VMMS else UNKNOWN_C


def save(fig, out_dir, name, drawn):
    fig.savefig(os.path.join(out_dir, name), dpi=DPI, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    drawn.append(name)


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------

def fig_per_vmm_1d(counts, key, ids, factor, xmax, xlabel, title, colour=None,
                   logy=False):
    fig, axes = grid(ids)
    fig.suptitle(title, fontsize=14)
    arr = np.asarray(counts[key])
    for ax, v in zip(axes, ids):
        h = rebin(arr[v].astype(float), factor)
        edges = np.linspace(0, xmax, len(h) + 1)
        ax.stairs(h, edges, fill=True, alpha=0.85,
                  color=colour or vmm_colour(v))
        ax.set_title(f"{vmm_label(v)}  ({int(arr[v].sum()):,})", fontsize=9)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("hits")
        ax.set_xlim(0, xmax)
        if logy:
            ax.set_yscale("log")
    fig.tight_layout()
    return fig


def fig_adc_ot(counts, ids):
    fig, axes = grid(ids)
    fig.suptitle("ADC split by the over-threshold flag", fontsize=14)
    a0, a1 = np.asarray(counts["adc_ot0"]), np.asarray(counts["adc_ot1"])
    for ax, v in zip(axes, ids):
        edges = np.linspace(0, ADC_BINS, ADC_BINS // 8 + 1)
        ax.stairs(rebin(a0[v].astype(float), 8), edges, fill=True, alpha=0.55,
                  color="steelblue", label=f"not OT ({int(a0[v].sum()):,})")
        ax.stairs(rebin(a1[v].astype(float), 8), edges, fill=True, alpha=0.55,
                  color="tomato", label=f"OT ({int(a1[v].sum()):,})")
        ax.set_title(vmm_label(v), fontsize=9)
        ax.set_xlabel("ADC")
        ax.set_ylabel("hits")
        ax.legend(fontsize=7)
    fig.tight_layout()
    return fig


def fig_adc_vs_ch(counts, ids):
    fig, axes = grid(ids)
    fig.suptitle("ADC vs channel (log colour)", fontsize=14)
    arr = np.asarray(counts["adc_vs_ch"])
    for ax, v in zip(axes, ids):
        img = arr[v].T.astype(float)          # (adc_coarse, ch)
        img[img == 0] = np.nan
        m = ax.imshow(img, origin="lower", aspect="auto", cmap="viridis",
                      norm=LogNorm(vmin=1, vmax=max(np.nanmax(img), 2)),
                      extent=[0, CH_BINS, 0, ADC_BINS])
        fig.colorbar(m, ax=ax, label="hits")
        ax.set_title(vmm_label(v), fontsize=9)
        ax.set_xlabel("channel")
        ax.set_ylabel("ADC")
    fig.tight_layout()
    return fig


def fig_hits_per_vmm(counts, ids):
    fig, ax = plt.subplots(figsize=(max(7, 0.55 * len(ids)), 4.4))
    n = np.asarray(counts["hits_per_vmm"])[ids].astype(float)
    ax.bar([str(v) for v in ids], n, color=[vmm_colour(v) for v in ids],
           alpha=0.9)
    ax.set_yscale("log")
    ax.set_xlabel("VMM id")
    ax.set_ylabel("hits (log)")
    ax.set_title("Hits per VMM — bar colour is the station; grey is not cabled "
                 "to the telescope")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    fig.tight_layout()
    return fig


def fig_ot_fraction(counts, ids):
    fig, ax = plt.subplots(figsize=(max(7, 0.55 * len(ids)), 4.0))
    ot = np.asarray(counts["ot"])[ids].astype(float)
    frac = ot[:, 1] / np.maximum(ot.sum(axis=1), 1)
    ax.bar([str(v) for v in ids], frac, color=[vmm_colour(v) for v in ids],
           alpha=0.9)
    ax.set_xlabel("VMM id")
    ax.set_ylabel("over-threshold fraction")
    ax.set_ylim(0, 1.05)
    ax.set_title("Over-threshold flag fraction per VMM")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    fig.tight_layout()
    return fig


def fig_time_profile(counts, ids):
    """Hit rate against position within a capture.

    The stored bins are a fraction of each capture's own frame-counter span, so
    summing across captures averages the shape rather than concatenating it.
    That is the useful thing here: it is the spill structure, seen ~45 s at a
    time and stacked.
    """
    fig, ax = plt.subplots(figsize=(9.5, 5.0))
    arr = np.asarray(counts["time_profile"]).astype(float)
    x = (np.arange(arr.shape[1]) + 0.5) / arr.shape[1]
    for st, vmms in vs.STATION_VMMS.items():
        y = arr[[v for v in vmms if v in ids]].sum(axis=0) if any(
            v in ids for v in vmms) else None
        if y is None or not y.sum():
            continue
        ax.plot(x, y, color=SC[st], lw=1.6, label=st)
    ax.set_xlabel("fraction through the capture")
    ax.set_ylabel("hits per bin, summed over captures")
    ax.set_title("Beam/spill structure within a capture")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def spectrum_marks(tot):
    """(low_edge, mpv, saturated_fraction) of one ADC histogram.

    Neither mark can be read off naively. The lowest *populated* bin is not the
    discriminator wall -- a handful of stray counts sit below it and would put
    the wall at 0 -- so the wall is the 0.5th percentile of the hits. And the
    top bin is an overflow: everything at or above full scale piles into 1023,
    which on a well-illuminated station beats the real peak and would report an
    MPV of 1023. The MPV therefore comes from bins 0..1022 only.
    """
    tot = np.asarray(tot, float)
    n = tot.sum()
    if n <= 0:
        return None, None, 0.0
    low = int(np.searchsorted(np.cumsum(tot), 0.005 * n))
    mpv = int(np.argmax(tot[:-1]))
    return low, mpv, float(tot[-1] / n)


def fig_station_adc(counts, ids):
    """Per-station ADC spectrum, and where its low edge sits.

    This is the plot the online set could not produce: one station's spectrum
    needs every capture behind it. The dashed marker is the discriminator wall,
    below which the chip records nothing.

    Read the shape, not the ratio. The counts store holds EVERY recorded hit,
    with no trigger coincidence, so on a run with a screaming channel this is
    mostly that channel: run_46's P2_IN is 1.14e9 hits sitting in a narrow
    spike, and its wall/MPV of 0.93 describes the noise, not a muon. The
    track-matched spectrum that gives the efficiency-limiting 0.68 lives in the
    autopsy (P2_basket_analysis/sps_beam_analysis/vmm_dream_matching), which
    starts from the uRWELL tracks and cannot be built from counts alone. What
    this figure is good for is the wall itself, the saturation fraction, and
    seeing at a glance which station is dominated by noise.
    """
    fig, ax = plt.subplots(figsize=(9.8, 5.6))
    arr = np.asarray(counts["adc"]).astype(float)
    edges = np.linspace(0, ADC_BINS, ADC_BINS // 8 + 1)
    notes = []
    for st, vmms in vs.STATION_VMMS.items():
        sel = [v for v in vmms if v in ids]
        if not sel:
            continue
        tot = arr[sel].sum(axis=0)
        low, mpv, sat = spectrum_marks(tot)
        if low is None:
            continue
        ax.stairs(rebin(tot, 8), edges, color=SC[st], lw=1.7,
                  label=f"{st}  ({int(tot.sum()):,} hits)")
        ax.axvline(low, color=SC[st], ls="--", lw=1.1, alpha=0.85)
        notes.append((st, f"{st}: wall {low} ADC, MPV {mpv}, "
                          f"wall/MPV {low / max(mpv, 1):.2f}, "
                          f"saturated {sat:.2%}"))
    for i, (st, txt) in enumerate(notes):
        ax.text(0.015, 0.06 + 0.055 * (len(notes) - 1 - i), txt,
                transform=ax.transAxes, color=SC[st], fontsize=9.5)
    ax.set_yscale("log")
    ax.set_xlabel("ADC")
    ax.set_ylabel("hits (log)")
    ax.set_xlim(0, ADC_BINS)
    ax.set_title("Pulse height per station — ALL recorded hits, no trigger "
                 "coincidence\ndashed line is the discriminator wall "
                 "(0.5th percentile)", fontsize=11)
    ax.legend(loc="upper right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def pad_polygons(cx, cy, w, h, ang_deg):
    """The four corners of each pad, rotated by its own pad_angle."""
    a = np.deg2rad(np.asarray(ang_deg, float))
    ca, sa = np.cos(a), np.sin(a)
    ux = np.array([-0.5, 0.5, 0.5, -0.5])
    uy = np.array([-0.5, -0.5, 0.5, 0.5])
    dx = ux[None, :] * np.asarray(w, float)[:, None]
    dy = uy[None, :] * np.asarray(h, float)[:, None]
    X = np.asarray(cx, float)[:, None] + dx * ca[:, None] - dy * sa[:, None]
    Y = np.asarray(cy, float)[:, None] + dx * sa[:, None] + dy * ca[:, None]
    return np.stack([X, Y], axis=-1)


def fig_padmap(counts, ids, tab):
    """Occupancy on the true fan-pad geometry, one panel per station.

    Only connectors c4-c6 (sectors 3-5, channel_id 384-767) are instrumented,
    so this is a wedge of the full 1280-pad anode, not the whole detector.
    """
    ch = np.asarray(counts["ch"])
    tab = tab[tab["mapped"]].copy()
    tab["n"] = [int(ch[int(r.vmm), int(r.ch)]) for r in tab.itertuples()]

    stations = [s for s in vs.STATION_VMMS
                if not tab[(tab.station == s) & (tab.n > 0)].empty]
    if not stations:
        return None

    fig, axes = plt.subplots(1, len(stations),
                             figsize=(5.6 * len(stations), 4.6), squeeze=False)
    for ax, st in zip(axes[0], stations):
        d = tab[tab.station == st]
        polys = pad_polygons(d.pad_cx, d.pad_cy, d.pad_w, d.pad_h, d.pad_angle)
        n = d["n"].to_numpy(float)
        pc = PolyCollection(polys, array=np.where(n > 0, n, np.nan),
                            cmap="viridis", edgecolors="#00000018",
                            linewidths=0.3,
                            norm=LogNorm(vmin=max(n[n > 0].min(), 1),
                                         vmax=max(n.max(), 2)))
        ax.add_collection(pc)
        # Tie the bar to the axes box: the pads force an equal aspect, so a
        # figure-level colorbar detaches from the panel it belongs to.
        cax = make_axes_locatable(ax).append_axes("right", size="4%", pad=0.08)
        fig.colorbar(pc, cax=cax, label="hits")
        ax.set_xlim(d.pad_cx.min() - 20, d.pad_cx.max() + 20)
        ax.set_ylim(d.pad_cy.min() - 20, d.pad_cy.max() + 20)
        ax.set_aspect("equal")
        ax.set_xlabel("x [mm]")
        ax.set_ylabel("y [mm]")
        live = int((n > 0).sum())
        ax.set_title(f"{st} — {live}/{len(d)} pads lit, {int(n.sum()):,} hits",
                     fontsize=11)
    fig.suptitle("Occupancy on the instrumented pads (connectors c4–c6)",
                 fontsize=14)
    fig.tight_layout()
    return fig


def fig_trends(scalars):
    """Per-capture trends across the sub_run, from scalars.json.

    These are the quantities that have already caught real problems on this
    setup: occupancy, live channel count, ADC median (gain), and the
    over-threshold fraction.
    """
    if not scalars:
        return None
    x = np.arange(len(scalars))
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 7.4))

    ax = axes[0][0]
    for st, vmms in vs.STATION_VMMS.items():
        y = [sum(int(s.get("hits_per_vmm", {}).get(str(v), 0)) for v in vmms)
             for s in scalars]
        if any(y):
            ax.plot(x, y, color=SC[st], marker=".", ms=4, lw=1.2, label=st)
    ax.set_ylabel("hits per capture")
    ax.set_yscale("log")
    ax.set_title("Occupancy")
    ax.legend(fontsize=8)

    ax = axes[0][1]
    for st, vmms in vs.STATION_VMMS.items():
        y = [np.mean([s["adc_p50_per_vmm"][str(v)] for v in vmms
                      if s.get("adc_p50_per_vmm", {}).get(str(v)) is not None]
                     or [np.nan]) for s in scalars]
        if not np.all(np.isnan(y)):
            ax.plot(x, y, color=SC[st], marker=".", ms=4, lw=1.2, label=st)
    ax.set_ylabel("median ADC")
    ax.set_title("Pulse height (gain proxy)")
    ax.legend(fontsize=8)

    ax = axes[1][0]
    for st, vmms in vs.STATION_VMMS.items():
        y = [sum(int(s.get("live_channels_per_vmm", {}).get(str(v), 0))
                 for v in vmms) for s in scalars]
        if any(y):
            ax.plot(x, y, color=SC[st], marker=".", ms=4, lw=1.2, label=st)
    ax.set_ylabel("channels that fired")
    ax.set_xlabel("capture index")
    ax.set_title("Live channels (384 instrumented per station)")
    ax.legend(fontsize=8)

    ax = axes[1][1]
    ax.plot(x, [s.get("ot_fraction", np.nan) for s in scalars],
            color="#555555", marker=".", ms=4, lw=1.2)
    ax.set_ylabel("over-threshold fraction")
    ax.set_xlabel("capture index")
    ax.set_title("Over-threshold fraction, all VMMs")

    for row in axes:
        for a in row:
            a.grid(alpha=0.3)
            a.set_axisbelow(True)
    fig.suptitle("Per-capture trends across the sub_run", fontsize=14)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------
# one sub_run
# --------------------------------------------------------------------------

FIG_BLURB = {
    "padmap.png": "Occupancy on the true fan-pad geometry, whole sub_run.",
    "station_adc.png": "Pulse height per station, with the discriminator wall. "
                       "All recorded hits — a noisy channel dominates it.",
    "hits_per_vmm.png": "Total hits per VMM, coloured by station.",
    "trends.png": "Per-capture trends: occupancy, gain, live channels, OT.",
    "time_profile.png": "Spill structure within a capture, stacked.",
    "adc.png": "ADC spectrum per VMM.",
    "adc_ot.png": "ADC per VMM, split by the over-threshold flag.",
    "adc_vs_ch.png": "ADC vs channel per VMM.",
    "chno.png": "Channel occupancy per VMM.",
    "ot.png": "Over-threshold fraction per VMM.",
    "bcid.png": "BCID per VMM.",
    "tdc.png": "TDC per VMM.",
    "offset.png": "Frame offset per VMM (firmware sanity).",
}


def render_subrun(run, sub, sub_dir, out_root, pad_table, only=None):
    counts, n_cap = load_counts(sub_dir)
    if counts is None:
        return {"run": run, "sub_run": sub, "status": "no counts.npz"}

    ids = active_vmms(counts)
    if not ids:
        return {"run": run, "sub_run": sub, "status": "no hits"}

    scalars = load_scalars(sub_dir)
    cond = run_conditions(sub_dir)
    out_dir = os.path.join(out_root, run, sub)
    os.makedirs(out_dir, exist_ok=True)

    hv_bits = ", ".join(
        f"{s} mesh {int(v.get('mesh', 0))}/drift {int(v.get('drift', 0))} V"
        for s, v in sorted(cond["hv"].items()) if v)
    n_hits = int(np.asarray(counts["hits_per_vmm"]).sum())
    header = (f"{run} / {sub} — {n_cap} captures, {n_hits:,} hits"
              + (f"\n{hv_bits}" if hv_bits else "")
              + (f" | {cond['gas']}" if cond.get("gas") else ""))

    drawn = []
    figs = {
        "padmap.png": lambda: fig_padmap(counts, ids, pad_table),
        "station_adc.png": lambda: fig_station_adc(counts, ids),
        "hits_per_vmm.png": lambda: fig_hits_per_vmm(counts, ids),
        "trends.png": lambda: fig_trends(scalars),
        "time_profile.png": lambda: fig_time_profile(counts, ids),
        "adc.png": lambda: fig_per_vmm_1d(
            counts, "adc", ids, 8, ADC_BINS, "ADC",
            "ADC spectrum per VMM", logy=True),
        "adc_ot.png": lambda: fig_adc_ot(counts, ids),
        "adc_vs_ch.png": lambda: fig_adc_vs_ch(counts, ids),
        "chno.png": lambda: fig_per_vmm_1d(
            counts, "ch", ids, 1, CH_BINS, "channel",
            "Channel occupancy per VMM"),
        "ot.png": lambda: fig_ot_fraction(counts, ids),
        "bcid.png": lambda: fig_per_vmm_1d(
            counts, "bcid", ids, 32, 4096, "BCID",
            "BCID distribution per VMM"),
        "tdc.png": lambda: fig_per_vmm_1d(
            counts, "tdc", ids, 2, 256, "TDC",
            "TDC distribution per VMM"),
        "offset.png": lambda: fig_per_vmm_1d(
            counts, "offset", ids, 1, 32, "offset + 16",
            "Frame offset per VMM"),
    }

    for name, build in figs.items():
        if only and name not in only:
            continue
        try:
            fig = build()
        except Exception:
            print(f"  ! {run}/{sub} {name}\n{traceback.format_exc()}",
                  file=sys.stderr)
            continue
        if fig is None:
            continue
        # Footer, not header: every figure already carries a suptitle, and a
        # top-left stamp lands on top of it. Negative y puts it just outside
        # the canvas, where bbox_inches='tight' will pick it up -- inside, it
        # collides with whatever xlabel tight_layout left at the bottom edge.
        fig.text(0.005, -0.012, header, va="top", ha="left", fontsize=8,
                 color="#555555")
        save(fig, out_dir, name, drawn)

    # The index lists what the directory HOLDS, not what this call drew.
    # Otherwise a targeted re-render (--only) rewrites the index to mention
    # only the one figure it refreshed and hides everything already there.
    on_disk = {os.path.basename(p)
               for p in glob.glob(os.path.join(out_dir, "*.png"))}
    made = [n for n in FIG_BLURB if n in on_disk]
    made += sorted(on_disk - set(made))

    summary = {
        "run": run, "sub_run": sub, "store": sub_dir,
        "n_captures": n_cap, "n_hits": n_hits,
        "active_vmms": ids,
        "trigger_vmms": [v for v in ids if v in TRIGGER_VMMS],
        "uncabled_vmms": [v for v in ids if v not in vs.VMM_TO_STATION
                          and v not in TRIGGER_VMMS],
        "hits_per_station": {
            st: int(np.asarray(counts["hits_per_vmm"])[
                [v for v in vmms if v in ids]].sum()) if any(
                    v in ids for v in vmms) else 0
            for st, vmms in vs.STATION_VMMS.items()},
        "gas": cond.get("gas"), "start_time": cond.get("start"),
        "hv": cond.get("hv"), "figures": made, "status": "ok",
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    write_subrun_index(out_dir, summary, header)
    return summary


# --------------------------------------------------------------------------
# browsable index
# --------------------------------------------------------------------------

CSS = """body{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;
margin:0 auto;padding:28px;max-width:1180px;color:#1a1a1a;background:#fff}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px}
.meta{color:#666;font-size:13px;margin-bottom:18px;white-space:pre-line}
img{max-width:100%;border:1px solid #e2e2e2;border-radius:4px;display:block}
figure{margin:0 0 26px}figcaption{color:#555;font-size:13px;margin:6px 0 0}
table{border-collapse:collapse;font-size:13px;margin:10px 0 22px}
td,th{border:1px solid #e2e2e2;padding:5px 10px;text-align:left}
th{background:#fafafa}a{color:#1f6feb}
.tag{display:inline-block;padding:1px 7px;border-radius:3px;color:#fff;
font-size:12px}"""


def write_subrun_index(out_dir, summary, header):
    e = html.escape
    parts = [f"<style>{CSS}</style>",
             f"<h1>{e(summary['run'])} / {e(summary['sub_run'])}</h1>",
             f"<div class='meta'>{e(header)}</div>", "<table>"]
    for st, n in summary["hits_per_station"].items():
        parts.append(f"<tr><th><span class='tag' style='background:{SC[st]}'>"
                     f"{e(st)}</span></th><td>{n:,} hits</td></tr>")
    parts.append("</table>")
    if summary["uncabled_vmms"]:
        parts.append("<p><b>VMM ids with hits but no cabling entry:</b> "
                     + ", ".join(str(v) for v in summary["uncabled_vmms"])
                     + " — firmware corruption, not physics.</p>")
    for name in summary["figures"]:
        parts.append(f"<figure><a href='{name}'><img src='{name}'></a>"
                     f"<figcaption><b>{name}</b> — "
                     f"{e(FIG_BLURB.get(name, ''))}</figcaption></figure>")
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write("\n".join(parts))


def write_top_index(out_root, results):
    e = html.escape
    ok = [r for r in results if r.get("status") == "ok"]
    rows = ["<style>%s</style>" % CSS,
            "<h1>VMM QA — rendered from the counts store</h1>",
            "<div class='meta'>One row per sub_run, summed over every capture. "
            f"{len(ok)} sub_runs, "
            f"{sum(r['n_captures'] for r in ok):,} captures, "
            f"{sum(r['n_hits'] for r in ok):,} hits.</div>",
            "<table><tr><th>run</th><th>sub_run</th><th>captures</th>"
            "<th>hits</th><th>P2_IN</th><th>P2_MID</th><th>P2_OUT</th>"
            "<th>gas</th></tr>"]
    for r in sorted(ok, key=lambda d: (d["run"], d["sub_run"])):
        link = f"{r['run']}/{r['sub_run']}/index.html"
        rows.append(
            f"<tr><td>{e(r['run'])}</td>"
            f"<td><a href='{link}'>{e(r['sub_run'])}</a></td>"
            f"<td>{r['n_captures']}</td><td>{r['n_hits']:,}</td>"
            + "".join(f"<td>{r['hits_per_station'].get(s, 0):,}</td>"
                      for s in ("P2_IN", "P2_MID", "P2_OUT"))
            + f"<td>{e(str(r.get('gas') or ''))}</td></tr>")
    rows.append("</table>")
    bad = [r for r in results if r.get("status") != "ok"]
    if bad:
        rows.append("<h2>Skipped</h2><table><tr><th>run</th><th>sub_run</th>"
                    "<th>why</th></tr>")
        for r in bad:
            rows.append(f"<tr><td>{e(r['run'])}</td><td>{e(r['sub_run'])}</td>"
                        f"<td>{e(r['status'])}</td></tr>")
        rows.append("</table>")
    with open(os.path.join(out_root, "index.html"), "w") as f:
        f.write("\n".join(rows))


# --------------------------------------------------------------------------

def _worker(args):
    run, sub, sub_dir, out_root, only = args
    try:
        return render_subrun(run, sub, sub_dir, out_root,
                             vs.build_pad_table(), only)
    except Exception as exc:
        traceback.print_exc()
        return {"run": run, "sub_run": sub,
                "status": f"{type(exc).__name__}: {exc}"}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("store_root",
                    help="the runs root, one run directory, or one sub_run")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--run", help="comma-separated run names to restrict to")
    ap.add_argument("--only", help="comma-separated figure filenames to draw")
    ap.add_argument("--jobs", type=int, default=1,
                    help="sub_runs to render in parallel. MEASURED: 352 MB "
                         "peak RSS and ~16 s per sub_run; the counts are "
                         "streamed, so memory is set by the figures and does "
                         "not grow with the number of captures. Budget "
                         "jobs x 400 MB.")
    ap.add_argument("--force", action="store_true",
                    help="re-render sub_runs that already have a summary.json")
    args = ap.parse_args()

    runs = set(args.run.split(",")) if args.run else None
    only = set(args.only.split(",")) if args.only else None
    subs = find_subruns(args.store_root, runs)
    if not subs:
        print(f"no sub_runs with a hits_store under {args.store_root}",
              file=sys.stderr)
        return 1

    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)

    todo = []
    for run, sub, sub_dir in subs:
        done = os.path.join(out_root, run, sub, "summary.json")
        if os.path.exists(done) and not args.force:
            with open(done) as f:
                todo.append(("cached", json.load(f)))
            continue
        todo.append(("render", (run, sub, sub_dir, out_root, only)))

    jobs = [t[1] for t in todo if t[0] == "render"]
    results = [t[1] for t in todo if t[0] == "cached"]
    print(f"{len(subs)} sub_runs: {len(jobs)} to render, "
          f"{len(results)} cached")

    if args.jobs > 1 and jobs:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for i, r in enumerate(ex.map(_worker, jobs), 1):
                print(f"[{i}/{len(jobs)}] {r['run']}/{r['sub_run']}: "
                      f"{r['status']}")
                results.append(r)
    else:
        pad_table = vs.build_pad_table()
        for i, (run, sub, sub_dir, _o, _only) in enumerate(jobs, 1):
            r = render_subrun(run, sub, sub_dir, out_root, pad_table, only)
            print(f"[{i}/{len(jobs)}] {run}/{sub}: {r['status']}")
            results.append(r)

    write_top_index(out_root, results)
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{n_ok}/{len(results)} rendered -> {out_root}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
