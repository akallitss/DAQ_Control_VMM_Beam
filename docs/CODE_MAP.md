# VMM DAQ — map of all the code, from the browser down to the chip

Surveyed on the DAQ machine (dedippce185, `ssh vmm_daplxa`) on 2026-11-12.
`ARCHITECTURE.md` describes this repository in depth; this file is the map of
**everything else it stands on** — the slow-control middleware, the bench
scripts, the configuration files — and of how a threshold travels from a text
file to a VMM register. Read this before touching chip configuration.

Companion: `PLAN_PER_CHANNEL_THRESHOLDS.md` (what to build next).

---

## 1. The five layers

```
 5  GUI + run control        DAQ_Control_VMM_Beam            (this repo, ours, Python)
 4  bench scripts            TestBenchCERN/*.py, *.sh         (P2 group, Python/bash)
 3  slow-control middleware  p2basket-sc  ("p2b")             (CEA developer, C++ + Python binding)
 2  firmware                 ALINX FEC + Spartan-7 hybrids    (bit files only on this machine)
 1  hardware                 10 hybrids x 2 VMM3a x 64 ch
```

A layer only ever calls the one below it, and always as a **subprocess** (5→4,
5→3) or through the **Python binding** (4→3). Nothing is imported across the
5/4 boundary and nothing is copied. Layer 3 talks to layer 2 over UDP.

Side branch, not in the control path: **readout/analysis** (`p2-readout`,
`cosmic_vmm_analysis`) reads the pcapng files that layer 5 writes.

| Layer | Lives at (DAQ machine) | Owner | Can we edit it? |
|---|---|---|---|
| 5 | `/local/p2/DAQ_Control_VMM_Beam` (deploy target; develop elsewhere, push, Git Reset) | us | yes |
| 4 | `/local/p2/p2testbench/TestBenchCERN` (+ `TestBenchCosmics`, older sibling) | P2 group, files owned by several colleagues | data files yes (new `config_ext/*.txt`); scripts are "called, never modified" |
| 3 | `/local/p2/deploy/` → `install_<UTC timestamp>/` | slow-control developer | no (read-only install; source on drf-gitlab `p2/p2basket-sc`) |
| 2 | bit/mcs files in `/local/p2/p2soft/p2basket-sc/` | firmware developers | no, source not on the machine |

---

## 2. Layer 1-2: hardware as the software sees it

- One subsystem, one FEC (the ALINX board): `subsystem[00]` / `fec[00]`.
- 10 hybrids enabled, `hybrid[00]`..`hybrid[09]`. **H0 is the trigger hybrid**
  (scintillator coincidence on ch40 in the lab); H1-H9 read the detectors.
- 2 VMM3a per hybrid, `vmm[00]`, `vmm[01]`. Chip number used everywhere in the
  analysis: **chip = 2 x hybrid + vmm** (0..19).
- 64 channels per VMM.
- Two separate networks: slow control (UDP to the FEC, on the USB adapter
  `enx…`, 192.168.0.x; the TDK supplies sit there too) and data
  (`enp4s0f1`, 192.168.1.16, UDP 6006) which only dumpcap and the live monitor
  listen to. Slow-control UDP ports are FEC registers: `fec_port` 6007,
  `vmmapp_port` 6600, `dvm_i2c_port` 6601, `vmmasic_port` 6603.
- Each hybrid carries an I2C ADC that digitises the VMM **monitor output (MO)**.
  The middleware uses ADC input 2 for it. That is the only analog read-back
  path: temperature, the threshold DAC, the pulser DAC, the band-gap, and a
  selected channel's baseline or trimmed threshold all come through MO.
- VMM configuration is **write-only**. The chip is written as one SPI frame
  (all registers at once); there is no command that reads registers back.

---

## 3. Layer 3: p2basket-sc, the slow-control middleware

### 3.1 What is installed

`/local/p2/deploy/{bin,lib,include,share,python-venv}` are symlinks into the
newest `install_<timestamp>/`. Build in use on 2026-11-12:
`2026-11-05T165959Z-d2cb9ef-dirty-main` (`p2basket-sc --version`). Older
installs stay next to it, so a roll-back is a symlink change (developer's job).

| File | What it is |
|---|---|
| `bin/p2basket-sc` | the command-line tool. Everything layer 5 does to the chips goes through it or its library |
| `bin/p2basket-python` | launcher: sets `PYTHONPATH`/`LD_LIBRARY_PATH`, activates the embedded venv, runs python. Bench scripts use it as their shebang |
| `bin/p2basket-gui` | new Qt GUI of the same developer (config editor, run control) |
| `bin/vmmdcs-p2b` | the **legacy RD51 "vmmsc" GUI**, rebuilt for the ALINX. Contains the calibration module (pedestal, threshold, trim calibration) |
| `bin/test_*`, `bin/cconfiged` | unit tests, config editor |
| `lib/libp2b-sc.so` | hardware model (`p2b::hw`) + UDP commands (`p2b::sc::cmd`) + `Backend` |
| `lib/libp2b-run.so` | CLI (`P2bScCli`), session lock, config loading |
| `lib/libp2b-acq.so`, `libmdcs-*.so`, `libcconfig-*.so` | acquisition, utilities (networking, bit fields), config framework |
| `lib/python/site-packages/p2basket/` | Python binding (nanobind): `__init__.py` + `_binding.*.so` |
| `include/p2b/...` | C++ headers of everything above — the only up-to-date source we can read |
| `share/p2b/cfg/`, `share/p2b/examples/p2basket_sc_example.py` | sample configs, binding example |

Source: `/local/p2/p2soft/p2basket-sc/` is a **stale checkout (Dec 2025)** —
useful to read how registers are packed (`src/sc/hw/RegisterVmm.cpp`,
`src/sc/cmd/ConfigureVmmsRegisters.cpp`) but it predates warm reset, the
session lock, the Python binding and the measurement commands. The current
source is on drf-gitlab (`p2/p2basket-sc`); we have no checkout of it.

### 3.2 The hardware model

`SubSystem → Fec → Hybrid → Vmm → Register → Field`, mirrored 1:1 by the base
YAML (30 764 lines for 10 hybrids). Every node has `id`, `alias`, `isEnabled`.

Registers of one VMM, in the order they are sent:

| id | alias | type | content |
|---|---|---|---|
| 0-2 | `bank0spi0..2` | VmmGlobal | L0/BCID settings (`nskipm_i`, `sL0ena`, `truncate`, `window`, `rollover`, `offset`, `l0offset`) |
| 3-66 | `channel[00]`..`channel[63]` | VmmChannel | **per-channel bits**, see below |
| 67 | `bank1spi0` | VmmGlobal | `sbip`, `srec`, `stlc`, `slvs*`, resets… |
| 68 | `bank1spi1` | VmmGlobal | **`sdt` (6 LSB of threshold DAC)**, `sdp10` (pulser DAC), ADC settings |
| 69 | `bank1spi2` | VmmGlobal | `sp`, `sg` (gain), `st` (peak time), `sng`, `sfm`, monitor select (`sbmx`, `sbfm`, `scmx`, `sm5_sm0`), **`sdt` (4 MSB)** |

Channel register fields (bit positions from the Dec 2025 source):

| field | bits | meaning |
|---|---|---|
| `sc` | 23 | large sensor capacitance mode |
| `sl` | 22 | leakage current disable |
| `st` | 21 | test capacitor on (channel receives the internal pulser) |
| `sth` | 20 | test pulse x10 |
| `sm` | 19 | **mask** (1 = channel off) |
| `smx` | 18 | channel monitor mode: analog output vs trimmed threshold |
| `sd` | 13-17 | **threshold trim, 5 bits, 0..31** — see the bit-order warning below |
| `sz10b` / `sz08b` / `sz06b` | 8-12 / 4-7 / 1-3, bit-reversed | ADC zero offsets |

**Bit order of `sd` (measured 2026-11-13).** The Dec 2025 source reverses the
bits of `sd` inside the library (the "VMM2 bug" handling of the RD51 software).
The build installed on 2026-11-05 does **not**: `sd = 1` is sent as bit 13 and
acts on the chip as trim 16. To get trim *t* one must write `bit_reverse_5(t)`.
`vmm_monitor_tool/vmm_trim.py` does this and comments each line with the real
trim. Also: the binding accepts `sd = 32` silently and stores 0.

Two homonyms to keep apart: global **`st`** in `bank1spi2` is the peak time;
channel **`st`** is the test-capacitor switch.

### 3.3 The command line

`p2basket-sc [--<command>] ...` — commands are executed in argument order.

- **Parameter commands** (stateful, no hardware access): `config-file <yaml>`,
  `dry-run`, `no-ping`, `no-print-results`, `log-datagrams`, `save-enabled`,
  `session-lock <minutes>`, `version`, `help`.
- **Action commands**: `ping`, `warm-reset`, `read-sys-regs`,
  `read-link-status`, `read-fw-version`, `read-chip-id`, `read-geo-pos`,
  `read-temperature`, `config-fec-regs`, `config-hyb-regs`,
  `config-vmm-regs`, `acq-on`, `acq-off`, `config-reset`, `config-diff`,
  `to-yaml-file`, `to-json-file`, `ssleep <s>`.

Facts that matter:

1. Only `config-fec-regs`, `config-hyb-regs`, `config-vmm-regs` write
   registers. `--config-file` alone only loads the model into memory (needed
   for addressing). So `acq-on/off` can never change a chip.
2. `config-vmm-regs` sends **one UDP frame per VMM containing all 70 register
   words**. There is no "write one channel": changing one trim rewrites the
   whole chip, for every enabled VMM. Consequence: a per-channel change costs
   the same as a full configuration and must respect the same rules (no
   capture running, warm reset first).
3. The register model lives **in the process**. Nothing is remembered between
   two invocations except the path of the last config file per user
   (`/tmp/p2basket-context-<login>.txt`). What is "in the chips" is therefore
   known only as *the base YAML + the ext file last applied*.
4. `to-yaml-file` from the Python binding hangs (noted in
   `p2basket_sc_config.py`), so there is no dump of the applied state either.
5. **Session lock**: `/run/p2basket/session.lock`, one user at a time. The
   directory `/run/p2basket` is created by `p2basket.service`, which is
   **disabled** — after a reboot it does not exist and the lock probe fails
   with `No P2basket Runtime Service directory '/run/p2basket'` until someone
   with sudo runs `sudo systemctl start p2basket.service`. (State on
   2026-11-12 after the OS-update reboot: missing.)

### 3.4 The Python binding

```python
import p2basket                      # under bin/p2basket-python
p2basket.exec_cmd("config-file", "config_base/p2b-config-cern-base.yaml")
vmm = p2basket.sc_backend().sub(0).fec(0).hyb(3).vmm(1)
vmm.reg("bank1spi2").field("sg").set_value(3)       # a global field
vmm.chan(45).field("sd").set_value(12)              # a channel field
vmm.reg("channel[45]").field("sm").set_value(1)     # same register, by alias
p2basket.exec_cmd("config-vmm-regs")                # now it is written
res = p2basket.exec_cmd("read-temperature")         # dict[sub][fec][alias] -> value
```

Bound: `exec_cmd`, `get_cmd`, `sc_backend`, `config_logger`; classes
`Subsystem/Fec/Hybrid/Vmm/Register/VmmChannel/NamedField`, `Cmd`, `CmdLine`,
`SessionLock`. **Not bound**: `Backend::Connector` (so no way to run a UDP
command that is not also a CLI command).

### 3.5 Measurement code that exists but is not reachable

`include/p2b/sc/cmd/MeasureVmmI2C.h` and `VmmAdcOperation.h`, compiled into
`libp2b-sc.so` and registered in `Backend::UdpCommandTuple`:

| class | id | what it measures through MO + hybrid ADC |
|---|---|---|
| `ReadVmmAdc` | 1300 | raw ADC value of every VMM |
| `MeasurePulserDac` | 1400 | pulser DAC voltage |
| `MeasureThresholdDac` | 1500 | **global threshold DAC voltage** (what `sdt` really gives) |
| `MeasureBandgapReference` | 1600 | band-gap |
| `MeasureTemperature` | 1700 | temperature — the only one exposed, as `read-temperature` |
| `MeasureVmmChannel(channel=0)` | 1800 | analog level of one channel |
| `MeasureChannelThreshold(channel=0)` | 1900 | **one channel with threshold mode on and off, returns the signed difference** (`…channel[N].thresholdDifference`) |

The channel is a constructor argument and the backend instantiates each
command once per FEC with the default, so as built only channel 0 would be
measured even from C++. None of 1300-1900 except 1700 has a CLI name, and the
binding cannot reach them. This is unfinished work of the slow-control
developer, present in every install since 2026-10-22.

Tested 2026-11-13 with our own program `vmm_monitor_tool/vmm_monitor.cpp`,
which constructs these command objects itself for any channel and calls
`run()` on them:

- `MeasureVmmChannel` works: with the channel bit `smx = 0` it returns the
  channel baseline, with `smx = 1` the trimmed threshold, in mV. About 4 s per
  channel for all 20 VMMs.
- `MeasureThresholdDac`, `MeasureTemperature` work.
- `MeasureChannelThreshold` always returns 0 (broken); not used.
- A measurement rewrites the VMM registers from the model in memory and
  leaves the monitor selection (`scmx`, `sm5_sm0`, `smx`) changed: the chips
  must be configured again before taking data.
- A masked channel (`sm = 1`) reads about 45 mV: no baseline. Measure with the
  masks lifted.

---

## 4. Layer 4: the bench scripts and the configuration files

`/local/p2/p2testbench/TestBenchCERN/` — the directory every slow-control
call runs in (`cwd`).

| File | Role | Called by |
|---|---|---|
| `p2basket_sc_config.py -c <dir> -b <base.yaml> -e <ext.txt>` | load base, apply ext exceptions in memory, then `config-fec-regs`, `config-hyb-regs`, `config-vmm-regs`. Exit 0 = done | GUI "Configure chip", `config_scan.py`, `capture_guard.py` |
| `../TestBenchCosmics/p2basket_sc_warm_reset.py -c -b -W -w <n>` | warm reset + link status, retried; **exit code = number of non-ready hybrids** (link status < 4) | same |
| `config_base/p2b-config-cern-base.yaml` | the full register model with defaults. Exactly one YAML in this folder (auto-detected) | both scripts, and every `acq-on/off` |
| `config_ext/*.txt` | **exception files**: the differences to the base. The folder *is* the GUI dropdown (178 files today) | chosen in the GUI / by the scan |
| `switchOn_tdk.sh`, `switchOff_tdk.sh`, `pilot_tdkl.py` (in `config/tdkl_*.json`) | TDK low-voltage supplies | GUI LV panel |
| `config_scan/`, `vmm_thr_scan.{py,sh}` | a colleague's own July threshold scan (template + sed + tshark) | nobody in our chain |
| `daq_control.sh`, `loop_daq.sh`, `mon_control.sh`, `convert.sh`… | the pre-GUI bench DAQ | nobody in our chain |

`TestBenchCERN/p2basket_sc_config.py` and the `TestBenchCosmics` copy are
identical.

### 4.1 Grammar of an exception file

One setting per line, whitespace separated, parsed by position:

```
Sys <s> Bec <f> Hyb <h> Vmm <v> Reg <register alias> Field <field> <integer> [# comment]
 0   1   2   3   4   5   6   7   8        9            10     11       12
```

- A line whose first word is not `Sys` is ignored (so `#...` and blank lines
  are comments). Text after the value is ignored.
- Only the `Vmm` form is implemented; the header mentions a hybrid-register
  form (`Hyb <h> Reg …`) that the parser does not handle.
- **No wildcards and no ranges** (`channel[*]` appears only in a commented
  line). 64 channels = 64 lines.
- A wrong alias or field name raises inside the binding and aborts the apply
  with a non-zero exit; a wrong *keyword* is only printed and skipped.
- The value is not range-checked by the script.

What the existing files set: `sg`, global `st`, `sng`, `sp`, `sfm`, `srec`,
`sdt` (two lines per chip) and, per channel, `sm` (mask) and channel `st`
(pulser). **No file sets `sd`; in the base YAML all 1280 `sd` and `sm` are 0.**

### 4.2 The global threshold in a file

```
sdt (10 bits) = 64 x <bank1spi2 sdt> + <bank1spi1 sdt>
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi1 Field sdt 46
Sys 0 Bec 0 Hyb 1 Vmm 0 Reg bank1spi2 Field sdt  3     # 3*64+46 = 238
```

The `#` numbers in the files are hand-written and sometimes wrong;
`quick_scripts/make_cosmic_sdt_configs.py` recomputes them and never trusts
the comment.

---

## 5. Layer 5: where this repository touches the chips

Everything else about this repo is in `ARCHITECTURE.md`. The complete list of
places that reach layer 3 or 4:

| Code | What it runs | When |
|---|---|---|
| `flask_app/chip_config.py::ChipConfig.apply` | warm-reset script, **then** `p2basket_sc_config.py` | GUI "Configure chip"; `/chip_config/apply` |
| `ChipConfig.warm_reset` | warm-reset script alone | GUI "Warm Reset" |
| `ChipConfig.run_armed` | nothing — the gate: a run may start only if the last apply succeeded and is newer than the last warm reset, and that warm reset has not been consumed by a run | Start Run |
| `vmm_daq_control.py::_run_sc` | `p2basket-sc --config-file <base> --read-link-status / --acq-on / --acq-off` | around every sub-run |
| `daq_control.py::copy_chip_config_provenance` | copies the selected ext file into the run directory | run start |
| `config_scan.py` | select → apply → warm reset → run, one **run per ext file** | unattended scans |
| `capture_guard.py::recover` | re-apply + warm reset after a dead readout | automatic |
| `quick_scripts/make_cosmic_sdt_configs.py` | writes ext files (per-chip `sdt = baseline + d`, optional `sm 1` masks) from a template | by hand before a scan |
| `session_lock.py` | reads the p2basket session lock for the GUI | status |
| `flask_app/power_control.py` | TDK scripts | LV panel |

Wiring is in the git-ignored `config/chip_config.json` (`conf_dir`,
`base_subdir`, `ext_subdir`, `apply_script`, `warm_reset_script`,
`warm_reset_loop`, `timeout_s`) and in `run_config_beam.py::SITES[site]`
(`p2basket_sc`, `sc_cwd`, `sc_config`). The selected file and the last
apply/warm-reset results persist in `config/chip_config_state.json`.

Rules encoded there, learned the hard way — any new chip operation must keep
them:

1. Never touch the chips while `dumpcap`/`tcpdump` runs.
2. Warm reset **immediately before** the configuration, never after (a later
   warm reset reverts the trigger hybrid and every chip reports offset -16).
3. One chip operation at a time (`ChipConfig._lock`), and the p2basket session
   lock must be free.
4. The file a run was taken with is copied into the run directory.

---

## 6. How a threshold reaches a channel today

```
config_ext/<file>.txt   ── chosen in GUI or by config_scan.py
        │  ChipConfig.apply()                         (layer 5)
        ▼
p2basket_sc_warm_reset.py -W -w 3                     (layer 4)  hybrids ready?
p2basket_sc_config.py -b base.yaml -e <file>.txt      (layer 4)
        │  config-file            → model in memory = base YAML
        │  each ext line          → reg.field(name).set_value(v)
        │  config-vmm-regs        → 20 UDP frames, 70 words each   (layer 3)
        ▼
FEC → SPI → VMM registers                              (layers 2, 1)
        channel threshold = chip threshold (sdt, ~0.77 mV/count) - trim x ~1.1 mV   (measured)
```

So the **per-channel trim already has a complete path**: a line
`… Reg channel[NN] Field sd <0..31>` in an ext file is handled by exactly the
code that handles today's `sm` lines. Verified on 2026-11-13 by measuring
the thresholds on the chips (see `PER_CHANNEL_THRESHOLDS.md`). The legacy GUI did write trims to this hardware in
November 2025 (`/local/p2/p2soft/_install/configs/config_alinx_channelThresholds_20251107-115622.json`,
`sd` between about 7 and 21 per channel), so the firmware accepts them.

Reading back: the registers are write-only and there is no dump of the applied
state, but the **analog** baseline and threshold of every channel can be
measured with `vmm_monitor_tool` (9 minutes for all 1280 channels). The
indirect read-back from data (lower edge of each channel's ADC spectrum, noise
rate) remains the cross-check.

---

## 7. Everything else under /local/p2

| Path | What | Status |
|---|---|---|
| `p2-readout/{src,venv,wheels}` | `p2-decode`, `p2-mapping`, `p2-monitor` — our decoder, pad mapping, live monitor/processor | in use (QA, processor, live tile) |
| `p2-setups/lab_saclay_2026.yaml` | the stand description (detectors, cabling, HV) read by `setup_source.py` | in use, its own git repo |
| `p2data/` | run data | in use |
| `ops/` | July beam-test helper scripts (beam gate, night scan) | historical |
| `p2soft/vmm-analysis` | first online analysis (`vmm_hybrid_pcapng_monitoring.py`, `vmm_decode.py`) | superseded by p2-readout |
| `p2soft/vmmsc` | RD51 slow control, original | legacy, reference for calibration procedures |
| `p2soft/p2basket-sc` | stale middleware checkout (Dec 2025) + firmware bit files | reference only |
| `p2soft/_install/configs` | legacy JSON configs of `vmmdcs`, incl. the Nov 2025 per-channel trims | reference |
| `p2soft/vmm-sdat`, `mordicus-2` | RD51 analysis framework, utility library | not used by us |
| `p2equipment/` | LVPS, scope, Arduino, ZedBoard helpers | bench |
| `p2setup/` | shell environment files of the P2 software | bench |
| `system_monitor/` | machine monitor | — |

Analysis code is not on this path: `cosmic_vmm_analysis` is a symlink into the
`P2_basket_analysis` repository.
