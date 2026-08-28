# HAL (https://github.com/emsec/hal) has no upstream Nix packaging and is a
# large CMake/Qt5 C++ project that assumes a normal FHS layout (system Qt,
# Boost, Z3, etc. at conventional paths) - the same shape of problem
# install_dependencies.sh solves with `apt-get install` on Ubuntu.
#
# Rather than hand-writing a from-scratch Nix derivation (patching every
# RPATH/include path HAL's CMake expects), this wraps buildFHSUserEnv: an
# FHS-emulating sandbox with exactly the packages install_dependencies.sh's
# Ubuntu branch lists, so HAL's own build system can find everything the
# normal way. The package set below is a direct nixpkgs-name translation of
# that script's `apt-get install` line - keep the two in sync if HAL's own
# dependency list changes upstream.
{ pkgs }:

pkgs.buildFHSUserEnv {
  name = "hal-fhs";

  targetPkgs = pkgs: with pkgs; [
    # build-essential, cmake, pkgconf, ninja-build, ccache, autoconf, autotools-dev
    gcc
    gnumake
    cmake
    ninja
    pkg-config
    ccache
    autoconf
    automake
    git

    # libboost-all-dev
    boost

    # qtbase5-dev, libqt5svg5-dev
    libsForQt5.qtbase
    libsForQt5.qtsvg

    # libpython3-dev, pybind11-dev/python3-pybind11, python3-pip,
    # python3-jedi, python3-dateutil, python3-sphinx, python3-sphinx-rtd-theme
    python3
    python3Packages.pybind11
    python3Packages.pip
    python3Packages.jedi
    python3Packages.python-dateutil
    python3Packages.sphinx
    python3Packages.sphinx-rtd-theme

    # libsodium-dev, rapidjson-dev, libspdlog-dev, libz3-dev/z3,
    # libreadline-dev, libgraphviz-dev/graphviz, libomp-dev,
    # libsuitesparse-dev, verilator
    libsodium
    rapidjson
    spdlog
    z3
    readline
    graphviz
    llvmPackages.openmp
    suitesparse
    verilator

    # lcov, gcovr, doxygen (documentation/coverage - optional but harmless)
    lcov
    gcovr
    doxygen
  ];

  # Drop into a normal shell inside the sandbox; from there HAL builds
  # exactly per its own README (`mkdir build && cd build && cmake -G Ninja
  # .. && ninja`), same as on a real Ubuntu box - nothing HAL-specific is
  # encoded here on purpose, so this stays correct if HAL's build steps
  # change upstream.
  runScript = "bash";
}
