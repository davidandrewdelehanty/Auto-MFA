# -*- mode: python ; coding: utf-8 -*-

# PyInstaller spec for the Auto-MFA GUI shell.
#
# The GUI is deliberately tiny: MFA itself lives in the bundled conda-pack
# "runtime" folder next to the exe, and is invoked as a separate process by the
# GUI.  Nothing MFA/torch-related is frozen here, so the build is fast and the
# exe stays small.

import sys

# Deep module graphs (torch etc.) are NOT bundled anymore, but keep a larger
# limit anyway in case future imports pull in heavy graphs.
sys.setrecursionlimit(10000)

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules("app")

a = Analysis(
    ["app/main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Must never be frozen into the GUI exe; they belong to the runtime.
        "torch", "torchaudio", "montreal_forced_aligner", "kalpy",
        "librosa", "numba", "llvmlite", "scipy", "matplotlib", "seaborn",
        "sklearn", "scikit_learn", "kaldi",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Auto-MFA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Auto-MFA",
)
