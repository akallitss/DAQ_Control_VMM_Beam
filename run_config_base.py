#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on January 21 1:17 PM 2026
Created in PyCharm
Created as nTof_x17_DAQ/run_config_base.py

@author: Dylan Neff, dylan
"""

import json
import copy

class RunConfigBase:

    def __init__(self, config_path=None):
        if config_path:
            self.load_from_file(config_path)

    def write_to_file(self, file_path):
        with open(file_path, "w") as f:
            json.dump(
                self.to_dict(),
                f,
                indent=4
            )

    def load_from_file(self, file_path):
        with open(file_path, "r") as f:
            data = json.load(f)

        self.from_dict(data)
        self.post_load()

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    def from_dict(self, data):
        self.__dict__.clear()
        self.__dict__.update(data)

    def post_load(self):
        """Hook for derived classes"""
        pass

