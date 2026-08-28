#!/usr/bin/env python3
"""
Clean LGE's raw Verilog output so it actually parses in HAL *and* wires
correctly once loaded.

Raw LGE output fails HAL's Verilog parser for two reasons:
  1. Net/pin names contain literal '/' characters (LGE's hierarchical
     naming scheme), which are invalid in unescaped Verilog identifiers.
  2. LGE emits one trailing self-referential "stub" module per gate type
     used (an output artifact), which collides with HAL's real gate
     library definitions of the same name and breaks import with errors
     like "not a port of module '<celltype>'".

Naively truncating away those stub modules (the original version of this
script) creates a THIRD, much worse problem that parses cleanly but silently
produces a wrongly-wired netlist: the top module instantiates gates with
*positional* connections (no module locally defines those cell types once
the stubs are gone), so HAL falls back to resolving each position against
the pin order declared in the real sky130 Liberty file. LGE's own assumed
pin order (visible in each stub module's port list, e.g.
"module sky130_fd_sc_hd__dfrtp_2 ( Q, RESET_B, D, CLK, GND, VDD );") does
not match Liberty's declaration order for every cell type - e.g. dfrtp_2's
real Liberty order is (VGND, VPWR, CLK, D, Q, RESET_B). The mismatch
silently scrambles pin connections (confirmed on dfrtp_2 and clkbuf_16: CLK
pins ended up wired to mux outputs, Q tied to GND, RESET_B tied to VDD).
Gate identity/type is unaffected (so placement matching via
build_locations.py, which never goes through HAL's parser, was never
affected), but any semantic/connectivity analysis inside HAL was silently
wrong.

Fix: before truncating away the stub modules, recover each cell type's
LGE-assumed pin order from its own stub header and rewrite every positional
gate instantiation in the top module to named '.PIN(net)' connections using
that order. Named connections are unambiguous regardless of what order
Liberty declares its pins in, so this removes the dependency on Liberty
pin-order matching LGE's assumption entirely. A handful of cell types (the
complex AOI/OAI cells: a21o_2, a21bo_2, a21boi_2, and3_2, o21bai_2) have a
second, independent bug where the stub header's "pin names" are themselves
net-name strings instead of real pin names - for those, the real signal pin
order is pulled from 05_extracted.spice's own '.subckt' header instead
(confirmed to be what LGE's positional arguments actually follow, for every
cell type, not just the broken ones).

See notes.md ("HAL import - issues hit and fixed") and PLAN.md (task 1 / the
pin-order bug) for the session history behind this.
"""

import re
import sys

MODULE_HEADER_RE = re.compile(r"module\s+(\w+)\s*\(([^;]*)\)\s*;")
INSTANCE_RE = re.compile(r"^(\s*)(\w+)\s+(\w+)\s*\(([^;]*)\)\s*;\s*$")
KEYWORD_PREFIXES = ("module", "input", "output", "inout", "wire", "endmodule", "//")

# LGE/convert_to_lge.py renamed the real Liberty power-pin names (VPWR/VPB
# -> VDD, VGND/VNB -> GND) when producing the SPICE fed to LGE, so every
# stub module header declares its power pins as "GND"/"VDD". HAL's gate
# library only exposes the real Liberty pin names (VGND/VPWR) as actual
# ports - named connections must target those, not LGE's renamed aliases.
# This only renames the PIN NAME side of a named connection; the net it's
# tied to (typically also literally named GND/VDD at the top level) is
# untouched.
PIN_NAME_TO_LIBERTY = {"GND": "VGND", "VDD": "VPWR"}

POWER_PIN_NAMES = {"VGND", "VPWR", "VPB", "VNB"}
SUBCKT_RE = re.compile(r"^\.subckt\s+(\S+)\s+(.*)$", re.IGNORECASE)


def join_spice_continuations(lines):
    joined = []
    for line in lines:
        if line.startswith("+") and joined:
            joined[-1] = joined[-1].rstrip("\n") + " " + line[1:].lstrip()
        else:
            joined.append(line)
    return joined


def parse_spice_signal_pin_order(spice_text: str):
    """Return {cell_type: [signal_pin_names_in_declared_order]} from
    05_extracted.spice's own '.subckt <celltype> <pin1> <pin2> ...' headers
    - the authoritative, GDS-derived pin order, needed as a fallback for the
    small number of cell types (the complex AOI/OAI cells: a21o_2, a21bo_2,
    a21boi_2, and3_2, o21bai_2) whose LGE stub header uses net names instead
    of real pin names as its "port list" (a genuine LGE output quirk - see
    module docstring). Confirmed by cross-checking a21bo_2 (SPICE order
    "B1_N A2 X A1 VGND VPWR VPB VNB") against LGE's raw positional
    instantiation args, and by confirming the *correctly*-labeled stub
    headers (dfrtp_2, clkbuf_16, ...) also match this same SPICE order
    (not Liberty's unrelated declaration order) - LGE's positional
    arguments always follow SPICE's real subckt order; only a handful of
    stub header *labels* are corrupted."""
    orders = {}
    for line in join_spice_continuations(spice_text.splitlines()):
        m = SUBCKT_RE.match(line.strip())
        if not m:
            continue
        cell_type, pins_str = m.groups()
        pins = [p for p in pins_str.split() if p not in POWER_PIN_NAMES]
        orders[cell_type] = pins
    return orders


def looks_like_net_name(pin_name: str) -> bool:
    return "/" in pin_name


def parse_pin_orders(text: str):
    """Return {cell_type: [pin_names_in_port_order]} from every 'module
    <name> ( p1, p2, ... );' header in the file, including the top module
    and every LGE-emitted per-gate-type stub module."""
    orders = {}
    for cell_type, portlist in MODULE_HEADER_RE.findall(text):
        pins = [p.strip() for p in portlist.split(",") if p.strip()]
        orders[cell_type] = pins
    return orders


def resolve_pin_names(cell_type: str, header_pin_names: list, spice_signal_orders: dict):
    """Return the real pin name for each position, substituting in the
    SPICE-declared signal pin name wherever the stub header's own name
    looks like a net name instead of a real pin (see module docstring)."""
    if not any(looks_like_net_name(p) for p in header_pin_names):
        return [PIN_NAME_TO_LIBERTY.get(p, p) for p in header_pin_names]

    real_signal_pins = spice_signal_orders.get(cell_type)
    if real_signal_pins is None:
        return None  # can't resolve - caller should leave the line untouched

    resolved = []
    signal_idx = 0
    for pin in header_pin_names:
        if looks_like_net_name(pin):
            if signal_idx >= len(real_signal_pins):
                return None
            resolved.append(real_signal_pins[signal_idx])
            signal_idx += 1
        else:
            resolved.append(PIN_NAME_TO_LIBERTY.get(pin, pin))
    return resolved


def positional_to_named(line: str, pin_orders: dict, spice_signal_orders: dict) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith(KEYWORD_PREFIXES):
        return line

    m = INSTANCE_RE.match(line)
    if not m:
        return line

    indent, cell_type, inst_name, args_str = m.groups()
    if cell_type not in pin_orders:
        return line
    if "." in args_str:
        # Already using named connections (e.g. inside a stub module's own
        # self-instantiation) - leave as-is.
        return line

    nets = [n.strip() for n in args_str.split(",") if n.strip()]
    header_pin_names = pin_orders[cell_type]
    if len(nets) != len(header_pin_names):
        # Can't safely rewrite - leave positional and let it fail loudly on
        # import rather than silently mis-wiring it.
        return line

    pin_names = resolve_pin_names(cell_type, header_pin_names, spice_signal_orders)
    if pin_names is None:
        return line

    named = ", ".join(".%s(%s)" % (pin, net) for pin, net in zip(pin_names, nets))
    return "%s%s %s ( %s );\n" % (indent, cell_type, inst_name, named)


def find_top_module_span(lines):
    """Return (start_index, end_index) of the first module...endmodule
    block, inclusive. LGE's gate-type stub modules are not nested, so the
    first 'endmodule' after the first 'module' closes the top module."""
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("module "):
            start = i
            break
    if start is None:
        raise ValueError("no 'module' declaration found")

    for i in range(start, len(lines)):
        if lines[i].strip().startswith("endmodule"):
            return start, i

    raise ValueError("no matching 'endmodule' found for top module")


def sanitize_slashes(line: str) -> str:
    if line.strip().startswith("//"):
        return line
    return line.replace("/", "_")


def clean(in_path: str, out_path: str, spice_path: str):
    with open(in_path) as f:
        lines = f.readlines()

    text = "".join(lines)
    pin_orders = parse_pin_orders(text)

    with open(spice_path) as f:
        spice_signal_orders = parse_spice_signal_pin_order(f.read())

    rewritten = [positional_to_named(line, pin_orders, spice_signal_orders) for line in lines]

    start, end = find_top_module_span(rewritten)
    kept = rewritten[: end + 1]

    out = [sanitize_slashes(line) for line in kept]

    with open(out_path, "w") as f:
        f.writelines(out)

    print("wrote %s (kept lines 1-%d of %d, module span %d-%d, %d cell types with known pin order)" % (
        out_path, end + 1, len(lines), start + 1, end + 1, len(pin_orders)))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: clean_for_hal_import.py <lge_output.v> <out_hal_import.v> <05_extracted.spice>")
        sys.exit(1)
    clean(sys.argv[1], sys.argv[2], sys.argv[3])
