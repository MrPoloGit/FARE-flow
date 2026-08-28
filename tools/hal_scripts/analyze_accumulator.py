"""
Investigative script (not part of the main decompile pipeline): find which
of the 27 dataflow-found register groups that decompile_to_rtl.py couldn't
bit-order are genuine accumulators (their own D-input logic depends on
their own group's Q output, i.e. real feedback/recurrence - a counter,
LFSR, or running checksum), as opposed to plain FSM/control state, and
trace what "success" itself structurally depends on.

Run inside HAL's own embedded Python interpreter:

  ./bin/hal --python-script scripts/analyze_accumulator.py --py-args "<netlist.v> <liberty.lib>"
"""

import builtins
import sys

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/hal/build/lib/hal_plugins")
import hal_py

if len(sys.argv) != 2:
    print("usage: --python-script analyze_accumulator.py --py-args \"<netlist.v> <liberty.lib>\"")
    sys.exit(1)
NETLIST, LIBERTY = sys.argv[0], sys.argv[1]

nl = hal_py.NetlistFactory.load_netlist(NETLIST, LIBERTY)
if nl is None:
    print("FAILED TO LOAD NETLIST")
    sys.exit(1)


def is_include_worthy(g):
    return g.type.has_property(hal_py.GateTypeProperty.combinational) and not g.is_gnd_gate() and not g.is_vcc_gate()


def combinational_cone(start_net):
    """All purely-combinational gates in start_net's backward fan-in cone,
    stopping at (not crossing into) any sequential gate."""
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


def boundary_nets(gates, output_net):
    """The real input variables of the subgraph function: any net that
    feeds one of `gates` from outside the set (a register Q, a primary
    input, ...)."""
    boundary = builtins.set()
    for g in gates:
        for n in g.get_fan_in_nets():
            srcs = n.get_sources()
            if not srcs or srcs[0].get_gate() not in gates:
                boundary.add(n)
    return boundary


def find_dataflow_registers(nl):
    import dataflow

    config = dataflow.Configuration(nl)
    config.min_group_size = 2
    config.gate_types = {g.type for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)}
    if not config.gate_types:
        return []
    config.control_pin_types = {hal_py.PinType.clock, hal_py.PinType.enable, hal_py.PinType.reset, hal_py.PinType.set}

    res = dataflow.analyze(config)
    if res is None:
        return []
    return [list(ff_gates) for ff_gates in res.get_groups().values()]


def data_pin_net(ff):
    pins = [p for p in ff.type.get_input_pin_names() if p not in ("CLK", "VGND", "VPWR", "VPB", "VNB")]
    nets = [ff.get_fan_in_net(p) for p in pins if ff.get_fan_in_net(p) is not None]
    return nets


by_id = {n.id: n for n in nl.get_nets()}

groups = find_dataflow_registers(nl)
print("total dataflow register groups:", len(groups))

q_to_group = {}
for gi, ffs in enumerate(groups):
    for ff in ffs:
        q = ff.get_fan_out_nets()[0]
        q_to_group[q] = gi

for gi, ffs in enumerate(groups):
    width = len(ffs)
    own_qs = {ff.get_fan_out_nets()[0] for ff in ffs}
    self_feedback = False
    cross_group_feedback = builtins.set()
    depends_on_named = builtins.set()
    for ff in ffs:
        for d_net in data_pin_net(ff):
            cone = combinational_cone(d_net)
            for b in boundary_nets(cone, d_net) | {d_net}:
                if b in own_qs:
                    self_feedback = True
                elif b in q_to_group and q_to_group[b] != gi:
                    cross_group_feedback.add(q_to_group[b])
                if b.is_global_input_net():
                    depends_on_named.add(b.name)
    tag = "ACCUMULATOR (self-feedback)" if self_feedback else "no self-feedback"
    extra = (" cross-group<-%s" % sorted(cross_group_feedback)) if cross_group_feedback else ""
    named = (" inputs=%s" % sorted(depends_on_named)) if depends_on_named else ""
    print("group %2d: %2d flip-flop(s) - %s%s%s" % (gi, width, tag, extra, named))

print()
print("=== tracing 'success' ===")
success_nets = [n for n in nl.get_global_output_nets() if n.name == "success"]
if not success_nets:
    print("no net literally named 'success' found among global outputs")
else:
    s_net = success_nets[0]
    cone = combinational_cone(s_net)
    print("success combinational fan-in cone: %d gate(s)" % len(cone))
    boundary = boundary_nets(cone, s_net)
    reg_deps = builtins.set()
    other_deps = builtins.set()
    for b in boundary:
        if b in q_to_group:
            reg_deps.add(q_to_group[b])
        elif b.is_global_input_net():
            other_deps.add(b.name)
        else:
            other_deps.add("net_%d(unnamed)" % b.id)
    print("success depends on register groups:", sorted(reg_deps))
    print("success also directly depends on:", sorted(other_deps))
