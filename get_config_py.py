#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on November 13 09:17 2025
Created in PyCharm
Created as Cosmic_Bench_DAQ_Control/get_config_py

@author: Dylan Neff, dn277127
"""

import json
import runpy

def main():
    # load run_config_beam.py AS A SCRIPT, not a module
    config_namespace = runpy.run_path("run_config_beam.py")

    # the file now acts like a dict namespace
    Config = config_namespace["Config"]

    config = Config()
    run_name = config.run_name

    print(json.dumps({
        "run_name": run_name,
    }))

if __name__ == "__main__":
    main()
