"""
Strips every `pg_pin ("NAME") { ... }` block out of a Liberty (.lib) file,
producing a copy where power/ground pins (VPWR/VGND on sky130 cells) no
longer exist as pins on the resulting GateType at all.

Why: HAL's own liberty_parser.cpp (parse_pg_pin()) turns each pg_pin block
into a real input pin on the GateType (just tagged power/ground), and the
GUI's graph rendering (graphics_gate.cpp) draws every input pin
unconditionally - there's no PinType-based filter anywhere in the GUI. So
VPWR/VGND end up drawn on every single gate box, which is real clutter on
a design with hundreds of gates. There's no GUI setting for this; stripping
the pg_pin declarations before HAL parses the library is the only way to
stop them being created as pins in the first place.

Brace-balanced text removal (not a full Liberty tokenizer) - safe here
because `pg_pin (...) { ... }` blocks in real Liberty files never contain
unbalanced braces/parens inside string literals in practice, and this is
a read-only viewing copy, not something fed back into synthesis.

Usage:
  python3 strip_liberty_pg_pins.py <in.lib> <out.lib>
"""
import sys

if len(sys.argv) != 3:
    print("usage: strip_liberty_pg_pins.py <in.lib> <out.lib>")
    sys.exit(1)

IN_PATH, OUT_PATH = sys.argv[1], sys.argv[2]

text = open(IN_PATH).read()

out = []
i = 0
n = len(text)
removed = 0
while i < n:
    idx = text.find("pg_pin", i)
    if idx == -1:
        out.append(text[i:])
        break
    # only treat as a real pg_pin group if followed (modulo whitespace) by '('
    j = idx + len("pg_pin")
    k = j
    while k < n and text[k] in " \t\r\n":
        k += 1
    if k >= n or text[k] != "(":
        # not actually a pg_pin group header (e.g. a comment/string mention) - keep as-is
        out.append(text[i:j])
        i = j
        continue

    # find the opening '{' of this group (after the "(...)" pin-name list)
    brace_start = text.find("{", k)
    if brace_start == -1:
        out.append(text[i:])
        break

    # brace-balanced scan to find the matching closing '}'
    depth = 0
    p = brace_start
    while p < n:
        if text[p] == "{":
            depth += 1
        elif text[p] == "}":
            depth -= 1
            if depth == 0:
                break
        p += 1
    block_end = p + 1  # one past the matching '}'

    out.append(text[i:idx])  # keep everything before "pg_pin"
    removed += 1
    i = block_end  # skip the whole "pg_pin (...) { ... }" block

open(OUT_PATH, "w").write("".join(out))
print("stripped %d pg_pin block(s): %s -> %s" % (removed, IN_PATH, OUT_PATH))
