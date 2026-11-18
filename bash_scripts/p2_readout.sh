#!/bin/bash
# Install / check the P2 readout packages (p2-decode, p2-mapping, p2-monitor)
# at exactly the versions pinned in p2_readout_versions.txt. Everything comes
# from git: the DAQ code from this repo, each package from its own repo's tag.
#
#   p2_readout.sh deploy  HOST [--force]  from any PC: on HOST, `git pull --ff-only`
#                                         the DAQ checkout, then run its `install`
#   p2_readout.sh install [--force]       on the DAQ box: fetch the pinned tags into
#                                         READOUT_DIR/src/<name> (git) and install
#                                         them from those checkouts
#   p2_readout.sh check   [READOUT_DIR]   do the venvs match the pins? (exit 1 if not)
#
# READOUT_DIR defaults to $P2_READOUT_DIR or /local/p2/p2-readout (venv/, src/).
# The packages are fetched over SSH ($P2_GIT, default git@github.com:akallitss):
# the DAQ box reaches GitHub over SSH, not HTTPS. src/<name> is a plain git
# checkout of github.com/akallitss/<name>, detached at the pinned tag vVERSION;
# `install` refuses one with local changes. The packages are built from it with
# the venv's own setuptools (pip --no-index --no-deps --no-build-isolation), so
# installing needs no PyPI and never changes their numpy/pandas.
# The DAQ's own .venv ($P2_DAQ_VENV, default $P2_DAQ_DIR/.venv; P2_DAQ_DIR
# defaults to /local/p2/DAQ_Control_VMM_Beam) imports p2-decode and p2-mapping too
# (run_config_beam/setup_source, the Flask app, rate_guard, vmm_qa): `install`
# puts the same two there and `check` checks both venvs.
#
# `install` refuses while dumpcap is capturing (a run in progress would have its
# captures processed by two versions); pass --force to override. Running
# services keep the old code until restarted: restart vmm_processor,
# vmm_qa_watcher and vmm_live at a sub-run boundary afterwards (vmm_flask and
# vmm_rate_guard, which import them from the DAQ .venv, at their next restart).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
READOUT_DIR="${P2_READOUT_DIR:-/local/p2/p2-readout}"
DAQ_DIR="${P2_DAQ_DIR:-/local/p2/DAQ_Control_VMM_Beam}"
DAQ_VENV="${P2_DAQ_VENV:-$DAQ_DIR/.venv}"
DAQ_PKGS="p2-decode p2-mapping"     # the pinned packages the DAQ .venv imports
P2_GIT="${P2_GIT:-git@github.com:akallitss}"

die() { echo "p2_readout: $*" >&2; exit 1; }

pins_file() {   # the pins next to this script, unless given
    local f="${1:-$HERE/../p2_readout_versions.txt}"
    [ -f "$f" ] || die "no pins file: $f"
    echo "$f"
}

pins() {        # NAME==VERSION lines, comments and blanks dropped
    grep -E '^[[:space:]]*[A-Za-z0-9_.-]+==[0-9]' "$1" | sed 's/[[:space:]]//g'
}

daq_pins() {    # the pins of DAQ_PKGS
    local pin
    while IFS= read -r pin; do
        case " $DAQ_PKGS " in *" ${pin%%==*} "*) echo "$pin" ;; esac
    done < <(pins "$1")
}

version_in() {  # installed version of package $2 in python $1, or "none"
    "$1" -c "import importlib.metadata as m,sys; print(m.version(sys.argv[1]))" "$2" 2>/dev/null || echo none
}

cmd_check() {
    local dir="${1:-$READOUT_DIR}" pf; pf="$(pins_file "${2:-}")"
    local py="$dir/venv/bin/python" bad=0
    [ -x "$py" ] || die "no venv at $dir/venv"
    echo "$dir/venv:"
    while IFS= read -r pin; do
        local name="${pin%%==*}" want="${pin#*==}" have src=""
        have="$(version_in "$py" "$name")"
        if [ -d "$dir/src/$name/.git" ]; then
            src="  (src/$name at $(git -C "$dir/src/$name" describe --tags --always 2>/dev/null))"
        fi
        if [ "$have" = "$want" ]; then
            printf '  ok    %-11s %s%s\n' "$name" "$have" "$src"
        else
            printf '  DIFF  %-11s installed %s, pinned %s%s\n' "$name" "$have" "$want" "$src"; bad=1
        fi
    done < <(pins "$pf")
    if [ -x "$DAQ_VENV/bin/python" ]; then
        echo "$DAQ_VENV:"
        while IFS= read -r pin; do
            local name="${pin%%==*}" want="${pin#*==}" have
            have="$(version_in "$DAQ_VENV/bin/python" "$name")"
            if [ "$have" = "$want" ]; then
                printf '  ok    %-11s %s\n' "$name" "$have"
            else
                printf '  DIFF  %-11s installed %s, pinned %s\n' "$name" "$have" "$want"; bad=1
            fi
        done < <(daq_pins "$pf")
    else
        echo "  (no DAQ venv at $DAQ_VENV: not checked)"
    fi
    return $bad
}

sync_src() {    # src/<name> -> a clean checkout of tag $2 from $P2_GIT/<name>.git
    local name="$1" tag="$2" s="$READOUT_DIR/src/$1" url="$P2_GIT/$1.git"
    if [ ! -d "$s/.git" ]; then
        echo "  clone $url -> src/$name"
        timeout 300 git clone -q "$url" "$s" || die "cannot clone $url"
    fi
    [ "$(git -C "$s" remote get-url origin)" = "$url" ] || git -C "$s" remote set-url origin "$url"
    [ -z "$(git -C "$s" status --porcelain)" ] || \
        die "src/$name has local changes (git -C $s status): commit them to the package's repo, or discard them"
    timeout 120 git -C "$s" fetch -q --tags --force origin || die "cannot fetch $url"
    git -C "$s" rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null || die "$url has no tag $tag"
    git -C "$s" checkout -q --detach "$tag"
    echo "  src/$name -> $tag ($(git -C "$s" rev-parse --short HEAD))"
}

pip_from_src() {    # pip_from_src VENV NAME...: install the src checkouts into VENV
    local venv="$1"; shift
    local -a dirs=()
    for n in "$@"; do dirs+=("$READOUT_DIR/src/$n"); done
    "$venv/bin/pip" install -q --disable-pip-version-check --no-index --no-deps \
        --no-build-isolation "${dirs[@]}"
}

cmd_install() {
    local force=0 pf=""
    for a in "$@"; do case "$a" in --force) force=1 ;; *) pf="$a" ;; esac; done
    pf="$(pins_file "$pf")"
    if [ $force = 0 ] && pgrep -x dumpcap >/dev/null; then
        die "dumpcap is capturing: install between runs (or pass --force)"
    fi
    [ -x "$READOUT_DIR/venv/bin/pip" ] || die "no venv at $READOUT_DIR/venv"
    mkdir -p "$READOUT_DIR/src"
    local -a names=() daq=()
    echo "fetch the pinned tags ($P2_GIT):"
    while IFS= read -r pin; do
        sync_src "${pin%%==*}" "v${pin#*==}"; names+=("${pin%%==*}")
    done < <(pins "$pf")
    while IFS= read -r pin; do daq+=("${pin%%==*}"); done < <(daq_pins "$pf")

    echo "install into $READOUT_DIR/venv: ${names[*]}"
    pip_from_src "$READOUT_DIR/venv" "${names[@]}"
    if [ -x "$DAQ_VENV/bin/pip" ]; then
        echo "install into $DAQ_VENV: ${daq[*]}"
        pip_from_src "$DAQ_VENV" "${daq[@]}"
    else
        echo "no DAQ venv at $DAQ_VENV (set P2_DAQ_VENV): only $READOUT_DIR/venv installed"
    fi
    cp "$pf" "$READOUT_DIR/p2_readout_versions.deployed.txt"
    cmd_check "$READOUT_DIR" "$pf"
}

cmd_deploy() {
    local host="${1:?usage: p2_readout.sh deploy HOST [--force]}"; shift
    # the DAQ code by git pull, then the pulled script installs the pulled pins
    ssh "$host" "set -e; cd '$DAQ_DIR'; git pull --ff-only; git log --oneline -1; \
        P2_READOUT_DIR='$READOUT_DIR' P2_DAQ_DIR='$DAQ_DIR' P2_DAQ_VENV='$DAQ_VENV' P2_GIT='$P2_GIT' \
        bash bash_scripts/p2_readout.sh install $*"
}

case "${1:-}" in
    check)   shift; cmd_check "$@" ;;
    install) shift; cmd_install "$@" ;;
    deploy)  shift; cmd_deploy "$@" ;;
    *) sed -n '2,29p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
