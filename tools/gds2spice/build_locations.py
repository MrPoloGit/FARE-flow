#!/usr/bin/env python3
"""
Extract per-instance (x, y) placement from Magic's .ext file and match each
placement to a gate instance in the LGE-reconstructed Verilog netlist.

Magic's .ext file contains lines like:
    use <celltype> <instname> <a> <b> <c> <d> <e> <f>
where the 6 values are a 2x3 transform matrix; (c, f) is the translation
offset, i.e. the instance's placement in Magic's internal units. <instname>
is the real physical instance identifier Magic assigned during layout.

Matching strategy (fixed, see "KNOWN BUG" note below for the old approach):
Magic's own hierarchical SPICE extraction (05_extracted.spice) uses the
exact same net-name strings for the adder_demo top-level nets as the
LGE-reconstructed Verilog netlist does (both come from the same underlying
Magic net identifiers - LGE just carried them over when reconstructing gate
types). That gives us the *real* instance name for free: for each Verilog
gate instance, compute the set of net names touching its signal pins
(power/ground pins excluded, since dialects differ: VDD/GND in the merged
Verilog vs VGND/VPWR/VPB/VNB in the raw SPICE), and match it against the
X-instance in the SPICE top-level subckt with the same cell type and the
exact same touching-net set. This uses full pin connectivity per instance
instead of trying to reverse-engineer identity from a single output pin's
auto-generated net label, so it doesn't depend on which pin Magic happened
to name a net after.

The matched SPICE X-instance name is the real Magic instance name, which is
what the .ext file's "use" lines key on - so once matched, look up (x, y)
there directly.

KNOWN BUG in the old approach (now replaced): it regexed a Verilog gate's
*output* pin's net name against a "<celltype>_<N>"-style label and matched
by elimination against same-typed .ext instances. Magic's auto net labels
don't reliably originate from the *driving* pin - they can be derived from
any pin touching the net, including a downstream gate's *input* pin - so
type-guessing from one label and assigning by elimination order produced
several confirmed duplicate-coordinate collisions. The full-connectivity-set
match below doesn't have this failure mode, since it uses every signal pin
on the instance, not one guessed label, and reports ambiguity/no-match
explicitly instead of guessing.

NOTE: as of this writing there is no .ext file available on disk (and no
`magic` binary in this environment to regenerate one from 04_final.gds), so
end-to-end (x, y) output can't be produced right now. The connectivity
matching in match_verilog_to_spice() below is independently verifiable
against 05_extracted.spice + the Verilog netlist and has been confirmed to
produce a unique match for all 79 gates with zero collisions - see PLAN.md.
"""

import re
import sys
from collections import defaultdict

EXT_USE_RE = re.compile(
    r"^use\s+(\S+)\s+(\S+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)"
)

POWER_PINS = {"VDD", "GND", "VPWR", "VGND", "VPB", "VNB"}


def parse_ext_placements(ext_path: str):
    """Return {instname: (celltype, x, y)}."""
    placements = {}
    with open(ext_path) as f:
        for line in f:
            m = EXT_USE_RE.match(line.strip())
            if not m:
                continue
            celltype, instname, a, b, c, d, e, f_ = m.groups()
            x, y = float(c), float(f_)
            placements[instname] = (celltype, x, y)
    return placements


def parse_verilog_module_pin_orders(text: str):
    """Return {cell_type: [pin_names_in_port_order]} from every 'module
    <name> ( p1, p2, ... );' header in the file. LGE's top-level module
    instantiates gates positionally (no .PIN(net) syntax), so the only place
    the pin order for each cell type is documented is in LGE's own trailing
    per-gate-type stub module headers - this recovers it from there."""
    header_re = re.compile(r"module\s+(\w+)\s*\(([^;]*)\)\s*;")
    orders = {}
    for cell_type, portlist in header_re.findall(text):
        pins = [p.strip() for p in portlist.split(",") if p.strip()]
        orders[cell_type] = pins
    return orders


def parse_verilog_instances(v_path: str):
    """Return list of (inst_name, cell_type, {pin: net}) for each gate
    instance in the top module, resolving positional connections via each
    cell type's pin order (see parse_verilog_module_pin_orders)."""
    with open(v_path) as f:
        text = f.read()

    pin_orders = parse_verilog_module_pin_orders(text)

    # Only the top module's body, i.e. everything up to the first endmodule.
    top_text = text.split("endmodule", 1)[0]

    inst_re = re.compile(r"^\s*(\w+)\s+(\w+)\s*\(([^;]*)\)\s*;", re.MULTILINE)

    instances = []
    for m in inst_re.finditer(top_text):
        cell_type, inst_name, conn_str = m.groups()
        if cell_type not in pin_orders:
            continue
        nets = [n.strip() for n in conn_str.split(",") if n.strip()]
        pin_names = pin_orders[cell_type]
        if len(nets) != len(pin_names):
            continue
        pins = dict(zip(pin_names, nets))
        instances.append((inst_name, cell_type, pins))
    return instances


def join_continuations(lines):
    joined = []
    for line in lines:
        if line.startswith("+") and joined:
            joined[-1] = joined[-1].rstrip("\n") + " " + line[1:].lstrip()
        else:
            joined.append(line)
    return joined


def parse_spice_subckt_headers(lines):
    """Return {celltype: [pin_names_in_declared_order]}."""
    headers = {}
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith(".subckt"):
            fields = stripped.split()
            headers[fields[1]] = fields[2:]
    return headers


def parse_spice_top_instances(spice_path: str, top_cell: str):
    """Return list of (inst_name, cell_type, {pin: net}) for each X-instance
    in the top-level subckt, using each cell type's declared pin order to
    turn positional SPICE nodes into named pin connections."""
    with open(spice_path) as f:
        raw_lines = f.readlines()

    lines = join_continuations(raw_lines)
    headers = parse_spice_subckt_headers(lines)

    in_top = False
    instances = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith(".subckt"):
            in_top = stripped.split()[1] == top_cell
            continue
        if low.startswith(".ends"):
            in_top = False
            continue
        if not in_top or not stripped.startswith("X"):
            continue

        fields = stripped.split()
        inst_name = fields[0][1:]
        cell_type = fields[-1]
        nodes = fields[1:-1]
        pin_names = headers.get(cell_type)
        if pin_names is None or len(pin_names) != len(nodes):
            # Can't reliably map positions to names - skip rather than guess.
            continue
        pins = dict(zip(pin_names, nodes))
        instances.append((inst_name, cell_type, pins))
    return instances


def signal_net_set(pins: dict):
    return frozenset(net for pin, net in pins.items() if pin.upper() not in POWER_PINS)


def match_verilog_to_spice(verilog_instances, spice_instances):
    """Match each Verilog gate instance to a SPICE top-level X-instance by
    (cell_type, exact set of signal-pin net names). Returns
    (matched: {verilog_inst_name: spice_inst_name}, unmatched: [...],
    ambiguous: {verilog_inst_name: [candidate_spice_names]})."""
    by_signature = defaultdict(list)
    for inst_name, cell_type, pins in spice_instances:
        key = (cell_type, signal_net_set(pins))
        by_signature[key].append(inst_name)

    matched = {}
    unmatched = []
    ambiguous = {}

    for inst_name, cell_type, pins in verilog_instances:
        key = (cell_type, signal_net_set(pins))
        candidates = by_signature.get(key, [])
        if len(candidates) == 1:
            matched[inst_name] = candidates[0]
        elif len(candidates) > 1:
            ambiguous[inst_name] = candidates
        else:
            unmatched.append(inst_name)

    return matched, unmatched, ambiguous


def main(spice_path, v_path, out_path, top_cell="adder_demo", ext_path=None):
    verilog_instances = parse_verilog_instances(v_path)
    spice_instances = parse_spice_top_instances(spice_path, top_cell)
    matched, unmatched, ambiguous = match_verilog_to_spice(verilog_instances, spice_instances)

    placements = parse_ext_placements(ext_path) if ext_path else {}

    with open(out_path, "w") as f:
        for inst_name, _, _ in verilog_instances:
            spice_name = matched.get(inst_name)
            if spice_name is None:
                status = "AMBIGUOUS" if inst_name in ambiguous else "UNMATCHED"
                f.write("%s %s\n" % (inst_name, status))
                continue
            if spice_name in placements:
                _, x, y = placements[spice_name]
                f.write("%s %f %f\n" % (inst_name, x, y))
            else:
                f.write("%s NO_EXT_DATA(%s)\n" % (inst_name, spice_name))

    n = len(verilog_instances)
    print("matched %d/%d instances (%d unmatched, %d ambiguous)" % (
        len(matched), n, len(unmatched), len(ambiguous)))
    if ambiguous:
        print("ambiguous matches (same cell type + signal nets, needs disambiguation):")
        for inst_name, candidates in ambiguous.items():
            print("  %s -> %s" % (inst_name, candidates))
    if not placements:
        print("note: no .ext file supplied/found - instance matching done, "
              "but no (x, y) data available to emit yet.")


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("usage: build_locations.py <05_extracted.spice> <netlist.v> <out_locations.txt> [file.ext]")
        sys.exit(1)
    ext = sys.argv[4] if len(sys.argv) == 5 else None
    main(sys.argv[1], sys.argv[2], sys.argv[3], ext_path=ext)
