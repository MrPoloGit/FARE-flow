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
      pkgs = import nixpkgs { inherit system; };

      # ciel's own overlay is a "python overlay" (nix-eda.composePythonOverlay)
      # that patches pkgs.python3.pkgs.ciel, not a top-level pkgs.ciel - and
      # nixpkgs already has an unrelated top-level `ciel` (AOSC's container
      # manager) that would otherwise silently win instead. Kept in its own
      # `pkgs` instance (rather than folded into the `pkgs` above) because
      # nix-eda's overlay also re-pins other packages we use (e.g. verilator)
      # to versions/patches that don't build cleanly here - scoping it keeps
      # that blast radius to just the one derivation we actually want.
      cielPkgs = import nixpkgs {
        inherit system;
        overlays = [ nix-eda.overlays.default ciel.overlays.default ];
      };
    in
    {
      devShells.${system} = {
        # Everything already packaged in plain nixpkgs. Does NOT include the
        # sky130 PDK itself - that's fetched lazily, only by the GDS entry
        # path, via scripts/ensure_pdk.sh (see that script for why).
        #
        # HAL's own build/runtime deps live here too, not in a separate FHS
        # sandbox: an earlier version of this shell used pkgs.buildFHSEnv on
        # the assumption that HAL's CMake needed a conventional /usr-style
        # layout the way apt gives it on Ubuntu. Real-world testing on this
        # project disproved that (a plain host apt install builds HAL fine -
        # see ../hal/ and its own install_dependencies.sh) and the FHS
        # sandbox itself turned out to have its own real cost: it needs
        # unprivileged Linux user namespaces via bubblewrap, which some
        # hardened setups (e.g. Ubuntu with
        # kernel.apparmor_restrict_unprivileged_userns=1) block outright.
        # A plain shell is both simpler and more portable.
        default = pkgs.mkShell {
          packages = with pkgs; [
            magic-vlsi
            klayout
            yosys
            sby
            verilator
            z3
            boolector
            cielPkgs.python3.pkgs.ciel
            python3

            # Build deps for ReGDS-Logic-Gate-Extraction (see docs/flow.md) -
            # cmdline.h is vendored in ReGDS's own src/ tree, nothing else to
            # add for it.
            cmake
            ninja
            gnumake
            flex
            bison
            boost
            gtest
            pkg-config

            # HAL build/runtime deps (nixpkgs-name translation of HAL's own
            # install_dependencies.sh Ubuntu apt list - keep the two in sync
            # if HAL's own dependency list changes upstream). autoconf/
            # automake/git/cmake/ninja/pkg-config/boost already covered
            # above.
            ccache
            libsForQt5.qtbase
            libsForQt5.qtsvg
            python3Packages.pybind11
            python3Packages.pip
            python3Packages.jedi
            python3Packages.python-dateutil
            python3Packages.sphinx
            python3Packages.sphinx-rtd-theme
            libsodium
            rapidjson
            spdlog
            readline
            graphviz
            llvmPackages.openmp
            suitesparse
            lcov
            gcovr
            doxygen
          ];

          shellHook = ''
            echo "FARE-flow dev shell (Magic, KLayout, Yosys, SymbiYosys, Verilator, Z3, Boolector, ciel, HAL build deps)"
            echo "sky130 PDK is not fetched yet - run 'make pdk' before a GDS-entry extraction."
            echo "Run 'make help' for the available pipeline targets."
          '';
        };
      };
    };
}
