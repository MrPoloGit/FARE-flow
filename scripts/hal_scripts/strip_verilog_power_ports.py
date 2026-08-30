r"""
Removes every `.VGND(VGND)` / `.VPWR(VPWR)` named port connection from a
gate-level Verilog netlist, as the other half of decluttering VPWR/VGND out
of the HAL GUI's graph view (see strip_liberty_pg_pins.py for the other
half and why both are needed together).

Why both files need editing together: HAL's liberty_parser turns each
Liberty `pg_pin` block into a real input pin on the resulting GateType, and
stripping only that side (so the GateType no longer has a VGND/VPWR pin)
breaks netlist import - the Verilog parser fails with "failed to assign
net 'VGND' to pin 'VGND' as it is not a pin of gate ... : unable to
instantiate top module" (confirmed directly). Removing the matching named
port connections from the Verilog instance lines keeps both sides
consistent, so import succeeds with the design's real function completely
unchanged (VGND/VPWR never carried functional information to begin with -
every gate instance connects the identical two global tie nets).

Two-pass approach (position-independent, handles VGND/VPWR appearing
anywhere in a port list - first, middle, last, or even the *only* port,
which decap/tie cells like `sky130_fd_sc_hd__decap_3` hit in practice):
pass 1 deletes the bare `.VGND(VGND)`/`.VPWR(VPWR)` text with no attempt
at comma bookkeeping; pass 2 cleans up whatever comma artifacts that left
behind (`(, ...)` -> `(...)`, `(... ,)` -> `(...)`, `,,` -> `,`).

Usage:
  python3 strip_verilog_power_ports.py <in.v> <out.v>
"""
import re
import sys

if len(sys.argv) != 3:
    print("usage: strip_verilog_power_ports.py <in.v> <out.v>")
    sys.exit(1)

IN_PATH, OUT_PATH = sys.argv[1], sys.argv[2]

text = open(IN_PATH).read()

# pass 1: delete the bare pin connections themselves
text, n = re.subn(r"\.(VGND|VPWR)\(\s*(VGND|VPWR)\s*\)", "", text)

# pass 2: clean up comma artifacts left behind, regardless of where the
# removed connection(s) sat in the port list. Both VGND and VPWR can be
# removed from the same instance (the common case), so a run of 2+
# commas can appear, not just a single doubled pair - collapse the whole
# run, not just one adjacent pair.
text = re.sub(r",(?:\s*,)+", ",", text)  # 2+ ports removed back-to-back
text = re.sub(r"\(\s*,\s*", "(", text)   # was the first port
text = re.sub(r",\s*\)", ")", text)      # was the last port

open(OUT_PATH, "w").write(text)
print("removed %d power/ground port connection(s): %s -> %s" % (n, IN_PATH, OUT_PATH))
