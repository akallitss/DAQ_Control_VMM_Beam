# Lab cosmics campaign: observations log

Things noticed while bringing up and running the three-chamber cosmic scan on vmm_daplxa.
Kept as feedback for the firmware / slow-control people: the stack was **upgraded between the
SPS beam test and this campaign**, so differences from July are worth reporting. Append new
entries at the bottom with the date; state what was seen, not what it means, unless verified.

Setup: ALINX/FEC at 192.168.0.12, hybrids H1-H9 (VMM 2-19), data NIC enp4s0f1, `p2basket-sc` from
`/local/p2/deploy`. No trigger hybrid in this setup (H0 not used).

## 2026-10-22

### Slow control / firmware

1. **Tool banner:** every `p2basket-sc` call prints `2026-10-20T161337Z-69ca52a-dirty-main`
   ("dirty"). The deploy dir is `/local/p2/deploy/install_2026-10-22T08h46m22sZ`.
2. **Error after every slow-control call:** warm reset, chip apply and link-status reads all end with
   `Error: [Errno 13] Permission denied: '/local/p2/deploy/install_2026-10-22T08h46m22sZ/python-venv'`
   then `p2basket-python: line 9: .../python-venv/bin/pip: No such file or directory` and
   `line 17: .../python-venv/bin/activate: No such file or directory`. The command itself still
   succeeds (rc 0, 0 failed hybrids). Looks like a wrapper trying to (re)create a venv in a directory
   our user cannot write to.
3. **Link status of hybrid 0:** `hybrid[00].linkStatus = 4` (ready) like hybrids 1-9, although this
   stand has no trigger hybrid. Hybrids 10-15: 0. No VMM 0/1 hits appear in the data.
4. **`vmmAppReg[15]` changed during a run:** `0x00000044` before the run, `0x00002044` in the link
   check logged when the run finished, and still `0x00002044` in a standalone read minutes later
   (hybrid links all still `4`). `vmmAppReg[16]` stayed `0x44444444`. Meaning unknown: the new
   bit is bit 13.
5. **July `_opt` config registers vs comments:** the H0 lines say `# 300` and `# 1023` but the
   registers (`spi2*64 + spi1`) give 428 for both. For H1-H9 the comments match the registers.
6. **Masks:** the `_opt` file's "Masked channels" section writes `sm 0`, which is the base yaml
   default for all 1280 channels. If `sm 1` is the masking value those three channels were not
   masked in July. Not verified on hardware.

### Readout

7. **Flood at the July `_opt` thresholds** (run with the chambers at HV, 2 min): 1.5 GB in one
   file, about 48 GB/h. From a 60 MB sample (6634 packets, 5.9 s): 1.66 MHz total, VMM 9 = 89 %,
   VMM 4 = 10 %, VMM 14 = 0.4 %, all others < 0.03 %. VMM 4 and 9 were at sdt 300 (the "walking
   burst" chips of the beam test). All of VMM 2-19 present.
8. **QA on a 1.5 GB file does not finish:** `vmm_pcapng_qa.py` with `--max-packets 100000` ran 3.5 min
   and wrote nothing (killed by timeout, mostly I/O wait). The same script on the first 60 MB took
   well under 200 s. At this rate every 120 s capture file is ~1.5 GB, so online QA would not keep up.

### Machine / software

9. `screen` is not installed, so `bash_scripts/restart_all_tmux_processes.sh` kills tmux and does not
   restart anything.
10. `start_servers.sh` is not idempotent: for a session that already exists it types its command into
    the existing pane.
11. The Kerberos ticket on the machine had expired on 3 Aug (needed for the EOS backup).
12. The apply returns before the chips have necessarily settled: the first `/chip_config/status` poll
    after `apply` still showed the previous file, so poll on `last.file` as well as `running`.
