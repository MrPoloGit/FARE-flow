"""
Investigative script (not part of the main decompile pipeline): print the
one-cycle D-input update equation for every flip-flop in the design, in
readable form, as a starting point for manual/structural analysis in the
HAL GUI - this is the same D-function extraction machinery built for
scripts/solve_success.py's (unsuccessful) SMT-solving attempt, just
printed instead of unrolled/handed to a solver. See notes.md's "Attempting
to solve for the puzzle.gds password via SMT" section for why the
automated approach was abandoned.

Each flip-flop is labeled by its HAL dataflow group (same grouping
scripts/analyze_accumulator.py uses) and bit position within that group
where determinable, e.g. "g8[0]" = group 8, flip-flop 0 of however many
dataflow found in that group (group membership does NOT imply a known bit
order - see decompile_to_rtl.py's own README-documented caveats on that).
Boundary variables are translated to these same "g<N>[<i>]" labels where
they're a known flip-flop's Q net, or to the primary port name (I/enable/
clk/rst_n) where applicable; anything else is left as "net_<id>" for
manual lookup in the netlist.

Run inside HAL's own embedded Python interpreter:

  ./bin/hal --python-script scripts/print_equations.py --py-args "<netlist.v> <liberty.lib>"
"""

import builtins
import sys

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/hal/build/lib/hal_plugins")
import hal_py

if len(sys.argv) != 2:
    print("usage: --python-script print_equations.py --py-args \"<netlist.v> <liberty.lib>\"")
    sys.exit(1)
NETLIST, LIBERTY = sys.argv


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

I_net = named_global_input("I")
enable_net = named_global_input("enable")
clk_net = named_global_input("clk")
rst_net = named_global_input("rst_n")
success_candidates = [n for n in nl.get_global_output_nets() if n.name == "success"]
success_net = success_candidates[0] if success_candidates else None

port_label = {}
if I_net:
    port_label[I_net] = "I"
if enable_net:
    port_label[enable_net] = "enable"
if clk_net:
    port_label[clk_net] = "clk"
if rst_net:
    port_label[rst_net] = "rst_n"


def d_net_of(ff):
    pins = [pn for pn in ff.type.get_input_pin_names() if pn not in ("CLK", "VGND", "VPWR", "VPB", "VNB", "RESET_B", "SET_B")]
    nets = [ff.get_fan_in_net(pn) for pn in pins if ff.get_fan_in_net(pn) is not None]
    return nets[0]


def has_reset(ff):
    comp = ff.type.get_component(lambda c: hal_py.FFComponent.is_class_of(c))
    return comp is not None and not comp.get_async_reset_function().is_empty()


def find_dataflow_registers(nl):
    import dataflow

    config = dataflow.Configuration(nl)
    config.min_group_size = 2
    config.gate_types = {g.type for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)}
    config.control_pin_types = {hal_py.PinType.clock, hal_py.PinType.enable, hal_py.PinType.reset, hal_py.PinType.set}
    res = dataflow.analyze(config)
    if res is None:
        return []
    return [list(ff_gates) for ff_gates in res.get_groups().values()]


ffs = [g for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)]
p("total flip-flops:", len(ffs))

groups = find_dataflow_registers(nl)
p("total dataflow groups:", len(groups))

q_net = {}
label = {}
group_of_ff = {}
for gi, group_ffs in enumerate(groups):
    for bit, ff in enumerate(group_ffs):
        q = ff.get_fan_out_nets()[0]
        q_net[ff] = q
        label[q] = "g%d[%d]" % (gi, bit)
        group_of_ff[ff] = gi

# any flip-flop dataflow's min_group_size=2 filtered out as a singleton
ungrouped_idx = 0
for ff in ffs:
    if ff not in q_net:
        q = ff.get_fan_out_nets()[0]
        q_net[ff] = q
        label[q] = "solo%d" % ungrouped_idx
        ungrouped_idx += 1

decorator = hal_py.SubgraphNetlistDecorator(nl)


import re


CONE_SIZE_LIMIT = 60


def format_bf(ff):
    d = d_net_of(ff)
    cone = combinational_cone(d)
    if len(cone) > CONE_SIZE_LIMIT:
        # a couple of flip-flops in this design have ~150-gate cones that
        # even after simplify_local() print as 1-2 MILLION character
        # single lines (confirmed directly) - not simplifiable down to
        # anything human-readable this way. Note it and move on rather
        # than block on it; inspect these directly in the GUI instead.
        return "%d gate(s): TOO LARGE TO PRINT USEFULLY - inspect in GUI" % len(cone)
    bf = decorator.get_subgraph_function(list(cone), d)
    # one simplify_local() call per flip-flop's OWN (small, fixed-size)
    # cone - cheap, unlike the repeated-across-cycles simplification that
    # caused real problems in scripts/solve_success.py's unrolling. The
    # raw unsimplified string form was unreadable (400,000+ characters for
    # the larger cones - confirmed directly, not a guess).
    bf = bf.simplify_local()
    text = str(bf)
    for var in bf.get_variable_names():
        net_id = int(var.split("_")[1])
        net = by_id[net_id]
        if net in port_label:
            replacement = port_label[net]
        elif net in label:
            replacement = label[net]
        else:
            replacement = "net_%d(%s)" % (net_id, net.name)
        # word-boundary replace - a naive str.replace() would corrupt
        # "net_50" when replacing the substring "net_5"
        text = re.sub(r"\b%s\b" % re.escape(var), replacement, text)
    return "%d gate(s): %s" % (len(cone), text)


ff_of_q = {}
for ff in ffs:
    ff_of_q[q_net[ff]] = ff

if success_net is not None and success_net in ff_of_q:
    success_ff = ff_of_q[success_net]
    p("")
    p("=== success (%s) ===" % label.get(success_net, "?"))
    p(format_bf(success_ff))
else:
    p("WARNING: success net not resolved to a flip-flop - skipping its equation")

p("")
p("=== all flip-flop update equations, by group ===")
for gi, group_ffs in enumerate(groups):
    p("--- group %d (%d flip-flop(s)) ---" % (gi, len(group_ffs)))
    for bit, ff in enumerate(group_ffs):
        p("  %s <= %s" % (label[q_net[ff]], format_bf(ff)))
