#!/bin/bash
# Install / check the P2 readout packages (p2-decode, p2-mapping, p2-monitor)
# at exactly the versions pinned in p2_readout_versions.txt.
#
#   p2_readout.sh check   [READOUT_DIR]   does the venv match the pins? (exit 1 if not)
#   p2_readout.sh build   OUT_DIR         wheels of the pinned GitHub tags (needs network)
#   p2_readout.sh deploy  HOST            build here, copy to HOST's wheelhouse, install there
#   p2_readout.sh install [PINS_FILE]     on the DAQ box: install the pins from the wheelhouse
#
# READOUT_DIR defaults to $P2_READOUT_DIR or /local/p2/p2-readout (venv/, wheels/, src/).
# The DAQ box's network is unreliable, so the box never builds: `deploy` builds
# the wheels on a PC that has network and installs them there offline
# (pip --no-index --no-deps), as p2_readout_config.py describes.
#
# `install` refuses while dumpcap is capturing (a run in progress would have its
# captures processed by two versions); pass --force to override. Running
# services keep the old code until restarted: restart vmm_processor,
# vmm_qa_watcher and vmm_live at a sub-run boundary afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
READOUT_DIR="${P2_READOUT_DIR:-/local/p2/p2-readout}"
GH="https://github.com/akallitss"
PYTHON="${PYTHON:-python3}"

die() { echo "p2_readout: $*" >&2; exit 1; }

pins_file() {   # the pins next to this script, unless given
    local f="${1:-$HERE/../p2_readout_versions.txt}"
    [ -f "$f" ] || die "no pins file: $f"
    echo "$f"
}

pins() {        # NAME==VERSION lines, comments and blanks dropped
    grep -E '^[[:space:]]*[A-Za-z0-9_.-]+==[0-9]' "$1" | sed 's/[[:space:]]//g'
}

cmd_check() {
    local dir="${1:-$READOUT_DIR}" pf; pf="$(pins_file "${2:-}")"
    local py="$dir/venv/bin/python" bad=0
    [ -x "$py" ] || die "no venv at $dir/venv"
    while IFS= read -r pin; do
        local name="${pin%%==*}" want="${pin#*==}" have src=""
        have="$("$py" -c "import importlib.metadata as m,sys; print(m.version(sys.argv[1]))" "$name" 2>/dev/null || echo none)"
        if [ -d "$dir/src/$name/.git" ]; then
            src="  (src/$name at $(git -C "$dir/src/$name" describe --tags --always 2>/dev/null))"
        fi
        if [ "$have" = "$want" ]; then
            printf '  ok    %-11s %s%s\n' "$name" "$have" "$src"
        else
            printf '  DIFF  %-11s installed %s, pinned %s%s\n' "$name" "$have" "$want" "$src"; bad=1
        fi
    done < <(pins "$pf")
    return $bad
}

cmd_build() {
    local out="${1:?usage: p2_readout.sh build OUT_DIR}" pf; pf="$(pins_file "${2:-}")"
    mkdir -p "$out"
    "$PYTHON" -m pip --version >/dev/null 2>&1 || die "$PYTHON has no pip (set PYTHON=...)"
    while IFS= read -r pin; do
        local name="${pin%%==*}" ver="${pin#*==}"
        echo "build $name $ver  <- $GH/$name.git@v$ver"
        "$PYTHON" -m pip wheel -q --no-deps --disable-pip-version-check -w "$out" \
            "git+$GH/$name.git@v$ver"
    done < <(pins "$pf")
}

cmd_install() {
    local force=0 pf=""
    for a in "$@"; do case "$a" in --force) force=1 ;; *) pf="$a" ;; esac; done
    pf="$(pins_file "$pf")"
    local dir="$READOUT_DIR"
    if [ $force = 0 ] && pgrep -x dumpcap >/dev/null; then
        die "dumpcap is capturing: install between runs (or pass --force)"
    fi
    local -a want=()
    while IFS= read -r pin; do want+=("$pin"); done < <(pins "$pf")
    echo "install into $dir/venv: ${want[*]}"
    "$dir/venv/bin/pip" install -q --disable-pip-version-check --no-index --no-deps \
        --find-links "$dir/wheels" "${want[@]}"
    # the tagged sources, for running the packages' tests on this box
    for pin in "${want[@]}"; do
        local name="${pin%%==*}" tag="v${pin#*==}" s="$dir/src/${pin%%==*}"
        [ -d "$s/.git" ] || continue
        if [ -n "$(git -C "$s" status --porcelain)" ]; then
            echo "  src/$name has local changes: left at $(git -C "$s" describe --tags --always)"; continue
        fi
        git -C "$s" rev-parse -q --verify "refs/tags/$tag" >/dev/null || \
            timeout 60 git -C "$s" fetch -q --tags origin 2>/dev/null || true
        if git -C "$s" checkout -q "$tag" 2>/dev/null; then
            echo "  src/$name -> $tag"
        else
            echo "  src/$name: tag $tag not available offline, left at $(git -C "$s" describe --tags --always)"
        fi
    done
    cmd_check "$dir" "$pf"
}

cmd_deploy() {
    local host="${1:?usage: p2_readout.sh deploy HOST [--force]}"; shift
    local pf; pf="$(pins_file)"
    TMP_DEPLOY="$(mktemp -d)"; trap 'rm -rf "$TMP_DEPLOY"' EXIT
    local tmp="$TMP_DEPLOY"
    cmd_build "$tmp/wheels" "$pf"
    echo "copy wheels -> $host:$READOUT_DIR/wheels/"
    scp -q "$tmp"/wheels/*.whl "$host:$READOUT_DIR/wheels/"
    scp -q "$pf" "$host:$READOUT_DIR/p2_readout_versions.deployed.txt"
    # run this same script on the host, reading it from stdin
    ssh "$host" "P2_READOUT_DIR='$READOUT_DIR' bash -s -- install '$READOUT_DIR/p2_readout_versions.deployed.txt' $*" < "${BASH_SOURCE[0]}"
}

case "${1:-}" in
    check)   shift; cmd_check "$@" ;;
    build)   shift; cmd_build "$@" ;;
    install) shift; cmd_install "$@" ;;
    deploy)  shift; cmd_deploy "$@" ;;
    *) sed -n '2,19p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
