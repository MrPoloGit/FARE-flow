#!/usr/bin/env python3
"""
Structural diff of our LGE-reconstructed netlist (06) against the real
post-PnR ground-truth netlist (02).

Net and instance names differ completely between the two files (02 has
meaningful synthesis-tool names like "sr_a/_16_", "a_reg[0]"; 06 has LGE's
own auto-generated names), so a textual diff is useless. Instead this does
structural (graph) matching: both netlists are modeled as a bipartite graph
of gate-nodes and net-nodes, and Weisfeiler-Leman-style color refinement is
run over the *combined* graph of both netlists at once. The refinement is
seeded by giving the primary I/O nets (A, B, S, clk, en, rst_n - the only
names guaranteed identical across both files, since they're the module's
own ports) a shared initial color across both netlists; iterating a few
rounds propagates that anchor outward through the shared connectivity
structure until (ideally) every gate converges on a color shared by exactly
one gate in each netlist - a confirmed structural correspondence.

After matching, a second, independent check cross-validates every matched
gate pair's actual pin connections and verifies a single-valued net-to-net
correspondence holds across the *entire* matched netlist - this is what
rules out a coincidental type-count match and confirms a true isomorphism
rather than just "same gates, unverified wiring."

Run this after producing scripts/clean_for_hal_import.py's output - see
PLAN.md task 5.
"""

import hashlib
import re
import sys
from collections import Counter, defaultdict

POWER_PINS = {"VGND", "VPWR", "VPB", "VNB", "VDD", "GND"}
FILLER_TYPES = {"sky130_fd_sc_hd__decap_3", "sky130_fd_sc_hd__tapvpwrvgnd_1"}

INSTANCE_RE = re.compile(r"(sky130_fd_sc_hd__\w+)\s+(\\?\S+?)\s*\(((?:[^()]|\([^()]*\))*)\)\s*;", re.DOTALL)
PIN_RE = re.compile(r"\.(\w+)\s*\(\s*(\\?\S+?)\s*\)")
MODULE_HEADER_RE = re.compile(r"module\s+\w+\s*\(((?:[^()]|\([^()]*\))*)\)\s*;", re.DOTALL)
PORT_DECL_RE = re.compile(r"\\?([A-Za-z_]\w*)\s*(?:;|,|$)")


def parse_module_ports(path: str):
    """Auto-detect the top module's own port names from its declaration
    header, instead of hardcoding a design-specific port list. Ports are
    the only net names guaranteed identical across a ground-truth netlist
    and a from-scratch reconstruction (everything internal is renamed by
    each tool's own convention), so they're what anchors the structural
    match below - this makes that anchor design-agnostic."""
    with open(path) as f:
        text = f.read()
    m = MODULE_HEADER_RE.search(text)
    if not m:
        return set()
    ports = set()
    for line in m.group(1).split(","):
        line = line.strip()
        line = re.sub(r"^(input|output|inout|wire|reg)\s+", "", line)
        line = line.strip().lstrip("\\").strip()
        if line:
            ports.add(line.split()[0] if " " in line else line)
    return ports


def parse_named_verilog(path: str, top_only: bool = True):
    """Return {inst_name: (cell_type, {pin: net})}. Requires named
    '.PIN(net)' connections (both 02's real netlist and our
    clean_for_hal_import.py output use this style)."""
    with open(path) as f:
        text = f.read()
    if top_only:
        text = text.split("endmodule", 1)[0]

    gates = {}
    for m in INSTANCE_RE.finditer(text):
        cell_type, inst_name, body = m.groups()
        inst_name = inst_name.strip().lstrip("\\").strip()
        pins = {pin: net.strip().lstrip("\\").strip() for pin, net in PIN_RE.findall(body)}
        gates[inst_name] = (cell_type, pins)
    return gates


def net_node_id(tag, net, primary_ports):
    return ("PORT", net) if net in primary_ports else (tag, "NET", net)


def gate_node_id(tag, inst):
    return (tag, "GATE", inst)


def build_graph(named_gates, primary_ports):
    """named_gates: [(tag, {inst: (cell_type, pins)}), ...]. Returns
    (label, edges) for the combined bipartite gate/net graph."""
    label = {}
    edges = defaultdict(list)
    for tag, gates in named_gates:
        for inst, (cell_type, pins) in gates.items():
            gnode = gate_node_id(tag, inst)
            label[gnode] = cell_type
            for pin, net in pins.items():
                if pin in POWER_PINS:
                    continue
                nnode = net_node_id(tag, net, primary_ports)
                if nnode not in label:
                    label[nnode] = "PORT" if nnode[0] == "PORT" else "NET"
                edges[gnode].append((pin, nnode))
                edges[nnode].append((pin, gnode))
    return label, edges


def refine(label: dict, edges: dict, rounds: int = 8):
    cur = dict(label)
    for _ in range(rounds):
        new = {}
        for node, lab in cur.items():
            nbr = sorted((edge_label, cur[neighbor]) for edge_label, neighbor in edges[node])
            key = (lab, tuple(nbr))
            new[node] = hashlib.sha1(repr(key).encode()).hexdigest()[:16]
        cur = new
    return cur


def match_gates(gates_a: dict, tag_a: str, gates_b: dict, tag_b: str, primary_ports, rounds: int = 8):
    """Return (matched: {inst_b: inst_a}, ambiguous_groups, unmatched_a, unmatched_b)."""
    label, edges = build_graph([(tag_a, gates_a), (tag_b, gates_b)], primary_ports)
    final = refine(label, edges, rounds=rounds)

    groups = defaultdict(lambda: defaultdict(list))
    for node, color in final.items():
        if len(node) == 3 and node[1] == "GATE":
            tag, _, inst = node
            groups[color][tag].append(inst)

    matched, ambiguous, unmatched_a, unmatched_b = {}, [], [], []
    for color, sides in groups.items():
        a, b = sides[tag_a], sides[tag_b]
        if len(a) == 1 and len(b) == 1:
            matched[b[0]] = a[0]
        elif not a and b:
            unmatched_b.extend(b)
        elif not b and a:
            unmatched_a.extend(a)
        else:
            ambiguous.append((a, b))
    return matched, ambiguous, unmatched_a, unmatched_b


def cross_validate_nets(matched: dict, gates_b: dict, gates_a: dict):
    """For every matched gate pair, derive a net_b -> net_a correspondence
    from shared pin connections and verify it's single-valued everywhere.
    Returns (net_map, conflicts)."""
    net_map = {}
    conflicts = []
    for inst_b, inst_a in matched.items():
        _, pins_b = gates_b[inst_b]
        _, pins_a = gates_a[inst_a]
        for pin, net_b in pins_b.items():
            if pin in POWER_PINS:
                continue
            net_a = pins_a.get(pin)
            if net_a is None:
                conflicts.append((inst_b, pin, "ground truth missing this pin"))
                continue
            if net_b in net_map and net_map[net_b] != net_a:
                conflicts.append((inst_b, pin, net_b, "already mapped to", net_map[net_b], "now sees", net_a))
            else:
                net_map[net_b] = net_a
    return net_map, conflicts


def main(ground_truth_path: str, reconstructed_path: str):
    gt_raw = parse_named_verilog(ground_truth_path)
    gt = {n: v for n, v in gt_raw.items() if v[0] not in FILLER_TYPES}
    recon = parse_named_verilog(reconstructed_path)

    primary_ports = parse_module_ports(ground_truth_path) & parse_module_ports(reconstructed_path)
    print("primary ports (structural anchor):", sorted(primary_ports))
    if not primary_ports:
        print("WARNING: no shared port names found between the two files - the match below has no anchor and will likely fail")

    print("ground truth real gates: %d, reconstructed gates: %d" % (len(gt), len(recon)))
    hist_match = Counter(t for t, _ in gt.values()) == Counter(t for t, _ in recon.values())
    print("gate-type histogram match:", hist_match)

    matched, ambiguous, unmatched_gt, unmatched_recon = match_gates(gt, "gt", recon, "recon", primary_ports)
    print("structurally matched: %d / %d" % (len(matched), len(recon)))
    print("ambiguous groups: %d, unmatched ground truth: %d, unmatched reconstructed: %d" % (
        len(ambiguous), len(unmatched_gt), len(unmatched_recon)))
    for a, b in ambiguous:
        print("  ambiguous: ground_truth=%s reconstructed=%s" % (a, b))
    for inst in unmatched_recon:
        print("  unmatched (reconstructed side):", inst)
    for inst in unmatched_gt:
        print("  unmatched (ground truth side):", inst)

    net_map, conflicts = cross_validate_nets(matched, recon, gt)
    print("net-level cross-check: %d distinct nets, %d conflicts" % (len(net_map), len(conflicts)))
    for c in conflicts:
        print("  CONFLICT:", c)

    return matched


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: structural_diff.py <02_netlist_with_power_rails.v> <06_hal_import.v>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
