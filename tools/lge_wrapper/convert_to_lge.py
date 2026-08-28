#!/usr/bin/env python3
"""
Convert a Magic-derived (ext2spice) SPICE netlist into the dialect that
ReGDS-Logic-Gate-Extraction's parser accepts.

Handles:
  - joining SPICE line continuations ('+' prefixed lines)
  - renaming X-prefixed transistor instances to M<n> (LGE convention)
  - stripping '#' and '.' characters from node names (invalid in LGE's grammar)
  - merging VPWR/VPB -> VDD and VGND/VNB -> GND (HAL's Liberty parser only
    exposes VGND/VPWR as real ports anyway, and LGE's native power-pin
    recognition path has unresolved crashes - see notes.md)
  - dropping power-only cells (e.g. decap_3) with no real signal pins,
    which break LGE's gate classification
  - deduping header pins post-merge with a positional keep-mask applied
    identically to every instance's connection list (mismatched
    header/instance pin counts is what causes LGE's vector::_M_range_check
    crashes)

Reconstructed from session notes - the original script that ran during the
warmup extraction is gone (see notes.md for what it produced). Treat this as
a faithful re-implementation of the documented behavior, not a byte-for-byte
restore.
"""

import re
import sys

DROP_CELLS = {"decap_3"}

RAIL_MERGE = {
    "VPWR": "VDD",
    "VPB": "VDD",
    "VGND": "GND",
    "VNB": "GND",
}


def clean_node(name: str) -> str:
    name = name.replace("#", "_").replace(".", "_")
    return RAIL_MERGE.get(name, name)


def rename_rail(name: str) -> str:
    return RAIL_MERGE.get(name, name)


def join_continuations(lines):
    joined = []
    for line in lines:
        if line.startswith("+") and joined:
            joined[-1] = joined[-1].rstrip("\n") + " " + line[1:].lstrip()
        else:
            joined.append(line)
    return joined


def is_transistor(fields):
    # X-instance is a transistor iff its model field starts with sky130_fd_pr__
    return len(fields) > 1 and fields[-1].startswith("sky130_fd_pr__")


def dedup_header_pins(header_pins):
    """Return (deduped_pins, keep_mask) - keep_mask must be applied to every
    instance's connection list in the same order to keep pin counts consistent."""
    seen = set()
    keep_mask = []
    deduped = []
    for pin in header_pins:
        if pin in seen:
            keep_mask.append(False)
            continue
        seen.add(pin)
        keep_mask.append(True)
        deduped.append(pin)
    return deduped, keep_mask


def convert(in_path: str, out_path: str, keep_hierarchy: bool = True):
    with open(in_path) as f:
        raw_lines = f.readlines()

    lines = join_continuations(raw_lines)

    out = []
    subckt_pin_masks = {}  # cell_type -> keep_mask, needed for X-instance lines
    m_counter = 0
    skipping = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue

        fields = stripped.split()
        directive = fields[0].upper()

        if directive == ".SUBCKT":
            cell_type = fields[1]
            if cell_type in DROP_CELLS:
                skipping = True
                continue
            header_pins = [clean_node(p) for p in fields[2:]]
            deduped, mask = dedup_header_pins(header_pins)
            subckt_pin_masks[cell_type] = mask
            out.append(".SUBCKT %s %s" % (cell_type, " ".join(deduped)))
            continue

        if directive == ".ENDS":
            if skipping:
                skipping = False
                continue
            out.append(".ENDS")
            continue

        if skipping:
            continue

        if stripped.startswith("X"):
            inst_type = fields[-1] if not is_transistor(fields) else None

            if is_transistor(fields):
                # Xname d g s b model -> M<n> d g s b model
                m_counter += 1
                nodes = [clean_node(n) for n in fields[1:-1]]
                out.append("M%d %s %s" % (m_counter, " ".join(nodes), fields[-1]))
                continue

            # hierarchical subckt call: Xname n1 n2 ... celltype
            cell_type = fields[-1]
            if cell_type in DROP_CELLS:
                continue
            nodes = [clean_node(n) for n in fields[1:-1]]
            mask = subckt_pin_masks.get(cell_type, [True] * len(nodes))
            kept = [n for n, keep in zip(nodes, mask) if keep]

            if keep_hierarchy:
                out.append("X%s %s %s" % (fields[0][1:], " ".join(kept), cell_type))
            else:
                out.append("X%s %s %s" % (fields[0][1:], " ".join(kept), cell_type))
            continue

        out.append(stripped)

    with open(out_path, "w") as f:
        f.write("\n".join(l for l in out if l is not None) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: convert_to_lge.py <in.spice> <out.spice>")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
