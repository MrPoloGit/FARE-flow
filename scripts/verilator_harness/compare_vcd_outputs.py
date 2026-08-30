#!/usr/bin/env python3
"""
Compare O[7:0]/success from a verilator-simulated VCD against the puzzle's
own reference example_inputs.vcd, to check whether the reconstructed
netlist reproduces the same (stimulus -> output) behavior as the real
design. Handles two different VCD dialects without needing a full VCD
parser:
  - example_inputs.vcd: O is one 8-bit vector signal (single $var, "b..."
    binary literal values).
  - a verilator-produced trace: every net is its own scalar $var (O[0]
    .. O[7] each separately, single-bit "0"/"1" value changes) - so O's
    8-bit value at any time has to be reconstructed bit-by-bit.

Usage: compare_vcd_outputs.py <reference.vcd> <verilator.vcd>
"""
import re
import sys


def parse_vcd(path):
    """Returns {name: [(time, value_str), ...]} - value_str is either a
    single bit char ('0'/'1'/'x'/'z') or, for O in the reference file, a
    binary string from a 'b...' literal.

    A VCD identifier code can be shared by multiple $var declarations that
    are the same underlying net viewed under different names/scopes (e.g.
    verilator aliases a top-level port directly to whichever internal
    gate pin drives it with no logic in between - confirmed directly:
    O[0] and some internal cell's "X" output pin shared one id here). So
    id -> name is one-to-MANY, and every value-change event for an id must
    be recorded under every name that id was ever declared with - not just
    the last one seen, which would silently drop earlier names entirely.
    """
    id_to_names = {}
    events = {}
    time = 0
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            m = re.match(r"\$var \w+ (\d+) (\S+) (\S+)(?: \[.*\])? \$end", stripped)
            if m:
                width, ident, name = m.groups()
                id_to_names.setdefault(ident, [])
                if name not in id_to_names[ident]:
                    id_to_names[ident].append(name)
                events.setdefault(name, [])
                continue
            if stripped.startswith("#"):
                time = int(stripped[1:])
                continue
            m = re.match(r"^b([01xz]+)\s+(\S+)$", stripped)
            if m:
                val, ident = m.groups()
                for name in id_to_names.get(ident, []):
                    events[name].append((time, val))
                continue
            m = re.match(r"^([01xz])(\S+)$", stripped)
            if m:
                val, ident = m.groups()
                for name in id_to_names.get(ident, []):
                    events[name].append((time, val))
                continue
    return events


def bits_to_int(bits):
    if "x" in bits or "z" in bits:
        return None
    return int(bits, 2)


def sample_at(events_list, t):
    """Last value at or before time t (VCD semantics: value holds until
    the next change)."""
    val = None
    for et, ev in events_list:
        if et > t:
            break
        val = ev
    return val


def build_o_timeline(events, o_is_vector):
    """Returns [(time, 8-bit-int-or-None)] for the O bus, merging per-bit
    events into a single vector value if O is scalar-per-bit."""
    if o_is_vector:
        out = []
        for t, v in events["O"]:
            out.append((t, bits_to_int(v)))
        return out

    bit_names = ["O[%d]" % i for i in range(8)]
    all_times = sorted({t for n in bit_names for t, _ in events.get(n, [])})
    out = []
    for t in all_times:
        bits = []
        ok = True
        for n in reversed(bit_names):  # O[7] is MSB
            v = sample_at(events.get(n, []), t)
            if v not in ("0", "1"):
                ok = False
                break
            bits.append(v)
        out.append((t, int("".join(bits), 2) if ok else None))
    return out


def main(ref_path, sim_path):
    ref = parse_vcd(ref_path)
    sim = parse_vcd(sim_path)

    ref_o_is_vector = "O" in ref
    sim_o_is_vector = "O" in sim

    ref_o = build_o_timeline(ref, ref_o_is_vector)
    sim_o = build_o_timeline(sim, sim_o_is_vector)

    ref_success = ref.get("success", [])
    sim_success = sim.get("success", [])

    all_times = sorted({t for t, _ in ref_o} | {t for t, _ in sim_o})

    mismatches = 0
    checked = 0
    last_ref_val = None
    last_sim_val = None
    printable_ref = []
    printable_sim = []
    for t in all_times:
        rv = sample_at(ref_o, t)
        sv = sample_at(sim_o, t)
        if rv is None or sv is None:
            continue
        checked += 1
        if rv != last_ref_val:
            printable_ref.append((t, rv))
            last_ref_val = rv
        if sv != last_sim_val:
            printable_sim.append((t, sv))
            last_sim_val = sv
        if rv != sv:
            mismatches += 1
            if mismatches <= 20:
                print("MISMATCH t=%d: ref O=0x%02x (%r) vs sim O=0x%02x (%r)" %
                      (t, rv, chr(rv) if 32 <= rv < 127 else "?", sv, chr(sv) if 32 <= sv < 127 else "?"))

    print()
    print("checked %d overlapping O-value timepoints, %d mismatches" % (checked, mismatches))
    print()
    print("reference O distinct-value sequence (as ASCII where printable):")
    print("".join(chr(v) if 32 <= v < 127 else "." for _, v in printable_ref))
    print("simulated O distinct-value sequence (as ASCII where printable):")
    print("".join(chr(v) if 32 <= v < 127 else "." for _, v in printable_sim))

    rs = [v for _, v in ref_success]
    ss = [v for _, v in sim_success]
    print()
    print("reference success ever asserted ('1'):", "1" in rs)
    print("simulated success ever asserted ('1'):", "1" in ss)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: compare_vcd_outputs.py <reference.vcd> <verilator.vcd>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
