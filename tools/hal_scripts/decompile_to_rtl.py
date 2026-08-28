"""
Decompile a sky130 gate-level netlist back into behavioral RTL, driven
entirely by computation over the actual netlist - no hand-typed constants,
no assumed bit ordering, no hardcoded design/port names. General-purpose:
not tied to adder_demo specifically (see PLAN.md/notes.md task 7 for the
adder_demo-specific validation history this was developed against).

For each piece:
  - Top module ports: read directly from the netlist's own global input/
    output nets (hal_py.Netlist.get_global_input_nets/get_global_output_nets).
  - Clock/reset nets: whichever global input net drives a sequential gate's
    clock/reset-type pin (hal_py.PinType.clock/.reset), through at most one
    level of buffering - not assumed to be named "clk"/"rst_n".
  - Register chains: traced structurally (mux -> FF -> mux, same technique
    as scripts/create_rtl_modules.py), any number of chains, not fixed to 2.
    Each chain's own construction order IS its bit order (position 0 = the
    stage fed directly by the primary port).
  - Verified word-level operations: ALL of module_identification's verified
    candidates are processed generically, not just one hardcoded type name
    (module_identification's own carry-chain-anchored dispatch plus a
    residual execute_on_gates() pass for anything it can't reach that way -
    e.g. a comparator with no carry chain of its own). Each candidate's
    operand/output bit order is trusted directly from its own
    VerifiedCandidate.operands/output_nets list order (index 0 = LSB) -
    NOT independently re-derived via register-bit reachability, which was
    tried first and found to actively disagree with what
    module_identification's own SMT equivalence proof already verified
    (see scripts/trace_bit_indices.py for that general reachability
    technique - it's still the right tool for other jobs, just not this
    one; see PLAN.md task 7 for the concrete case that ruled it out here).
    VALUE_CHECK/EQUAL-style candidates' comparison constant is derived by
    brute-force evaluating the real gate-level Boolean function
    (SubgraphNetlistDecorator.get_subgraph_function) - never assumed.
  - Anything that can't be confidently bit-indexed or isn't a Verilog-
    operator-representable type is emitted as a clearly-labeled comment
    (gate count + candidate type/name) instead of silently skipped or
    aborting the whole run - on an unfamiliar design, partial output you
    can see the gaps in is much more useful than an all-or-nothing crash.

Must run inside HAL's own embedded Python interpreter:

  ./bin/hal --python-script scripts/decompile_to_rtl.py <netlist> <liberty> <out.v>

from hal/build/, not as a standalone `python3` script.
"""

import builtins
import itertools
import sys

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/hal/build/lib/hal_plugins")
import hal_py

# Note: sys.argv inside HAL's --python-script/--py-args does NOT include the
# script name as argv[0] the way a normal `python3 script.py a b c` would -
# confirmed directly (argv is just the space-separated --py-args values).
if len(sys.argv) != 3:
    print("usage: --python-script decompile_to_rtl.py --py-args \"<netlist.v> <liberty.lib> <out.v>\"")
    sys.exit(1)
NETLIST, LIBERTY, OUT_V = sys.argv[0], sys.argv[1], sys.argv[2]

import re

# A net name straight from the netlist isn't always a valid plain Verilog
# identifier - this design's own O[0]..O[7] output ports are literally
# named with brackets as flat identifiers (there's no real Verilog vector
# "wire [7:0] O" behind them), which produced invalid syntax
# ("output wire O[0],") the first time this script ran on puzzle.gds: a
# real Verilog parser reads that as a bit-select into an undeclared vector
# O, not a scalar port named "O[0]". Same fix as scripts/spice_to_verilog.py:
# Verilog's escaped-identifier syntax (leading backslash, terminated by
# whitespace) for anything that isn't a plain [A-Za-z_][A-Za-z0-9_$]*.
_SIMPLE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def escape_id(name):
    if _SIMPLE_ID_RE.match(name):
        return name
    return "\\" + name + " "


# Verilog operator mapping for candidate types this script knows how to
# render directly. Anything not in this map still gets found and reported,
# just not turned into an operator - see emit_candidate().
BINARY_OPS = {
    "ADDITION": "+",
    "SUBTRACTION": "-",
    "EQUAL": "==",
    "LESS_EQUAL": "<=",
}


def trace_register_chains(nl):
    """mux -> flip-flop chains: a chain starts at a mux whose data input is
    a primary port net, and continues as long as the next mux's data input
    is the previous stage's flip-flop Q output. Any number of chains."""
    all_gates = nl.get_gates()
    muxes = [g for g in all_gates if "mux" in g.type.name]
    ffs = {g for g in all_gates if g.type.has_property(hal_py.GateTypeProperty.sequential)}

    def data_pins(g):
        return [p for p in g.type.get_input_pin_names() if p not in ("S", "VGND", "VPWR", "VPB", "VNB")]

    def mux_data_nets(g):
        return [g.get_fan_in_net(p) for p in data_pins(g) if g.get_fan_in_net(p) is not None]

    def ff_of_mux_output(g):
        out_net = g.get_fan_out_nets()[0] if g.get_fan_out_nets() else None
        if out_net is None:
            return None
        for ep in out_net.get_destinations():
            if ep.get_gate() in ffs:
                return ep.get_gate()
        return None

    chain_starts = []
    for m in muxes:
        for n in mux_data_nets(m):
            if n.is_global_input_net() and not n.is_gnd_net() and not n.is_vcc_net():
                chain_starts.append((m, n.name))
                break

    chains = []
    used = builtins.set()
    for start, origin_port in chain_starts:
        if start in used:
            continue
        chain_muxes = [start]
        cur_ff = ff_of_mux_output(start)
        if cur_ff is None:
            continue
        chain_ffs = [cur_ff]
        while True:
            q_net = cur_ff.get_fan_out_nets()[0]
            next_mux = None
            for m in muxes:
                if m in chain_muxes:
                    continue
                if q_net in mux_data_nets(m):
                    next_mux = m
                    break
            if next_mux is None:
                break
            chain_muxes.append(next_mux)
            cur_ff = ff_of_mux_output(next_mux)
            if cur_ff is None:
                break
            chain_ffs.append(cur_ff)
        used.update(chain_muxes)
        chains.append({"origin_port": origin_port, "muxes": chain_muxes, "ffs": chain_ffs})
    return chains


def find_dataflow_registers(nl):
    """General-purpose register discovery via HAL's own `dataflow` plugin
    (control-signal/successor-predecessor grouping - the same technique
    scripts/create_rtl_modules.py uses for module boundaries), unlike
    trace_register_chains() above which only ever finds shift-loaded
    registers (mux -> FF chain anchored at a primary port). This finds
    ANY register regardless of load style - parallel-loaded included -
    but only tells you *which* flip-flops belong together, not their bit
    *order*; see order_parallel_register() below for that half. Returns a
    list of (ff_gates, load_logic_gates) pairs."""
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

    registers = []
    for _gid, ff_gates in res.get_groups().items():
        load_logic = builtins.set()
        for ff in ff_gates:
            for ep_in in ff.get_fan_in_endpoints():
                if ep_in.get_pin().name == "CLK":
                    continue
                for ep in ep_in.get_net().get_sources():
                    pred = ep.get_gate()
                    if pred.type.has_property(hal_py.GateTypeProperty.combinational) and not pred.is_gnd_gate() and not pred.is_vcc_gate():
                        load_logic.add(pred)
        registers.append((list(ff_gates), list(load_logic)))
    return registers


def build_port_bit_anchors(nl):
    """Known bit-indexed nets to anchor parallel-register bit-order
    inference against: any global input/output port literally named
    "<base>[<digit(s)>]" (e.g. this design's own O[0]..O[7] - a flat name,
    not a real Verilog vector, same as noted at escape_id() above),
    grouped by base name. Returns {net: (base_name, bit_index)}."""
    anchors = {}
    port_re = re.compile(r"^(.*)\[(\d+)\]$")
    for n in list(nl.get_global_input_nets()) + list(nl.get_global_output_nets()):
        m = port_re.match(n.name)
        if m:
            anchors[n] = (m.group(1), int(m.group(2)))
    return anchors


def build_candidate_bit_anchors(verified):
    """Bit-indexed nets from the netlist's own verified word-level
    operations (module_identification's SMT-checked ADDITION/etc. operand
    and output words), on top of build_port_bit_anchors()'s primary-port
    anchors above - some registers never reach a primary port directly at
    all (they only ever feed, or are fed by, an internal word-level
    operation - e.g. an accumulator register wired straight into the
    adder's own operand pins, with no primary port anywhere in between),
    so those need an anchor of their own to get bit-indexed. Each operand
    word and the output word of every verified candidate is trusted in its
    own list order (same trust basis as bit_index_nets() elsewhere in this
    file - see its docstring for why). Returns {net: (group_name,
    bit_index)}, one group per operand plus one for the output so two
    different operands of the same candidate can never be confused for
    each other."""
    anchors = {}
    for vc in verified:
        for k, operand in enumerate(vc.operands):
            group = "%s_operand%d" % (vc.get_name(), k)
            for b, n in enumerate(operand):
                anchors[n] = (group, b)
        group = "%s_output" % vc.get_name()
        for b, n in enumerate(vc.output_nets):
            anchors[n] = (group, b)
    return anchors


def reachable_anchors(start_net, anchors):
    """All (base_name, bit_index) anchors reachable from start_net through
    purely combinational logic, without crossing into another sequential
    gate (so this can't accidentally walk through one register into the
    bit-order of an unrelated downstream one)."""
    frontier = [start_net]
    visited_nets = builtins.set()
    visited_gates = builtins.set()
    found = []
    while frontier:
        n = frontier.pop()
        if n in visited_nets:
            continue
        visited_nets.add(n)
        if n in anchors:
            found.append(anchors[n])
            continue
        for ep in n.get_destinations():
            g = ep.get_gate()
            if g in visited_gates or g.type.has_property(hal_py.GateTypeProperty.sequential) or not is_include_worthy(g):
                continue
            visited_gates.add(g)
            for out_net in g.get_fan_out_nets():
                frontier.append(out_net)
    return found


def order_parallel_register(ffs, anchors):
    """Try to bit-index a dataflow-found register group (unordered) by
    tracing each flip-flop's Q output forward to a known bit-indexed
    anchor net. A candidate anchor group only "wins" if EVERY flip-flop in
    the group reaches exactly one of its bits, and no two flip-flops
    reach the same bit - anything ambiguous is rejected rather than
    guessed, same philosophy as the rest of this script. Returns
    (anchor_group_name, {bit_index: ff_gate}) or None."""
    per_ff_reach = {ff: reachable_anchors(ff.get_fan_out_nets()[0], anchors) for ff in ffs}
    candidate_groups = {g for reach in per_ff_reach.values() for (g, _b) in reach}
    for group_name in candidate_groups:
        bit_of_ff = {}
        used_bits = builtins.set()
        ok = True
        for ff, reach in per_ff_reach.items():
            matches = [b for (g, b) in reach if g == group_name]
            if len(matches) != 1 or matches[0] in used_bits:
                ok = False
                break
            bit_of_ff[matches[0]] = ff
            used_bits.add(matches[0])
        if ok:
            return group_name, bit_of_ff
    return None


def find_clock_and_reset(nl):
    """Whichever global input net drives a sequential gate's clock/async-
    reset pin, through at most one level of buffering. Uses the flip-flop's
    own FFComponent.clock_function/async_reset_function (real Boolean
    functions in terms of real pin names, e.g. "(! RESET_B)") rather than
    GatePin.type - confirmed empirically that PinType.reset isn't always
    populated by the Liberty parser (this design's own RESET_B pin type is
    PinType.none despite genuinely being the async reset), while
    FFComponent's functions are always populated for a real flip-flop.
    Not assumed to be named 'clk'/'rst_n'. Returns (clk_net_or_None,
    reset_net_or_None)."""

    def real_fan_in_nets(g):
        # Net.is_gnd_net()/is_vcc_net() don't reliably flag this design's
        # GND/VDD nets (confirmed empirically), so filter by pin name
        # instead - same technique already used in trace_register_chains's
        # data_pins() above.
        pins = [p for p in g.type.get_input_pin_names() if p not in ("VGND", "VPWR", "VPB", "VNB")]
        return [n for p in pins if (n := g.get_fan_in_net(p)) is not None]

    def resolve_pin_to_global_input(g, pin_name):
        n = g.get_fan_in_net(pin_name)
        for _ in range(4):    # allow a few buffer stages (this design's clock tree is 2-level)
            if n is None or n.is_global_input_net():
                return n
            srcs = n.get_sources()
            if not srcs:
                return None
            pred_in = real_fan_in_nets(srcs[0].get_gate())
            n = pred_in[0] if len(pred_in) == 1 else None
        return n if n is not None and n.is_global_input_net() else None

    clk, rst = None, None
    for g in nl.get_gates():
        if not g.type.has_property(hal_py.GateTypeProperty.sequential):
            continue
        comp = g.type.get_component(lambda c: hal_py.FFComponent.is_class_of(c))
        if comp is None:
            continue
        if clk is None:
            for pin_name in comp.get_clock_function().get_variable_names():
                clk = resolve_pin_to_global_input(g, pin_name) or clk
        if rst is None:
            for pin_name in comp.get_async_reset_function().get_variable_names():
                rst = resolve_pin_to_global_input(g, pin_name) or rst
        if clk is not None and rst is not None:
            break
    return clk, rst


def is_include_worthy(g):
    return g.type.has_property(hal_py.GateTypeProperty.combinational) and not g.is_gnd_gate() and not g.is_vcc_gate()


def bit_index_by_reachability(net, ff_q_to_bit):
    """Max register bit reachable in net's combinational fan-in cone."""
    frontier = [net]
    visited_nets = builtins.set()
    visited_gates = builtins.set()
    best = None
    while frontier:
        n = frontier.pop()
        if n in visited_nets:
            continue
        visited_nets.add(n)
        if n in ff_q_to_bit:
            _chain, pos = ff_q_to_bit[n]
            best = pos if best is None else max(best, pos)
            continue
        src_ep_list = n.get_sources()
        if not src_ep_list:
            continue
        src_gate = src_ep_list[0].get_gate()
        if src_gate in visited_gates or not is_include_worthy(src_gate):
            continue
        visited_gates.add(src_gate)
        for in_net in src_gate.get_fan_in_nets():
            frontier.append(in_net)
    return best


def bit_index_nets(nets):
    """Bit-index a VerifiedCandidate's operand/output net LIST directly by
    its own list order (index 0 = LSB). This is NOT re-derived via
    reachability - it's exactly the ordering module_identification's own
    SMT equivalence check already verified is correct
    (build_input_operands()/order_output_signals() in
    create_functional_candidates.cpp; confirmed by reading that code that
    ADDITION's own operand order comes from bucket iteration there, not a
    later re-ordering step). Re-deriving it independently via reachability
    was tried first and found to actively disagree with this in a real
    case - see PLAN.md task 7 for the concrete example (a register-bit
    reachability measure can't tell "operand bit 3" apart between the two
    operands the way HAL's own influence-bucket construction already did,
    since both registers' same-significance bits reach the same set of
    output bits)."""
    return {i: n for i, n in enumerate(nets)}


def derive_constant(nl, gates, output_net, bits_by_net):
    """Brute-force the real gate-level Boolean function to find the exact
    input combination that makes output_net true - never assumed."""
    decorator = hal_py.SubgraphNetlistDecorator(nl)
    bf = decorator.get_subgraph_function(list(gates), output_net)
    var_names = bf.get_variable_names()
    by_id = {n.id: n for n in nl.get_nets()}
    var_bit = {}
    for v in var_names:
        n = by_id[int(v.split("_")[1])]
        var_bit[v] = bits_by_net.get(n)
    if any(b is None for b in var_bit.values()):
        return None

    found = None
    for combo in itertools.product([hal_py.BooleanFunction.Value.ZERO, hal_py.BooleanFunction.Value.ONE], repeat=len(var_names)):
        inputs = dict(zip(var_names, combo))
        if bf.evaluate(inputs) != hal_py.BooleanFunction.Value.ONE:
            continue
        value = 0
        for v, val in inputs.items():
            if val == hal_py.BooleanFunction.Value.ONE:
                value |= 1 << var_bit[v]
        if found is not None and found != value:
            return None    # not a single-value check - can't represent as "== constant"
        found = value
    return found


def power_constant(n):
    """"1'b0"/"1'b1" if n is structurally a ground/power net, else None.
    Two real shapes both need covering, confirmed empirically (neither
    Net.is_gnd_net()/is_vcc_net() reliably flags either - both checked and
    ruled out directly, not assumed):
      1. An internal tie net synthesized for constant-padding (e.g. an
         "ADDITION" operand's width-extension bit) - driven by a single
         gate that is itself a tie cell (Gate.is_gnd_gate()/is_vcc_gate()).
      2. A literal top-level GND/VDD *port* net (this design declares
         these as primary ports rather than tying them to an internal tie
         cell) - has zero sources, identified instead by every one of its
         destinations being a power/ground *pin type*."""
    srcs = n.get_sources()
    if len(srcs) == 1:
        driver = srcs[0].get_gate()
        if driver.is_gnd_gate():
            return "1'b0"
        if driver.is_vcc_gate():
            return "1'b1"
    dests = n.get_destinations()
    if dests and all(ep.get_pin().type == hal_py.PinType.ground for ep in dests):
        return "1'b0"
    if dests and all(ep.get_pin().type == hal_py.PinType.power for ep in dests):
        return "1'b1"
    return None


def net_literal(n, net_to_wire):
    """A net's Verilog reference for use inside this script's brand-new
    standalone module: a known register-bit wire, a power_constant(), or
    None if it's neither (an arbitrary internal net from the source
    netlist has no valid meaning as a bare identifier in a freshly-written
    module)."""
    if n in net_to_wire:
        return net_to_wire[n]
    return power_constant(n)


def emit_candidate(nl, vc, net_to_wire):
    """Return a list of Verilog lines for one verified candidate, or a
    single explanatory comment line if it can't be auto-rendered."""
    name = vc.get_name()
    gates = vc.gates

    # operand/output bit order is each list's own index (0 = LSB) - see
    # bit_index_nets()'s docstring for why this is trusted directly
    # instead of re-derived.
    operand_words = [bit_index_nets(op) for op in vc.operands]
    out_bits = bit_index_nets(vc.output_nets)

    def word_expr(bits):
        width = max(bits.keys()) + 1
        literals = [net_literal(bits[b], net_to_wire) for b in range(width - 1, -1, -1)]
        if any(l is None for l in literals):
            return None
        return "{" + ", ".join(literals) + "}"

    out_width = max(out_bits.keys()) + 1
    out_wire = "w_%s_out" % name.lower()

    def register_output_bits(width):
        # so later candidates whose operands are THIS candidate's outputs
        # (e.g. a comparator reading an adder's sum) can resolve them too.
        for b in range(width):
            net_to_wire[out_bits[b]] = "%s[%d]" % (out_wire, b) if width > 1 else out_wire

    if name in ("EQUAL", "LESS_EQUAL", "VALUE_CHECK") and len(operand_words) <= 1:
        # comparison against a derived constant, not another operand. The
        # constant's own bit width is the OPERAND's width (the value being
        # compared), not out_width (the 1-bit boolean comparison result) -
        # a real bug caught by inspecting the first real output: it
        # rendered as the nonsensical "1'd103" before this fix.
        compared_bits = operand_words[0] if operand_words else out_bits
        compared_width = max(compared_bits.keys()) + 1
        global_outputs = [n for n in vc.output_nets if n.is_global_output_net()]
        output_net = global_outputs[0] if global_outputs else vc.output_nets[0]
        operand_bits_by_net = {n: b for b, n in compared_bits.items()}
        const = derive_constant(nl, gates, output_net, operand_bits_by_net)
        if const is None:
            return ["    // %s (%d gates): could not derive a single comparison constant - not auto-decompiled" % (name, len(gates))]
        operand_expr = word_expr(compared_bits)
        if operand_expr is None:
            return ["    // %s (%d gates): an operand bit isn't a known register bit or constant - not auto-decompiled" % (name, len(gates))]
        register_output_bits(out_width)
        return ["    wire %s = (%s == %d'd%d);" % (out_wire, operand_expr, compared_width, const)]

    if name in BINARY_OPS and len(operand_words) == 2:
        expr0, expr1 = word_expr(operand_words[0]), word_expr(operand_words[1])
        if expr0 is None or expr1 is None:
            return ["    // %s (%d gates): an operand bit isn't a known register bit or constant - not auto-decompiled" % (name, len(gates))]
        register_output_bits(out_width)
        return ["    wire [%d:0] %s = %s %s %s;" % (out_width - 1, out_wire, expr0, BINARY_OPS[name], expr1)]

    return ["    // %s (%d gates, %d operand(s)): recognized but no Verilog-operator mapping in this script - not auto-decompiled" % (name, len(gates), len(operand_words))]


def is_clock_only_buffer(g, clk_net, _cache=None):
    if _cache is None:
        _cache = {}
    if g in _cache:
        return _cache[g]
    _cache[g] = False
    if g.type.has_property(hal_py.GateTypeProperty.sequential):
        return False
    out_nets = g.get_fan_out_nets()
    if not out_nets:
        return False
    found_any_dest = False
    result = True
    for n in out_nets:
        for ep in n.get_destinations():
            found_any_dest = True
            if ep.get_pin().type == hal_py.PinType.clock:
                continue
            if is_clock_only_buffer(ep.get_gate(), clk_net, _cache):
                continue
            result = False
    result = result and found_any_dest
    _cache[g] = result
    return result


nl = hal_py.NetlistFactory.load_netlist(NETLIST, LIBERTY)
if nl is None:
    print("FAILED TO LOAD NETLIST")
    sys.exit(1)

import module_identification as mi

top_name = nl.get_top_module().get_type()

clk_net, rst_net = find_clock_and_reset(nl)
print("clock net:", clk_net.name if clk_net else None, " reset net:", rst_net.name if rst_net else None)

chains = trace_register_chains(nl)
for chain in chains:
    print("register chain from port %s: %d stages" % (chain["origin_port"], len(chain["ffs"])))

ff_q_to_bit = {}
for chain in chains:
    for i, ff in enumerate(chain["ffs"]):
        ff_q_to_bit[ff.get_fan_out_nets()[0]] = (chain["origin_port"], i)

config = mi.Configuration(nl)
result = mi.execute(config)
verified = list(result.get_verified_candidates().values()) if result is not None else []
print("verified word-level candidates:", [vc.get_name() for vc in verified])

net_to_wire = {}
body_lines = []

for chain in chains:
    reg_name = "reg_%s" % chain["origin_port"]
    width = len(chain["ffs"])
    body_lines.append("    reg [%d:0] %s;" % (width - 1, reg_name))
    if clk_net is not None:
        cond = " or negedge %s" % escape_id(rst_net.name) if rst_net is not None else ""
        body_lines.append("    always @(posedge %s%s) begin" % (escape_id(clk_net.name), cond))
        if rst_net is not None:
            body_lines.append("        if (!%s)" % escape_id(rst_net.name))
            body_lines.append("            %s <= %d'b0;" % (reg_name, width))
            body_lines.append("        else")
        body_lines.append("            %s <= {%s[%d:0], %s};" % (reg_name, reg_name, width - 2, escape_id(chain["origin_port"])))
        body_lines.append("    end")
    body_lines.append("")
    for i, ff in enumerate(chain["ffs"]):
        net_to_wire[ff.get_fan_out_nets()[0]] = "%s[%d]" % (reg_name, i)

assigned = builtins.set()
for chain in chains:
    assigned.update(chain["muxes"])
    assigned.update(chain["ffs"])

# Parallel-loaded registers: trace_register_chains() above only ever finds
# shift-loaded registers (mux -> FF chain anchored at a primary port - see
# its own docstring). A register that loads a whole word at once from
# internal combinational logic (an accumulator, FSM state, ...) has no
# such chain to anchor on, so it's invisible to that method regardless of
# how well-formed it is - this was the concrete gap flagged in README.md's
# "Known limits". Recovered here in two steps: (1) find every register
# HAL's own dataflow plugin can identify (control-signal grouping, doesn't
# need bit order), same technique create_rtl_modules.py already uses for
# module boundaries; (2) for whichever of those aren't already covered by
# a shift chain, try to recover bit order by tracing each flip-flop's Q
# output forward to a bit-indexed output port (O[0]..O[7] in this design).
# Only registers where EVERY bit resolves unambiguously get bit-indexed
# and declared; anything left ambiguous is deliberately left alone rather
# than guessed, so it still shows up honestly in the final "not covered"
# count - no attempt is made to reconstruct the register's own load logic
# (the combinational equation driving its D inputs), only its bit order.
#
# Anchors come from two independent sources - a primary port
# (build_port_bit_anchors) or a verified word-level operation's own
# trusted operand/output bit order (build_candidate_bit_anchors) - since a
# register can be structurally "paired" with either. Once a group resolves
# against either kind, its own Q nets are folded into the anchor set too,
# so a THIRD register wired directly into an already-solved one (no port,
# no verified operation of its own) can still resolve on a later pass -
# run to a fixpoint rather than a single pass for exactly that reason.
anchors = build_port_bit_anchors(nl)
anchors.update(build_candidate_bit_anchors(verified))
already_ff = builtins.set()
for chain in chains:
    already_ff.update(chain["ffs"])

pending = []
for ff_gates, load_logic_gates in find_dataflow_registers(nl):
    ffs = [g for g in ff_gates if g.type.has_property(hal_py.GateTypeProperty.sequential) and g not in already_ff]
    if ffs:
        pending.append(ffs)

parallel_reg_idx = 0
progress = True
while progress:
    progress = False
    still_pending = []
    for ffs in pending:
        ordered = order_parallel_register(ffs, anchors)
        if ordered is None:
            still_pending.append(ffs)
            continue
        anchor_name, bit_to_ff = ordered
        width = len(ffs)
        reg_name = "preg_%s_%d" % (re.sub(r"[^A-Za-z0-9_]", "_", anchor_name), parallel_reg_idx)
        parallel_reg_idx += 1
        print("register (%d flip-flops): bit order inferred via '%s' -> %s" % (width, anchor_name, reg_name))
        body_lines.append("    // parallel-loaded register, bit order inferred from '%s' - load logic not auto-decompiled" % anchor_name)
        body_lines.append("    reg [%d:0] %s;" % (width - 1, reg_name))
        body_lines.append("")
        for b, ff in bit_to_ff.items():
            q_net = ff.get_fan_out_nets()[0]
            net_to_wire[q_net] = "%s[%d]" % (reg_name, b)
            anchors[q_net] = (reg_name, b)
        # Only the flip-flops themselves are marked "explained" here (we
        # know each one's identity: bit b of this register) - NOT the
        # group's own load logic, since the D-input equation was never
        # actually rendered. Marking them assigned would silently drop
        # real unexplained logic from the final "not covered" count.
        assigned.update(ffs)
        progress = True
    pending = still_pending

for ffs in pending:
    print("register (%d flip-flops): could not infer bit order - not auto-decompiled" % len(ffs))

for vc in verified:
    lines = emit_candidate(nl, vc, net_to_wire)
    body_lines.extend(lines)
    body_lines.append("")
    assigned.update(vc.gates)

# mi.execute()'s dispatch is carry-chain-anchored (see PLAN.md task 7), so
# it only ever finds operations built around a recognized carry chain
# (ADDITION here). Anything else - e.g. this design's comparator, which has
# no carry chain of its own at all - is invisible to it. Feed whatever's
# left after the first pass to execute_on_gates() as one group and let the
# same generic verification pipeline classify it directly; repeat until
# nothing new is found, in case multiple independent residual structures
# exist (comparator + something else, say).
while True:
    residual = [g for g in nl.get_gates() if g not in assigned and not g.is_gnd_gate() and not g.is_vcc_gate() and not is_clock_only_buffer(g, clk_net)]
    if not residual:
        break
    residual_result = mi.execute_on_gates(residual, mi.Configuration(nl))
    residual_verified = list(residual_result.get_verified_candidates().values()) if residual_result is not None else []
    if not residual_verified:
        break
    for vc in residual_verified:
        lines = emit_candidate(nl, vc, net_to_wire)
        body_lines.extend(lines)
        body_lines.append("")
        assigned.update(vc.gates)

remaining = [g for g in nl.get_gates() if g not in assigned and not g.is_gnd_gate() and not g.is_vcc_gate() and not is_clock_only_buffer(g, clk_net)]
if remaining:
    body_lines.append("    // %d gate(s) not covered by any verified word-level candidate or traced register - not auto-decompiled" % len(remaining))
    body_lines.append("")

def is_tie_net(n):
    return power_constant(n) is not None


real_inputs = [n for n in nl.get_global_input_nets() if not is_tie_net(n)]
real_outputs = [n for n in nl.get_global_output_nets() if not is_tie_net(n)]

port_lines = []
for n in real_inputs:
    port_lines.append("    input  wire %s," % escape_id(n.name))
for i, n in enumerate(real_outputs):
    sep = "," if i < len(real_outputs) - 1 else ""
    port_lines.append("    output wire %s%s" % (escape_id(n.name), sep))
if port_lines and port_lines[-1].endswith(","):
    port_lines[-1] = port_lines[-1][:-1]

lines = []
lines.append("// Behavioral RTL decompiled from the gate-level netlist by")
lines.append("// scripts/decompile_to_rtl.py. Every structural fact below (ports, clock/")
lines.append("// reset, register bit order, operand/output bit assignment, and any derived")
lines.append("// constants) was computed from the netlist itself - nothing here is asserted")
lines.append("// from outside knowledge of the design. Comments mark anything found but not")
lines.append("// automatically renderable as an operator - inspect those manually.")
lines.append("")
lines.append("module %s (" % top_name)
lines.extend(port_lines)
lines.append(");")
lines.append("")
lines.extend(body_lines)
lines.append("endmodule")

with open(OUT_V, "w") as f:
    f.write("\n".join(lines) + "\n")

print("wrote", OUT_V)
