"""
Cross-checks scripts/print_equations.py's extracted D-input equations
against a REAL gate-level simulation trace, rather than just trusting the
extraction code. For every flip-flop in the design, this:
  1. extracts the D-function exactly as print_equations.py does
     (SubgraphNetlistDecorator over the combinational fan-in cone), then
  2. at every clock edge in a real verilator-produced waveform.vcd,
     evaluates that D-function using the REAL simulated value of each of
     its variables sampled just before the edge, and compares the result
     against that flip-flop's own REAL simulated Q value just after the
     edge.

If these agree at every single cycle across the whole trace, that's
direct evidence the extraction is correct - not just "the code looks
right".

The waveform.vcd must be a REAL per-signal verilator trace (i.e. produced
by manually running the compiled verilator testbench binary, NOT via
HAL's own netlist_simulator_controller generate_vcd()/get_waveform_by_net()
path - both were tried first and confirmed to only ever dump the design's
global INPUT nets in this HAL build, never outputs or internal nets, even
though get_waveform_by_net() is documented as pulling a net's result in as
a side effect; see notes.md for the full story). Concretely: recompile
with `--trace-depth <large number>` instead of the default `2` (2 only
captures the top-level ports; going deeper into the flat gate-level
module exposes each net Magic named `<gate_instance>/<pin>`) and rerun the
testbench binary, exactly as already set up under
`hal/build/hal_simulation_<name>_*/` by an earlier run of
scripts/simulate_vs_vcd.py.

Run inside HAL's own embedded Python interpreter:

  ./bin/hal --python-script scripts/verify_equations.py \
    --py-args "<netlist.v> <liberty.lib> <waveform.vcd>"
"""

import builtins
import sys

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/hal/build/lib/hal_plugins")
import hal_py

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/scripts")
import compare_vcd_outputs

if len(sys.argv) != 3:
    print("usage: --python-script verify_equations.py --py-args \"<netlist.v> <liberty.lib> <waveform.vcd>\"")
    sys.exit(1)
NETLIST, LIBERTY, WAVEFORM_VCD = sys.argv


def p(*a):
    print(*a)
    sys.stdout.flush()


nl = hal_py.NetlistFactory.load_netlist(NETLIST, LIBERTY)
if nl is None:
    p("FAILED TO LOAD NETLIST")
    sys.exit(1)


def is_include_worthy(g):
    return g.type.has_property(hal_py.GateTypeProperty.combinational) and not g.is_gnd_gate() and not g.is_vcc_gate()


def combinational_cone(start_net):
    frontier = [start_net]
    visited_nets = builtins.set()
    gates = builtins.set()
    while frontier:
        n = frontier.pop()
        if n in visited_nets:
            continue
        visited_nets.add(n)
        for ep in n.get_sources():
            g = ep.get_gate()
            if g in gates or not is_include_worthy(g):
                continue
            gates.add(g)
            for in_net in g.get_fan_in_nets():
                frontier.append(in_net)
    return gates


def named_global_input(name):
    matches = [n for n in nl.get_global_input_nets() if n.name == name]
    return matches[0] if matches else None


by_id = {n.id: n for n in nl.get_nets()}
clk_net = named_global_input("clk")

CONE_SIZE_LIMIT = 200  # skip pathologically large cones (slow to evaluate 300+ times each)


def d_net_of(ff):
    pins = [pn for pn in ff.type.get_input_pin_names() if pn not in ("CLK", "VGND", "VPWR", "VPB", "VNB", "RESET_B", "SET_B")]
    nets = [ff.get_fan_in_net(pn) for pn in pins if ff.get_fan_in_net(pn) is not None]
    return nets[0]


ffs = [g for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)]
decorator = hal_py.SubgraphNetlistDecorator(nl)

p("total flip-flops:", len(ffs))
p("parsing", WAVEFORM_VCD, "...")
events_by_name = compare_vcd_outputs.parse_vcd(WAVEFORM_VCD)
sample_at = compare_vcd_outputs.sample_at
p("parsed %d distinct signal name(s)" % len(events_by_name))


def events_for(net_id):
    return events_by_name.get(by_id[net_id].name)


clk_events = events_for(clk_net.id)
if clk_events is None:
    p("FATAL: clk not present in the waveform VCD")
    sys.exit(1)
edge_times = [t for t, v in clk_events if v == "1"]
p("found %d rising clock edges" % len(edge_times))

total_checked = 0
total_mismatches = 0
n_skipped = 0
n_verified_clean = 0

for ff in ffs:
    q_net = ff.get_fan_out_nets()[0]
    d = d_net_of(ff)
    cone = combinational_cone(d)
    if len(cone) > CONE_SIZE_LIMIT:
        n_skipped += 1
        continue
    bf = decorator.get_subgraph_function(list(cone), d)

    var_net_ids = [int(v.split("_")[1]) for v in bf.get_variable_names()]
    q_events = events_for(q_net.id)
    var_events = {nid: events_for(nid) for nid in var_net_ids}
    missing_vars = [nid for nid in var_net_ids if var_events[nid] is None]
    if q_events is None or missing_vars:
        n_skipped += 1
        p("%s: SKIPPED - not present in waveform VCD (q missing=%s, vars missing=%s)" % (ff.name, q_events is None, [by_id[nid].name for nid in missing_vars]))
        continue

    mismatches = 0
    checked = 0
    for et in edge_times:
        pre_t = et - 1
        if pre_t < 0:
            continue
        inputs = {}
        ok_all = True
        for var in bf.get_variable_names():
            net_id = int(var.split("_")[1])
            val = sample_at(var_events[net_id], pre_t)
            if val not in ("0", "1"):
                ok_all = False
                break
            inputs[var] = hal_py.BooleanFunction.Value.ONE if val == "1" else hal_py.BooleanFunction.Value.ZERO
        if not ok_all:
            continue
        predicted = bf.evaluate(inputs)
        predicted_bit = 1 if predicted == hal_py.BooleanFunction.Value.ONE else 0
        post_t = et + 1
        actual = sample_at(q_events, post_t)
        if actual not in ("0", "1"):
            continue
        actual_bit = int(actual)
        checked += 1
        if actual_bit != predicted_bit:
            mismatches += 1
            if mismatches <= 3:
                p("  MISMATCH %s at t=%d: predicted=%d actual=%d" % (ff.name, et, predicted_bit, actual_bit))

    total_checked += checked
    total_mismatches += mismatches
    if mismatches == 0 and checked > 0:
        n_verified_clean += 1
    p("%s (%d gate cone): checked %d cycles, %d mismatch(es)" % (ff.name, len(cone), checked, mismatches))

p("")
p("=== SUMMARY ===")
p("flip-flops fully verified with zero mismatches: %d / %d" % (n_verified_clean, len(ffs)))
p("flip-flops skipped (too large or missing waveform data): %d" % n_skipped)
p("total (cycle, flip-flop) checks: %d, total mismatches: %d" % (total_checked, total_mismatches))
