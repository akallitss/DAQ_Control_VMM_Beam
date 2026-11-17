# fastscan/: writing a scan point with the VMM registers only

Copied on 16 Nov 2026 from `~/trim_work/fastscan/` on the DAQ machine, where another
Claude session (`vmm-threshold-efficiency-options`) wrote and ran them, HV off, no DAQ
run, 19:46–19:49. Kept here unchanged as the starting point of the fast rate scan
(see `docs/HANDOFF_FAST_RATESCAN.md`).

| file | what it is |
|---|---|
| `vmm_only_config.py` | copy of `TestBenchCERN/p2basket_sc_config.py` (P2 group) with the FEC and hybrid writes **and the warm reset removed**: parses the base YAML + the exception file and runs only `config-vmm-regs`. Runs with `/local/p2/deploy/bin/p2basket-python`, from `TestBenchCERN/` (`-c . -b config_base/p2b-config-cern-base.yaml -e config_ext/FILE.txt`). The unused `get_interface_name_by_ip` refers to `ifaddr`, which is not imported (harmless, never called). |
| `fast_apply_test.py` | the test: GUI apply vs VMM-only apply on the step-1 files of chips 2,3 (`…step1_c2c3_ratescan_sdt{m4,m6,p10}.txt`), 20 s captures with `noise_probe.capture/count`, link status after each point, restore of the GUI file at the end. |
| `fast_apply_test_20261116.log` | its log. |
| `fast_ratescan.py` | **the fast scan** (16 Nov): references + monitor map, sweep of all chips with VMM-only writes, cumulative masks, stall recovery, `--resume`, `--dry-run`. See `docs/HANDOFF_FAST_RATESCAN.md` §4, §9 |
| `results_20261116/` | fit of the scan of 16 Nov (`fit_full2.json`) and the scan logs |

Result: apply 3.8 s (VMM only) vs 12.0 s (GUI: warm reset + FEC + hybrid + VMM); all ten
hybrids `linkStatus 4` after every point and after the restore; per-channel noise rates
equal within ~5–10 % (median fast/GUI ratio 0.99–1.06, ≥ 100 hits per channel).
The threshold really moves both ways (p10: 10 / 14 Hz on chips 2 / 3).

Do not use the VMM-only write as the FIRST configure after a power cycle, a reboot or
a GUI apply of another base: the FEC/hybrid registers and the warm reset come only from
the full apply. It replaces the full apply only BETWEEN points of a scan that started
with one.
