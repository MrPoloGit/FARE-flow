"""
Reconstruct a netlist's original RTL module hierarchy as real HAL Modules,
starting from a flat, gate-level netlist only - no hardcoded gate names.

General approach (works on any similarly-structured design, not just
adder_demo):
  1. Word-level operations (adders, comparators, ...) -> module_identification.
  2. Registers -> HAL's own `dataflow` plugin (the general, timing/control-
     signal-based flip-flop grouping tool - not a hand-rolled pattern
     matcher tied to one loading style), extended with each group's direct
     combinational load logic (whatever computes each flip-flop's own D
     input - a mux for a shift/parallel-load register here, but this step
     doesn't assume that; it just absorbs immediate predecessors generically).
  3. Whatever combinational logic is left after (1) and (2) are removed is
     handed to module_identification as one group (execute_on_gates) to
     auto-classify, instead of assuming what it is.
  4. Tie cells (gnd/vcc) and any gates still unclassified after all of the
     above (clock-tree buffers etc. - genuine post-synthesis artifacts with
     no RTL equivalent) are bundled into one "physical_only" module, so the
     top module's own gate list ends up empty - only real modules remain
     visible at the top level, as requested.

Note: `dataflow` finds *which* flip-flops belong together (general - any
control-signal/successor-predecessor pattern), but not their bit *order*
within the word. That's a separate, narrower problem (see
scripts/decompile_to_rtl.py, which still uses the mux-chain trace
specifically because it needs bit order for behavioral RTL and this
design's registers happen to be shift-loaded) - not needed just to draw
correct module boundaries, which is all this script does.

Must run inside HAL's own embedded Python interpreter (module_identification's
bindings only register there):

  ./bin/hal --python-script scripts/create_rtl_modules.py

from hal/build/, not as a standalone `python3` script.
"""

import builtins
import sys

sys.path.insert(0, "/home/mrpolo/Projects/asic-reverse-engineering/hal/build/lib/hal_plugins")
import hal_py

# Note: sys.argv inside HAL's --python-script/--py-args does NOT include the
# script name as argv[0] (confirmed directly - it's just the space-separated
# --py-args values).
if len(sys.argv) != 3:
    print("usage: --python-script create_rtl_modules.py --py-args \"<netlist.v> <liberty.lib> <out.hal>\"")
    sys.exit(1)
NETLIST, LIBERTY, OUT_HAL = sys.argv[0], sys.argv[1], sys.argv[2]


def find_registers(nl):
    """Group flip-flops into registers using HAL's own `dataflow` plugin -
    general-purpose (control-signal/successor-predecessor based), not tied
    to any one loading pattern. Each group is then extended with its direct
    combinational load logic (whatever computes each flip-flop's own D
    input - absorbed generically, without assuming it's a mux). Returns a
    list of gate lists, one per register found."""
    import dataflow

    config = dataflow.Configuration(nl)
    config.min_group_size = 2
    config.gate_types = {g.type for g in nl.get_gates() if g.type.has_property(hal_py.GateTypeProperty.sequential)}
    if not config.gate_types:
        return []
    config.control_pin_types = {hal_py.PinType.clock, hal_py.PinType.enable, hal_py.PinType.reset, hal_py.PinType.set}

    res = dataflow.analyze(config)
    if res is None:
        print("dataflow.analyze() failed - no registers found")
        return []

    registers = []
    for gid, ff_gates in res.get_groups().items():
        group = builtins.set(ff_gates)
        for ff in ff_gates:
            for ep_in in ff.get_fan_in_endpoints():
                if ep_in.get_pin().name == "CLK":
                    continue    # clock-tree infrastructure, not load logic - keep it out of the register module
                n = ep_in.get_net()
                for ep in n.get_sources():
                    pred = ep.get_gate()
                    if pred.type.has_property(hal_py.GateTypeProperty.combinational) and not pred.is_gnd_gate() and not pred.is_vcc_gate():
                        group.add(pred)
        registers.append(list(group))
    return registers


nl = hal_py.NetlistFactory.load_netlist(NETLIST, LIBERTY)
if nl is None:
    print("FAILED TO LOAD NETLIST")
    sys.exit(1)

import module_identification as mi

top = nl.get_top_module()
assigned = builtins.set()

# 1) word-level ops via module_identification's own carry-chain dispatch.
config = mi.Configuration(nl)
op_result = mi.execute(config)
if op_result is None:
    print("module_identification execute() failed")
    sys.exit(1)
for key, vc in op_result.get_verified_candidates().items():
    print("word-level op: %s is_verified=%s gates=%d" % (vc.get_name(), vc.is_verified(), len(vc.gates)))
op_result.create_modules_in_netlist()
for m in top.submodules:
    assigned.update(m.gates)

# 2) registers, found via HAL's dataflow plugin (general - see find_registers()).
for i, reg_gates in enumerate(find_registers(nl)):
    reg_gates = [g for g in reg_gates if g not in assigned]
    if not reg_gates:
        continue
    name = "register_%d" % i
    m = nl.create_module(name, top, reg_gates)
    print("register: %s, %d gates" % (name, len(reg_gates)))
    assigned.update(reg_gates)


def is_clock_only_buffer(g, _cache=None):
    """A gate is pure clock-tree infrastructure if every destination of
    every output net it drives is either a sequential gate's clock pin or
    another clock-only buffer (transitive, for multi-stage clock trees).
    Checked structurally (not via GateTypeProperty.c_buffer, which this
    library's Liberty parse doesn't populate for these cells - confirmed
    empirically). Requires at least one real destination - a gate whose
    output only reaches a top-level port (no gate-level Endpoint at all)
    must not vacuously pass this check."""
    if _cache is None:
        _cache = {}
    if g in _cache:
        return _cache[g]
    _cache[g] = False    # break cycles conservatively; none expected in a real clock tree
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
            if ep.get_pin().name == "CLK":
                continue
            if is_clock_only_buffer(ep.get_gate(), _cache):
                continue
            result = False
    result = result and found_any_dest
    _cache[g] = result
    return result


# 3) whatever combinational logic remains, classified as one group.
remaining = [g for g in nl.get_gates() if g not in assigned and not g.is_gnd_gate() and not g.is_vcc_gate() and not is_clock_only_buffer(g)]
if remaining:
    rest_result = mi.execute_on_gates(remaining, mi.Configuration(nl))
    if rest_result is not None:
        verified = rest_result.get_verified_candidates()
        for key, vc in verified.items():
            print("residual logic: %s is_verified=%s gates=%d" % (vc.get_name(), vc.is_verified(), len(vc.gates)))
        if len(verified) > 0:
            rest_result.create_modules_in_netlist()
            for m in top.submodules:
                assigned.update(m.gates)

# 4) tie cells + anything still unclassified (clock-tree buffers etc. - real
#    post-synthesis artifacts, not RTL constructs) go into one physical-only
#    module so the top module's own gate list ends up empty.
leftover = [g for g in nl.get_gates() if g not in assigned]
if leftover:
    print("physical_only: %d gates (%s)" % (len(leftover), [g.type.name for g in leftover]))
    nl.create_module("physical_only", top, leftover)

print("modules in netlist after creation:")
for m in nl.get_modules():
    print("  id=%d name=%s gates=%d" % (m.id, m.name, len(m.gates)))
print("top_module direct gates (should be empty):", [g.name for g in top.gates])

if not hal_py.NetlistSerializer.serialize_to_file(nl, OUT_HAL):
    print("failed to serialize netlist")
    sys.exit(1)

print("wrote", OUT_HAL)
