{
  description = "FARE-flow - ASIC reverse-engineering flow toolkit";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # ciel (sky130/gf180mcu/ihp-sg13g2 PDK manager) has its own official
    # flake - reused as-is rather than reimplemented, same as LibreLane does
    # (see ../librelane/flake.nix). nix-eda is ciel's own overlay dependency.
    nix-eda.url = "github:fossi-foundation/nix-eda/6.11.0";
    ciel.url = "github:fossi-foundation/ciel";
  };

  inputs.ciel.inputs.nix-eda.follows = "nix-eda";

  outputs = { self, nixpkgs, nix-eda, ciel }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ ciel.overlays.default ];
      };
    in
    {
      devShells.${system} = {
        # Everything already packaged in plain nixpkgs. Does NOT include the
        # sky130 PDK itself - that's fetched lazily, only by the GDS entry
        # path, via scripts/ensure_pdk.sh (see that script for why).
        default = pkgs.mkShell {
          packages = with pkgs; [
            magic-vlsi
            klayout
            yosys
            sby
            verilator
            z3
            boolector
            ciel
            python3

            # Build deps for ReGDS-Logic-Gate-Extraction (see docs/flow.md) -
            # lightweight enough (no Qt/Python bindings) to build directly
            # in this shell, unlike HAL. cmdline.h is vendored in ReGDS's
            # own src/ tree, nothing else to add for it.
            cmake
            ninja
            gnumake
            flex
            bison
            boost
            gtest
            pkg-config
          ];

          shellHook = ''
            echo "FARE-flow dev shell (Magic, KLayout, Yosys, SymbiYosys, Verilator, Z3, Boolector, ciel)"
            echo "sky130 PDK is not fetched yet - run 'make pdk' before a GDS-entry extraction."
            echo "Run 'make help' for the available pipeline targets."
          '';
        };

        # HAL has no upstream Nix packaging (see nix/hal-fhs.nix for why).
        # `nix develop .#hal` drops into an FHS sandbox with HAL's own
        # documented build dependencies; build/run HAL inside it exactly as
        # its own README describes.
        hal = (import ./nix/hal-fhs.nix { inherit pkgs; }).env;
      };
    };
}
