#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run configuration for the P2 VMM DAQ at the SPS beam test.

Clone of the Dream beam configuration (DAQ_Control_Dream_Beam) for the VMM
readout: raw UDP from the front-ends is captured into rotating .pcapng files
per network interface (dumpcap), with optional ALINX slow control (alinx-sc)
around each sub-run. No fdf/ROOT reconstruction — online QA runs directly on
the finalized pcapng files (vmm_qa/vmm_pcapng_qa.py).

Site switching: set SITE below.
  'local' — full simulation on this machine (fake CAEN HV + fake TTi LV + fake
            VMM DAQ that replays a sample pcapng), for testing the whole chain
            without hardware.
  'sps'   — real hardware at the SPS beam line. Fields marked TODO-SPS must be
            filled in once the beam-area network / crate details are known.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's nTof config)
"""

import os

from run_config_base import RunConfigBase

# ---------------------------------------------------------------------------
# Site configuration — the ONE place to switch local test <-> SPS machine
# ---------------------------------------------------------------------------
SITE = 'local'  # 'local' or 'sps'

SITES = {
    'local': {
        # All data under a local test tree (runs/, analysis/, sim_pcapng/, ...)
        'base_data_dir': '/local/home/ak271430/Documents/PostDocSaclay/data/sps_vmm_test/',
        'daq_host': '127.0.0.1',    # hv_control / vmm_daq / lv_control servers
        'hv_ip': 'sim',             # 'sim' -> hv_control uses FakeCAENHVController
        'hv_n_cards': 4,
        'lv_units': {               # name -> ip; 'sim' -> lv_control uses FakeTTiPSU
            'tti1': 'sim',
            'tti2': 'sim',
        },
        'simulate': True,           # fake HV + fake LV + fake VMM DAQ (replay sample pcapng)
        # Interfaces to capture on. In simulation the iface name is only used
        # for file naming; alinx_config=None skips alinx-sc entirely.
        'interfaces': [
            {'name': 'alinx', 'iface': 'enp4s0f1', 'slow_control': 'alinx',
             'alinx_config': None},
        ],
    },
    'sps': {
        'base_data_dir': '/local/p2/p2data/sps_vmm_2026/',  # TODO-SPS: data disk on VMM DAQ machine
        'daq_host': '127.0.0.1',                            # TODO-SPS: DAQ computer IP (as seen by clients)
        'hv_ip': '192.168.10.81',                           # TODO-SPS: CAEN mainframe IP at SPS
        'hv_n_cards': 4,                                    # TODO-SPS: number of cards in SPS crate
        'lv_units': {
            'tti1': '192.168.0.241',                        # TODO-SPS: Aim-TTi unit 1 IP
            'tti2': '192.168.0.242',                        # TODO-SPS: Aim-TTi unit 2 IP
        },
        'simulate': False,
        'interfaces': [
            {'name': 'alinx', 'iface': 'enp4s0f1', 'slow_control': 'alinx',
             # TODO-SPS: path to config_alinx_noThresholds.json on the DAQ machine
             'alinx_config': '/local/p2/vmm_config/config_alinx_noThresholds.json'},
            # SRS chain — uncomment to also capture the SRS/FEC interface:
            # {'name': 'srs', 'iface': 'enx0c37968d3d99', 'slow_control': 'none',
            #  'alinx_config': None},
        ],
    },
}

_SITE_CFG = SITES[SITE]
BASE_DATA_DIR = _SITE_CFG['base_data_dir']
SIMULATE = _SITE_CFG['simulate']

# ---------------------------------------------------------------------------
# Run schedule
#   N_SUBRUNS identical sub-runs of SUBRUN_MIN minutes at the nominal P2
#   operating point. Short values for local simulation; set beam values at SPS.
# ---------------------------------------------------------------------------
N_SUBRUNS = 2       # number of identical sub-runs
SUBRUN_MIN = 2      # run time per sub-run (minutes)
POST_SUBRUN_PAUSE_MIN = 0   # optional pause AFTER each sub-run (minutes); 0 = no pause

# Nominal P2 operating point (cosmic bench long-run values, Ar/Iso 95/5):
MESH_V = 440    # V, P2 mesh
DRIFT_V = 600   # V, P2 drift (drift gap = drift - mesh = 160 V)

# P2 HV channels: (card, channel). TODO-SPS: update once the SPS crate is cabled.
P2_HV = {
    'mesh': (1, 0),
    'drift': (1, 1),
}

# Capture: seconds per pcapng file (dumpcap ring-buffer rotation interval).
CAPTURE_DURATION_S = 44


class Config(RunConfigBase):
    def __init__(self, config_path=None):
        if not config_path:
            self._set_defaults()

        super().__init__(config_path)

    def _set_defaults(self, config_path=None):
        self.run_name = 'run_2'
        self.base_out_dir = BASE_DATA_DIR
        self.data_out_dir = f'{self.base_out_dir}runs/'
        self.run_out_dir = f'{self.data_out_dir}{self.run_name}/'
        self.raw_daq_inner_dir = 'raw_daq_data'
        self.detector_info_dir = f'{self.base_out_dir}config/detectors/'
        self.start_time = None
        self.power_off_hv_at_end = False  # True to power off all CAEN HV at the end of the run.
        self.resume = False  # True to resume an existing run: skip sub-runs already marked .subrun_complete.
        self.write_all_detectors_to_json = True  # Only when making run config json template. Maybe do always?
        self.gas = 'Ar/Iso 95/5'  # Gas type for run
        # self.gas = 'Ar/CO2/Iso 93/5/2'
        self.beam_type = 'sps_beam'
        # self.beam_type = 'cosmics'
        self.target_type = 'none'
        self.trigger = 'self-triggered VMM readout'  # TODO-SPS: describe actual trigger formation

        self.vmm_daq_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2101,
            'interfaces': _SITE_CFG['interfaces'],
            'capture_duration_s': CAPTURE_DURATION_S,  # seconds per pcapng file
            'capture_tool': 'dumpcap',  # 'dumpcap' (ring buffer) or 'tcpdump' (loop, one file per invocation)
            'snaplen': 0,               # 0 = full packets
            'bpf_filter': 'udp',        # capture filter (VMM data is UDP)
            'data_out_dir': f'{self.run_out_dir}',
            'raw_daq_inner_dir': self.raw_daq_inner_dir,
            'status_interval_s': 10,    # seconds between [vmm daq] status lines
            'acq_on_retries': 3,        # alinx-sc --acq-on retries before giving up
            'max_run_time_addition': 60 * 5,  # seconds past requested run time before force stop
            # --- Simulation (SITE='local' only): instead of launching dumpcap,
            # replay a sample pcapng from sim_source_pcap_dir into the run directory.
            'simulate': SIMULATE,
            'sim_source_pcap_dir': f'{self.base_out_dir}sim_pcapng/',
            'sim_chunk_mb': 4,            # MB appended to the growing pcapng per step
            'sim_chunk_interval': 2,      # seconds between append steps
            'sim_bytes_per_file_mb': 20,  # target size of each simulated pcapng file
            'sim_file_duration_s': 30,    # seconds each simulated file takes to 'record'
        }

        self.hv_control_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2100,
        }

        self.hv_info = {
            'ip': _SITE_CFG['hv_ip'],
            'n_cards': _SITE_CFG['hv_n_cards'],
            'n_channels_per_card': 12,
            'run_out_dir': self.run_out_dir,
            'hv_monitoring': True,  # True to monitor HV during run, False to not monitor
            'monitor_interval': 1,  # Seconds between HV monitoring
            'simulate': SIMULATE,   # True -> hv_control uses FakeCAENHVController
        }

        # HV credentials: hv_creds.txt (username on line 1, password on line 2) next
        # to this file. Optional in simulation; required for the real CAEN crate.
        creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hv_creds.txt')
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                lines = f.readlines()
                self.hv_info['username'] = lines[0].strip()
                self.hv_info['password'] = lines[1].strip()
        else:
            self.hv_info['username'] = 'admin'
            self.hv_info['password'] = 'admin'
            if not SIMULATE:
                print(f'WARNING: {creds_path} not found — using default admin/admin HV credentials.')

        self.lv_control_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2102,
        }

        self.lv_info = {
            # Aim-TTi bench PSUs, SCPI over raw TCP (port 9221), up to 3 channels each.
            'units': {
                name: {'ip': ip, 'port': 9221, 'channels': [1, 2, 3]}
                for name, ip in _SITE_CFG['lv_units'].items()
            },
            'run_out_dir': self.run_out_dir,
            'lv_monitoring': True,      # True to monitor LV during run, False to not monitor
            'monitor_interval': 2,      # seconds between LV monitoring rows
            'also_write_tti_logs': False,  # True: additionally write legacy tti*_mon.log files
            'reconnect_interval': 5,    # seconds between reconnect attempts after a socket error
            'simulate': SIMULATE,       # True -> lv_control uses FakeTTiPSU
            # Optional pre-sub-run LV gate: check voltages against 'expected' below
            # and skip the sub-run if out of tolerance. Off by default.
            'check_before_subrun': False,
            'expected': {
                # 'tti1': {1: {'v': 2.5, 'v_tol': 0.2}},
            },
        }

        # ----- Run schedule (built from module constants above) -----
        self.sub_runs = []
        mesh_card, mesh_ch = P2_HV['mesh']
        drift_card, drift_ch = P2_HV['drift']
        for i in range(N_SUBRUNS):
            hvs = {}
            hvs.setdefault(str(mesh_card), {})[str(mesh_ch)] = MESH_V
            hvs.setdefault(str(drift_card), {})[str(drift_ch)] = DRIFT_V
            self.sub_runs.append({
                'sub_run_name': f'mesh_{MESH_V}V_drift_{DRIFT_V}V_{i:02d}',
                'run_time': SUBRUN_MIN,  # Minutes
                'post_pause_s': int(round(POST_SUBRUN_PAUSE_MIN * 60)),  # pause after this sub-run (seconds)
                'hvs': hvs,
            })

        # --- HV scan template (uncomment and adapt at the beam) ---
        # for mesh_v in range(430, 465, 5):
        #     self.sub_runs.append({
        #         'sub_run_name': f'mesh_{mesh_v}V_drift_{mesh_v + 160}V',
        #         'run_time': 20,
        #         'hvs': {str(mesh_card): {str(mesh_ch): mesh_v},
        #                 str(drift_card): {str(drift_ch): mesh_v + 160}},
        #     })

        self.included_detectors = ['P2_1']

        self.detectors = [
            {
                'name': 'P2_1',
                'description': 'Bulked at 11-6-26 with footprint on the mesh from the frame gluing',
                'det_type': 'P2',
                'resist_type': 'none',
                'bulked_from': 'Alex+Arnaud',
                'det_center_coords': {  # Center of detector. TODO-SPS: beam-line survey coordinates
                    'x': 0,  # mm
                    'y': 0,  # mm
                    'z': 0,  # mm
                },
                'det_orientation': {
                    'x': 0,  # deg  Rotation about x axis
                    'y': 0,  # deg  Rotation about y axis
                    'z': 0,  # deg  Rotation about z axis
                },
                'hv_channels': {
                    'mesh': P2_HV['mesh'],
                    'drift': P2_HV['drift'],
                },
                # VMM readout cabling (informational; used to label QA plots).
                # iface -> hybrid/VMM ids seen in the data. TODO-SPS: fill actual map.
                'vmm_map': {
                    'enp4s0f1': {'hybrids': 'alinx', 'vmms': list(range(16))},
                },
            },
        ]

        if not self.write_all_detectors_to_json:
            self.detectors = [det for det in self.detectors if det['name'] in self.included_detectors]


if __name__ == '__main__':
    out_run_dir = 'config/json_run_configs/'
    os.makedirs(out_run_dir, exist_ok=True)

    config_name = 'run_config_beam.json'

    config = Config()

    config.write_to_file(f'{out_run_dir}{config_name}')

    # Schedule summary — sanity-check timing and the HV setpoints.
    run_min = sum(sr['run_time'] for sr in config.sub_runs)
    n_sub = len(config.sub_runs)
    total_h = run_min / 60
    ifaces = [f"{i['name']}({i['iface']})" for i in config.vmm_daq_info['interfaces']]
    print(f'Site: {SITE}  (simulate={SIMULATE})')
    print(f'Base data dir: {BASE_DATA_DIR}')
    print(f'Gas: {config.gas}')
    print(f'P2 mesh: {MESH_V} V   drift: {DRIFT_V} V   (gap = {DRIFT_V - MESH_V} V)')
    print(f'Capture: {", ".join(ifaces)}  ({config.vmm_daq_info["capture_tool"]}, '
          f'{config.vmm_daq_info["capture_duration_s"]} s/file)')
    print(f'LV units: {", ".join(config.lv_info["units"].keys())}')
    print(f'Sub-runs: {n_sub} x {SUBRUN_MIN} min = {run_min} min (~{total_h:.2f} h + overhead)')

    print('donzo')
