#!/usr/bin/env python3
"""
General-purpose bit-index tracer for a two-operand word-level netlist.

Given a named-port Verilog netlist and two regexes identifying the "anchor"
nets of each operand (a net name with a captured bit index - e.g. an
`a_reg[3]` wire, or any other per-bit signal), computes for a set of target
gates which operand bit indices are reachable in each target's full
combinational fan-in cone (walking backward through combinational logic,
stopping at anchor nets or dead ends). The MAXIMUM reachable index per
operand is the target's own bit position: in any ripple/lookahead-style
word-level operation, bit i can only depend on operand bits 0..i, never
higher, so this is a robust way to bit-index gates whose *direct* fan-out
adjacency doesn't reliably tell you their bit position (e.g. a DAG-shaped
carry chain with fan-out reuse between bit-positions).

Written to settle exactly which physical gate computes which bit of
adder_demo's `add0` block, after 1-hop successor adjacency turned out to be
unreliable for its DAG-shaped carry logic (see PLAN.md task 7). Not specific
to that design - works on any named-port sky130_fd_sc_hd Verilog netlist
given suitable anchor regexes and targets.

Usage:
  python3 trace_bit_indices.py <netlist.v> \\
      --operand-a-regex 'a_reg\\[(\\d+)\\]' --operand-b-regex 'b_reg\\[(\\d+)\\]' \\
      [--targets inst1,inst2,...] [--target-types type1,type2,...]

If neither --targets nor --target-types is given, every gate in the netlist
is treated as a target.
"""

import argparse
import re
import sys
from collections import defaultdict

POWER_PINS = {"VGND", "VPWR", "VPB", "VNB", "VDD", "GND"}
OUTPUT_PIN_NAMES = {"X", "Y", "Q", "Q_N", "QN"}

INSTANCE_RE = re.compile(r"(sky130_fd_sc_hd__\w+)\s+(\\?\S+?)\s*\(((?:[^()]|\([^()]*\))*)\)\s*;", re.DOTALL)
PIN_RE = re.compile(r"\.(\w+)\s*\(\s*(\\?\S+?)\s*\)")


def parse_named_verilog(path: str, top_only: bool = True):
    """Return {inst_name: (cell_type, {pin: net})}. Requires named
    '.PIN(net)' connections."""
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


def build_driver_map(gates: dict):
    """net -> (inst, pin) for whichever gate's output pin drives it. Sky130
    output pins are always named X, Y, Q, Q_N, or QN."""
    driver = {}
    for inst, (_cell_type, pins) in gates.items():
        for pin, net in pins.items():
            if pin in OUTPUT_PIN_NAMES:
                driver[net] = (inst, pin)
    return driver


def bit_index_of_net(net: str, operand_a_re, operand_b_re):
    m = operand_a_re.search(net)
    if m:
        return "A", int(m.group(1))
    m = operand_b_re.search(net)
    if m:
        return "B", int(m.group(1))
    return None


def reachable_bits(start_inst: str, gates: dict, driver: dict, operand_a_re, operand_b_re):
    """BFS backward from start_inst's input nets through combinational
    logic. Returns {"A": set(bit indices), "B": set(bit indices)}."""
    reached = {"A": set(), "B": set()}
    visited_insts = set()
    visited_nets = set()
    frontier = [start_inst]

    while frontier:
        inst = frontier.pop()
        if inst in visited_insts:
            continue
        visited_insts.add(inst)

        _cell_type, pins = gates[inst]
        for pin, net in pins.items():
            if pin in POWER_PINS or pin in OUTPUT_PIN_NAMES:
                continue
            if net in visited_nets:
                continue
            visited_nets.add(net)

            hit = bit_index_of_net(net, operand_a_re, operand_b_re)
            if hit is not None:
                operand, bit = hit
                reached[operand].add(bit)
                continue    # anchor net - do not walk further upstream

            src = driver.get(net)
            if src is not None:
                frontier.append(src[0])
            # else: dead end (primary input/constant with no bit-index match) - ignore

    return reached


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist", help="named-port Verilog netlist")
    parser.add_argument("--operand-a-regex", required=True, help="regex with one capture group for operand A's bit index, matched against net names")
    parser.add_argument("--operand-b-regex", required=True, help="regex with one capture group for operand B's bit index, matched against net names")
    parser.add_argument("--targets", help="comma-separated instance names to report on")
    parser.add_argument("--target-types", help="comma-separated cell type names (substring match) to report on")
    args = parser.parse_args()

    gates = parse_named_verilog(args.netlist)
    driver = build_driver_map(gates)
    operand_a_re = re.compile(args.operand_a_regex)
    operand_b_re = re.compile(args.operand_b_regex)

    if args.targets:
        target_insts = [t.strip() for t in args.targets.split(",")]
    elif args.target_types:
        type_substrings = [t.strip() for t in args.target_types.split(",")]
        target_insts = [inst for inst, (cell_type, _pins) in gates.items() if any(ts in cell_type for ts in type_substrings)]
    else:
        target_insts = list(gates.keys())

    print("%-20s %-30s %-25s %-25s %s" % ("instance", "type", "reachable A bits", "reachable B bits", "inferred bit index"))
    for inst in sorted(target_insts):
        if inst not in gates:
            print("%-20s NOT FOUND IN NETLIST" % inst)
            continue
        cell_type, _pins = gates[inst]
        reached = reachable_bits(inst, gates, driver, operand_a_re, operand_b_re)
        max_a = max(reached["A"]) if reached["A"] else None
        max_b = max(reached["B"]) if reached["B"] else None
        if max_a is not None and max_b is not None and max_a == max_b:
            inferred = str(max_a)
        elif max_a is not None or max_b is not None:
            inferred = "MISMATCH a=%s b=%s" % (max_a, max_b)
        else:
            inferred = "none reached"

        a_str = ",".join(str(b) for b in sorted(reached["A"])) or "-"
        b_str = ",".join(str(b) for b in sorted(reached["B"])) or "-"
        print("%-20s %-30s %-25s %-25s %s" % (inst, cell_type.replace("sky130_fd_sc_hd__", ""), a_str, b_str, inferred))


if __name__ == "__main__":
    main()
