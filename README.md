# FARE-flow

A general-purpose flow for reverse-engineering an ASIC: drop in a **GDS**, a **SPICE netlist**, or an already-extracted **gate-level netlist**, and get a consistent path from there to (a) readable behavior and (b) a way to formally search for inputs that produce a target output.

This isn't a monolithic tool - it's orchestration around existing, purpose-built tools (Magic, KLayout, HAL, Yosys/SymbiYosys, Verilator), each used for what it's actually good at. See [`docs/flow.md`](docs/flow.md) for the full plan: the three entry points, the toolchain map with every gotcha hit while building this, the proposed repository layout, and a wider survey of adjacent open-source/free/paid tools worth knowing about.

## Getting a dev shell

This repo uses a Nix flake for reproducible tooling.

```bash
nix develop
```

gives you Magic, KLayout, Yosys, SymbiYosys (`sby`), Verilator, Z3, Boolector, and `ciel` (the PDK manager) - everything except the sky130 PDK itself and HAL.

### The sky130 PDK (fetched lazily, on purpose)

Only the GDS entry point needs a PDK, so it isn't fetched just for entering the dev shell. Run:

```bash
./scripts/ensure_pdk.sh
```

before a GDS extraction. It activates the exact sky130 version this flow was validated against (pinned in the script itself, not left to "whatever's latest") and prints the resulting `PDK_ROOT`. A mismatched PDK version makes Magic silently produce wrong or missing connectivity, with no error at extraction time - see `docs/flow.md`'s Magic gotchas for why this pin matters.

### HAL (no upstream Nix packaging)

HAL is a large CMake/Qt5 project with no official Nix package, and its build assumes a normal FHS layout the way `apt-get install` on Ubuntu does. Rather than hand-patching a from-scratch derivation, it gets its own sandboxed shell:

```bash
nix develop .#hal
```

This drops you into an FHS environment with HAL's own documented build dependencies (see `nix/hal-fhs.nix`, which mirrors HAL's `install_dependencies.sh` Ubuntu package list). Clone HAL and build it inside that shell exactly as its own README describes.

## Running the pipeline

From inside `nix develop`, the `Makefile` drives everything from a GDS down to a Verilog netlist, plus a couple of follow-up cleanup steps. Run `make help` for the full target list. Typical flow:

```bash
make pdk                                    # fetch/activate the pinned sky130 PDK
make extract GDS=path/to/design.gds TOP=my_top_cell   # Magic: GDS -> SPICE
make check-anon TOP=my_top_cell             # real cell names, or anonymized?
make netlist TOP=my_top_cell                # ...if real: direct SPICE -> Verilog
# make lge && make lge-lib LIB_SPICE=... && make netlist-lge TOP=my_top_cell   # ...if anonymized instead
```

`make hal`, `make lge`, and `make deps` clone and build the external tools (HAL, ReGDS-Logic-Gate-Extraction) that aren't in the flake - see `docs/flow.md` for why each of those lives outside Nix. `make strip-power` and `make remove-decap` are follow-up steps for decluttering a finished netlist before analysis in HAL's GUI. `make clean`/`make distclean` remove generated artifacts (and, for the latter, the cloned external tools).

The `lge-lib` target needs a `LIB_SPICE=` you supply yourself (a Magic-extracted SPICE file with full subckt definitions for the sky130_fd_sc_hd cells you're using) - this repo doesn't ship or pin one canonical source for it.

## License

MIT - see [`LICENSE`](LICENSE).
