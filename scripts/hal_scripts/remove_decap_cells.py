"""
Deletes every decoupling-capacitor (decap) gate from a netlist - purely
physical filler cells (a MOS capacitor tied between VPWR/VGND, inserted
during place-and-route to stabilize local supply voltage) with no logic
pins and no role in the design's function. Safe to remove unconditionally:
each one is checked to have zero fan-in and zero fan-out nets before being
deleted (true by construction once VPWR/VGND have already been stripped
via strip_verilog_power_ports.py - decap cells only ever connected to
those two nets to begin with).

Run inside HAL's own embedded Python interpreter:
  ./bin/hal --python-script remove_decap_cells.py \
    --py-args "<in.hal> <liberty.lib> <out.hal>"
"""
import sys
import hal_py

if len(sys.argv) != 3:
    print("usage: --python-script remove_decap_cells.py --py-args \"<in.hal> <liberty.lib> <out.hal>\"")
    sys.exit(1)
NETLIST, LIBERTY, OUT = sys.argv

nl = hal_py.NetlistFactory.load_netlist(NETLIST, LIBERTY)
if nl is None:
    print("FAILED TO LOAD NETLIST")
    sys.exit(1)

decap_gates = [g for g in nl.get_gates() if "decap" in g.type.name]
print("found %d decap gate(s)" % len(decap_gates))

skipped = 0
deleted = 0
for g in decap_gates:
    if g.get_fan_in_nets() or g.get_fan_out_nets():
        print("  SKIPPING %s - unexpectedly has real net connections, not deleting" % g.name)
        skipped += 1
        continue
    nl.delete_gate(g)
    deleted += 1

print("deleted %d, skipped %d (had real connections)" % (deleted, skipped))
print("remaining gates: %d" % len(nl.get_gates()))

ok = hal_py.NetlistSerializer.serialize_to_file(nl, OUT)
print("saved:", ok, "->", OUT)
