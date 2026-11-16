#!/local/p2/deploy/bin/p2basket-python
# Use the p2basket python VM ^^^ for hassle-free operation
"""
Test script for p2basket Python bindings
"""
import os
import p2basket
import ipaddress

def get_interface_name_by_ip(target_ip):
    # Retrieve all network adapters from the OS
    adapters = ifaddr.get_adapters()
    
    for adapter in adapters:
        for ip_info in adapter.ips:
            # Check if the IP matches (handles both string format and IPv6 tuples)
            if ip_info.ip == target_ip:
                # nice_name returns the human-readable name (e.g., 'Wi-Fi' or 'eth0')
                return adapter.nice_name
    return None
# end of def get_interface_name_by_ip(target_ip):

# Verbosity of the script
# sure there are smarter ways to do this
verbose = 0

# Set indentation parameters
class Indent:
    def __init__(self, wid, prefix):
        self.wid = wid
        self.prefix = prefix
        self.level = 0

    def incr(self):
        self.level += 1
        return self.level

    def decr(self):
        self.level = max(0, self.level - 1)
        return self.level

    @property
    def blank(self):
        return " " * self.wid * self.level

    @property
    def prompt(self):
        self.incr()
        prompt_str = (self.prefix * self.level * self.wid)[:self.level * self.wid - 1] + " "
        return prompt_str

    def incr_blank(self):
        self.incr()
        return self.blank

    def empty_decr(self):
        self.decr()
        return ""

######################################
# Count non-ready hybrids based on their link status
######################################
def read_link_status():
    results = p2basket.exec_cmd("read-link-status")
    indent.incr()
    failed_hyb_count = 0
    for sub in p2basket.sc_backend().subs():
        for fec in sub.fecs():
            regs = results[sub.id][fec.id]
            link_status = [0]*len(fec.hybs())
            for hyb in fec.hybs():
                link_status = regs[f"{hyb.alias()}.linkStatus"]
                if verbose :
                    print(f"{indent.blank}{hyb.qualified_alias()}: {link_status}")
                if link_status < 4 :
                    failed_hyb_count += 1
    print(f"{indent.empty_decr()}")
    return failed_hyb_count
# end of def read_link_status():

######################################
# Parse configuration extention file
######################################
def ext_config(ext_cfg_file_name):
    print(f"{indent.prompt}ext_config: Parsing {ext_cfg_file_name} file")

    # Check for input file
    try:
        ext_cfg_fptr = open(ext_cfg_file_name, 'r')
        if verbose > 1 :
            print(f"{indent.blank}ext_config: {ext_cfg_file_name} has size of {str(os.path.getsize(ext_cfg_file_name))}")
    except IOError:
        print(f"{indent.blank}ext_config: {ext_cfg_file_name} not accessible; quitting")
        raise IOError

    #indent.incr_blank()
    line_cntr = 0
    sys_id = -1
    bec_if = -1
    hyb_id = -1
    Vmm_id = -1
    reg_alias = "None"
    fld_name  = "None"
    value = -1
    for line in ext_cfg_fptr :
        line_cntr += 1
        # Split the line in non whitespace elements
        line_array=line.split()
        if verbose > 1 :
            print(f"{indent.blank}ext_config: Line {line_cntr} with {len( line_array )} words is {line}", end="")
            print(f"{indent.blank}Line {line_cntr} with {line_array}\n")

	# skip empty lines
        if len( line_array ) == 0 :
            continue
	# skip commented lines
        if line_array[0] == "#" :
            continue

        # Analyse the line
        if( line_array[0] == "Sys" ) :
            sys_id = int(line_array[1])
            if( line_array[2] == "Bec" ) :
                bec_id = int(line_array[3])
                if( line_array[4] == "Hyb" ) :
                    hyb_id = int(line_array[5])
                    if( line_array[6] == "Vmm" ) :
                        vmm_id = int(line_array[7])
                        if( line_array[8] != "Reg" ) :
                            print( f"{indent.blank}ext_config: Line {line_cntr} is {line}" )
                            print( f"{indent.blank}ext_config: Line {line_cntr} : Unexpected key word {line_array[8]} != Reg" )
                            print( f"{indent.blank}ext_config: Ignoring the line and continue parsing" )
                            continue
                        reg_alias = line_array[9]
                        if( line_array[10] != "Field" ) :
                            print( f"{indent.blank}ext_config: Line {line_cntr} is {line}" )
                            print( f"{indent.blank}ext_config: Line {line_cntr} : Unexpected key word {line_array[8]} != Field" )
                            print( f"{indent.blank}ext_config: Ignoring the line and continue parsing" )
                            continue
                        fld_name = line_array[11]
                        value = int(line_array[12])
                        reg = p2basket.sc_backend().sub(sys_id).fec(bec_id).hyb(hyb_id).vmm(vmm_id).reg(reg_alias)
                        if verbose :
                            print(f"{indent.blank}ext_config: {reg.qualified_alias()} field {fld_name} before:{reg.field(fld_name).value()}")
                        reg.field(fld_name).set_value(value)
                        if verbose :
                            print(f"{indent.blank}ext_config: {reg.qualified_alias()} field {fld_name}  after:{reg.field(fld_name).value()}")
                        if verbose :
                            print(f"{indent.blank}ext_config: ")
                    # end of if( line_array[6] == "Vmm" ) :
                 # end of if( line_array[4] == "Hyb" ) :
            # end of if( line_array[2] == "Bec" ) :
        # and of if( line_array[0] == "Sys" ) :
    # end of for line in src_fptr :
# end of def ext_config(ext_cfg_file_name)

######################################
# Main
######################################
if __name__ == '__main__':
    import argparse
    import sys
    import signal
    import time
    import subprocess
    from datetime import datetime

    parser = argparse.ArgumentParser(description='TbSalsa0Analog')
    parser.add_argument('-c', '--conf_dir', action='store',
            dest="conf_dir",
            help="Configuration directroy",
            default=".", type=str, required=False)
    parser.add_argument('-b', '--base_conf_file', action='store',
            dest="base_conf_file",
            help="Base configuration file name",
            default="None", type=str, required=True)
    parser.add_argument('-e', '--ext_conf_file', action='store',
            dest="ext_conf_file",
            help="Extensyion configuration file name",
            default="None", type=str, required=False)
    parser.add_argument('-Y', '--write_conf_file_yaml', action='store_true',
            dest="write_conf_file_yaml",
            help="Write current config in yaml",
            default=False, required=False)
    parser.add_argument("-v", "--verbose", help="increase output verbosity", default=0, action="count", required=False)
    args = parser.parse_args()
    if args.verbose:
        print(args)

    # set verbosity
    verbose = args.verbose

    # Initialise nice printing feature
    indent = Indent(4, ".")

    # Set logger to console prompts type
    p2basket.config_logger("ConsolePrompt");

    # Print version of the p2basket-sc software
    p2basket.exec_cmd("version")

    # Load config file
    conf_file_name = args.conf_dir + '//' + args.base_conf_file
    p2basket.exec_cmd("config-file", conf_file_name)

    # Read extended configuration with exceptions
    if args.ext_conf_file != "None":
        ext_cfg_file_name = args.conf_dir + '//' + args.ext_conf_file
        ext_config(ext_cfg_file_name)
    # end of if args.ext_conf_file != "None"

    # Apply configuration
    # FAST SCAN TEST: VMM registers only (no warm reset, no FEC/hybrid write)
    p2basket.exec_cmd("config-vmm-regs")

    # write configuration file if requested
    print(f"{indent.empty_decr()}")
    if args.write_conf_file_yaml == True :
        #p2basket.exec_cmd("to-yaml-file")
        print(f"{indent.blank}p2basket.exec_cmd(\"to-yaml-file\") hangs! To be debugged ! No action !")

    sys.exit(0)
# end of if __name__ == '__main__':
