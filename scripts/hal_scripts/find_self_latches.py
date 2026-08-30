"""
Finds every flip-flop whose D-input equation has the self-latching
"sticky flag" shape: D = (...) | Q  (i.e. once Q goes high, it stays high
forever regardless of any other input). This is the same shape flagged by
hand in print_equations.py's output for one flip-flop (labeled `g4[0]`
there) - this script finds ALL of them directly and by real gate name
(dataflow group labels aren't stable across runs, gate names are), so you
can select the right gate in the GUI without guessing which `register_N`
module it landed in.

Detection is algebraic, not a text-shape guess: for each flip-flop, take
its D-function, substitute its own Q variable with the constant TRUE, then
brute-force-check every remaining input combination. If D(Q=1, anything)
== 1 for every possible input, that's a tautology once latched - exactly
the self-latch condition. (Tried `simplify_local()` first to prove this
algebraically instead of brute force - it does NOT fully collapse
absorption (`x | 1 = 1`) when the `1` ends up nested inside the tree, it
just substitutes and leaves the constant sitting there unreduced. Confirmed
directly by inspecting several substituted-but-unsimplified results.
Brute force over the small number of remaining variables in these
already-cone-size-limited equations is cheap and exact instead.)

Run either:
  - inside the GUI's own embedded Python console (paste the body below,
    or use %run/exec on this file) for interactive selection, or
  - headless for a quick report:
      ./bin/hal --python-script find_self_latches.py \
        --py-args "<netlist.v_or_.hal> <liberty.lib>"
"""
import builtins
import sys

import hal_py

if len(sys.argv) != 2:
    print("usage: --python-script find_self_latches.py --py-args \"<netlist> <liberty.lib>\"")
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


def d_net_of(ff):
    pins = [pn for pn in ff.type.get_input_pin_names() if pn not in ("CLK", "VGND", "VPWR", "VPB", "VNB", "RESET_B", "SET_B")]
    nets = [ff.get_fan_in_net(pn) for pn in pins if ff.get_fan_in_net(pn) is not None]
    return nets[0]


ffs = [g for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)]
decorator = hal_py.SubgraphNetlistDecorator(nl)

p("checking %d flip-flop(s) for the self-latch shape D = (...) | Q ..." % len(ffs))

TRUE = hal_py.BooleanFunction.Const(hal_py.BooleanFunction.Value.ONE)
ONE = hal_py.BooleanFunction.Value.ONE
ZERO = hal_py.BooleanFunction.Value.ZERO
CONE_SIZE_LIMIT = 60   # same threshold print_equations.py uses
VAR_COUNT_LIMIT = 18   # cap brute-force truth-table size to 2**18 evaluations

found = []
n_skipped_cone = 0
n_skipped_vars = 0
for i, ff in builtins.enumerate(ffs):
    q_net = ff.get_fan_out_nets()[0]
    q_var = "net_%d" % q_net.id
    d = d_net_of(ff)
    cone = combinational_cone(d)
    p("[%d/%d] %s: %d-gate cone" % (i + 1, len(ffs), ff.name, len(cone)))
    if len(cone) > CONE_SIZE_LIMIT:
        n_skipped_cone += 1
        continue
    bf = decorator.get_subgraph_function(list(cone), d)

    if q_var not in bf.get_variable_names():
        continue  # can't be self-latching if D doesn't even depend on its own Q

    with_q1 = bf.substitute(q_var, TRUE)
    if with_q1 is None:
        continue

    remaining_vars = builtins.sorted(with_q1.get_variable_names())
    if len(remaining_vars) > VAR_COUNT_LIMIT:
        n_skipped_vars += 1
        continue

    is_tautology = True
    for bits in builtins.range(1 << len(remaining_vars)):
        inputs = {}
        for j, var in builtins.enumerate(remaining_vars):
            inputs[var] = ONE if (bits >> j) & 1 else ZERO
        if with_q1.evaluate(inputs) != ONE:
            is_tautology = False
            break
    if is_tautology:
        found.append((ff, q_net, cone, bf))

p("(skipped %d flip-flop(s) with cones over %d gates, %d with over %d remaining variables)"
  % (n_skipped_cone, CONE_SIZE_LIMIT, n_skipped_vars, VAR_COUNT_LIMIT))

p("")
p("=== SELF-LATCHING FLIP-FLOPS: %d found ===" % len(found))
for ff, q_net, cone, bf in found:
    module = ff.get_module()
    p("gate: %s  (Q net: %s [id %d], %d-gate D-cone, module: %s[ID:%d])"
      % (ff.name, q_net.name, q_net.id, len(cone), module.name, module.id))
