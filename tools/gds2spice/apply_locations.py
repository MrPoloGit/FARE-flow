#!/usr/bin/env python3
"""
Apply GDS-derived (x, y) placements (produced by build_locations.py) to gate
locations in a HAL netlist, then serialize the result.

Notes from the session (gotchas that caused real errors before):
  - Gate.set_location() takes a single (x, y) tuple, not two positional args.
  - The serializer method is NetlistSerializer.serialize_to_file(), not
    serialize_netlist().
  - Locations with duplicate (x, y) pairs cause HAL's default graph layouter
    to fall back to a grid layout (CoordinateFromDataMap::good() returns
    False if any two nodes share a coordinate) - see the known bug in
    build_locations.py's matching heuristic, documented in notes.md.

Reconstructed from session notes; the original script is gone.
"""

import sys

import hal_py


def load_locations(locations_path):
    locations = {}
    with open(locations_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) != 3 or parts[1] == "UNMATCHED":
                continue
            inst_name, x, y = parts
            # Gate.set_location() requires Tuple[int, int] - HAL rejects floats.
            locations[inst_name] = (int(float(x)), int(float(y)))
    return locations


def main(verilog_path, lib_path, locations_path, out_path):
    hal_py.plugin_manager.load_all_plugins()

    nl = hal_py.NetlistFactory.load_netlist(verilog_path, lib_path)
    if nl is None:
        print("failed to load netlist")
        sys.exit(1)

    locations = load_locations(locations_path)

    applied = 0
    for gate in nl.get_gates():
        loc = locations.get(gate.name)
        if loc is None:
            continue
        gate.set_location(loc)
        applied += 1

    print("applied locations to %d/%d gates" % (applied, len(nl.get_gates())))

    if not hal_py.NetlistSerializer.serialize_to_file(nl, out_path):
        print("failed to serialize netlist")
        sys.exit(1)

    print("wrote %s" % out_path)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: apply_locations.py <netlist.v> <gate_lib.lib> <locations.txt> <out.hal>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
