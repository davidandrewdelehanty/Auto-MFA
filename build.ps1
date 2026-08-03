# Build Auto-MFA into a self-contained folder: dist\Auto-MFA\
#
#   Auto-MFA.exe   small PyInstaller GUI shell (tkinter only)
#   runtime\       relocatable conda-pack environment with MFA + kalpy + kaldi
#                  + ffmpeg; the GUI runs MFA inside this runtime as a separate
#                  process.
#
# MFA 3.x requires `kalpy`/`kaldi`, which are only distributed through conda
# (there are no pip wheels), so a pure pip + PyInstaller bundle is impossible.
# conda-pack makes the created environment relocatable, so the whole dist
# folder can be moved anywhere.
#
# Requirements: Windows, PowerShell 5.1+.  Conda is auto-downloaded into
# build\miniconda on first run (or use your own `conda`/`mamba`).

param(
    [string]$PythonVersion = "3.10",   # for the small GUI-shell venv only
    [switch]$SkipGui,                  # skip rebuilding the GUI exe
    [switch]$SkipRuntime,              # skip rebuilding the MFA runtime
    [switch]$NoConda                   # fail instead of downloading Miniconda
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$build = Join-Path $root "build"
$dist = Join-Path $root "dist\Auto-MFA"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$mini = Join-Path $build "miniconda"
$condaExe = Join-Path $mini "Scripts\conda.exe"
$runtimeEnv = Join-Path $build "runtime_env"
$runtimeDest = Join-Path $dist "runtime"

function Write-Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------- GUI shell
if (-not $SkipGui) {
    Write-Step "Building GUI shell ($dist\Auto-MFA.exe)"

    # gui.py is a Tkinter app, but PyInstaller can only bundle tkinter if the
    # Python building it actually HAS a working tkinter -- some Python
    # installs (a "customize install" with the tcl/tk component unchecked,
    # some minimal/embeddable builds) don't. When that happens PyInstaller
    # does NOT fail the build; it just silently produces an exe that dies at
    # launch with "ModuleNotFoundError: No module named 'tkinter'". Catch it
    # here instead, before wasting a build on it -- and check EVERY run, not
    # just when creating a fresh venv, since a stale `.venv` from an earlier
    # attempt (e.g. before this check existed) would otherwise keep being
    # silently reused forever.
    function Test-Tkinter($pythonExe) {
        # $ErrorActionPreference = "Stop" (set at the top of this script) makes
        # PowerShell treat ANY stderr write from a native command as a
        # terminating exception -- even one redirected with 2>$null, in some
        # PowerShell versions/hosts the redirect doesn't fully suppress the
        # escalation. python -c "import tkinter" on a broken install writes
        # exactly that (an ImportError traceback to stderr), which is why this
        # check itself was aborting the whole script instead of returning
        # $false like it's supposed to. Flip to "Continue" for just this call
        # so we can inspect $LASTEXITCODE ourselves instead.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $pythonExe -c "import tkinter" 2>$null 1>$null
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        return $LASTEXITCODE -eq 0
    }

    if (-not (Test-Path $venvPy)) {
        $py = (py -$PythonVersion -c "import sys; print(sys.executable)" 2>$null).Trim()
        if (-not $py) { throw "Python $PythonVersion not found. Run 'py -0p' to list versions." }
        if (-not (Test-Tkinter $py)) {
            throw ("Python at '$py' has no working tkinter, so it can't build a Tkinter " +
                "GUI. Fix: reinstall Python $PythonVersion from https://python.org (the " +
                "default installer includes 'tcl/tk and IDLE' -- just don't uncheck it " +
                "under Optional Features), or run 'py -0p' to see what else is installed " +
                "and pass a different one via -PythonVersion.")
        }
        & $py -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
    }

    if (-not (Test-Tkinter $venvPy)) {
        Write-Step "Existing .venv has no working tkinter -- recreating it"
        Remove-Item -Recurse -Force (Join-Path $root ".venv") -ErrorAction SilentlyContinue
        $py = (py -$PythonVersion -c "import sys; print(sys.executable)" 2>$null).Trim()
        if (-not $py) { throw "Python $PythonVersion not found. Run 'py -0p' to list versions." }
        if (-not (Test-Tkinter $py)) {
            throw ("Python at '$py' has no working tkinter, so it can't build a Tkinter " +
                "GUI. Fix: reinstall Python $PythonVersion from https://python.org (the " +
                "default installer includes 'tcl/tk and IDLE' -- just don't uncheck it " +
                "under Optional Features), or run 'py -0p' to see what else is installed " +
                "and pass a different one via -PythonVersion.")
        }
        & $py -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
        if (-not (Test-Tkinter $venvPy)) {
            throw "Recreated .venv still has no working tkinter -- something is wrong with this Python install."
        }
    }

    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }
    & $venvPy -m PyInstaller Auto-MFA.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (see Auto-MFA.spec)" }

    # Belt-and-suspenders: also verify the FROZEN exe actually has tkinter,
    # so a PyInstaller-side gap (not just a source-Python gap -- e.g. a hook
    # issue, or something quarantined by antivirus after the build) is caught
    # here too instead of surfacing as a launch crash later. --worker mode
    # does NOT exercise this (it never imports app.gui / tkinter at all), so
    # this uses the dedicated --selftest mode instead, which imports app.gui
    # without opening a window.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $selftest = & (Join-Path $dist "Auto-MFA.exe") --selftest 2>&1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($LASTEXITCODE -ne 0 -or $selftest -notmatch "selftest ok") {
        throw ("Auto-MFA.exe --selftest failed after build (tkinter likely " +
            "missing from the frozen exe despite the build Python having it -- " +
            "see output above). Output: $selftest")
    }
}

# ------------------------------------------------------------ MFA runtime
if (-not $SkipRuntime) {
    # ---- ensure conda ----------------------------------------------------
    if (-not (Test-Path $condaExe)) {
        if ($NoConda) { throw "conda not found and -NoConda was given." }
        Write-Step "Downloading Miniconda into $mini (one-time)"
        New-Item -ItemType Directory -Force -Path $mini | Out-Null
        $installer = Join-Path $build "miniconda_installer.exe"
        Invoke-WebRequest -Uri "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe" -OutFile $installer
        $p = Start-Process -Wait -PassThru -FilePath $installer -ArgumentList `
            "/InstallationType=JustMe", "/RegisterPython=0", "/AddToPath=0", "/S", "/D=$mini"
        if ($p.ExitCode -ne 0) { throw "Miniconda install failed (exit $($p.ExitCode))" }
    }

    # The local build conda must not depend on Anaconda's commercial "defaults"
    # channels (they require ToS acceptance).  Configure it to use conda-forge
    # only and accept the ToS in case those channels are still referenced.
    Write-Step "Configuring conda (conda-forge only)"
    & $condaExe config --set auto_update_conda false 2>$null | Out-Null
    & $condaExe config --remove channels defaults 2>$null | Out-Null
    & $condaExe config --add channels conda-forge 2>$null | Out-Null
    & $condaExe config --set channel_priority strict 2>$null | Out-Null
    foreach ($ch in @(
            "https://repo.anaconda.com/pkgs/main",
            "https://repo.anaconda.com/pkgs/r",
            "https://repo.anaconda.com/pkgs/msys2")) {
        & $condaExe tos accept --override-channels --channel $ch 2>$null | Out-Null
    }

    # ---- create env ------------------------------------------------------
    if (-not (Test-Path (Join-Path $runtimeEnv "python.exe"))) {
        Write-Step "Creating MFA runtime env (conda-forge; large download)"
        & $condaExe create -p $runtimeEnv -y --override-channels -c conda-forge `
            "python=3.11" "montreal-forced-aligner" "kaldi=*=cpu*" "ffmpeg"
        if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
    }

    # conda-pack (used to make the env relocatable) goes into conda base.
    & $condaExe install -n base -y --override-channels -c conda-forge conda-pack
    if ($LASTEXITCODE -ne 0) { throw "conda-pack install failed" }

    # ---- install our package into the runtime ----------------------------
    Write-Step "Installing app package into runtime"
    $sp = Join-Path $runtimeEnv "Lib\site-packages\app"
    Remove-Item -Recurse -Force $sp -ErrorAction SilentlyContinue
    Copy-Item -Recurse (Join-Path $root "app") $sp

    # ---- pack & relocate --------------------------------------------------
    Write-Step "Packing relocatable runtime"
    $pack = Join-Path $build "runtime.tar.gz"
    & (Join-Path $mini "Scripts\conda-pack.exe") -p $runtimeEnv -o $pack -f
    if ($LASTEXITCODE -ne 0) { throw "conda-pack failed" }

    Write-Step "Extracting runtime into $runtimeDest"
    New-Item -ItemType Directory -Force -Path $runtimeDest | Out-Null
    tar -xzf $pack -C $runtimeDest
    if ($LASTEXITCODE -ne 0) { throw "tar extract failed" }

    # ---- verify ------------------------------------------------------------
    # Invoking python.exe directly here (not through conda's own activate.bat)
    # means native extensions that dlopen a DLL by name -- e.g. `soundfile`
    # loading libsndfile.dll -- can fail even though libsndfile.dll is right
    # there in Library\bin: the failure is actually one of libsndfile's OWN
    # transitive dependencies (mingw runtime DLLs etc.) not being found,
    # surfacing as "cannot load library 'libsndfile.dll': error 0x7e". A
    # normal conda activation fixes this by putting Library\bin and
    # Library\mingw-w64\bin on PATH; conda-pack's relocated env never gets
    # that activation, so we do the same PATH prepend here that gui.py's
    # _runtime_env() applies when the GUI spawns the real worker process --
    # without it, this verification step can fail even on a build that would
    # otherwise work fine once the app actually runs it.
    $verifyBinDirs = @(
        (Join-Path $runtimeDest "Library\mingw-w64\bin"),
        (Join-Path $runtimeDest "Library\bin"),
        (Join-Path $runtimeDest "Scripts"),
        $runtimeDest
    )
    $prevPath = $env:PATH
    $env:PATH = ($verifyBinDirs -join ";") + ";" + $prevPath
    # Also guard against the same stderr-becomes-terminating-error gotcha
    # documented in Test-Tkinter above: if this import ever fails, python
    # writes a traceback to stderr, which $ErrorActionPreference = "Stop"
    # would otherwise turn into an opaque NativeCommandError instead of the
    # clear "runtime verification failed" message below.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & (Join-Path $runtimeDest "python.exe") -c "import montreal_forced_aligner, kalpy; print('runtime ok:', montreal_forced_aligner.__version__)"
        if ($LASTEXITCODE -ne 0) { throw "runtime verification failed" }
    } finally {
        $ErrorActionPreference = $prevEAP
        $env:PATH = $prevPath
    }
}

Write-Step "Done"
Write-Host "App:     $dist\Auto-MFA.exe" -ForegroundColor Green
Write-Host "Runtime: $runtimeDest" -ForegroundColor Green
Write-Host "Ship the whole '$dist' folder." -ForegroundColor Green
