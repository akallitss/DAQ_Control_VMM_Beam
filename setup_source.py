#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The stand's setup -- detectors and their positions, readout cabling, HV channel
map and operating point -- from ONE file: a p2-mapping setup (YAML).

Everything that needs the setup reads it from here: run_config_beam.py (the
run configs, HV targets, cabling written into run_config.json), and through
p2_readout_config.P2_SETUP the processor, QA and live monitor. Before this,
run_config_beam.py held its own copy of the cabling and HV, kept in step with
the p2-mapping file by hand.

Which file: SITE_SETUPS names the setup per site. A machine keeps its own,
editable copy in SETUPS_DIR (on the DAQ box: /local/p2/p2-setups, a git
repository, so every change has an author, a date and a reason); without one
the copy shipped with p2-mapping is used.

Cabling is dated: cabling_at(when) gives the entry in force at a time, so a
run started now gets today's cabling and old runs keep theirs.

@author: Alexandra Kallitsopoulou
"""

import os

SETUPS_DIR = os.environ.get('P2_SETUPS_LOCAL', '/local/p2/p2-setups')

# setup per DAQ site. 'local' and 'bench' have no stand of their own: they ran
# with the beam test's cabling and HV, i.e. sps_h4_2026.
SITE_SETUPS = {
    'lab': 'lab_saclay_2026',
    'sps': 'sps_h4_2026',
    'local': 'sps_h4_2026',
    'bench': 'sps_h4_2026',
}


def setup_ref(site):
    """Path of this machine's copy of the site's setup, else its name; None if none."""
    name = SITE_SETUPS.get(site)
    if not name:
        return None
    p = os.path.join(SETUPS_DIR, f'{name}.yaml')
    return p if os.path.isfile(p) else name


def load(site):
    """The p2_mapping Setup for this site (validated on load)."""
    ref = setup_ref(site)
    if ref is None:
        raise SystemExit(f'no setup for site {site!r}: add it to setup_source.SITE_SETUPS')
    try:
        from p2_mapping.setups import load_setup
    except ImportError as e:
        raise SystemExit(f'p2-mapping is not installed in this Python ({e}); the run '
                         'configuration needs it to read the setup file') from e
    return load_setup(ref)
