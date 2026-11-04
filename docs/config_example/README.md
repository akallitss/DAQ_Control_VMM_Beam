# p2basket-sc configuration example (VMM hybrids on one ALINX FEC)

These are the configuration files we use for the lab-cosmics stand on `vmm_daplxa`, plus a small script that writes the same files for any number of hybrids.

The software is `2026-10-23T084325Z-2fb3f16-main`, from `/local/p2/deploy`. All the files come from `/local/p2/p2testbench/TestBenchCERN/` on that machine.

## Files

| File | What it is |
|---|---|
| `p2b-config-cern-base_10hyb.yaml` | **Base YAML**, our base file exactly as used. It is the full register map: subsystem → FEC → 10 hybrids (0–9) → 2 VMMs each → 6 global + 64 channel registers. |
| `lab-cosmics_ext_as-run_2026-11-04.txt` | **Ext file** applied on top of the base YAML on 4 Nov 2026. It holds the gain, peaking time, thresholds and masks for our three chambers. Use it as an example only, because the masks and thresholds are specific to our chips. |
| `example_ext_4hyb.txt` | A clean ext file made by the script: hybrids 0–3, 4.5 mV/fC, 200 ns, threshold 220, one block of masked channels. |
| `make_p2b_config.py` | Script that writes a base YAML and/or an ext file for any list of hybrids. |

## How a configuration is applied

The configuration has two parts:

1. The **base YAML** gives every register of every hybrid and VMM. A hybrid is configured only if it is listed in this file.
2. The **ext file** lists only the exceptions, one per line:
   ```
   Sys <sys> Bec <fec> Hyb <hyb> Vmm <vmm> Reg <register> Field <field> <value>   # comment
   ```

On the DAQ machine, `p2basket_sc_config.py` loads the YAML, applies the ext lines one by one, and sends the result to the FEC, hybrid and VMM registers:

```bash
cd /local/p2/p2testbench/TestBenchCERN
./p2basket_sc_warm_reset.py -b config_base/<base>.yaml -W -w 5   # warm reset; up to 5 retries until every hybrid is linked
./p2basket_sc_config.py     -b config_base/<base>.yaml -e config_ext/<ext>.txt
p2basket-sc --acq-on                                              # start acquisition
p2basket-sc --acq-off                                             # stop acquisition
```

The warm-reset script counts a hybrid as failed when `linkStatus` < 4. Every hybrid listed in the YAML must therefore be connected, or the script will report it as failed.

## Making files for many hybrids

The hybrid blocks in the base YAML differ only in `id` and `alias`, so the script copies `hybrid[00]` once for each hybrid you ask for. Rebuilding hybrids 0–9 with it gives back the original file.

```bash
# base YAML for hybrids 0-15; set the FEC address and the DAQ-PC NIC on the FEC subnet
./make_p2b_config.py base --hybrids 0-15 --ip-fec 192.168.0.12 --ip-daq 192.168.0.14 \
    -o p2b-config-16hyb-base.yaml

# ext file: same settings on every VMM, plus some masked channels (hyb:vmm:channels)
./make_p2b_config.py ext --hybrids 0-15 --gain 4.5 --peaktime 200 --threshold 220 \
    --mask 3:0:17 --mask 5:1:40-42 -o my-ext.txt
```

`--hybrids` takes lists such as `0-7,10,11`.

The ext file is plain text, so you can then edit it by hand. Per-chip thresholds, a trigger hybrid with different settings and per-channel masks are easiest to add that way. Lines further down win.

**Change in the base YAML for your setup:**
- `ip_fec`: the FEC address. Ours is 192.168.0.12.
- `ip_daq`: the address of the DAQ-PC network interface on the FEC subnet. Ours is 192.168.0.14.
- `fec_port` 6007 and the other ports normally stay as they are.

## Field reference (the fields we change)

| Register | Field | Meaning | Values |
|---|---|---|---|
| `bank1spi2` | `sg` | gain | 0/1/2/3/4/5/6/7 = 0.5/1/3/4.5/6/9/12/16 mV/fC. The base default is 2 (3 mV/fC). |
| `bank1spi2` | `st` | peaking time | 0/1/2/3 = 200/100/50/25 ns |
| `bank1spi2` | `sp` | input polarity | 0 = negative |
| `bank1spi2` | `sng` | neighbour logic | 0 off, 1 on |
| `bank1spi2` | `sfm` | SFM (doubled leakage current) | 0/1. We set it per chip. |
| `bank1spi1` + `bank1spi2` | `sdt` | global threshold DAC (10 bit) | **two lines:** `bank1spi1 sdt` = thr % 64 (low 6 bits), `bank1spi2 sdt` = thr // 64 (high 4 bits). Example: 220 = 3·64 + 28 → `bank1spi1 sdt 28`, `bank1spi2 sdt 3`. The base default is 300 (44 / 4). |
| `bank1spi0` | `srec` | fast undershoot recovery | 0/1 |
| `channel[NN]` | `sm` | channel mask | **1 = masked**, 0 = enabled. NN runs from 00 to 63. |
| `channel[NN]` | `sd` | channel threshold trim | 0–31 |
| `channel[NN]` | `st` | channel test-pulse enable | 0/1 |

## Pitfalls in the parser and in our file

- **Only lines that name a `Vmm` are applied.** The header of the ext file also gives a `Hyb … Reg …` form without `Vmm`, but `p2basket_sc_config.py` skips such lines without any warning. Hybrid-level registers must be set in the YAML.
- A line is a comment only if its first word is exactly `#`. Lines like `#Sys …` are also skipped, because they don't start with `Sys`. Everything after the value is ignored, so trailing `# …` comments are fine.
- Lines are applied from top to bottom. In our as-run file, `Hyb 2 Vmm 0 channel[40]` is first unmasked (`sm 0`) and later masked (`sm 1`). It ends up **masked**.
- Some comments in our as-run file are out of date:
  - The detector section says "Here : 3 mV/fC" but sets `sg 3`, which is **4.5 mV/fC** (as the filename says).
  - The trigger-hybrid thresholds are commented "300" and "1023". They are really 6·64 + 44 = **428**.
  - The `sdt` values in the "Setup SFM and thresholds" section already include the −14 DAC step of our adaptive scan, compared with the `…_h0open.txt` file it started from. The masks the scan added are listed after the "adaptive threshold scan wk2" line.
- Configured hybrids are given by the base YAML, not by the ext file. Ours lists hybrids 0–9 only. A hybrid that is connected but not in the YAML (our hybrid 11) is never configured.
