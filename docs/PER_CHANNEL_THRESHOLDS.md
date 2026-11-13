# Thresholds per channel — what works, what was measured, how to use it

Built and tested on the lab stand in the night of 12-13 November 2026 (LV on,
**HV off**, no DAQ run started). Code: `vmm_monitor_tool/`. Background on the
layers below: `CODE_MAP.md`.

## In one paragraph

Every VMM channel has a 5-bit **trim** that lowers its threshold by about
1.1 mV per step (0..31, so up to about 33 mV, the equivalent of about 43 `sdt`
counts). We can now **set** it from the normal exception files, **measure**
the result on the chip (baseline and threshold of each channel, in mV, through
the monitor output and the ADC on the hybrid), **equalise** all channels of a
chip (spread 3.8 mV rms before, 0.7 mV after), and **raise single noisy
channels instead of masking them**. Nothing of the slow-control developer's
installation was modified: our program only links to his library.

## The idea behind "raise instead of mask"

A trim can only *lower* a channel. So:

1. give **every** channel a trim with some room (equalising does this: all
   channels end up between about 12 and 30), and raise the chip `sdt` to
   compensate — nothing has moved, but every channel now has room to go up;
2. a channel that is too noisy gets a **smaller trim**: its threshold goes up
   by 1.1 mV per step while all the other channels of the chip stay where
   they are;
3. only a channel that is still noisy at trim 0 has to be masked.

When the chip threshold is lowered (`sdt` down), the noisy channels are raised
by the same amount and the quiet ones follow the chip down.

## What was measured

| Quantity | Result |
|---|---|
| Trim step | −0.97 to −1.25 mV per step depending on the chip, linear over 0..31 (worst deviation 1-3 mV) |
| Chip threshold DAC (`sdt`) | 0.73 to 0.81 mV per count; so 1 trim step ≈ 1.4 `sdt` counts |
| Baseline of a normal channel | 150-180 mV; reproducible to 0.4 mV rms between two measurements |
| Threshold above baseline at the lowest point of the wk2 scan (K=−18) | 14-20 mV median per chip (27 on VMM 6) |
| Spread between channels of one chip | 3.8 mV rms, range about ±9 mV → **0.7 mV rms, ±2 mV** after equalise + refine |
| Time | 4 s per channel for all 20 chips at once; a full map (64 channels, baseline and threshold) takes 9 minutes |

**Bit order.** With the slow-control build of 5 Nov 2026 the five trim bits
reach the chip in reverse order: writing 1 acts as 16. `vmm_trim.py` writes
the reversed value and puts the real trim in the comment of every line:

```
Sys 0 Bec 0 Hyb 3 Vmm 1 Reg channel[45] Field sd  6   # trim 12
```

Never type `sd` lines by hand; use the tool. If a later build changes the
order, `vmm_monitor_scan.py trimcurve` shows it at once (the curve is no longer
a straight line) and one constant in `vmm_trim.py` (`SD_BIT_REVERSED`) flips it.

**The noise test with data** (60 s captures, HV off, file of the wk2 point
K=−18, 15 well-behaved chips, their 244 masked channels unmasked):

| Formerly masked channels… | above 50 Hz | above 1 kHz | loudest | capture |
|---|---|---|---|---|
| unmasked, at the equalised threshold | 75 | 35 | 529 kHz | 328 MB/min |
| unmasked, raised 6 steps (≈ 7 mV) | 3 | 1 | 68 kHz | 37 MB/min |
| unmasked, raised 12 steps (≈ 13 mV) | **0** | 0 | — | 12 MB/min |
| (reference: masked) | — | — | — | 7 MB/min |

The automatic tuner then found the minimum: of the 244, 164 needed nothing,
60 one raise of 3 steps, 18 two, 2 three; of the 716 channels that had never
been masked, 20 were raised as well. Result: clean (6 MB/min) with **no mask**
on those 15 chips, in 5 iterations of 80 s.

## The blind channel blocks on VMM 4, 7 and 9 — found, explained enough, recovered

On these chips the baseline of a block of channels (VMM 4: channels 36-63,
VMM 7: 23-63, VMM 9: 19-63; 114 channels) was 30 to 270 mV **above** the
threshold instead of 15-20 mV below it. The channels masked by hand in the
scan files (`m9c16to21`, `m4c36to41`) are the ones at the edge of these blocks.

- They were blind: in run_230 they count 0.2 Hz while their neighbours count
  1-2 Hz. With the internal test pulser (10.85 kHz), small and medium pulses
  (pulser DAC 20, 40, 80): normal channels see 100 %, these channels **0 %**.
- It is a configuration effect, not broken hardware. The displaced baseline
  scales with the gain and goes back to normal (about 160 mV) with any of:
  `sfm 0` on the chip (these chips run `sfm 1`), the leakage bit `sl 1` on the
  channel, `slg 1`, the other polarity, the lowest gain. Chips 2 and 13, also
  at `sfm 1`, do not show it, so something on the input of those channels
  (detector or connector side) draws a DC current. Measured with HV off.
- **Cure used: one line `sl 1` per affected channel.** With it the same
  channels see **100 %** of the test pulses at all three amplitudes, and their
  baseline and threshold distance are those of their neighbours. `sfm 0` on
  the whole chip also normalises the baseline but made the chips flood.
- VMM 16 channel 33 (baseline 619 mV) is not cured by `sl 1`: it stays masked.
- **Not checked:** what `sl 1` does to these channels with HV on and real
  signals. That is the first thing to look at in the next cosmic run.

Also seen: with HV off the original K=−18 file was not quiet tonight (VMM 5
ch 46 at 876 kHz, VMM 15 ch 56 at 476 kHz, saturating the link). These are the
channels closest to their baseline (5-8 mV). The equalised file has none of it.

## The file to try first

`p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt`
(in the GUI drop-down). Same gain, peaking time and chip thresholds as the
lowest wk2 point (K=−18), with:

| | wk2 file (K=−18, i5) | trimmed v1 |
|---|---|---|
| masked channels on the 18 detector chips | 276 | **1** (VMM 16 ch 33) |
| blind channels | 114 | 0 (recovered with `sl 1`) |
| spread of threshold−baseline inside a chip | 3.8 mV rms | 0.7 mV rms |
| channels raised above the common level | — | 135 (87 by 2 steps, 39 by 4, 8 by 6, 1 by 8) |
| noise capture, HV off | saturated tonight (single channels at 0.5-0.9 MHz) | 6.3 MB/min, no channel above 50 Hz |

It was tuned with **HV off**. With HV on some channels will need more; that
is what `adaptive_thr_scan.py --raise-steps` is for.

## How far down can the chips go?

Starting from the tuned file (15 chips), the chip thresholds were lowered four
times by 4 `sdt` counts (3 mV), re-tuning each time:

| chip thresholds | channels above 50 Hz at first | after tuning | channels at trim 0 | masks |
|---|---|---|---|---|
| −4 counts | 274 of 960 | clean | 0 | 0 |
| −8 | 498 | clean | 0 | 0 |
| −12 | 688 | clean except 1 channel | 4 | 0 |
| −16 | 781 | clean except 4 channels | 21 | 0 |

The tuner always ends clean, but look at what it does: at −16 it has raised
four fifths of the channels back up. The median channel ends about 3 mV below
the equalised K=−18 level and no lower. So with HV off the noise floor of
these chips is about 3 mV below the K=−18 point; once the channels are
equalised they reach it almost together. The gain from trimming is therefore
not a much lower threshold: it is **no masks, no blind channels, and every
channel sitting at its own noise edge**.

## The tools

All in `vmm_monitor_tool/`. On the DAQ machine they are in `~/trim_work/vmm_monitor_tool/`
until this branch is merged and deployed; the program itself is built at
`~/trim_work/build/vmm_monitor`.

| File | What it does | Touches the chips? |
|---|---|---|
| `vmm_trim.py` | reads and edits exception files: summary, trims, raise a channel, shift `sdt`, set a channel bit (`sm`, `sl`, `st`) | no (text only) |
| `vmm_monitor.cpp`, `build.sh` | our measurement program, linked to the installed slow-control library | yes (rewrites the VMM registers) |
| `vmm_monitor_scan.py` | maps, trim curve, DAC calibration, equalise, refine | yes, except `equalize`/`refine`/`report` |
| `noise_probe.py` | 60 s noise capture with a given file, without a DAQ run and **without HV** | yes |
| `trim_tune.py` | raises the noisy channels step by step until the capture is clean | yes |
| `quick_scripts/adaptive_thr_scan.py --raise-steps N` | the existing adaptive scan, raising before masking | (through DAQ runs) |

Rules that all of them keep: they refuse to start while a capture or a run is
active, and they configure the chips only through the GUI's own "Configure
chip" (warm reset first, then the P2 group's script). After any **measurement**
(`map`, `trimcurve`, `sdtcal`) the chips are left in monitor mode: press
**Configure chip** before a run (the tools do it themselves unless told
`--no-reapply`).

### Look at a file

```
python3 vmm_trim.py summary /local/p2/p2testbench/TestBenchCERN/config_ext/FILE.txt
```
One line per chip: `sdt`, gain, peaking time, number of masked channels,
number of trim lines and their range.

```
python3 vmm_trim.py trims FILE.txt --chips 7
```
The trim of each of the 64 channels of chip 7 (real trims, 0..31).
`chip = 2 × hybrid + vmm`.

### Raise one channel by hand, instead of masking it

```
python3 vmm_trim.py raise FILE.txt --out NEWFILE.txt --chip 7 --channels 12,13 --steps 4 --unmask
```
- `--chip 7` chip number (several: `7,9`); `--channels 12,13` (ranges: `10-15`).
- `--steps 4` four trim steps up, about 4.4 mV.
- `--unmask` also removes the mask lines of these channels.
- It refuses if the channel has fewer than 4 steps left (add `--clip` to stop at trim 0).
- `--out` is a new file; the original is never changed. Add `--force` to overwrite an existing output.

The new file appears in the GUI drop-down. Select it, **Configure chip**, run.

### Set the leakage bit (or a mask, or the test pulse) on channels

```
python3 vmm_trim.py bit FILE.txt --out NEWFILE.txt --chip 7 --channels 23-63 --field sl --value 1 --why "baseline too high"
```
`--field` is one of `sm` (mask), `sl` (leakage), `st` (test pulse), `sth`, `sc`, `smx`.
`--value 0` removes the lines again.

### Lower or raise a whole chip

```
python3 vmm_trim.py sdt FILE.txt --out NEWFILE.txt --chips 2-19 --by -4
```
`--by -4` lowers the chip threshold by 4 DAC counts (about 3 mV).

### Measure the chips (9 minutes)

```
python3 vmm_monitor_scan.py map --unmask --bin ~/trim_work/build/vmm_monitor \
    --ext /local/p2/p2testbench/TestBenchCERN/config_ext/FILE.txt --out maps/FILE.json
```
- `--ext` the file describing the state to measure (full path).
- `--unmask` measures with all masks lifted; a masked channel has no baseline to read.
- Prints, per chip: `sdt`, the DAC voltage, baseline and threshold-minus-baseline (median, min, max, spread).
- `python3 vmm_monitor_scan.py report maps/FILE.json` prints it again.

### Equalise a file (once per gain/peaking-time setting)

```
python3 vmm_monitor_scan.py equalize --map maps/FILE.json --headroom 12 \
    --trimcurve out/trimcurve_ch5_40.json --sdtcal out/sdtcal.json --out tables/FILE_eq.json
python3 vmm_trim.py apply-table FILE.txt --table tables/FILE_eq.json --out FILE_eq.txt
python3 vmm_monitor_scan.py map --unmask ... --ext FILE_eq.txt --out maps/FILE_eq.json     # 9 min
python3 vmm_monitor_scan.py refine --map maps/FILE_eq.json --min-trim 8 \
    --trimcurve out/trimcurve_ch5_40.json --out tables/FILE_eq_r.json
python3 vmm_trim.py apply-table FILE_eq.txt --table tables/FILE_eq_r.json --out FILE_eqr.txt
```
- `--headroom 12`: every channel keeps at least 12 trim steps (13 mV) to go up later.
- `equalize` raises each chip's `sdt` so that the median distance to the baseline stays what it was.
- Channels with an abnormal baseline (the dead blocks above) are left out and listed.
- `refine` is the second pass that brings the spread from about 1.8 to 0.7 mV rms.

### Let the tuner raise what needs raising (HV off)

```
python3 trim_tune.py --ext FILE_eqr.txt --tag mytest --k -4 --chips 2,3,5,6,8,10-19 \
    --unmask 2,3,5,6,8,10-19 --steps 3
```
- `--k -4` first lowers the `sdt` of the tuned chips by 4 counts.
- `--unmask …` removes the masks of those chips at the start, so the tuner decides.
- `--steps 3` a channel above the limit is raised 3 trim steps per iteration.
- `--ch-limit-hz 50` the limit (default 50 Hz, as in the adaptive scan).
- Result: `FILE_eqr_tune<tag>_K<k>_i<n>.txt` in `config_ext/` and a JSON log in `~/trim_work/out/tune/`.
- It measures electronic noise only (HV off). Noise that appears with HV on
  is handled during data taking by the adaptive scan:

```
quick_scripts/adaptive_thr_scan.py --base FILE_tuned.txt --raise-steps 3 --points 0,-4,-8 --minutes 180 --tag wk3
```
With `--raise-steps` a noisy channel is raised 3 steps per probe and is masked
only when its trim has reached 0. Without the option the scan behaves exactly
as before. This mode is unit-tested but has **not yet been run on the stand**
(it starts DAQ runs, which set the HV).

## Limits and open points

- Range: 31 steps ≈ 33 mV in total. After equalising, a typical channel has
  12-25 steps left to go up.
- Tuned against noise with **HV off** only.
- The measurement program must be rebuilt (`vmm_monitor_tool/build.sh`) after
  every new slow-control installation; it records the build it was made
  against, and the maps record it too.
- The developer's own `MeasureChannelThreshold` returns 0; we use his
  `MeasureVmmChannel` twice (threshold mode on and off) instead.
- The DAQ GUI does not know about these tools yet (no panel). The files they
  write are ordinary exception files, so everything else works unchanged:
  drop-down, provenance copy into the run folder, config scans.
- Why the inputs of those channel blocks on VMM 4, 7, 9 draw a DC current is not understood;
  the `sl 1` cure is verified with test pulses only.
- File names grow with every tuning step; copy the final file to a short name (as done for `trimmed_Km18_v1`).
