# Pedestal and noise per channel with the internal pulser — check of 16 Nov 2026 and plan

> **Status at the end of 16 Nov 2026 (read this first).** This file is the lab
> notebook of the day, in the order things were done; sections 1–5 are the
> morning plan, sections 6–9 what was measured. Outcome:
> - The internal pulser works (rate = `tp_number` × 10.85 kHz) but its neighbour-ADC σ
>   does **not** follow the real discriminator noise channel by channel (§6, §7):
>   a ~3 ADC shift of the whole chip on every pulse, and a different ADC from the one the
>   threshold uses. **Put on the back burner by the user.**
> - **Calibration method chosen**: baseline from the monitor read-back (`smx 0`, mV, at
>   the discriminator) and σ from the **noise rate scan** (§6, §8): reproducible to < 1 %
>   per channel, agrees with a single-channel trim scan to a few %.
> - The noise of some channels **changes in time** (§9: 12 chip-3 channels stopped
>   firing on noise within 25 min, nothing else changed).
> - A **fast all-chip rate scan** (~35 min instead of ~3 h) is designed and its key
>   ingredient (VMM-only register write between points) is tested (§9).
>   Next session: **`docs/HANDOFF_FAST_RATESCAN.md`**.

Goal: give every VMM channel its own threshold at **baseline + n·σ**, where σ
is the noise of that channel, instead of the same distance in mV for every
channel (what `vmm_monitor_scan.py equalize` does today). σ is measured with
the method of I. Mandjavidze (*On software utilities for P2/Basket health
evaluation*, 3 Aug 2026): neighbour read-out on, internal test pulser on every
third channel, the ADC values of the neighbours give their pedestal (mean) and
noise (σ). The external Zedboard pulser used in `hybrid_performance/` is NOT
used: everything here runs on the stand as cabled, with the detectors connected.

Words used below:
- **pedestal**: the mean ADC value of a channel when no signal is there (baseline seen by the ADC).
- **σ (sigma)**: the spread (standard deviation) of those ADC values = the noise.
- **neighbour read-out (`sng 1`)**: when a channel fires, the chip also reads its two
  neighbours, even below threshold. Those read-outs carry the over-threshold flag 0.
- **L / U**: lower (pulsed − 1) and upper (pulsed + 1) neighbour.
- **phase**: which third of the channels is pulsed: phase k pulses channels with ch % 3 == k.
- **sdp10**: pulser amplitude DAC of the chip. **sdt**: chip threshold DAC. **trim (`sd`)**: per-channel threshold lowering, 0..31 steps of ~1.1 mV.
- **frame**: 4096 clock ticks of 22.5 ns (44.444 MHz bunch-crossing clock) = 92.16 µs.

## 1. How the internal pulser rate is set (from the ESS `vmmsc` source and our base YAML)

The chip has no rate setting. The FEC sends the test pulses, at fixed clock
ticks inside each 4096-tick frame:

| FEC register (base YAML) | default | meaning |
|---|---|---|
| `tp_offset_first` | 100 | tick of the first pulse in the frame |
| `tp_number` | 1 | pulses per frame |
| `tp_offset` | 1000 | ticks between pulses (only used when `tp_number` > 1) |
| `tp_latency` | 65 | timing |

`vmmsc` (`/local/p2/p2soft/vmmsc/src/daq_window.cpp:1666`) requires
`tp_offset_first + (tp_number − 1)·tp_offset ≤ 4095`. Rate = `tp_number` × 10.85 kHz;
10.85 kHz is the minimum. Amplitude = `sdp10` per chip; which channels = `st 1` per channel.

## 2. The check of 16 Nov 2026 (chips 2 and 3 = hybrid 1, HV off, no DAQ run)

Tools: `vmm_monitor_tool/pulser_files.py` (phase files), `noise_probe.py --fec-yaml`
(capture without a run; optional FEC registers from another base YAML),
`pulser_pedestal.py` (analysis). Data on the DAQ machine: `~/trim_work/out/pulsercheck/`
(pcaps kept, 1.1 GB; `*.ped.json` = results). Start file: `…_adwk2_Km18_i5.txt`, applied
again at the end. HV read back < 0.5 V on all six channels before starting.

Captures (10 s each, phase 0 = channels 0, 3, …, 63 pulsed, chips 2 and 3 unmasked):

| file | chip threshold | sdp10 | tp_number |
|---|---|---|---|
| `…pulsercheck_Km18i5_c2c3_p0_a40` | K=−18 (as in the scan) | 40 | 1 and 2 |
| `…pulsercheck_Km18i5_c2c3_sdtp40_p0_a80` | K=−18 + 40 counts (~30 mV) | 80 | 1 and 2 |
| `…pulsercheck_Km18i5_c2c3_sdtp40_p0_a160` | K=−18 + 40 counts | 160 | 1 |

Results:

1. **Rate confirmed**: every pulsed channel counts **10 851 Hz** with `tp_number 1` and
   **21 702 Hz** with `tp_number 2` (44.444 MHz / 4096 = 10 850.6 Hz). With the raised threshold
   every one of the 64 channels of both chips is read exactly once per pulse (22 pulsed +
   42 neighbours = 64), i.e. 694 kHz of hits per chip. The pulse reaches only the `st 1`
   channels: chips without `st` see nothing extra.
2. **The chip threshold must be raised during the measurement.** At K=−18 with the masks
   removed, noisy channels fire on their own and every such hit also reads out *their*
   neighbours: the pulsed channels of chip 3 got 1.7 million neighbour read-outs in 10 s
   that have nothing to do with the pulser. With sdt + 40 counts: **0**. (Irakli's note says
   the same: "global VMM threshold adjusted to pulse amplitude".)
3. **Pedestal and noise (raised threshold, sdp10 80)**, ~108 000 read-outs per channel in 10 s:

   | chip | pedestal (ADC) L / U median | σ (ADC) L / U median | σ range |
   |---|---|---|---|
   | 2 | 42.2 / 39.4 | 4.1 / 4.3 | 2.6 … 5.3 |
   | 3 | 52.2 / 53.5 | 4.1 / 4.8 | 2.5 … 5.8 |

   σ has a clear pattern along the chip: **2.5–3.7 ADC at the ends (ch 1–5, 55–62),
   4.5–5.8 in the middle**. This is exactly the per-channel information the equaliser lacks.
4. **Crosstalk from the pulsed channel**: doubling the pulse (sdp10 80 → 160, +87 ADC on the
   pulsed channel) moves the neighbours' pedestal up by ~3 ADC (≈ 3.5 % of the pulse) and σ by
   +0.3–0.4 ADC. L and U differ by 1–3 ADC. ⇒ use a small pulse and correct (see 4.2).
5. **Pulse transfer (internal pulser → ADC)**: 1.11 (chip 2) and 1.14 (chip 3) ADC per sdp10
   count, channel range 0.90–1.23, linear between 80 and 160.
6. **Odd channel**: chip 2 ch 59 reads ADC = 0 on all 108 000 read-outs (dead ADC on that
   channel, or not connected). To follow up.
7. The ADC↔mV link could **not** be taken from our monitor baselines: those only spread over
   8 mV per chip, and pedestal-vs-baseline gives r = 0.7 (chip 2) and 0.3 (chip 3). Another
   calibration is needed (4.3).

## 3. What the transfer function gives us (Iakovidis et al., VMM shaper, from P. Schöler)

The note gives the VMM shaper as one real and two complex poles (normalised: pole₀ = 1.263/α,
pole₁ = (1.149 − 0.789 j)/α, α = t_peak/1.5). We implemented it and checked it peaks at
198 ns for t_peak = 200 ns. Two uses:

- **Noise rate from σ (Rice formula)**: a channel with Gaussian noise σ and threshold at
  n·σ above its baseline fires `f0 · exp(−n²/2)` times per second. f0 comes from the shaper:
  at 200 ns, **f0 = 0.99 MHz** for white current noise and **2.62 MHz** for white voltage
  noise at the input (scales as 1/t_peak). So:

  | n | rate per channel (f0 1.0 … 2.6 MHz) |
  |---|---|
  | 3 | 11 – 29 kHz |
  | 4 | 0.3 – 0.9 kHz |
  | 4.5 | 40 – 100 Hz |
  | 5 | 4 – 10 Hz |
  | 5.5 | 0.3 – 0.7 Hz |

  ⇒ **n is chosen from the noise rate we accept**: n = √(2·ln(f0/R)). For R = 50 Hz/channel
  (the tuner's limit) n ≈ 4.5; for R = 5 Hz n ≈ 5. A threshold at 3σ would give ~10–30 kHz per
  channel. The measured f0 (step 4.1) replaces the 1.0–2.6 MHz range.
- **Pulse height**: the peak of the response is gain × charge, so the internal pulse in mV
  at the shaper output is sdp10 step (mV, read by `MeasurePulserDac`) × test capacitance ×
  gain (4.5 mV/fC). This is one route to ADC↔mV (4.3).

## 4. Plan: from pedestal/noise to per-channel thresholds

Each step: LV on, HV off, no DAQ run, unless written otherwise. Every step ends with
the stand back on the file it started from.

### 4.1 Calibrate σ_ADC into mV and check the Rice rate on chips 2 and 3 (~40 min)
The trims and the chip threshold work in mV; the pulser method gives σ in ADC counts.
Cross-calibration with a **noise rate scan**: for 8–10 chip thresholds near the noise
edge (sdt steps of 2–4 counts, 30 s `noise_probe` each, masks off), count each channel.
Fit per channel `rate = f0 · exp(−(thr − baseline)² / 2σ_mV²)` with thr and baseline in mV
from our monitor map and DAC calibration. This gives per channel **σ_mV and f0** directly.
Then `k = σ_mV / σ_ADC` per chip. If k is the same for all channels of a chip
(expected: one ADC per chip), the pulser method is validated and the 30 s × 10 scan is not
needed for the other chips. If not, the neighbour ADC is not a plain sample of the noise
(possible: the peak detector may pick the maximum in a window), and we fall back to the
rate scan for σ.

### 4.2 Crosstalk correction (built into every measurement)
Each phase taken at two pulse amplitudes (e.g. sdp10 60 and 120, 3 s each); the neighbour
pedestal and σ are extrapolated linearly to zero pulse. L and U are kept apart and both
reported; a channel where they differ by more than ~2σ is flagged.

### 4.3 ADC ↔ mV, second route
`MeasurePulserDac` through our `vmm_monitor` at two sdp10 values → mV per sdp10 count;
with the measured 1.1 ADC per sdp10 count and the nominal test capacitance and gain this
gives ADC per mV at the shaper output. Used only as a cross-check of 4.1 (the test
capacitance is a nominal value).

### 4.4 Full map: all 18 detector chips (2–19), ~20 min
Five groups of two hybrids (4 chips), so the link carries ~2.8 M hits/s as in the check
(all 20 chips at once would be ~14 M hits/s, too close to the 1 Gbit limit). Per group:
3 phases × 2 amplitudes × 3 s. Start file: `trimmed_Km18_v1` (quiet: the other chips must
not flood the link; chip 5 and 15 at K=−18 gave 0.9 and 0.55 MHz of noise in the check).
Measured chips: threshold + 40 counts, masks off, `sng 1`. H0 (chips 0–1, trigger) not
measured. Output: one table per channel: pedestal, σ (L, U, combined, zero-pulse), flags
(dead ADC, L/U mismatch, σ outlier, blind-block channel). The blind blocks of VMM 4/7/9 are
measured both without and with `sl 1` (neighbour read-out does not need the channel's own
threshold, so it sees them).

### 4.5 Per-channel targets → trims (new `target` mode in `vmm_monitor_scan.py`)
For each channel c of a chip:

1. target_c = baseline_c + n·σ_c (mV), baseline from the monitor map (`smx 0`), σ_c from 4.4
   converted with k (4.1), n from the accepted noise rate (section 3).
2. Leave out of the scale: dead channels, blind channels not cured, σ outliers
   (> median + 3·MAD of the chip). They are handled at step 5.
3. The chip threshold (sdt) is set at the highest target of the normal channels plus the
   headroom for later raising (e.g. 6 trim steps), since a trim can only lower a channel.
4. trim_c = round((sdt level − target_c) / step_chip), 0..31. A channel that would need
   more than 31 stays a bit high (safe side) and is listed. With σ of a few mV (2.5–5.8 ADC; mV after 4.1)
   and baselines within 8 mV per chip, the needed spread is expected to fit in 31 steps
   (~33 mV); the tool reports it per chip.
5. Noisy outliers: start at the chip level and are raised by the tuner if needed.
   Quiet channels (small σ) get **lower** thresholds than today: this is the gain in
   efficiency compared to the equaliser. Silent channels (no hits, normal pedestal) are
   only reported.
6. `refine` (existing) changed to aim at target_c instead of the chip median, after a
   new monitor map with the trims in.

### 4.6 Check against the prediction (60 s `noise_probe`, HV off)
Measured rate per channel vs the Rice prediction for its n. Channels far above it
(non-Gaussian tails, bursts) → raised by `trim_tune.py`; masked only at trim 0.
Compare with `trimmed_Km18_v1`: thresholds per channel (mV and fC), total noise rate,
number of masks.

### 4.7 With HV (needs the user: HV and gas)
- Repeat 4.4 with HV on: shows the noise added by the detector/HV per channel
  (Irakli: "quick evaluation of impacts of detectors, cables, adaptation boards, HV").
- A cosmic run with the n·σ file and the scintillator trigger: hits per coincidence
  per chamber vs `trimmed_Km18_v1` and the wk2 K points.

### 4.8 Later
GUI button "pedestal & noise" (Irakli: "can (must) be integrated in the run control"),
same lock as Configure chip; per-channel σ plot in the Live/Setup tab; redo after any
firmware, cable or gain/peaking-time change (σ and f0 depend on peaking time).

## 5. Points to watch and how they are dealt with

| point | how |
|---|---|
| neighbour ADC may be a peak value, not a random sample | 4.1 compares σ_ADC with σ from the noise rate scan |
| crosstalk from the pulsed channel | small pulse, two amplitudes, extrapolate to 0; L/U kept apart |
| noisy channels firing by themselves contaminate the neighbours | chip threshold + 40 counts during the measurement (verified: 0 extra read-outs) |
| masked channels give no data | masks removed for the measurement only (`--unmask`) |
| data volume | 4 chips per capture, 3 s per point |
| ADC ↔ mV | 4.1 (rate scan), cross-checked with 4.3 (pulser DAC) |
| dead/odd channels (chip 2 ch 59 ADC 0) | flagged in the table, kept out of the scale |
| blind blocks VMM 4/7/9 | measured with and without `sl 1` |
| HV-off vs HV-on noise | 4.4 now, 4.7 with HV |
| the trims only lower | chip level at the highest target + headroom |
| test files in `config_ext` | named `*_pulsercheck_*` (5 files from this check) and `*_step1_c2c3_*` (17 files from step 1) |

## 6. Step 1 done (16 Nov 2026, 17:48–18:09, chips 2 and 3, HV off, no run)

Script `~/trim_work/pulser/step1.sh` (copy: `vmm_monitor_tool/step1_c2c3.sh`), results in `~/trim_work/out/step1/`:
`map_trimmed_v1_now.json` (fresh monitor map), `ratescan/*.rates.json` (11 thresholds),
`ratefit.json`, `pulser/*.ped.json` (3 phases × sdp10 80 and 160, 5 s each, pcaps kept),
`compare.json`. Fit and comparison: `vmm_monitor_tool/noise_ratefit.py` (`fit`, `compare`).
Start file `trimmed_Km18_v1`; the GUI file (`Km18_i5`) was configured again at the end (rc 0, armed).

**Rate scan.** Chips 2 and 3 moved together by −14 … +10 sdt counts (30 s each); the
three lowest points filled the 300 MB capture limit within 7–27 s (chip 3 floods first).
Fit `ln R = a + b·d + c·d²` per channel, d = threshold − baseline from the monitor map,
points with 1 Hz … 30 kHz and ≥ 30 hits (a common ~0.3 Hz of hits on every channel in
one capture had to be cut out this way).

| | chip 2 | chip 3 |
|---|---|---|
| channels with a 3-parameter fit | 45 / 64 | 61 / 64 |
| σ (mV), median [range] | 3.6 [2.5 … 5.9] | 3.1 [2.0 … 3.9] |
| d0 = noise centre − monitor baseline (mV), median | −1.0 | −0.3 |
| f0 (MHz), median | 0.9 | 0.35 |

- The monitor baseline **is** the noise centre within 1–2 mV for most channels: baseline +
  n·σ can be computed from our map directly.
- Measured f0 (0.35–0.9 MHz) is **lower** than the 1.0–2.6 MHz from the transfer function
  (dead time after each hit, and f0/σ are correlated in a fit over 4–5 decades). For the
  choice of n, use the measured f0: n = √(2 ln(f0/R)) gives n ≈ 4.0–4.6 for R = 50 Hz.
- Chip 2 is quieter at its thresholds: 9 channels never fired even 14 counts down
  (~11 mV) and 8 have only 1–2 usable points. **The scan has to go deeper for chip 2,
  chip by chip** (one chip flooding stops the capture for the other).

**Pulser σ (ADC) against rate-scan σ (mV)**, all 3 phases, zero-pulse extrapolation:

| | chip 2 | chip 3 |
|---|---|---|
| channels compared (well-constrained fits) | 29 | 45 |
| k = σ_mV / σ_ADC, median | 0.94 mV/ADC | 0.79 mV/ADC |
| spread of k between channels | 9 % | 20 % |
| correlation σ_mV vs σ_ADC (r) | 0.57 | 0.45 |
| ends (ch 0–7) : middle (8–55), σ_mV | 2.98 : 3.60 | 2.87 : 3.29 |
| ends : middle, σ_ADC | 2.91 : 3.92 | 3.04 : 4.26 |

- Chip 2: the two agree channel by channel within ~9 %, one factor k.
- Chip 3: the pulser σ grows much more towards the middle of the chip than the rate-scan
  σ does (middle channels ratio 0.56–0.70, ends ~1.0). The pulser σ also grows with the
  pulse amplitude (+0.3–0.7 ADC from sdp10 80 to 160). So the neighbour ADC contains
  something beyond the discriminator noise: pulse-related pick-up and/or noise common
  to the channels (all 22 pulsed channels fire at the same instant).
- **Conclusion of step 1**: the pulser method is not (yet) a substitute for the rate
  scan when setting thresholds. The rate scan measures directly what the
  discriminator sees, in mV, and agrees with the monitor baseline. The pulser method
  stays as the fast relative check (HV on/off, cables), once its excess is understood.

**Next, proposed:**
1. From the kept pulser pcaps (no stand time): event-by-event correlation of the
   neighbour ADC values between channels of the same pulse (common-mode), and the
   σ extrapolation done in quadrature; pulse fewer channels at once (every 6th) to
   see whether the excess scales with the number of pulsed channels.
2. Rate scan chip by chip, deeper (to −26 counts) and with 1-count steps near the edge,
   for chips 2–19: ~10 min per chip, ~3 h for all, can run unattended (HV off).
3. Then the `target` mode (4.5) with σ from the rate scan and f0 measured.

## 7. Where the extra pulser spread comes from (16 Nov 2026, 18:15–18:45)

Tool: `vmm_monitor_tool/pulser_commonmode.py`. With the pulser, every pulse gives one
hit per channel, read out in channel order 0..63, so each pulse can be rebuilt as one
event (64 ADC values). Per event: CM = mean deviation of the neighbour channels from
their pedestals; per channel the part following CM is fitted (coefficient β) and removed.

Data: the 6 step-1 captures (3 phases × sdp10 80/160, threshold +40) and 4 new 5 s
captures, phase 0 (`~/trim_work/out/step1b/`, script `vmm_monitor_tool/step1b_c2c3.sh`):
every 6th channel pulsed at sdp10 80 (threshold +40); and at the normal `trimmed_v1`
thresholds, every 3rd channel at sdp10 25 and 40 and every 6th at sdp10 40.
At the normal thresholds sdp10 25 is enough: every pulsed channel counts 10.85 kHz,
and the quiet `trimmed_v1` file needs no threshold raise (no contamination seen).

| | chip 2 | chip 3 |
|---|---|---|
| CM spread per pulse (ADC) | 3.0–3.5 | 2.8–3.2 |
| expected if the channels were independent | 0.6–1.0 | 0.6–0.9 |
| mean correlation of neighbour pairs, near (≤3 ch) / far (≥10 ch) | 0.53–0.68 / 0.45–0.64 | 0.45–0.63 / 0.36–0.59 |
| correlation of CM with the pulsed channels' ADC | 0.5–0.89 | 0.70–0.88 |
| σ after removing CM, ends / middle (ADC) | 2.2–2.6 / 2.4–3.3 | 2.1–2.5 / 2.6–3.9 |

What this shows:
- **Most of the neighbour spread is one common shift of the whole chip per pulse**,
  ~3 ADC, almost the same for neighbours next to each other and far apart. It also
  moves the pulsed channels (hence the correlation with the pulse ADC).
- **It does not depend on the pulse**: the same ~3 ADC for sdp10 25, 40, 80, 160 and with
  half as many pulsed channels (every 6th). So it is not crosstalk proportional to the
  pulse; it is a baseline shift of the chip at the moment of the read-out.
- After removing it, the middle-vs-end difference is smaller, especially with every 6th
  channel pulsed (ratio middle/ends 1.09–1.17, rate scan 1.15–1.21); with every 3rd
  channel pulsed it stays 1.2–1.5.
- Channel by channel the corrected pulser σ still does **not** follow the rate-scan σ
  (r = 0.07–0.49, k = 1.1–1.5 mV/ADC depending on the settings).

What it does **not** tell: whether that common shift also exists without the pulser
(real low-frequency common noise that the discriminator sees, and that the rate scan
includes) or is produced by the test-pulse injection itself (same for every amplitude).
Part of the poor channel-by-channel agreement can also come from the rate scan's own
precision (few points per channel on chip 2), which has not been measured yet.

**Status of the pulser method**: fast and stable at chip level (pedestal, a σ per chip,
a 3-ADC common mode per pulse), but not yet usable for **per-channel** thresholds.
The rate scan stays the method for the thresholds.

**Next, proposed:**
1. Repeat the rate scan of chip 3 once (≈10 min, HV off) to measure how reproducible
   the rate-scan σ is per channel. That fixes how good any agreement can be.
2. Neighbour read-out **without the pulser**: one channel of a chip set at its noise edge
   (trim) so that it fires at ~1 kHz by noise; its neighbours are then read at random
   moments, with no injection. If their σ equals the CM-corrected pulser σ, the common
   shift comes from the injection; if it equals the raw pulser σ, it is real noise.
3. Meanwhile the per-chip deep rate scan (section 6, next 2) can start: it does not
   depend on this.

**Decision (16 Nov evening, user):** calibrate with the monitor read-back (baseline) and
the rate scan (σ); the pulser method is kept for later (open question: origin of the
~3 ADC common shift).

## 8. How good is the rate-scan σ? (16 Nov 2026, 19:13–19:38, chips 2–3, HV off, no run)

Script `vmm_monitor_tool/step1c_c2c3.sh`, data `~/trim_work/out/step1c/`.

**A. Repeat** of the step-1 sdt scan with the same 11 files, 75 min later. The raw rates
agree within a few % at every point from sdt 0 to −10 counts. The first fit did not
reproduce, because of two kinds of bad points that the fit now rejects
(`noise_ratefit.py`):
- a whole-chip burst during one capture gives ~1 Hz on every channel far above its noise
  (both scans had one, at different points) → walking up in threshold, the first point
  below 3 Hz ends the usable range;
- at the lowest points chip 3 counts 3–5 MHz in total and saturates; and when the
  threshold is within ~2 mV of the baseline a channel's count falls again (down to 0:
  the comparator stays high) → captures with a chip above 1 MHz are skipped, and only
  points above a channel's highest rate are used.

With these rules (σ in mV, channels with ≥ 4 points):

| | chip 2 | chip 3 |
|---|---|---|
| σ median, scan A / repeat B (centre fixed at the monitor baseline) | 3.48 / 3.52 | 3.22 / 3.20 |
| per-channel reproducibility, centre fixed | **0.7 %** (r 0.99) | **0.5 %** (r 1.00) |
| per-channel reproducibility, centre free | 8.5 % (r 0.25) | 7.7 % (r 0.45) |
| noise centre − monitor baseline, median (free fit) | +1.1 … +2.4 mV | −1.9 … −0.2 mV |
| f0 median (free fit) | 0.4–0.7 MHz | 1.2–1.4 MHz |

The 3-parameter fit is too loose with 3–6 points per channel; the fit with the centre at
the monitor baseline is very reproducible. Chip 3's f0 now matches the transfer-function
value (1.0 MHz for white current noise at 200 ns).

**B. Trim scan**: only 8 channels of chip 3 (3, 7, 10, 24, 25, 39, 46, 52) lowered with
their own trim (+0 … +14 steps), the rest of the chip unchanged; each point has its own
monitor map, so the threshold axis uses no DAC calibration at all. σ (centre fixed),
trim scan vs sdt scans A / B:

| ch | 3 | 7 | 10 | 24 | 25 | 39 | 46 | 52 |
|---|---|---|---|---|---|---|---|---|
| trim scan | 2.35 | 3.45 | 2.88 | 3.42 | 3.34 | 2.90 | 2.61 | 2.74 |
| sdt scan A / B | 2.55 / 2.80 | 3.41 / 3.39 | 2.74 / 2.72 | 3.34 / 3.31 | 3.63 / 3.63 | 2.86 / 3.05 | – / 3.04 | 2.73 / 2.75 |

Agreement within 0–15 %, typically ~3 %, with a different DAC, the threshold read
directly, and only 8 channels firing instead of 64: no sign of chip-wide effects
(dead time, saturation, common noise switched on by the flood) in the sdt scan, and the
trim and sdt calibrations are consistent. (Both DACs were calibrated with the monitor, so
a wrong monitor mV scale would not show here; nothing points to one.)

**Limits seen**: the monitor reads in 1 mV steps; with 2–4 sdt counts between points
a channel has only 3–6 points between 3 Hz and 30 kHz. → For the full scan:
**1-count steps (~0.8 mV) over the noise window**, so each channel gets ~10 points.

**Conclusion**: per channel, the rate-scan σ with the centre at the monitor baseline is
reproducible to < 1 % and agrees with an independent single-channel trim scan to a few %.
It can be used for baseline + n·σ.

## 9. Evening, 16 Nov 2026: fast point write, and channels whose noise went away

**9.1 Why the deep scan would take 3 h** (analysis by the session
`vmm-threshold-efficiency-options`, read-only on our files): one point ≈ 47 s
(GUI apply ≈ 12–15 s: warm reset + FEC + hybrid + VMM for 20 chips, polled every 4 s;
30 s capture; 1–8 s decode). The link is not the limit (5.5 MHz of hits ≈ 0.33 Gbit/s);
the 300 MB capture cap is, and it is filled by channels far above the fit's 30 kHz, i.e.
by useless hits. Each channel's own 10 Hz → 10 kHz window is ~8 sdt counts wide, but
where it sits varies by ≥ 14 counts inside a chip, so a whole-chip sweep floods with the
early channels long before it reaches the late ones.

**9.2 VMM-only write between points: tested, works** (that session, 19:46–19:49, HV off,
no run, restored to `Km18_i5`; scripts and log in `vmm_monitor_tool/fastscan/`).
`config-vmm-regs` only (no warm reset, no FEC/hybrid write): 3.8 s instead of 12.0 s, all
ten hybrids `linkStatus 4` after every point and after the restore, per-channel rates equal
to the GUI apply within ~5–10 % (median ratio 0.99–1.06), the threshold moves both ways.
Remaining per-point overhead: ~5 s for the two `p2basket-sc` process starts (acq-on/off)
and ~2 s for the count and the link read: a 20 s capture took 25.3 s.

**9.3 Twelve channels of chip 3 stopped firing on noise.** That test seemed to show a
drift (chip-3 total at sdt −4/−6: 10/55 kHz vs 18/104 kHz at 18:00 and 19:17). Channel by
channel (`out/diag/`, my checks 19:52–19:55, HV off, restored each time):
- every channel that still counts agrees with step 1 within 3–9 %;
- **12 channels count exactly 0**: chip 3 ch 2, 14, 16, 24, 25, 26, 29, 42, 54, 57, 59, 63
  (at sdt −6 they counted 0.2–11 kHz at 18:00 and at 19:17); re-capture at 19:54 with
  `noise_probe` + the GUI apply: still 0. Chip 2: no channel lost;
- monitor map of those channels at 19:52 (`out/diag/map_c3_dead.json`): baseline and
  threshold unchanged (≤ 1 mV) since 17:57;
- every channel of chips 2 and 3 pulsed (`pulser_files.py --stride 1 --sdp10 40` on
  `trimmed_v1`, file `…_diag_c2c3_allpulsed_a40.txt`): **all 128 count 10.85 kHz**. The
  channels are alive, their digital path works.

So their **noise** went down, between 19:21 (end of the repeat, still normal) and 19:46.
In that interval: the deep points of the repeat (chip 3 at 3–5 MHz), the trim scan
(8 channels up to 640 kHz, ch 24/25 stuck at 0 Hz), GUI restores. Not known whether it is
an external pickup source that went off (evening) or a chip state left by the floods.
Channels 24 and 25 are in both groups. **To check first next time** (§ handoff).

Consequences: the σ of some channels is not stable in time → every scan needs a
reference point at its start and end (same file as today's `step1 … sdt m6`, so it ties
to today's data), and the thresholds may need a margin; a scan should not drive
channels into deep noise (hundreds of kHz) longer than necessary.

**9.4 User decision (end of 16 Nov)**: document everything, continue the fast scan in a
new session: `docs/HANDOFF_FAST_RATESCAN.md`.

## 10. Night of 16 Nov 2026: the fast scan of chips 2–19 — see `HANDOFF_FAST_RATESCAN.md` §9

σ per channel for 1105 of 1152 channels (`~/trim_work/out/fastscan_20261116/full2/fit_full2.json`),
reproducible to a few % between two full scans. The silent channels of §9.3 are explained:
channels held at/below their baseline for minutes stop firing on noise and come back
with test pulses (handoff §9.2). A VMM-only write sometimes stops the read-out until a warm
reset (handoff §9.1).
