#!/bin/bash
# Build vmm_monitor against the installed p2basket-sc (read-only use of it).
#   ./build.sh [OUT_DIR]      default OUT_DIR: ~/vmm_monitor_build
# Rebuild after every new /local/p2/deploy/install_* (the library has no stable ABI).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="${P2B_DEPLOY:-/local/p2/deploy}"
OUT="${1:-$HOME/vmm_monitor_build}"
mkdir -p "$OUT"
VER="$("$DEPLOY/bin/p2basket-sc" --version 2>/dev/null | tail -1 | sed 's/^--- //')"
g++ -std=c++23 -O1 -w -I"$DEPLOY/include/p2b" -I"$DEPLOY/include" \
    "$HERE/vmm_monitor.cpp" -o "$OUT/vmm_monitor" \
    -L"$DEPLOY/lib" -Wl,-rpath,"$(readlink -f "$DEPLOY/lib")" \
    -lp2b-run -lp2b-sc -lmdcs-utl -lboost_program_options -lboost_json -lyaml-cpp
echo "$VER" > "$OUT/BUILT_AGAINST"
echo "built $OUT/vmm_monitor against $VER"
