# FARE-flow

A general-purpose flow for reverse-engineering an ASIC: drop in a **GDS**, a **SPICE netlist**, or an already-extracted **gate-level netlist**, and get a consistent path from there to (a) readable behavior and (b) a way to formally search for inputs that produce a target output.

This isn't a monolithic tool - it's orchestration around existing, purpose-built tools (Magic, KLayout, HAL, Yosys/SymbiYosys, Verilator), each used for what it's actually good at. See [`docs/flow.md`](docs/flow.md) for the full plan: the three entry points, the toolchain map with every gotcha hit while building this, the proposed repository layout, and a wider survey of adjacent open-source/free/paid tools worth knowing about.

## Getting a dev shell

This repo uses a Nix flake for reproducible tooling.

```bash
nix develop
```

gives you Magic, KLayout, Yosys, SymbiYosys (`sby`), Verilator, Z3, Boolector, `ciel` (the PDK manager), and HAL's own build dependencies - everything except the sky130 PDK itself, which is fetched separately (see below), and HAL's actual source/build (`make hal` - see below).

### The sky130 PDK (fetched lazily, on purpose)

Only the GDS entry point needs a PDK, so it isn't fetched just for entering the dev shell. Run:

```bash
./scripts/ensure_pdk.sh
```

before a GDS extraction. It activates the exact sky130 version this flow was validated against (pinned in the script itself, not left to "whatever's latest") and prints the resulting `PDK_ROOT`. A mismatched PDK version makes Magic silently produce wrong or missing connectivity, with no error at extraction time - see `docs/flow.md`'s Magic gotchas for why this pin matters.

### HAL (no upstream Nix packaging)

HAL is a large CMake/Qt5 project with no official Nix package. Its build/runtime dependencies (a nixpkgs-name translation of HAL's own `install_dependencies.sh` Ubuntu package list) are in the same default `nix develop` shell as everything else - `make hal` clones HAL and builds it there directly.

An earlier version of this flake tried isolating HAL in its own `buildFHSEnv` sandbox, on the assumption its CMake needed a conventional `/usr`-style layout. That turned out to be unnecessary (a plain shell builds it fine) and came with a real cost of its own: the sandbox needs unprivileged Linux user namespaces via `bubblewrap`, which some hardened setups block outright (e.g. Ubuntu with `kernel.apparmor_restrict_unprivileged_userns=1`). One shell for everything is both simpler and more portable.

## Running the pipeline

From inside `nix develop`, the `Makefile` drives everything from a GDS down to a Verilog netlist, plus a couple of follow-up cleanup steps. Run `make help` for the full target list. Typical flow:

```bash
make pdk                                               # fetch/activate the pinned sky130 PDK
make extract GDS=path/to/design.gds TOP=my_top_cell    # Magic: GDS -> SPICE
make check-anon TOP=my_top_cell                        # real cell names, or anonymized?
make netlist TOP=my_top_cell LIBERTY=path/to/cells.lib # ...if real: direct SPICE -> Verilog
# make lge && make lge-lib LIB_SPICE=... && make netlist-lge TOP=my_top_cell   # ...if anonymized instead
```

`LIBERTY=` on `netlist` is optional but recommended: without it every top-level port comes out declared `inout` (SPICE alone carries no direction info), where a real Liberty file lets the script recover actual `input`/`output` directions. Point it at the corner file matching your PDK, e.g. sky130's `.../sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib` once `make pdk` has fetched it.

`make hal`, `make lge`, and `make deps` clone and build the external tools (HAL, ReGDS-Logic-Gate-Extraction) that aren't in the flake - see `docs/flow.md` for why each of those lives outside Nix. `make strip-power` and `make remove-decap` are follow-up steps for decluttering a finished netlist before analysis in HAL's GUI - run in that order (`remove-decap` expects power-stripped input), and both take explicit `NET=`/`LIBERTY=` rather than assuming a specific prior target already ran, so they work on any netlist you already have too. `make clean`/`make distclean` remove generated artifacts (and, for the latter, the cloned external tools).

The `lge-lib` target needs a `LIB_SPICE=` you supply yourself (a Magic-extracted SPICE file with full subckt definitions for the sky130_fd_sc_hd cells you're using) - this repo doesn't ship or pin one canonical source for it.

## License

MIT - see [`LICENSE`](LICENSE).
