# Plan — set and read thresholds per channel

Written 2026-11-12. Background and all file locations: `CODE_MAP.md`.
Status: **plan only, nothing built, no hardware touched.**

## What "threshold per channel" means on a VMM3a

```
effective threshold(channel) = global DAC (sdt, 10 bit, one per chip)
                               shifted by the channel trim (sd, 5 bit, 0..31)
```

There is no independent threshold per channel: each channel can only be moved
by up to 31 trim steps relative to its chip's `sdt`. Nominal step is about
1 mV; the real step, its sign convention and its linearity on our chips are
not known and are the first thing to measure (step 2).

## What is already there (verified by reading, not by running)

| | State |
|---|---|
| Firmware accepts per-channel trim | yes — the whole channel word is already written at every configuration; the legacy GUI applied trims on this ALINX in Nov 2025 |
| Middleware models `sd` | yes — `channel[NN]` field `sd`, bit reversal handled |
| Config path | yes — an ext-file line `Sys 0 Bec 0 Hyb h Vmm v Reg channel[NN] Field sd N` uses the same code as today's `sm` lines |
| Ever used with `sd` | **no** — all 1280 `sd` are 0, no ext file sets one |
| Read registers back | impossible (write-only) |
| Dump of applied state | no (`to-yaml-file` hangs from Python) |
| Analog measurement of a channel threshold | code exists in `libp2b-sc.so` (`MeasureChannelThreshold`, `MeasureThresholdDac`) but has no CLI command, no Python binding, and is built for channel 0 only |

So **setting is a tooling job on our side; reading needs the middleware
developer or a small C++ program of ours.**

## Blocker to clear first

`/run/p2basket` does not exist since the 12 Nov reboot (`p2basket.service` is
disabled). The session lock cannot be taken, so most likely no slow-control
command works at all right now. Needs sudo:
`sudo systemctl start p2basket.service` (and `enable`, so the next reboot
does not repeat it).

## Steps

### Step 1 — Setting, software only (no hardware needed) — ~half a day

1. `quick_scripts/make_channel_trim_config.py`: template ext file + a trim
   table → new ext file with one `sd` line per non-zero channel, appended in a
   clearly marked block. Trim table format, one file per calibration:
   `{"chip": {"channel": sd}}` (JSON, chip = 2 x hyb + vmm), plus a header with
   date, source of the values and the template name. Checks: 0 ≤ sd ≤ 31,
   chip 0..19, channel 0..63, no duplicate lines for the same field, H0
   untouched unless asked.
2. `quick_scripts/ext_config_summary.py`: parse any ext file with the same
   positional grammar as `p2basket_sc_config.py` and print/write JSON of what
   it sets per chip: `sdt`, gain, peak time, masked channels, **trims**. This
   is the software "read": *what was applied*, from the file copied into each
   run directory.
3. Unit tests for both (the grammar is positional and easy to get wrong).
4. Dry-run on the DAQ machine: `p2basket-sc --dry-run --log-datagrams` with a
   file containing one `sd` line, compare the channel word in the datagram
   with the expected bit pattern (`sd=1` → bit 17, because of the reversal).
   Sends nothing to the FEC.

No change to the GUI is needed for this: a new file in `config_ext/` appears
in the dropdown, is applied with the warm-reset-then-apply sequence, and is
copied into the run directory like any other.

### Step 2 — Prove the trim acts, on one chip, with data — ~2 h of stand time, user present

LV on, HV off, one detector chip (not H0), threshold near the noise edge.
Three short runs with identical files except `sd` = 0 / 15 / 31 on **half** of
the channels of that chip (the other half stays at 0 as a control).
Read out per channel: noise rate and lower ADC edge
(`adc_edge_vs_sdt.py`, `baseline_per_vmm.py`). Answers, all needed before
anything else: does the trim act at all through this chain; in which direction;
how many `sdt` counts is one `sd` step; do the control channels stay put.

### Step 3 — Reading the analog threshold — depends on the developer

Ask the slow-control developer (questions below). Preferred outcome: a CLI
action such as `measure-channel-threshold <ch>` / `measure-threshold-dac`
(then `exec_cmd` from Python works too). Fallback if that is not coming soon:
a small C++ program of ours built against `deploy/include` + `libp2b-sc.so`
that constructs `MeasureChannelThreshold(fecConnect, subHub, channel)` for
each channel — open item 3 of the cosmic campaign. The fallback depends on an
ABI the developer changes at every install, so it must be rebuilt with each
new `install_*` and is only worth it if the CLI route is weeks away.

Either way, wrap it as `vmm_monitor_scan.py` (JSON out: chip, channel, ADC
with threshold on/off, difference, temperature, applied ext file, timestamp)
with the same guards as chip config: refuse during a capture, take
`ChipConfig`'s lock, and **force a re-apply afterwards** (the run gate must
be un-armed by a measurement, since it rewrites the chips).

First hardware test: one channel of one hybrid, user watching, then time a
full 64-channel chip to know what 20 chips cost.

Independent of step 3, the data-driven read-back of step 2 stays available
and is the cross-check.

### Step 4 — Equalisation (the physics goal) — after 2 and 3

Per chip: measure every channel at a few trim values (or use the slope from
step 2), pick `sd` per channel so that all channels sit at the same distance
from their baseline, write the trim table, generate the ext files for the
scan with `make_cosmic_sdt_configs.py` + the trim block. Keep **all** chips
in, including VMM 4 and 9. Compare the noise-rate spread per chip before and
after — that number is the result.

### Step 5 — GUI, only once 1-4 work from the terminal

- Chip panel: show the summary of the selected ext file (per chip: sdt, number
  of masked and trimmed channels), from `ext_config_summary.py`.
- Trim/threshold panel (the 23 Oct placeholder): pick chip, 64-channel plot
  of measured threshold and applied trim, start a measurement as a background
  job with progress, export a trim table. Lab site first.

## Decisions that are yours

1. **Who exposes the measurement** — ask the developer for CLI commands, or
   build our own C++ tool now? Recommendation: ask first; do steps 1-2
   meanwhile, they do not depend on it.
2. **Where trim values live** — merged into each ext file (recommended: one
   file = one complete chip state, provenance unchanged) or a second file
   passed alongside (needs a change to the group's `p2basket_sc_config.py`,
   which we do not own).
3. **Legacy GUI as a shortcut** — `vmmdcs-p2b` can run the RD51 threshold
   calibration and write trims into a legacy JSON; a converter to ext lines is
   small. Fast, but it is a GUI (X forwarding), holds the session lock, and
   its results were last produced a year ago with other settings. Worth one
   try only if step 3 stalls.
4. **When** — step 2 needs LV on, the chips configured and you at the stand.

## Questions for the slow-control developer

1. Are `MeasureChannelThreshold`, `MeasureVmmChannel`, `MeasureThresholdDac`
   finished and tested on hardware? Can they get CLI names, with the channel
   as parameter (today it is fixed to 0 at construction)?
2. What exactly is `thresholdDifference` — ADC counts or mV, and which sign?
   Conversion to mV (ADC reference, gain)?
3. After a measurement, are `scmx`, `sm5_sm0`, `smx` restored and the chip
   rewritten, or is it left in monitor mode?
4. How long does one channel take; can 64 channels of all VMMs be looped in
   one invocation?
5. Is `to-yaml-file` expected to work from the Python binding (it hangs)? We
   would use it to record the applied state.
6. Could `p2basket.service` be enabled so `/run/p2basket` survives a reboot?

## Question for the firmware developers

Is there any constraint on rewriting the full VMM configuration repeatedly
(64 measurements = at least 128 full rewrites per chip)? And is the known
ordering rule (warm reset before the configuration, never after, or the
trigger hybrid loses its registers) affected by rewriting only the VMM
registers many times in a row?
