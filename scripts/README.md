# scripts/

FARE-flow's own first-party Python scripts, ported from the original
puzzle-solving session and sorted into the layout `docs/flow.md`
proposes. These are copies, not yet generalized - most still carry
puzzle-specific assumptions (port names, cell library paths) that
`docs/flow.md`'s goal #4 calls out as needing a cleanup pass.

Downloaded/cloned third-party tools (HAL, ReGDS-Logic-Gate-Extraction,
NetA) live separately under `tools/` (gitignored, fetched via `make hal`
/ `make lge` / `make neta` - see the top-level README) - not to be
confused with this directory.

- **`gds2spice/`** - `build_locations.py` / `apply_locations.py`: recover
  real GDS coordinates from Magic's `.ext` output, for placing gates in
  the HAL GUI.
- **`spice2verilog/`** - `spice_to_verilog.py`: the direct, no-identification
  SPICE→Verilog path (used when Magic recovers real cell names).
- **`lge_wrapper/`** - `convert_to_lge.py`, `clean_for_hal_import.py`,
  `expand_power_pins.py`, `structural_diff.py`: the anonymized-names path,
  built around the external `ReGDS-Logic-Gate-Extraction` tool (see
  `docs/flow.md`'s LGE section for its pinned commit - not vendored here,
  it's a separate GPL-3.0 repo).
- **`hal_scripts/`** - `create_rtl_modules.py`, `decompile_to_rtl.py`,
  `print_equations.py`, `verify_equations.py`, `find_self_latches.py`,
  `analyze_accumulator.py`, `trace_bit_indices.py`,
  `preprocess_and_dana.py`, `strip_liberty_pg_pins.py`,
  `strip_verilog_power_ports.py`, `remove_decap_cells.py`: netlist
  analysis, equation extraction/verification, and GUI-declutter scripts,
  all run inside HAL.
- **`verilator_harness/`** - `load_witness_bits.py`, `compare_vcd_outputs.py`,
  `simulate_vs_vcd.py`: VCD-driven simulation/comparison tooling,
  including the anti-hand-transcription witness loader.

## Deliberately not copied

- **`solve_success.py`** - the hand-rolled Z3-via-HAL symbolic solver.
  Documented dead end (failed to scale past a handful of cycles) - see
  `docs/flow.md`'s "Yosys + SymbiYosys" section for why BMC replaced it.
  Kept in the original session repo as a historical record, not brought
  forward as a working tool.
- **`create_addition_module.py`** - a one-off demo tied specifically to
  the warm-up's adder structure, not a generalized tool.

## Not yet built at all

- `entry/from_gds.sh`, `entry/from_spice.sh`, `entry/from_netlist.sh` -
  the auto-picking entry-point wrappers `docs/flow.md` proposes.
- `klayout_crosscheck/` - optional second-opinion extraction via
  KLayout, never implemented.
- `formal/` - a reusable SymbiYosys `.sby`/harness template and the
  PDK-cell async-reset preprocessing pass; the working version that
  solved the puzzle was one-off files, not generalized here.
