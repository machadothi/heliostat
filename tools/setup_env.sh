#!/usr/bin/env bash
# Prepare everything needed to work on the heliostat firmware, on Linux.
#
#   tools/setup_env.sh                  # everything: Python env + firmware toolchain
#   tools/setup_env.sh --python-only    # just the Python env (tests, deploy, tools)
#   tools/setup_env.sh --check          # report what is missing, change nothing
#   tools/setup_env.sh --apt            # also install missing system packages (sudo)
#
# Options:
#   --esp-dir DIR   where ESP-IDF and MicroPython live (default: $ESP_DIR or ~/esp;
#                   tools/deploy.py looks in the same place)
#   --venv DIR      the Python virtual environment (default: .venv in the repo)
#   --test          run the test suite at the end
#
# Safe to run again: every step checks first and skips what is already done.
# What it sets up, and why each piece is needed, is in the README ("Setting up").

set -euo pipefail

# --- versions: change them HERE (and only here) --------------------------------
IDF_VERSION="v5.2.2"          # ESP-IDF the firmware is built with
MICROPYTHON_VERSION="v1.24.1"  # MicroPython (mpy-cross in pyproject.toml must match)
IDF_TARGETS="esp32,esp32s3"    # devkit + AtomS3R
# MicroPython submodules the esp32 port builds need (tinyusb: the S3 build
# includes its headers even with USB device mode off).
MICROPYTHON_SUBMODULES="lib/berkeley-db-1.xx lib/micropython-lib lib/tinyusb"
# Debian/Ubuntu packages. REQUIRED: without them nothing below works.
# RECOMMENDED: Espressif's list for ESP-IDF; the build itself brings cmake and
# ninja, and has been seen to work without the rest, so these are only reported.
APT_REQUIRED="git curl python3 python3-pip python3-venv"
APT_RECOMMENDED="wget flex bison gperf cmake ninja-build ccache libffi-dev libssl-dev dfu-util libusb-1.0-0"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ESP_DIR="${ESP_DIR:-$HOME/esp}"
VENV="$REPO/.venv"
PYTHON_ONLY=0
CHECK_ONLY=0
USE_APT=0
RUN_TESTS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python-only) PYTHON_ONLY=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --apt) USE_APT=1; shift ;;
        --test) RUN_TESTS=1; shift ;;
        --esp-dir) ESP_DIR="$2"; shift 2 ;;
        --venv) VENV="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

PROBLEMS=0
say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok    %s\n' "$*"; }
todo() { printf '   TODO  %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
run()  { if [[ $CHECK_ONLY == 1 ]]; then todo "would run: $*"; else "$@"; fi; }

# --- 1. system ------------------------------------------------------------------
say "System"
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "   This script is written for Linux (Debian/Ubuntu). On macOS follow the"
    echo "   README's manual steps; ESP-IDF documents its own macOS prerequisites."
fi

PY=python3
if ! command -v $PY >/dev/null; then
    todo "python3 is not installed"; exit 1
fi
if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    ok "$($PY --version)"
else
    todo "$($PY --version): need Python 3.10 or newer"; exit 1
fi

if command -v dpkg >/dev/null; then
    missing="" optional=""
    for p in $APT_REQUIRED; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
    for p in $APT_RECOMMENDED; do dpkg -s "$p" >/dev/null 2>&1 || optional="$optional $p"; done
    if [[ $USE_APT == 1 && $CHECK_ONLY == 0 && -n "$missing$optional" ]]; then
        echo "   installing:$missing$optional"
        sudo apt-get update -qq && sudo apt-get install -y $missing $optional
        missing="" optional=""
    fi
    if [[ -z "$missing" ]]; then ok "required system packages"; else
        todo "missing system packages:$missing"
        echo "         install them with: sudo apt-get install$missing   (or rerun with --apt)"
    fi
    if [[ -n "$optional" ]]; then
        echo "   note  recommended by Espressif, not installed:$optional (--apt installs them)"
    fi
else
    echo "   (not a Debian-family system: check ESP-IDF's prerequisites by hand)"
fi

# Serial ports (/dev/ttyUSB0, /dev/ttyACM0) belong to the dialout group.
if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    ok "$USER is in the dialout group (serial port access)"
else
    todo "$USER is not in the dialout group: sudo usermod -aG dialout $USER, then log out and in"
fi

# --- 2. Python environment ----------------------------------------------------------
say "Python environment ($VENV)"
if [[ ! -x "$VENV/bin/python" ]]; then
    run $PY -m venv "$VENV"
fi
if [[ -x "$VENV/bin/python" ]]; then
    if [[ $CHECK_ONLY == 1 ]]; then
        if (cd "$REPO" && "$VENV/bin/python" -c 'import pytest, numpy, serial, esptool, mpremote, pygeomag, PIL, bleak' 2>/dev/null) \
           && "$VENV/bin/mpy-cross" --version >/dev/null 2>&1; then
            ok "all Python dependencies importable"
        else
            todo "Python dependencies incomplete: run without --check"
        fi
    else
        "$VENV/bin/python" -m pip install -q --upgrade pip
        # Everything in pyproject.toml's "dev" extra: test + board + tools.
        "$VENV/bin/python" -m pip install -q -e "$REPO[dev]"
        ok "installed: $("$VENV/bin/python" -m pip list --format=freeze 2>/dev/null \
            | grep -iE '^(pytest|numpy|mpremote|pyserial|esptool|mpy-cross|pygeomag|pillow|bleak)==' | tr '\n' ' ')"
    fi
fi

# --- 3. firmware toolchain ------------------------------------------------------------
if [[ $PYTHON_ONLY == 0 ]]; then
    say "ESP-IDF $IDF_VERSION ($ESP_DIR/esp-idf)"
    IDF="$ESP_DIR/esp-idf"
    if [[ -d "$IDF/.git" ]]; then
        have="$(git -C "$IDF" describe --tags --exact-match 2>/dev/null || git -C "$IDF" rev-parse --short HEAD)"
        if [[ "$have" == "$IDF_VERSION" ]]; then ok "checked out"; else
            todo "$IDF is at $have, not $IDF_VERSION: move it aside, then rerun"; fi
    else
        run mkdir -p "$ESP_DIR"
        run git clone -q -b "$IDF_VERSION" --recursive --shallow-submodules --depth 1 \
            https://github.com/espressif/esp-idf.git "$IDF"
    fi
    if [[ -x "$IDF/install.sh" ]]; then
        # Downloads the cross-compilers into ~/.espressif; quick when already there.
        if [[ $CHECK_ONLY == 1 ]]; then
            if [[ -d "$HOME/.espressif/tools" ]]; then ok "toolchains in ~/.espressif"; else
                todo "toolchains not installed: run without --check"; fi
        else
            (cd "$IDF" && ./install.sh "$IDF_TARGETS" >/dev/null) && ok "toolchains for $IDF_TARGETS"
        fi
    fi

    say "MicroPython $MICROPYTHON_VERSION ($ESP_DIR/micropython)"
    MP="$ESP_DIR/micropython"
    if [[ -d "$MP/.git" ]]; then
        have="$(git -C "$MP" describe --tags --exact-match 2>/dev/null || git -C "$MP" rev-parse --short HEAD)"
        if [[ "$have" == "$MICROPYTHON_VERSION" ]]; then ok "checked out"; else
            todo "$MP is at $have, not $MICROPYTHON_VERSION: move it aside, then rerun"; fi
    else
        run git clone -q -b "$MICROPYTHON_VERSION" --depth 1 https://github.com/micropython/micropython.git "$MP"
    fi
    if [[ -d "$MP/.git" ]]; then
        for sub in $MICROPYTHON_SUBMODULES; do
            if [[ -n "$(ls -A "$MP/$sub" 2>/dev/null)" ]]; then
                ok "submodule $sub"
            else
                run git -C "$MP" submodule update --init --depth 1 "$sub"
            fi
        done
    fi
fi

# --- 4. check ---------------------------------------------------------------------------
if [[ $RUN_TESTS == 1 && $CHECK_ONLY == 0 ]]; then
    say "Tests"
    (cd "$REPO" && "$VENV/bin/python" -m pytest -q)
fi

say "Summary"
if [[ $PROBLEMS == 0 ]]; then
    echo "   Ready. Activate the environment with:   source $VENV/bin/activate"
    echo "   Then:  pytest                                   (tests)"
    [[ $PYTHON_ONLY == 0 ]] && echo "          python3 tools/deploy.py --dry-run --board atoms3r   (build the firmware)"
    exit 0
else
    echo "   $PROBLEMS thing(s) to fix: see TODO above."
    exit 1
fi
