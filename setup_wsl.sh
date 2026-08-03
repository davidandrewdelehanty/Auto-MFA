#!/usr/bin/env bash
# Set up Auto-MFA to run directly under WSL Ubuntu (or any Linux), instead of
# as a packaged Windows .exe.
#
# Why this exists: MFA wraps Kaldi, which spawns many short-lived
# subprocesses per alignment job. On native Windows that means paying
# CreateProcess overhead (no fork()) on every one of them, plus Windows
# Defender real-time-scanning freshly-extracted, unsigned binaries, plus a
# parallel (--num_jobs > 1) TextGrid export bug that has been observed
# silently dropping most of its own output on Windows. None of that applies
# running a normal conda-forge Linux build of MFA under WSL -- this is also
# MFA/Kaldi's actual primary, best-tested platform.
#
# Unlike build.ps1, this does NOT freeze the app into a standalone .exe or
# conda-pack/relocate the environment -- there's no need to, since we're not
# distributing this to someone else's machine. It just creates a normal
# conda env with MFA in it and runs the app straight from source against
# that env's python, the same way "Running from source (development)" in
# README.md already describes for a native Linux/Mac setup.
#
# Usage:
#   ./setup_wsl.sh
#   ./run.sh              # launches the GUI (this generates run.sh for you)
#
# Re-running this script is safe and fast -- it skips steps that are already
# done (existing Miniconda install, existing env).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_DIR="${AUTO_MFA_CONDA_DIR:-$HOME/miniconda3}"
ENV_NAME="auto-mfa"
ENV_DIR="$CONDA_DIR/envs/$ENV_NAME"

step() { printf '\n=== %s\n' "$1"; }

if [ -n "${WSL_DISTRO_NAME:-}" ]; then
    case "$ROOT" in
        /mnt/c/*|/mnt/[a-z]/*)
            echo "NOTE: this project lives at $ROOT, on the Windows-side filesystem"
            echo "(under /mnt/...). That's fine for the small app source itself, but"
            echo "for real speed, point the GUI's 'Book folder' and output folder at"
            echo "paths INSIDE the Linux filesystem (e.g. ~/audiobooks), not /mnt/c/...:"
            echo "crossing that Windows<->Linux filesystem boundary for the audio"
            echo "files and MFA's own working files is slow and would eat into the"
            echo "exact overhead this script exists to avoid."
            ;;
    esac
fi

step "Ensuring a Cyrillic-capable system font is installed"
# Tk's glyph rendering goes through the *system's* fontconfig, not the
# conda env -- a minimal WSL/Ubuntu image can ship with essentially no
# fonts installed at all, which makes Cyrillic text (routine here --
# these are Russian audiobooks) render as boxes/garbled placeholder
# glyphs no matter which font family the app asks for. Best-effort: safe
# to skip if there's no apt-get (not Debian/Ubuntu) or no sudo access --
# the app still runs either way, just with worse font rendering until a
# Cyrillic-capable font is available some other way.
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y >/dev/null 2>&1 || true
    sudo apt-get install -y fonts-dejavu-core fonts-noto-core >/dev/null 2>&1 || true
fi

if [ ! -x "$CONDA_DIR/bin/conda" ]; then
    step "Downloading Miniconda into $CONDA_DIR (one-time)"
    installer="$(mktemp /tmp/miniconda-XXXXXX.sh)"
    curl -fsSL -o "$installer" \
        "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
    bash "$installer" -b -p "$CONDA_DIR"
    rm -f "$installer"
fi

CONDA="$CONDA_DIR/bin/conda"

step "Configuring conda (conda-forge only)"
# Same reasoning as build.ps1: don't depend on Anaconda's commercial
# "defaults" channel (requires ToS acceptance). Every one of these is
# allowed to fail harmlessly (e.g. "defaults" already removed on a re-run).
"$CONDA" config --set auto_update_conda false || true
"$CONDA" config --remove channels defaults || true
"$CONDA" config --add channels conda-forge || true
"$CONDA" config --set channel_priority strict || true

if [ ! -x "$ENV_DIR/bin/python" ]; then
    step "Creating the MFA environment (conda-forge; downloads ~1-2 GB)"
    # `tk` is listed explicitly so tkinter is guaranteed to work -- don't
    # rely on it coming along for free with `python`, the same mistake that
    # caused the "no module named tkinter" issue on the Windows build.
    # `openfst`/`pynini` are listed explicitly too: they're transitive deps
    # of montreal-forced-aligner (needed for the g2p step's `fstcompile` and
    # friends), but a partial/interrupted create can leave them out of a
    # from-scratch solve, and there's no cheap way to detect that after the
    # fact -- pinning them here makes a fresh create fail loudly instead.
    "$CONDA" create -p "$ENV_DIR" -y --override-channels -c conda-forge \
        "python=3.11" tk montreal-forced-aligner "kaldi=*=cpu*" openfst pynini ffmpeg
fi

step "Verifying tkinter and MFA both import cleanly"
# Not all MFA releases expose a __version__ attribute, so don't depend on
# it -- a clean import of all three modules is all we actually need here.
"$ENV_DIR/bin/python" -c \
    "import tkinter, montreal_forced_aligner, kalpy; print('ok: tkinter + montreal_forced_aligner + kalpy import cleanly')"

step "Verifying openfst CLI tools are on the env's PATH"
# fstcompile etc. are compiled binaries, not Python-importable, so the check
# above wouldn't have caught them being missing -- MFA's g2p step needs them
# directly on PATH at runtime.
if [ ! -x "$ENV_DIR/bin/fstcompile" ]; then
    echo "ERROR: $ENV_DIR/bin/fstcompile not found -- the openfst package"
    echo "didn't install correctly. Try:"
    echo "  $CONDA env remove -p $ENV_DIR -y"
    echo "  bash setup_wsl.sh"
    exit 1
fi
echo "ok: fstcompile found at $ENV_DIR/bin/fstcompile"

step "Done"
echo
echo "The MFA environment is ready at:"
echo "  $ENV_DIR"
echo
echo "You don't launch anything from here. Set up jobs in the Auto-MFA GUI on"
echo "the Windows side (it needs only tkinter -- no conda, no MFA), press"
echo "GENERATE SCRIPT, and it writes an align_<slug>.sh next to your book."
echo "Then run that script here, e.g.:"
echo "  bash /mnt/c/Users/<you>/path/to/book/align_<slug>.sh"
echo
echo "The GUI copies the exact command to your clipboard when it generates it."
