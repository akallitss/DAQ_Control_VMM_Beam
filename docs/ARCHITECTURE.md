# VMM Beam DAQ — code structure, design principles, and flow of information

A walkthrough of `DAQ_Control_VMM_Beam` for the group discussion. The README is
the operator manual ("how do I run it"); this document is the developer/reviewer
view ("why is it built this way, and what talks to what").

---

## 1. What the system is

It is a **control and monitoring layer around a raw-packet capture**. It does
not read out the detector itself — the VMM front-ends push UDP onto an Ethernet
link on their own. What this repo does is:

1. decide *when* acquisition is on (run/sub-run schedule),
2. set and log the *conditions* (HV, LV) for each sub-run,
3. write the raw stream to disk in bounded, self-describing files,
4. analyze each file *while the run continues* (online QA),
5. show all of that to a shifter in a browser, and shout when something breaks,
6. get the data off the machine (EOS / local mirror) and keep the disk alive.

Everything is Python 3 + a few bash wrappers. No compiled components, no
framework: the deployment unit is `git pull` + restart.

### 1.1 Where the code came from — provenance

This comes up in every discussion, so it is worth being precise. There are
**two** ancestries, and they cover different layers.

**The run-control framework is a port of Dylan Neff's Dream/nTof stack**
(`Cosmic_Bench_DAQ_Control`, `nTof_x17_DAQ`, `DAQ_Control_Dream_Beam`), not of
the Saclay/CERN VMM test bench. The initial commit says so
(`0a10e38 Initial commit: VMM SPS DAQ control (cloned from Dream Beam DAQ
architecture)`) and landed 49 files in one go. Keeping the same skeleton is what
lets the two stacks be operated by the same people and co-host on one machine
(disjoint ports, `vmm_`-prefixed tmux sessions).

**The TestBenchCERN code is used — but it is *called*, not copied in.** The
front-ends are never configured by code in this repo.

| Layer | Origin | How it is used here |
|---|---|---|
| `Server.py`, `Client.py`, `daq_control.py`, `DAQController.py` | Dream/nTof | ported, VMM-adapted |
| `hv_control.py` (CAEN) | Dream/nTof | ported ~unchanged |
| `lv_control.py` (Aim-TTi) | **new** | written as a structural clone of `hv_control.py` |
| `qa_watcher.py`, `backup_watcher.py`, `space_manager.py`, `flask_app/` | Dream/nTof | ported, VMM-adapted |
| **ALINX slow control** | **TestBenchCERN** | **external binary `alinx-sc`, invoked as a subprocess** |
| **TDK-Lambda hybrid LV power** | **TestBenchCERN** | **external scripts, invoked from the GUI** |
| `vmm_qa/vmm_pcapng_qa.py` | **P2_basket_online_analysis** | **ported into this repo, ROOT stripped** |
| capture loop | bench `loop_daq` → **replaced** | dumpcap ring buffer; old loop kept as fallback |

Detail on the three VMM-side items:

* **ALINX / front-end arming — entirely the bench code.** `vmm_daq_control.py`
  (`_run_alinx_sc`, `alinx_acq_on`, `alinx_acq_off`) shells out to
  `alinx-sc --config-file <cfg> --read-link-status | --acq-on | --acq-off`
  around every sub-run, with retries; a failed `acq-on` aborts the sub-run
  rather than record nothing. The config handed to it is TestBenchCERN's own
  `config/config_alinx_noThresholds.json` (bench path hard-coded in
  `SITES['bench']`), and `copy_provenance()` copies it into each sub-run's raw
  directory. Nothing about VMM configuration was reimplemented here.
* **Hybrid LV power** — `switchOn_tdk.sh` / `switchOff_tdk.sh` / `pilot_tdkl.py`
  from the bench, run with `cwd` set to the TestBenchCERN checkout, wired up
  through the gitignored `config/power_config.json` (see the `.example`).
* **Online QA** — the one place old VMM code was genuinely taken in and
  rewritten: `vmm_qa/vmm_pcapng_qa.py` is an adaptation of
  `P2_basket_online_analysis/vmm_hybrid_pcapng_monitoring.py` with the PyROOT
  re-exec and ROOT histogram/TTree output removed, so it runs in the DAQ venv
  on scapy/numpy/pandas/matplotlib alone.

**What was deliberately *not* reused:** the bench's `loop_daq` capture loop
(`tcpdump -G -W 1` in a loop) was replaced by `dumpcap -b duration:<N>`, for the
rotation guarantee that online QA depends on (§5.4). The old approach survives
as the `TcpdumpLoopCapture` fallback class, and `common_functions.parse_pcapng_name()`
still parses `loop_daq`-style filenames so bench files remain readable.

Also replaced from the Dream side: the `RunCtrl` driver → the dumpcap capture
server; the ROOT conversion + pedestal/processor chain → **nothing at all** (QA
runs directly on pcapng). Added: Aim-TTi LV monitoring, mirroring the HV wiring.

**Two consequences worth discussing:**

1. `alinx-sc` is an **unpinned external dependency**. It must be on the `PATH`
   of the shell that runs `start_servers.sh` (TODO_SPS item 11), and nothing
   here records its version — only its config file gets provenance-copied. If
   the bench tool changes, this repo cannot notice.
2. The alinx config path is **hard-coded per site**, and the SPS entry is still
   a `TODO-SPS` placeholder (`/local/p2/vmm_config/config_alinx_noThresholds.json`).
   Someone has to locate the real file on the SPS machine before the first run.

---

## 2. The five design principles

Almost every design decision in the repo follows from one of these. They are
worth stating explicitly in the discussion, because they explain a lot of code
that otherwise looks over-engineered.

### P1 — One process per subsystem, each owning exactly one resource

The CAEN crate, the TTi supplies and the network interface are each **owned by a
single long-lived process**. Nothing else is allowed to touch them. That is why
HV/LV/DAQ are servers rather than library calls inside the orchestrator: a device
handle is opened once, and concurrency questions ("who is talking to the crate
right now?") have a trivial answer.

Consequence: the orchestrator (`daq_control.py`) is a *client of three servers*
and holds no hardware state itself. It can be killed and restarted without
disturbing the connections to hardware.

### P2 — Talk over sockets, coordinate over the filesystem

Two distinct communication channels, used for two distinct purposes:

* **TCP, length-prefixed JSON/text** (`Server.py` / `Client.py`) for the
  *in-run command protocol* — strictly request/response, strictly ordered.
* **Flag files and state JSONs** for *asynchronous, out-of-band events* — Stop
  Run, Stop Sub-Run, Pause, "capture finished", "QA already did this file".

The second is deliberate. The GUI runs as a separate process (possibly a
separate user session) and must be able to interrupt a run without being part of
the socket conversation. Racing a Ctrl-C into a tmux pane is not deterministic;
`touch .stop_run` and letting the loop notice it at a well-defined point is.
Every stop path in the system is a file the loop polls, never a signal.

### P3 — Configuration is a Python file, but a run is frozen JSON

`run_config_beam.py` is executable configuration: the run plan is *computed*
(loops over voltages, sub-run lists built programmatically), and machine-specific
values live in a `SITES` dict selected by the untracked `config/site.txt`.

But a *run* never reads that file. Pressing Start serializes the config to
`config/json_run_configs/run_config_beam.json`, hands the path to
`daq_control.py`, and also copies it into the run directory as
`run_config.json`. So:

* editing the .py mid-run cannot corrupt a running run,
* every dataset carries the exact conditions it was taken under,
* `RunConfigBase` (load/write/to_dict/from_dict) is the whole mechanism — the
  config object *is* its `__dict__`.

Site selection through an untracked file (not an edited constant) means a DAQ
machine never has local modifications, which is what makes the hard-reset deploy
model safe.

### P4 — Everything must be restartable, nothing may block the DAQ

Each server sits in a `while True:` around its whole session: on any unhandled
exception it logs, sends the client an unblocking reply, and re-listens. The LV
monitor keeps writing rows with empty cells when a PSU drops off the network
rather than raising. The QA watcher is entirely independent of the run — it can
be started, stopped, reset, and it re-derives what still needs doing from the
filesystem. A killed QA job is simply not marked done and comes back next poll.

The ordering rule underneath: **the capture is the only thing that matters**.
Monitoring, QA, backup, and GUI are all downstream and are all expendable
individually. Hence QA runs at `nice 19` + `ionice idle` + optional CPU
affinity, and `mem_guardian.py` will kill a runaway QA job (allow-list) but is
explicitly forbidden (veto-list) from touching `vmm_daq_control`, `dumpcap`,
`hv_control`, `lv_control`, `daq_control` or Flask.

### P5 — The filesystem layout *is* the data model

There is no database and no message queue. Directory structure and marker files
carry the state:

```
runs/<run>/run_config.json                                     frozen conditions
runs/<run>/<subrun>/.subrun_complete                           completed cleanly
runs/<run>/<subrun>/raw_daq_data/<iface>_<seq>_<ts>.pcapng     the data
runs/<run>/<subrun>/raw_daq_data/.capture_done                 capture ended
runs/<run>/<subrun>/{hv,lv}_monitor.csv                        slow control
analysis/<run>/<subrun>/<pcap_base>/*.png + events.json        QA output
```

Resume, "what is left to analyze", "what is safe to delete", and the GUI's hit
counter are all *queries against this tree*. Any component can be restarted and
recover its state by looking at disk.

---

## 3. Process map

Started by `./start_servers.sh`, one tmux session each (scrollback capped per
session — a chatty pane is a memory leak on an 8 GB box):

| tmux session | program | port | owns |
|---|---|---|---|
| `vmm_hv_control` | `hv_control.py` | 2100 | CAEN mainframe |
| `vmm_lv_control` | `lv_control.py` | 2102 | Aim-TTi PSUs (SCPI/TCP 9221) |
| `vmm_daq` | `vmm_daq_control.py` | 2101 | capture NICs + ALINX slow control |
| `vmm_daq_control` | interactive shell | — | runs `daq_control.py <config.json>` |
| `vmm_flask` | `flask_app/` | 5002 | the GUI |
| `vmm_mem_guardian` | `mem_guardian.py` | — | RAM backstop |
| `vmm_qa_watcher` | `qa_watcher.py` | — | online QA (started from GUI) |
| `vmm_backup_watcher` | `backup_watcher.py` | — | EOS transfer (started from GUI) |
| `vmm_beam_watcher` | `beam_watcher.py` | — | NXCALS beam intensity (own venv) |

Three "tiers", and they are only loosely coupled:

* **hardware servers** (2100/2101/2102) — persistent, hardware-owning, dumb;
  they do what the current client tells them.
* **the orchestrator** — transient, exists only for the duration of a run.
* **watchers + GUI** — permanently running, entirely filesystem-driven, never
  in the run's critical path.

The GUI is *not* a controller. It only starts scripts, touches flag files, and
reads the filesystem/tmux panes. If Flask dies mid-run, the run continues.

---

## 4. The wire protocol

`Server.py`/`Client.py` are ~100 lines each and unchanged from the Dream stack.
Frames are `[4-byte big-endian length][payload]`, payload either UTF-8 text or
JSON. `_recv_exactly()` handles short reads — the only real subtlety.

The conversation is a fixed handshake plus a per-sub-run exchange. For the DAQ
server:

```
daq_control                          vmm_daq_control (2101)
  ── "Connected to daq_control" ──▶
  ◀──── "VMM DAQ control connected"      (client waits — this is the sync point)
  ── vmm_daq_info JSON ───────────▶      creates run dir, opens run log

  per sub-run:
  ── "Start" + subrun JSON ───────▶      alinx acq-on, spawn dumpcap per iface
  ◀──── "VMM DAQ starting"               ← DAQController now blocks
        ... run_time minutes ...
  ◀──── "VMM DAQ stopped"                after acq-off + .capture_done written

  ── "Finished" ──────────────────▶      end of run
```

HV and LV speak the same shape with their own verbs (`Begin Monitoring` /
`Start` / `End Monitoring` / `Power Off` / `Check` / `Finished`), each reply
being a token the orchestrator matches on (`'HV Set' in res`). Two points worth
raising in the discussion:

* it is **strictly synchronous** — every `send` has a matching `receive`, and a
  missing one desynchronizes the stream. This is why the code has bare
  `hv.receive()` calls with only a comment for documentation. It is the most
  fragile part of the design and the place to be careful when adding a verb.
* the servers accept **one client at a time**. That is the concurrency model:
  the socket *is* the lock on the hardware.

---

## 5. Information flow, end to end

### 5.1 Starting a run (GUI → disk)

1. Shifter presses **Start Run**. Flask calls `iterate_run_num.py`, which is
   resume-aware: if `resume=True` and sub-runs are still missing their
   `.subrun_complete` markers it keeps the run name; otherwise it rewrites the
   single uncommented `self.run_name = '...'` line in `run_config_beam.py` to the
   next free `_<n>`.
2. Flask executes `run_config_beam.py`, which writes
   `config/json_run_configs/run_config_beam.json` (P3).
3. `bash_scripts/start_run.sh` types the command into the `vmm_daq_control` tmux
   pane: `.venv/bin/python daq_control.py <config.json>`. (Absolute venv path —
   an interactive shell's rc may put a pyenv/conda python first on `PATH`; that
   bit us on the Saclay bench.)
4. `daq_control.py` connects to HV, LV and DAQ, ships each its config block,
   creates the run directory and copies `run_config.json` into it.

### 5.2 The sub-run loop (`daq_control.py`)

For each entry in `config.sub_runs`:

```
check .stop_run          → break out of the run
check .pause_run         → block here until Resume (HV stays at setpoint)
check .subrun_complete   → skip if resuming
optional LV gate         → "Check"; skip the sub-run if out of tolerance
print [status] line      → so the GUI card updates before the ramp, not after
HV/LV "Begin Monitoring" → each server spawns a thread writing its CSV
HV "Start" + subrun      → ramp; wait for "HV Set"; optional settle_time
DAQController.run()      → "Start"; block until "VMM DAQ stopped"
HV/LV "End Monitoring"
mark .subrun_complete    → ONLY if no manual stop was requested
10 s + optional post_pause_s
```

The asymmetry at the end is deliberate: a manually stopped sub-run is left
*unmarked*, so a later `resume=True` re-runs it. A cut-short sub-run is not a
sub-run.

`DAQController` is thin on purpose — send Start, block on receive, write
`run_time.txt`, and on `KeyboardInterrupt` drop the `.stop_vmm` flag instead of
signalling anything.

### 5.3 Capture (`vmm_daq_control.py`)

Per sub-run: `alinx-sc --read-link-status` then `--acq-on` (with retries; a
failure aborts the sub-run rather than recording nothing), then one capture
handle per configured interface. Three interchangeable handle classes with the
same `alive()/returncode()/stop()` interface:

* **`DumpcapCapture`** (default) — `dumpcap -b duration:<N>`, a ring buffer that
  rotates a new `.pcapng` every `capture_duration_s` (44 s). `stop()` sends
  SIGINT so dumpcap *finalizes* the in-progress file, escalating to
  TERM/KILL only on timeout.
* **`TcpdumpLoopCapture`** — fallback, one `tcpdump -G -W 1` per file in a loop.
* **`SimulatedCapture`** — replays a sample pcapng from `sim_pcapng/` for the
  `local` site, so the full chain is testable with no hardware.

A daemon thread prints a status line every 10 s:

```
[vmm daq] status subrun=<name> elapsed=0h 1m 30s files=3 mb=61.2 file=<newest>
```

This line is the *interface to the GUI* — `flask_app/daq_status.py` regexes it
out of the tmux pane. The run loop also ends the sub-run early if all capture
processes die. (`vmm_daq_info['max_run_time_addition']` is declared in the config
but currently read by nothing — there is no watchdog on an overrunning capture.)

At the end: `acq-off`, write `.capture_done`, copy the alinx config into the raw
directory for provenance, log file count and MB.

### 5.4 Rotation is what makes online QA possible

The single most important number in the system is `CAPTURE_DURATION_S`. Because
dumpcap rotates, the run produces a stream of **closed, complete, independent
files** instead of one growing file. That is what lets QA run at full-run rate
with a bounded latency and no coordination with the DAQ. It is also why QA is
strictly per-file with no cross-file accumulation.

The cost is the *finalization* question: when is a file safe to read?
`qa_watcher._finalized_pcapngs()` declares a file final when **any** of:

* a higher-sequence file for the same interface exists (dumpcap rotated past it), or
* `.capture_done` exists in the directory (sub-run ended), or
* its mtime is older than `2 × capture_duration_s` (the DAQ died — don't lose the file).

...plus one extra guard: the size must be unchanged for a full poll interval.
Belt and braces, because reading a half-written pcapng produces a plausible-
looking wrong answer rather than an error.

### 5.5 Online QA (`qa_watcher.py` → `vmm_qa/vmm_pcapng_qa.py`)

The watcher polls the whole `runs/` tree (not just the current run), skipping
stale runs (no new files for `stale_run_days`) and honouring include/exclude
lists. For each finalized, not-yet-done file it launches:

```
taskset -c <cores> nice -n 19 ionice -c idle \
  .venv/bin/python vmm_qa/vmm_pcapng_qa.py <file.pcapng> \
  --out-dir analysis/<run>/<subrun>/<pcap_base>/ --events-json [--format TRG] [--calibration ...]
```

and monitors system memory while it runs, killing it above `memory_kill_pct`
(the file stays unmarked → retried later). Done-files are persisted in
`config/qa_state.json`, so a watcher restart does not reprocess the run; the GUI
"Rerun QA" drops a reset signal that removes entries for selected runs.

The QA script itself parses the SRS/VMM3a data words out of the UDP payload with
scapy+numpy — FEC/VMM/channel/ADC, Gray-decoded BCID, TDC, offset, and a
reconstructed `abs_time_ns` relative to the SRS marker timestamp — and emits
per-VMM ADC and occupancy PNGs plus `events.json` (`n_hits`, `hits_per_vmm`).

**Note for the discussion:** "events" is a misnomer we inherited. The SRS stream
is self-triggered, there is no event building — the GUI counter is a *hit* sum.
`get_run_events.py` walks `analysis/<run>/*/*/events.json` and adds up `n_hits`.
It therefore trails the DAQ by ~one rotation plus QA time, by construction.

### 5.6 Slow control logging

`hv_control.py` and `lv_control.py` are structurally identical: a monitor thread
started by `Begin Monitoring` writes one CSV row per interval into the sub-run
directory (`hv_monitor.csv` at 1 s, `lv_monitor.csv` at 2 s) and stops on
`End Monitoring`. The GUI's `/hv_data` and `/lv_data` endpoints read those CSVs
directly and plot them — there is no in-memory history anywhere, so plots
survive any restart and old runs are browsable with the same code path.

LV's disconnect behaviour is worth flagging: on a socket error it writes empty
cells (a visible gap in the plot) and retries every `reconnect_interval`. LV
never blocks the DAQ. The optional `check_before_subrun` gate is the only place
LV can influence the run, and it is off by default.

### 5.7 The GUI (`flask_app/`)

Roughly 1400 lines of routes over a single-page Bootstrap/Plotly front end.
Its information sources, in order of weirdness:

* **tmux pane scraping** (`daq_status.py`) — `tmux capture-pane -pS -500` per
  session, regex out the last status line, reduce to
  `{status, color, fields}` for the cards. Ugly, but it means the servers need
  no status API and printing to stdout is the whole contract.
* **the filesystem** — runs, sub-runs, CSVs, QA PNGs, `events.json`.
* **state JSONs** in `config/` — current run, beam state, QA state.
* **outbound HTTPS** — CERN Vistar PNGs, Telegram/WhatsApp alerts.

Control actions are: run a script (`start_run.sh`, `git_reset.sh`,
`restart_*_tmux_processes.sh`, the site's power scripts), or touch a flag file
(`.stop_run`, `.stop_subrun`, `.pause_run`). Nothing else.

The beam monitor deserves a mention because it is unusual: SPS Page 1 is
published only as an image, so `beam_state.py` reads the target intensity table
out of the PNG by **exact bitmap-glyph matching** (`beam_glyphs.json`). The
Vistar font is un-antialiased, so a glyph either matches exactly or the parse
fails loudly — there is no fuzzy path that could misread an intensity. Beam ON
is `intensity ≥ threshold` on the tracked target, debounced over 2 samples at
the CERN-requested 7 s poll; transitions are appended to `logs/beam_history.csv`
and state is persisted so an off-period survives a Flask restart. Separately,
`beam_watcher.py` pulls the real NXCALS intensity trend — it lives in its own
process and its own venv because pytimber drags in a ~1 GB local Spark session
that must never share memory with the DAQ.

`monitor.py` turns all of this into alerts: rules are just methods named
`rule_<name>(self) -> (bool, str)`, so adding an alert is adding a method, and
disabling one is a key in `monitor_config.json`.

### 5.8 Getting data out, and staying alive

* `backup_watcher.py` — per-sub-run sync to EOS over **native xrootd**
  (`xrdcp`/`xrdfs`, not the FUSE mount, which cannot mkdir/rename), skipping
  files already present at matching size (data is write-once), with a slow daily
  full reconcile that re-checks every run — that is what propagates
  after-the-fact edits like a rewritten `run_config.json`. Kerberos via
  `kinit -R` with a GPG-encrypted password as fallback.
* `bash_scripts/local_mirror.sh` — rsync of the data tree to an external disk.
* `space_manager.py` — the Disk Space tab. A run is deletable **only** if every
  file in its tree is on EOS at matching size, *and* it is not the current run,
  *and* it is not the newest run on disk, *and* every sub-run has its
  `.subrun_complete` marker. Four independent guards, because the failure mode
  is unrecoverable.
* `mem_guardian.py` + `bash_scripts/setup_oom_protection.sh` (earlyoom +
  swappiness + oom_score bias on the QA) — the 8 GB machine's survival kit
  (see P4).

---

## 6. Operational model (worth 5 minutes in the discussion)

* **The GUI has no authentication.** Access control *is* the SSH tunnel
  (`ssh -f -N -L 15002:localhost:5002 <daq-host>`). Never ask for port 5002 to
  be opened.
* **The DAQ checkout is a deploy target, never a workspace.** Develop and commit
  elsewhere, push, then Git Reset (`git reset --hard origin && git pull`) +
  Restart All. The hard reset means any uncommitted edit on the DAQ machine is
  wiped at the next deploy — by design, so the button always produces a known
  state mid-shift. Runtime state (`config/*_state.json`, credentials, `logs/`)
  is gitignored and never touched.
* **One account owns the stack** (tmux sockets are per-user, so a stack started
  by one user is invisible to others). Shifters need only a login for the
  tunnel and then drive everything from the browser; the event log records the
  client IP of every Start/Stop, so actions stay attributable under a shared
  service account.

---

## 7. Where to look first

| Question | File |
|---|---|
| What does a run *do*? | `daq_control.py` (~250 lines, read top to bottom) |
| What are the conditions? | `run_config_beam.py` (`SITES`, then `Config._set_defaults`) |
| How is data written? | `vmm_daq_control.py` (`start_captures`, `DumpcapCapture`) |
| How is data read? | `vmm_qa/vmm_pcapng_qa.py` (docstring lists every hit column) |
| When is a file ready? | `qa_watcher.py::_finalized_pcapngs` |
| How does the GUI know anything? | `flask_app/daq_status.py` |
| How does anything stop? | grep for `.stop_run`, `.stop_subrun`, `.stop_vmm`, `.pause_run` |
| Which parts are bench code vs ported? | §1.1, and `vmm_daq_control.py::_run_alinx_sc` |

---

## 8. Known weak points — good discussion material

1. **The synchronous protocol has no timeouts on most receives.** A server that
   dies between "Start" and its reply leaves the orchestrator blocked; the DAQ
   server's exception handler sends a fake "VMM DAQ stopped" specifically to
   unblock it, which is a workaround, not a fix.
2. **tmux-scraping for status** couples the GUI to print statements. It works
   and it is debuggable by eye, but changing a status line format silently
   breaks a card.
3. **`iterate_run_num.py` rewrites the tracked config file.** Run numbering lives
   in git-tracked source, which interacts awkwardly with the hard-reset deploy
   model.
4. **QA is per-file only.** Nothing accumulates across a sub-run, so anything
   that needs sub-run-level statistics has to be built on top of `events.json`
   after the fact.
5. **No event building / no trigger correlation** in the online path — by design
   for now, but it is the obvious next layer.
6. **`alinx-sc` is an unpinned external dependency** owned by another repo
   (§1.1). It is the only path to the front-ends, it is not versioned here, and
   its config path is hard-coded per site — still a placeholder for SPS.
7. **The 8 GB machine is the real constraint** behind mem_guardian, the nice/
   ionice/affinity throttling, and the tmux scrollback caps. A bigger box would
   delete a fair amount of this code.
