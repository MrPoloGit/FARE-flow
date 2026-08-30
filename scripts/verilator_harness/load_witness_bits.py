#!/usr/bin/env python3
"""
Extract a signal's per-clock-cycle value programmatically from a VCD
(a SymbiYosys witness replay, a Yosys `sim -r` replay, or any ordinary
simulation trace) instead of hand-transcribing bits out of a waveform
viewer or terminal printout into source code.

This exists because of a real bug: a verification testbench's witness
strings were hand-typed from a BMC witness and silently truncated (121
bits typed as 115/119 characters, no error/warning from the compiler),
which produced a false "the witness doesn't work" result and cost a long
debugging detour chasing imaginary bugs in unrelated tools before the
transcription error was found. See yosys-attempt.md's "Lesson for next
time". Any script that consumes a solver/BMC witness should go through
this loader (or equivalent programmatic extraction) rather than ever
having a human retype bit strings.

Works generically: give it a VCD, a clock signal name, and a target
signal name (scalar, or a bus reconstructed from per-bit `name[i]`
sub-signals). It has no knowledge of any particular design's port names.

Usage as a library:
    from load_witness_bits import load_witness_bits
    bits = load_witness_bits("trace.vcd", "I_w", clock="clk",
                              cycle_start=4, cycle_end=125)
    # bits == "000...0" (one char per cycle in [cycle_start, cycle_end))

Usage from the command line:
    load_witness_bits.py trace.vcd --signal I_w --clock clk \
        --start 4 --end 125 --format bits

    load_witness_bits.py trace.vcd --signal O --width 8 --clock clk \
        --start 281 --end 296 --format ascii

    load_witness_bits.py trace.vcd --signal I_w --clock clk \
        --start 160 --end 281 --format c-string --name BURST2
"""
import argparse
import sys

from compare_vcd_outputs import parse_vcd, sample_at


def get_rising_edges(events, clock_name):
    """Sorted list of times at which `clock_name` transitions to '1'.

    Cycle N (0-indexed) is defined as the Nth rising edge - this matches
    a `reg [N:0] cyc = 0; always @(posedge clk) cyc <= cyc + 1;` counter
    sampled *before* its own increment, which is the convention used by
    this repo's formal harnesses (see formal_top.v).
    """
    clk_events = events.get(clock_name)
    if not clk_events:
        raise ValueError(
            "no signal named %r in this VCD - check --clock, or list "
            "available signals with --list" % clock_name)
    edges = []
    prev = None
    for t, v in clk_events:
        if v == "1" and prev != "1":
            edges.append(t)
        prev = v
    if not edges:
        raise ValueError("signal %r never rises in this VCD" % clock_name)
    return edges


def sample_scalar(events, name, times):
    sig_events = events.get(name)
    if sig_events is None:
        raise ValueError(
            "no signal named %r in this VCD - list available signals "
            "with --list" % name)
    return [sample_at(sig_events, t) for t in times]


def sample_bus(events, base_name, width, times, msb_first=True):
    """Reconstructs an N-bit value per time from `base_name[0]` ..
    `base_name[width-1]` sub-signals (the common case when a bus wasn't
    declared as one vector $var in the source VCD)."""
    bit_names = ["%s[%d]" % (base_name, i) for i in range(width)]
    missing = [n for n in bit_names if n not in events]
    if missing:
        raise ValueError(
            "missing bus sub-signals in this VCD: %s - list available "
            "signals with --list" % ", ".join(missing))
    out = []
    order = list(reversed(bit_names)) if msb_first else bit_names
    for t in times:
        bits = []
        for n in order:
            v = sample_at(events[n], t)
            if v not in ("0", "1"):
                bits.append("x")
            else:
                bits.append(v)
        out.append("".join(bits))
    return out


def load_witness_bits(vcd_path, signal, clock="clk", width=1,
                       cycle_start=0, cycle_end=None, msb_first=True):
    """Returns a string, one character per cycle in
    [cycle_start, cycle_end) - '0'/'1' if width == 1, else each
    cycle's value as a width-bit binary group (still just characters,
    concatenated) - callers wanting integers should use the sample_*
    helpers directly instead.

    cycle_end=None means "through the last rising edge in the VCD".
    """
    events = parse_vcd(vcd_path)
    edges = get_rising_edges(events, clock)
    if cycle_end is None:
        cycle_end = len(edges)
    if cycle_start < 0 or cycle_end > len(edges) or cycle_start >= cycle_end:
        raise ValueError(
            "cycle range [%d, %d) out of bounds - this VCD has %d rising "
            "clock edges" % (cycle_start, cycle_end, len(edges)))
    times = edges[cycle_start:cycle_end]

    if width == 1:
        values = sample_scalar(events, signal, times)
        if any(v not in ("0", "1") for v in values):
            bad = [cycle_start + i for i, v in enumerate(values)
                   if v not in ("0", "1")]
            raise ValueError(
                "signal %r is not a clean 0/1 at cycles %s (x/z/unknown) "
                "- this witness may not be fully defined there" %
                (signal, bad))
        return "".join(values)
    else:
        return "".join(sample_bus(events, signal, width, times, msb_first))


def _list_signals(vcd_path):
    events = parse_vcd(vcd_path)
    for name in sorted(events):
        print(name)


def _format_output(bits, width, fmt, name):
    if fmt == "bits":
        return bits
    if fmt == "ascii":
        if width != 8:
            raise ValueError("--format ascii requires --width 8")
        chars = []
        for i in range(0, len(bits), 8):
            byte = int(bits[i:i + 8], 2)
            chars.append(chr(byte) if 32 <= byte < 127 else ".")
        return "".join(chars)
    if fmt == "c-string":
        var = name or "WITNESS_BITS"
        return 'static const char *%s =\n    "%s";' % (var, bits)
    if fmt == "python-list":
        return "[%s]" % ", ".join(bits)
    raise ValueError("unknown format %r" % fmt)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vcd", help="path to the VCD to extract from")
    ap.add_argument("--signal", help="signal name (or bus base name with --width > 1)")
    ap.add_argument("--clock", default="clk", help="clock signal name (default: clk)")
    ap.add_argument("--width", type=int, default=1,
                     help="bus width; 1 (default) means a plain scalar signal")
    ap.add_argument("--start", type=int, default=0, help="first cycle, inclusive (default: 0)")
    ap.add_argument("--end", type=int, default=None,
                     help="last cycle, exclusive (default: through end of trace)")
    ap.add_argument("--format", choices=["bits", "ascii", "c-string", "python-list"],
                     default="bits")
    ap.add_argument("--name", help="variable name to use for --format c-string")
    ap.add_argument("--list", action="store_true",
                     help="list every signal name found in the VCD and exit")
    args = ap.parse_args()

    if args.list:
        _list_signals(args.vcd)
        return

    if not args.signal:
        ap.error("--signal is required unless --list is given")

    bits = load_witness_bits(args.vcd, args.signal, clock=args.clock,
                              width=args.width, cycle_start=args.start,
                              cycle_end=args.end)
    print(_format_output(bits, args.width, args.format, args.name))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)
