\
# FARE-flow pipeline driver. Run `make help` for the target list.
#
# Expected to run inside `nix develop` (see flake.nix) - one shell for
# everything here, including HAL's own build/runtime deps.
#
# GDS/TOP are supplied by you - this repo intentionally doesn't ship any
# puzzle/design files of its own:
#   make extract GDS=path/to/design.gds TOP=my_top_cell

.DEFAULT_GOAL := help

BUILD := build
TOOLS := tools

PDK_ROOT_FILE := .pdk_root
PDK_VERSION   := 8afc8346a57fe1ab7934ba5a6056ea8b43078e71

HAL_DIR    := $(TOOLS)/hal
HAL_REPO   := https://github.com/emsec/hal.git
HAL_COMMIT := 29685878a7d35f346fbde2fd8584e275d00112c3
HAL_BIN    := $(HAL_DIR)/build/bin/hal

LGE_DIR    := $(TOOLS)/ReGDS-Logic-Gate-Extraction
LGE_REPO   := https://github.com/rachelselinar/ReGDS-Logic-Gate-Extraction.git
LGE_COMMIT := 4f4553ee8002b4aec8ee9cd6443f9469b3dd4dbd
LGE_BIN    := $(LGE_DIR)/build/LGE

NETA_DIR    := $(TOOLS)/NetA
NETA_REPO   := https://github.com/jinyier/NetA.git
NETA_COMMIT := 2ee3a6007fd343e3b5650e2a1095011a2ad26c4e

GDS ?=
TOP ?=

.PHONY: help
help: ## Show this list
	@grep -E '^[a-zA-Z0-9_.%-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Bootstrap: downloaded tools not in the flake -----------------------

.PHONY: pdk
pdk: $(PDK_ROOT_FILE) ## Fetch/activate the pinned sky130 PDK via ciel (lazy - only needed before `extract`)

$(PDK_ROOT_FILE):
	./scripts/ensure_pdk.sh | tail -1 > $@
	@echo "PDK_ROOT: $$(cat $@)"

$(HAL_DIR):
	git clone $(HAL_REPO) $(HAL_DIR)
	cd $(HAL_DIR) && git checkout $(HAL_COMMIT)

$(HAL_BIN): | $(HAL_DIR) ## (order-only: clone happens first, doesn't force a rebuild on its own)
	cd $(HAL_DIR) && mkdir -p build && cd build && cmake -G Ninja -DCMAKE_BUILD_TYPE=Release .. && ninja

.PHONY: hal
hal: $(HAL_BIN) ## Clone (pinned commit) + build HAL (needs the default `nix develop` shell - see flake.nix)

$(LGE_DIR):
	git clone $(LGE_REPO) $(LGE_DIR)
	cd $(LGE_DIR) && git checkout $(LGE_COMMIT)

$(LGE_BIN): | $(LGE_DIR)
	cd $(LGE_DIR)/src/parser && $(MAKE) clean && $(MAKE)
	mkdir -p $(LGE_DIR)/bin $(LGE_DIR)/build
	cd $(LGE_DIR)/build && cmake .. && $(MAKE)

.PHONY: lge
lge: $(LGE_BIN) ## Clone (pinned commit) + build ReGDS-Logic-Gate-Extraction - only needed for the anonymized-names path

.PHONY: lge-lib
lge-lib: $(LGE_BIN) ## Build LGE's sky130_fd_sc_hd library DCGs - REQUIRED once before `netlist-lge` works. Needs LIB_SPICE=<magic-extracted .spice with full subckt defs for the cell library>
	@test -n "$(LIB_SPICE)" || (echo "usage: make lge-lib LIB_SPICE=path/to/library.spice"; echo "(a Magic-extracted SPICE file containing full subckt definitions for every sky130_fd_sc_hd cell you need - this session never pinned down one canonical source file for this step, see docs/flow.md)"; exit 1)
	cd $(LGE_DIR)/bin && ./LGE --lib=1 --sp=$(abspath $(LIB_SPICE))
	cd $(LGE_DIR)/src/parser && $(MAKE) clean && $(MAKE)
	cd $(LGE_DIR)/build && $(MAKE)
	@echo "library DCGs built and LGE rebuilt - re-run is required any time you clear or replace the library (see clearLibrary.sh in $(LGE_DIR))"

$(NETA_DIR):
	git clone $(NETA_REPO) $(NETA_DIR)
	cd $(NETA_DIR) && git checkout $(NETA_COMMIT)
	chmod +x $(NETA_DIR)/*/bin/*

.PHONY: neta
neta: $(NETA_DIR) ## Clone (pinned commit) NetA (precompiled logic-identification/word-partition binaries, no source/build - see docs/flow.md). No LICENSE file upstream; site states free for non-commercial use only.

.PHONY: deps
deps: pdk hal lge neta ## Fetch/build everything external (PDK + HAL + ReGDS-LGE + NetA)

# --- Core pipeline: GDS -> SPICE -> Verilog ----------------------------

$(BUILD)/$(TOP).spice: $(PDK_ROOT_FILE)
	@test -n "$(GDS)" -a -n "$(TOP)" || (echo "usage: make extract GDS=path/to/design.gds TOP=cell_name"; exit 1)
	mkdir -p $(BUILD)
	export PDK_ROOT=$$(cat $(PDK_ROOT_FILE)); \
	RCFILE=$$PDK_ROOT/sky130A/libs.tech/magic/sky130A.magicrc; \
	printf 'gds read %s\nload %s\nselect top cell\nextract all\next2spice lvs\next2spice %s\nquit -noprompt\n' \
	  "$(abspath $(GDS))" "$(TOP)" "$(TOP)" > $(BUILD)/extract.tcl; \
	cd $(BUILD) && magic -noconsole -dnull -T sky130A -rcfile "$$RCFILE" extract.tcl

.PHONY: extract
extract: $(BUILD)/$(TOP).spice ## Magic: GDS -> SPICE + placement (.ext). Needs GDS= and TOP=

.PHONY: check-anon
check-anon: $(BUILD)/$(TOP).spice ## Report whether instance names came back real or anonymized - decides netlist vs netlist-lge (see docs/flow.md)
	@if grep -q '^\.SUBCKT sky130_fd_sc_hd__' $(BUILD)/$(TOP).spice 2>/dev/null || grep -qi '^\.subckt sky130_fd_sc_hd__' $(BUILD)/$(TOP).spice; then \
	  echo "real sky130_fd_sc_hd__* cell names found -> use 'make netlist' (direct path)"; \
	else \
	  echo "no real cell names found in .SUBCKT headers -> names look anonymized -> use 'make netlist-lge' (needs 'make lge' + 'make lge-lib' first)"; \
	fi

$(BUILD)/$(TOP).v: $(BUILD)/$(TOP).spice
	python3 scripts/spice2verilog/spice_to_verilog.py $(BUILD)/$(TOP).spice $(TOP) $(BUILD)/$(TOP).v $(LIBERTY)

.PHONY: netlist
netlist: $(BUILD)/$(TOP).v ## SPICE -> Verilog, direct path (use when `check-anon` says names are real). Optional LIBERTY= gives real input/output port directions instead of blanket "inout"

$(BUILD)/$(TOP)_lge.spice: $(BUILD)/$(TOP).spice
	python3 scripts/lge_wrapper/convert_to_lge.py $(BUILD)/$(TOP).spice $(BUILD)/$(TOP)_lge.spice

$(LGE_DIR)/bin/$(TOP).v: $(BUILD)/$(TOP)_lge.spice $(LGE_BIN)
	cd $(LGE_DIR)/bin && ./LGE --lib=0 --sp=$(abspath $(BUILD)/$(TOP)_lge.spice)

$(BUILD)/$(TOP)_hal_import.v: $(LGE_DIR)/bin/$(TOP).v $(BUILD)/$(TOP).spice
	python3 scripts/lge_wrapper/clean_for_hal_import.py $(LGE_DIR)/bin/$(TOP).v $(BUILD)/$(TOP)_hal_import.v $(BUILD)/$(TOP).spice

.PHONY: netlist-lge
netlist-lge: $(BUILD)/$(TOP)_hal_import.v ## SPICE -> Verilog, LGE path (use when `check-anon` says names are anonymized). Needs `make lge-lib` run once first. Known scaling risk past ~79 gates (see docs/flow.md)

# --- Follow-up scripts on an existing netlist --------------------------
#
# These take an explicit NET=/LIBERTY= rather than assuming the pipeline
# above just ran, so they also work on a netlist you already have.

NET     ?= $(BUILD)/$(TOP).v
LIBERTY ?=

# NET/LIBERTY may point anywhere (e.g. LIBERTY is typically read straight out
# of the read-only ciel PDK cache) - derived outputs always land in $(BUILD),
# named after the input's own basename, rather than next to the input itself.
NET_BASE     = $(basename $(notdir $(NET)))
LIBERTY_BASE = $(basename $(notdir $(LIBERTY)))

.PHONY: strip-power
strip-power: ## Strip VPWR/VGND from a Verilog netlist (+ pg_pins from its Liberty) for readable HAL-GUI analysis. Needs NET= and LIBERTY=
	@test -n "$(LIBERTY)" || (echo "usage: make strip-power NET=path/to/netlist.v LIBERTY=path/to/cells.lib"; exit 1)
	mkdir -p $(BUILD)
	python3 scripts/hal_scripts/strip_verilog_power_ports.py $(NET) $(BUILD)/$(NET_BASE)_no_pg.v
	python3 scripts/hal_scripts/strip_liberty_pg_pins.py $(LIBERTY) $(BUILD)/$(LIBERTY_BASE)_no_pg.lib
	@echo "wrote $(BUILD)/$(NET_BASE)_no_pg.v and $(BUILD)/$(LIBERTY_BASE)_no_pg.lib"

.PHONY: remove-decap
remove-decap: $(HAL_BIN) ## Delete decap filler cells from a netlist (run AFTER strip-power). Needs NET= and LIBERTY=, runs inside HAL
	@test -n "$(LIBERTY)" || (echo "usage: make remove-decap NET=path/to/netlist.v LIBERTY=path/to/cells.lib"; exit 1)
	mkdir -p $(BUILD)
	$(HAL_BIN) --python-script scripts/hal_scripts/remove_decap_cells.py \
	  --py-args "$(NET) $(LIBERTY) $(BUILD)/$(NET_BASE)_no_decap.hal"

# --- Housekeeping -------------------------------------------------------

.PHONY: clean
clean: ## Remove generated pipeline artifacts (build/), keeps tools/ clones and the PDK
	rm -rf $(BUILD)

.PHONY: distclean
distclean: clean ## Also remove downloaded tools (HAL, ReGDS-LGE, NetA) and the PDK activation cache
	rm -rf $(TOOLS) $(PDK_ROOT_FILE)
