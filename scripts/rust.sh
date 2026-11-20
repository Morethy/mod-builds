#!/usr/bin/env bash
# The Rust a build's SDK was made with (sdk.json's toolchain.rust, which `store.py sdk` puts in RUST),
# with the targets of ABIS: a mod's library has to come out of the same compiler as the game's, or the
# game's loader refuses it (its identity names the rustc). Only trusted code runs here, before any mod.
set -euo pipefail
[[ "${RUST:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Not a Rust release: ${RUST:-}"; exit 1; }
targets=$(python3 -I -c '
import os, sys
sys.path.insert(0, "scripts")
import store
print(" ".join(store.TARGETS[a] for a in store.abis()))')
rustup toolchain install "$RUST" --profile minimal --no-self-update
for target in $targets; do
  rustup target add --toolchain "$RUST" "$target"
done
rustup default "$RUST"
rustc -vV
