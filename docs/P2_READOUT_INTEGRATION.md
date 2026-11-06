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

## 1. Processor and Online QA

`vmm_processor_watcher.py` runs `p2-monitor-reduce` on every finalized capture
and `qa_watcher.py` runs `p2-monitor-qa` on every processed store (both from the
p2-readout venv), under the watchers' guards: memory kill, stall and hard
timeouts, nice/ionice, thread cap, oom_score_adj, reset, mem_guardian. The stores
hold `counts.npz`, `counts_chip.npz` (per FEC:VMM chip) and `scalars.json`
(rates per FEC/chip/hybrid/connector/detector, data volume); the per-hit columns
are not kept (`P2_KEEP_COLUMNS`). A capture no cabling entry covers is still
reduced (electronics view, the reason in `scalars.json` `mapping_error`).

The previous in-repo worker and QA script (`vmm_qa/vmm_reduce.py`,
`vmm_qa/vmm_pcapng_qa.py`, and `qa_catchup.py` that drove them) were replaced on
2026-10-28, after identical `counts.npz` and scalars on run_123/run_124 captures,
and removed on 2026-11-02.

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

## 3. The setup file: one place for detectors, cabling and HV

`setup_source.py` loads the site's p2-mapping setup (`lab_saclay_2026` in the
lab, `sps_h4_2026` at the beam; `local` and `bench` use the beam's). A machine
keeps its own editable copy in `/local/p2/p2-setups/<name>.yaml` (a git
repository: every change has an author, date and message); without one the
copy shipped with p2-mapping is used.

`run_config_beam.py` takes from it, under the same names as before:
`P2_VMM_CABLING` and the cable per connector (the cabling entry in force when
the run starts), `LAB_CHAMBERS` (detector aliases), the z positions, `P2_HV`,
`URWELL_HV`, `OPERATING_HV` and the ceilings. `TRIGGER_VMM` keeps its notes but
must agree with the file's trigger, or no run is configured.
`p2_readout_config.P2_SETUP` is the same file, so the processor, QA and live
monitor read exactly what the run configuration read.

Checked when switching (2026-10-29): for every site (local, bench, sps, lab)
and every run plan, the module constants and the generated run configuration
were byte-identical before and after (28 cases); a modified copy of the file
changed the run configuration and the processor's `--setup` accordingly.

Still carrying their own copies (old QA path): `vmm_qa/vmm_stations.py`
(cabling; used by rate_guard's culprit decode) and `vmm_qa/vmm_scan_curves.py`
(HV channels).

The GUI's **Setup** tab shows the file read-only: detectors, HV map and
operating point, every dated cabling entry with what changed from the previous
one, and the file's git history.

### Changing the stand and defining the run (Setup tab)

- **Run** (what the next Start Run records): measurement (cosmics, Fe55, Am241,
  pulser, noise), run type, seconds per capture file, number and length of
  sub-runs, and for `hv_scan` the detectors stepped, what moves (mesh, drift, or
  both keeping each detector's operating gap), first voltage, signed step,
  number of points and minutes per point. Stored as GUI settings
  (gui_settings.py, group `run`); checked against the stand (detectors exist,
  voltages under the setup's ceilings; no voltage scan where this DAQ does not
  set the HV, i.e. the lab). Calibration runs are not defined yet.
- **Stand** ("Change the stand…"): detectors (station, chamber, z), external
  trigger (on/off, hybrid, VMM, channel) and the cabling, connector by
  connector with its bottom and top half (VMM ID and strip order each; hybrid,
  FEC and cable per connector). "Check and preview" validates the whole file and
  lists the changes; "Save" adds a dated entry in force from now (p2-mapping
  0.5.0 `edit.add_entry`: inserted as text, comments kept, re-checked) and
  commits it to /local/p2/p2-setups with the note. Refused while a run is in
  progress: the processor and live monitor apply the entry in force at each
  packet's time. The run configuration, processor, QA and live monitor all follow
  the new entry; earlier runs keep theirs.

vmm_qa/vmm_stations.py and vmm_qa/vmm_scan_curves.py keep their own copies on
purpose (standalone offline analysis, e.g. on lxplus); rate_guard only uses
vmm_stations' decoder, not its cabling.
