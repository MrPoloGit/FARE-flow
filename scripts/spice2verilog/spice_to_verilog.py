#!/usr/bin/env python3
"""
Convert a Magic ext2spice hierarchical SPICE netlist directly to
HAL-importable named-port Verilog, when the SPICE already names every
instance by its real standard-cell type (confirmed for puzzle.gds: Magic's
own extraction recovers real sky130_fd_sc_hd__* cell types directly from
the GDS hierarchy, unlike the warmup path which needed LGE-based subgraph-
isomorphism identification on top of Magic's extraction - see README.md/
notes.md for that distinction). This script has no gate-identification
step at all - it's a pure, mechanical SPICE subckt/instance -> Verilog
module/instance syntax translation, trustworthy because Magic's own
.subckt pin-order header and each instance's positional connection list
are self-consistent by construction (extracted from the same real layout
in the same run) - unlike LGE's reconstructed stub headers, which turned
out to sometimes be broken (see the "Pin-order wiring bug" in PLAN.md).

Only the top-level subckt's own instances are emitted (matching
clean_for_hal_import.py's "truncate to the top module" step) - HAL gets
its gate definitions from the Liberty file, not from this SPICE's own
per-cell-type subckt bodies.

VPB/VNB (body-bias/well-tie pins) are dropped from the emitted connections
entirely, not merged into VGND/VPWR - HAL's Liberty parser doesn't expose
them as real ports for this library anyway (confirmed this session), and
named-port syntax means omitting them is safe (order-independent), unlike
the warmup's positional-connection path which needed careful rail-merging
to keep pin counts consistent.

Usage: spice_to_verilog.py <in.spice> <top_cell_name> <out.v> [liberty.lib]

If a Liberty file is given, each top-level port's real direction (input/
output) is derived from it instead of declaring every port "inout" -
required for HAL's netlist_simulator_controller (specifically its
verilator-engine subgraph-copy step), which otherwise fails with "unable
to create pin '<name>': name '<name>' is already taken" for a real output
net declared inout (confirmed directly: this doesn't affect plain
NetlistFactory.load_netlist() import, only simulation). Without a Liberty
file, every port still falls back to "inout" as before - fine for import,
not for simulation.
"""

import re
import sys

SUBCKT_RE = re.compile(r"^\.subckt\s+(\S+)\s+(.*)$", re.IGNORECASE)
ENDS_RE = re.compile(r"^\.ends\b", re.IGNORECASE)
INSTANCE_RE = re.compile(r"^X(\S+)\s+(.*)$", re.IGNORECASE)

DROP_PINS = {"VPB", "VNB"}

# Magic's net names aren't always plain Verilog identifiers: internal nets
# get named "<owning-instance>/<pin>" (contains '/'), and this design's own
# top-level pins are literally named "O[0]".."O[7]" (flat names, not a real
# Verilog bus - there's no "wire [7:0] O" - so left bare, "O[0]" would parse
# as a bit-select into an undeclared vector). Both need Verilog's escaped-
# identifier syntax: a leading backslash, terminated by whitespace.
SIMPLE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def escape_id(name):
    if SIMPLE_ID_RE.match(name):
        return name
    return "\\" + name + " "


CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
PIN_RE = re.compile(r'^\s*pin\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
DIRECTION_RE = re.compile(r'^\s*direction\s*:\s*"?(\w+)"?\s*;')


def parse_liberty_pin_directions(path):
    """Returns {cell_type: {pin_name: 'input'|'output'|'inout'}} by a
    simple brace-depth scan of the Liberty file - only tracks cell/pin
    blocks and their own "direction" attribute, ignores everything else."""
    directions = {}
    cell_name = None
    pin_name = None
    cell_depth = None
    pin_depth = None
    depth = 0
    with open(path) as f:
        for line in f:
            m = CELL_RE.match(line)
            if m and cell_name is None:
                cell_name = m.group(1)
                cell_depth = depth
                directions[cell_name] = {}
            m = PIN_RE.match(line)
            if m and cell_name is not None and pin_name is None:
                pin_name = m.group(1)
                pin_depth = depth
            m = DIRECTION_RE.match(line)
            if m and cell_name is not None and pin_name is not None:
                directions[cell_name][pin_name] = m.group(1)

            depth += line.count("{") - line.count("}")

            if pin_name is not None and depth <= pin_depth:
                pin_name = None
            if cell_name is not None and depth <= cell_depth:
                cell_name = None
    return directions


def join_continuations(lines):
    joined = []
    for line in lines:
        if line.startswith("+") and joined:
            joined[-1] = joined[-1].rstrip("\n") + " " + line[1:].lstrip()
        else:
            joined.append(line)
    return joined


def parse_spice(path):
    """Returns {cell_type: [pins]} for every .subckt, and, separately, the
    raw instance lines of every subckt (needed to find the top cell's own
    instances)."""
    with open(path) as f:
        raw_lines = f.readlines()
    lines = join_continuations(raw_lines)

    pin_order = {}
    instances_by_subckt = {}
    current = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue

        m = SUBCKT_RE.match(stripped)
        if m:
            current = m.group(1)
            pin_order[current] = m.group(2).split()
            instances_by_subckt[current] = []
            continue

        if ENDS_RE.match(stripped):
            current = None
            continue

        if current is None:
            continue

        m = INSTANCE_RE.match(stripped)
        if m:
            inst_name, rest = m.groups()
            fields = rest.split()
            instances_by_subckt[current].append((inst_name, fields))

    return pin_order, instances_by_subckt


POWER_PINS = {"VGND", "VPWR", "VPB", "VNB"}


def determine_port_directions(top_pins, top_instances, pin_order, liberty_dirs):
    """A top-level port is a real design OUTPUT if any internal instance
    drives it from a Liberty-declared "output" pin; otherwise it's an
    externally-driven INPUT (the correct default for anything never seen
    driving, e.g. a genuinely unused port). Power pins are always "input"
    (undriven by any gate, same as how the simulator's own connectivity
    analysis would classify them regardless)."""
    port_dir = {p: ("input" if p in POWER_PINS else None) for p in top_pins}
    for _inst_name, fields in top_instances:
        cell_type = fields[-1]
        nets = fields[:-1]
        cell_pins = pin_order.get(cell_type, [])
        cell_dirs = liberty_dirs.get(cell_type, {})
        for pin, net in zip(cell_pins, nets):
            if net in port_dir and cell_dirs.get(pin) == "output":
                port_dir[net] = "output"
    for p in top_pins:
        if port_dir[p] is None:
            port_dir[p] = "input"
    return port_dir


def convert(in_path, top_cell, out_path, liberty_path=None):
    pin_order, instances_by_subckt = parse_spice(in_path)

    if top_cell not in instances_by_subckt:
        print("top cell '%s' not found as a .subckt in %s" % (top_cell, in_path))
        sys.exit(1)

    top_pins = pin_order[top_cell]
    top_pins_set = set(top_pins)

    port_dir = None
    if liberty_path:
        liberty_dirs = parse_liberty_pin_directions(liberty_path)
        port_dir = determine_port_directions(top_pins, instances_by_subckt[top_cell], pin_order, liberty_dirs)

    # HAL's Verilog parser only registers a net for an identifier appearing
    # in the bare, non-ANSI port list (e.g. "module puzzle (I, O[0], ...);")
    # if it's *also* separately declared with a direction (input/output/
    # inout) or, for internal nets, with "wire" - confirmed by reading
    # verilog_parser.cpp's parse_port_list() (registers m_ports only, no
    # signal) vs. parse_port_definition()/parse_signal_definition() (both
    # populate m_signals, which is what net-name resolution during instance
    # elaboration - "failed to find alias for net ..." - actually looks up).
    # Real per-port direction (input/output, from Liberty if given) is used
    # when available; "inout" for everything is fine for plain HAL import
    # but breaks netlist_simulator_controller's verilator engine (a real
    # output net declared inout makes its subgraph-copy step try to create
    # the same-named pin twice - "name '<name>' is already taken").
    instance_lines = []
    internal_nets = []
    seen_nets = set(top_pins_set)

    for inst_name, fields in instances_by_subckt[top_cell]:
        # last field is the cell type; everything before it is the
        # positional net list, in the cell type's own declared pin order.
        cell_type = fields[-1]
        nets = fields[:-1]
        if cell_type not in pin_order:
            print("warning: instance %s references unknown cell type %s - skipped" % (inst_name, cell_type))
            continue
        cell_pins = pin_order[cell_type]
        if len(cell_pins) != len(nets):
            print("warning: instance %s (%s) has %d nets but %d declared pins - skipped" % (inst_name, cell_type, len(nets), len(cell_pins)))
            continue

        conns = []
        for pin, net in zip(cell_pins, nets):
            if pin in DROP_PINS:
                continue
            if net not in seen_nets:
                seen_nets.add(net)
                internal_nets.append(net)
            conns.append(".%s(%s)" % (escape_id(pin), escape_id(net)))

        instance_lines.append("%s %s (%s);" % (cell_type, escape_id(inst_name), ", ".join(conns)))

    lines = []
    lines.append("// Verilog netlist mechanically translated from Magic's ext2spice output")
    lines.append("// by scripts/spice_to_verilog.py - no gate-identification step involved,")
    lines.append("// every instance is already named by its real cell type in the source SPICE.")
    lines.append("")
    lines.append("module %s (%s);" % (top_cell, ", ".join(escape_id(p) for p in top_pins)))
    lines.append("")
    if port_dir is not None:
        by_dir = {"input": [], "output": []}
        for p in top_pins:
            by_dir[port_dir[p]].append(p)
        if by_dir["input"]:
            lines.append("input %s;" % ", ".join(escape_id(p) for p in by_dir["input"]))
        if by_dir["output"]:
            lines.append("output %s;" % ", ".join(escape_id(p) for p in by_dir["output"]))
    else:
        lines.append("inout %s;" % ", ".join(escape_id(p) for p in top_pins))
    if internal_nets:
        lines.append("wire %s;" % ", ".join(escape_id(n) for n in internal_nets))
    lines.append("")
    lines.extend(instance_lines)

    lines.append("")
    lines.append("endmodule")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("wrote %s: %d instances" % (out_path, len(instances_by_subckt[top_cell])))


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("usage: spice_to_verilog.py <in.spice> <top_cell_name> <out.v> [liberty.lib]")
        sys.exit(1)
    liberty = sys.argv[4] if len(sys.argv) == 5 else None
    convert(sys.argv[1], sys.argv[2], sys.argv[3], liberty)
