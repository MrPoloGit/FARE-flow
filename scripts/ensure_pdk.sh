#!/usr/bin/env bash
# Lazily fetches and activates the exact pinned sky130 PDK version this flow
# was validated against, via ciel. Only the GDS entry path calls this -
# SPICE/netlist entry points never need a PDK at all, so this is never run
# just for entering the default dev shell.
#
# The version below is not a default, it's a correctness pin: a mismatched
# PDK version makes Magic silently produce wrong or missing connectivity,
# with no error at extraction time (see docs/flow.md's Magic gotchas). Bump
# it deliberately, and re-validate extraction against a known design after
# doing so.
set -euo pipefail

SKY130_VERSION="8afc8346a57fe1ab7934ba5a6056ea8b43078e71"

if ! command -v ciel >/dev/null 2>&1; then
  echo "ensure_pdk: ciel not on PATH - run this from 'nix develop' (see flake.nix)" >&2
  exit 1
fi

if ! ciel ls --pdk sky130 2>/dev/null | grep -q "$SKY130_VERSION"; then
  echo "ensure_pdk: sky130 $SKY130_VERSION not found locally, fetching via ciel (first use only)..." >&2
fi

# `ciel enable` fetches-then-activates when the version isn't present yet,
# and is a fast no-op re-activation when it already is - one call handles
# both cases.
ciel enable --pdk sky130 "$SKY130_VERSION" >&2

ciel path --pdk sky130 "$SKY130_VERSION"
