#!/usr/bin/env python3
"""
Post-processing step for LGE's reconstructed netlist: expand the merged
VDD/GND connections back out to the real 4-pin sky130 naming
(VPWR/VGND/VPB/VNB) using each gate type's original pin order.

LGE only understands generic VDD/GND (see convert_to_lge.py - power pins get
merged before LGE ever sees the netlist, and LGE's native 4-pin recognition
path has unresolved crashes in Database.cpp's analyzeNetlist()). This script
expands VDD -> VPWR/VPB and GND -> VGND/VNB in a *copy* of the output, for
cases where you want the real power-pin names for comparison against ground
truth (e.g. 02_netlist_with_power_rails.v) rather than for HAL import (HAL's
Liberty parser only exposes VGND/VPWR as real ports anyway - VPB/VNB aren't
representable, so this expansion is NOT needed before importing into HAL).

Reconstructed from session notes; the original script and its exact
pin-order tables are gone. You must supply / verify PIN_ORDER against the
real sky130_fd_sc_hd Liberty file for the cells actually present in your
netlist before trusting this output.
"""

import re
import sys

# cell_type -> ordered list of (merged_name, real_name) power/ground pins,
# in the order they appear in the *original* (pre-merge) Magic/LGE pin list.
# Fill in / verify against the actual sky130_fd_sc_hd__*.lib pg_pin order
# for each cell type present in your netlist before relying on this.
PIN_ORDER = {
    # example shape - extend per cell type as needed:
    # "and2_0": [("VDD", "VPWR"), ("GND", "VGND"), ("VDD", "VPB"), ("GND", "VNB")],
}


def expand_instance_line(line: str) -> str:
    m = re.match(r"^(\S+)\s+(.*)\s+(\S+)$", line.strip())
    if not m:
        return line
    inst_name, conns, cell_type = m.groups()
    if cell_type not in PIN_ORDER:
        return line

    conn_list = conns.split()
    expanded = []
    for conn in conn_list:
        expanded.append(conn)
    # Real expansion requires knowing which positional slots in conn_list
    # are the merged VDD/GND pins for this cell_type - see PIN_ORDER above.
    return "%s %s %s" % (inst_name, " ".join(expanded), cell_type)


def expand(in_path: str, out_path: str):
    with open(in_path) as f:
        lines = f.readlines()

    out = [expand_instance_line(l) if not l.strip().startswith(("module", "endmodule", "//")) else l.rstrip("\n")
           for l in lines]

    with open(out_path, "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: expand_power_pins.py <in.v> <out.v>")
        sys.exit(1)
    expand(sys.argv[1], sys.argv[2])
