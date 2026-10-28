# P2 readout packages in the DAQ

The decoding, mapping and monitoring code now lives in three packages
(github.com/akallitss/p2-decode, p2-mapping, p2-monitor). This repo uses them in
two places, both configured in `p2_readout_config.py` and both off or opt-in by
default.

## Where the packages are on the DAQ box

| Path | What |
| --- | --- |
| `/local/p2/p2-readout/venv` | the packages and their numpy/pandas/matplotlib/pyyaml; separate from this repo's `.venv` |
| `/local/p2/p2-readout/wheels` | the wheelhouse they were installed from (the box's network to GitHub/PyPI drops connections) |
| `/local/p2/p2-readout/src` | the tagged sources, for running their tests on the box |

Updating: build wheels on a machine with network (`pip wheel --no-deps <repo at tag>`),
copy them into `wheels/`, then

```bash
/local/p2/p2-readout/venv/bin/pip install --no-index --no-deps --find-links /local/p2/p2-readout/wheels p2-monitor==X.Y.Z
```

## 1. Processor engine

`PROCESSOR_ENGINE = 'p2'` in `p2_readout_config.py` makes `vmm_processor_watcher.py`
run `p2-monitor-reduce` instead of `vmm_qa/vmm_reduce.py`. Only the worker changes:
the memory kill, stall and hard timeouts, nice/ionice, thread cap, oom_score_adj,
the reset signal and mem_guardian all stay as they are. The p2 engine:

- writes the same store layout (`counts.npz`, `scalars.json`) the QA watcher and
  dashboard read, plus `counts_chip.npz` (per FEC:VMM chip), rates per
  FEC/chip/hybrid/connector/detector and the data volume in `scalars.json`;
- drops the per-hit columns by default (`P2_KEEP_COLUMNS`): measured on this box,
  a 1.1 GB capture reduces in 36 s without them and 114 s (plus 3.8 GB on disk) with;
- keeps its own state file (`config/processor_state_p2.json`); `P2_STORE_INNER =
  'hits_store_p2'` runs it next to the legacy processor for a comparison;
- still reduces a capture its setup has no cabling for (electronics view only, the
  reason in `scalars.json` `mapping_error`);
- has no efficiency stage yet, so the trend dashboard shows no efficiency for its
  stores. The lab runs without efficiency anyway.

After changing the engine: Stop and Start Processor in the GUI (the start
regenerates `config/processor_config.json`).

## 2. Live tab

The GUI's Live tab starts and stops one `p2-monitor live` process (tmux
`vmm_live`) that follows the newest sub-run and writes `config/live_status.json`
every 5 s, plus three PNGs in `live_png/` every 60 s. Flask only reads them; it
never decodes. The monitor bounds its own work, not the data: at most 16 MB read
per poll, skip to the newest data beyond 64 MB of backlog (the tab then says how
much was read), its own memory capped at 600 MB, nice 19, oom_score_adj 500, and
mem_guardian may kill it. Limits are `LIVE_OPTIONS` in `p2_readout_config.py`.

Measured on this box with run_120's 1.1 GB flood capture already on disk: a cold
start read 1.5 % of it (16.6 MB) in 4.1 s with 235 MB peak memory and reported
~1550 MB/min.

### Reading from the network instead of the file

The tab's source selector offers "from the network port" where the site has
`LIVE_UDP_ADDRESS` (lab: `192.168.1.16:6006`, the FEC's destination). The
monitor then binds a plain UDP socket on that address and decodes the FEC's
datagrams directly, without waiting for dumpcap to write them. dumpcap is not
affected: the kernel gives every datagram both to its capture and to this
socket. It polls every second and decodes at most 4 MB per poll
(`LIVE_UDP_OPTIONS`); what it cannot read, the kernel drops for this socket only
and counts. The tab's "sampling" badge shows the fraction of packets decoded,
and all rates and MB/min are scaled to the full stream by it. The same scaling
now applies when the file reader skips ahead under a flood.

## Guards

- `mem_guardian`: `p2-monitor-reduce`, `p2_monitor.cli live` and
  `p2_monitor.cli reduce` are killable (code defaults and `config/mem_guardian.json`).
- `rate_guard`: unchanged. Its trigger needs no decoding and its culprit decode is
  limited to 3000 packets; making the safety net depend on the optional live
  monitor would add a failure mode.
