#!/usr/bin/env python3
"""
Run netlist_preprocessing.remove_buffers() on a HAL netlist before DANA
dataflow analysis, in an attempt to collapse clock-tree-synthesis buffer
chains that otherwise leave each flip-flop branch on its own distinct clock
net (which stops DANA from grouping flip-flops into clean register words).

Gotcha from the session: the netlist_preprocessing plugin's pybind11 module
is a *separate* top-level importable module living in
<hal_build>/lib/hal_plugins/, not exposed under the hal_py namespace -
plain `import hal_py.netlist_preprocessing` or `hal_py.NetlistPreprocessingPlugin`
both fail. You have to append that directory to sys.path and import it
directly. remove_buffers() is a module-level function (bound via m.def),
not a class method.

Result documented in notes.md: this only removed 1 buffer gate in the
warmup netlist - not enough to unify the 16 distinct clock-branch nets, so
DANA still reports 16 individual flip-flops rather than grouped 8-bit
register words. Left here as a starting point / for re-running once a
better clock-tree-collapsing approach exists.

Reconstructed from session notes; the original script is gone. Update
HAL_BUILD_DIR below to match your local HAL build.
"""

import sys

HAL_BUILD_DIR = "/home/mrpolo/Projects/asic-reverse-engineering/hal/build"

sys.path.append("%s/lib/hal_plugins" % HAL_BUILD_DIR)

import hal_py
import netlist_preprocessing


def main(verilog_path, lib_path, out_path):
    nl = hal_py.NetlistFactory.load_netlist(verilog_path, lib_path)
    if nl is None:
        print("failed to load netlist")
        sys.exit(1)

    before = len(nl.get_gates())
    res = netlist_preprocessing.remove_buffers(nl)
    after = len(nl.get_gates())

    print("remove_buffers: %d -> %d gates (removed %d), result=%s" % (
        before, after, before - after, res))

    if not hal_py.NetlistSerializer.serialize_to_file(nl, out_path):
        print("failed to serialize netlist")
        sys.exit(1)

    print("wrote %s" % out_path)
    print("run DANA on this file next, e.g.:")
    print("  hal --input %s --dataflow --sizes 8,16,32 --path <out_dir>" % out_path)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: preprocess_and_dana.py <netlist.v> <gate_lib.lib> <out.hal>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
