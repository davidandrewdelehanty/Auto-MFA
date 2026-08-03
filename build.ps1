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

function Expand-Runtime([string]$TarPath, [string]$Dest) {
    Write-Step "Extracting runtime into $Dest"
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    tar -xzf $TarPath -C $Dest
    if ($LASTEXITCODE -ne 0) { throw "tar extract failed" }
}

# Run a native command that is EXPECTED to sometimes write to stderr (a
# warning, "nothing to remove", etc.) without the script-wide
# $ErrorActionPreference = "Stop" turning that stderr write into a fatal
# terminating error -- which is what a bare `2>$null` does NOT prevent: the
# escalation to a NativeCommandError happens before the redirect ever gets a
# chance to swallow it. Defined at top level (not nested in the GUI section)
# because both the GUI and the runtime sections need it, and the runtime
# section can run on its own via -SkipGui.
function Invoke-Probe([scriptblock]$block) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $block } finally { $ErrorActionPreference = $prevEAP }
}

# PyInstaller's COLLECT step deletes and rebuilds the whole dist\Auto-MFA
# folder on every GUI build. If Auto-MFA.exe (or a worker subprocess it
# spawned -- those run runtime\python.exe, also under dist\Auto-MFA) is
# still running, Windows keeps its DLLs locked and the delete fails partway
# through with "Access is denied", leaving dist\Auto-MFA half-deleted.
# Close anything still running from under dist\Auto-MFA before rebuilding.
function Stop-DistProcesses([string]$DistPath) {
    $victims = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ExecutablePath -and
            $_.ExecutablePath.StartsWith($DistPath, [System.StringComparison]::OrdinalIgnoreCase)
        }
    if ($victims) {
        Write-Step "Stopping running Auto-MFA process(es) so dist\ can be rebuilt"
        foreach ($v in $victims) {
            Write-Host "  killing PID $($v.ProcessId): $($v.ExecutablePath)"
            Stop-Process -Id $v.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
}

# Refresh the `app` package inside the runtime from the source tree.
#
# This matters more than it looks: the GUI shell exe does NOT run the
# alignment itself -- it spawns `runtime\python.exe -m app.main --worker ...`,
# so the code that actually does the work is the copy at
# runtime\Lib\site-packages\app, NOT the copy frozen into the exe. A runtime
# restored from an older runtime.tar.gz therefore carries whatever app code
# was current when that tarball was packed. Re-syncing on every build keeps
# the two halves from silently drifting apart (and makes editing Python code
# + re-running with -SkipGui -SkipRuntime a fast iteration loop).
function Sync-AppIntoRuntime([string]$RuntimeRoot) {
    if (-not (Test-Path (Join-Path $RuntimeRoot "python.exe"))) { return }
    $sp = Join-Path $RuntimeRoot "Lib\site-packages\app"
    Remove-Item -Recurse -Force $sp -ErrorAction SilentlyContinue
    Copy-Item -Recurse (Join-Path $root "app") $sp
    Write-Host "Synced app package into runtime." -ForegroundColor Green
}

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
        if (-not $pythonExe) { return $false }
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $pythonExe -c "import tkinter" 2>$null 1>$null
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        return $LASTEXITCODE -eq 0
    }

    # Every Python this machine has, as absolute paths to python.exe.
    # Some perfectly normal Python installs ship WITHOUT tcl/tk -- notably the
    # "pythoncore" packages under %LOCALAPPDATA%\Python that winget and the
    # NuGet feed install, which are deliberately minimal. So rather than
    # insisting on one hardcoded version, collect every candidate and pick one
    # that can actually build a Tkinter GUI.
    function Get-PythonCandidates {
        $found = New-Object System.Collections.ArrayList

        $requested = $null
        $out = Invoke-Probe { & py "-$PythonVersion" -c "import sys; print(sys.executable)" 2>$null }
        if ($out) { $requested = ($out | Select-Object -First 1).ToString().Trim() }
        if ($requested) { [void]$found.Add($requested) }   # requested version wins

        # Everything else the py launcher knows about.
        $listing = Invoke-Probe { & py -0p 2>$null }
        foreach ($line in @($listing)) {
            if ("$line" -match '([A-Za-z]:\\[^\r\n]*?python\.exe)') {
                [void]$found.Add($Matches[1].Trim())
            }
        }

        # Fallback if the py launcher isn't installed at all.
        foreach ($name in @("python", "python3")) {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source) { [void]$found.Add($cmd.Source) }
        }

        return ($found | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)
    }

    function Resolve-BuildPython {
        $candidates = @(Get-PythonCandidates)
        if ($candidates.Count -eq 0) {
            throw ("No Python installation found. Install Python 3.10+ from " +
                "https://python.org (keep the default 'tcl/tk and IDLE' option checked).")
        }
        foreach ($candidate in $candidates) {
            if (Test-Tkinter $candidate) { return $candidate }
        }
        throw ("None of the Python installations on this machine have a working tkinter, " +
            "so none can build a Tkinter GUI. Checked:`n  " + ($candidates -join "`n  ") +
            "`n`nFix: install Python from https://python.org and keep 'tcl/tk and IDLE' " +
            "checked under Optional Features (the minimal 'pythoncore' builds that winget " +
            "and the NuGet feed install do not include it). Then re-run this script.")
    }

    $needVenv = $false
    if (-not (Test-Path $venvPy)) {
        $needVenv = $true
    } elseif (-not (Test-Tkinter $venvPy)) {
        Write-Step "Existing .venv has no working tkinter -- recreating it"
        Remove-Item -Recurse -Force (Join-Path $root ".venv") -ErrorAction SilentlyContinue
        $needVenv = $true
    }

    if ($needVenv) {
        $py = Resolve-BuildPython
        Write-Host "Using Python: $py" -ForegroundColor Green
        & $py -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
        if (-not (Test-Tkinter $venvPy)) {
            throw ("Created .venv from '$py' but it still has no working tkinter. " +
                "This usually means that Python's tcl/tk files are present but broken; " +
                "reinstalling it from https://python.org should fix it.")
        }
    }

    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

    # See Stop-DistProcesses above: PyInstaller's COLLECT step is about to
    # delete the whole dist\Auto-MFA folder, which fails with "Access is
    # denied" on a locked DLL if Auto-MFA.exe (or a worker it spawned) is
    # still running from a previous test.
    Stop-DistProcesses $dist

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
    #
    # Every one of these calls is wrapped in Invoke-Probe: `conda config
    # --remove channels defaults` (and friends) write a harmless message to
    # stderr when there's nothing to do -- e.g. re-running this after
    # "defaults" was already removed on a previous run -- and a bare
    # `2>$null` does NOT stop $ErrorActionPreference = "Stop" from turning
    # that stderr write into a terminating NativeCommandError first. Without
    # this, the script died here on every run after the first.
    Write-Step "Configuring conda (conda-forge only)"
    Invoke-Probe { & $condaExe config --set auto_update_conda false 2>$null | Out-Null }
    Invoke-Probe { & $condaExe config --remove channels defaults 2>$null | Out-Null }
    Invoke-Probe { & $condaExe config --add channels conda-forge 2>$null | Out-Null }
    Invoke-Probe { & $condaExe config --set channel_priority strict 2>$null | Out-Null }
    foreach ($ch in @(
            "https://repo.anaconda.com/pkgs/main",
            "https://repo.anaconda.com/pkgs/r",
            "https://repo.anaconda.com/pkgs/msys2")) {
        Invoke-Probe { & $condaExe tos accept --override-channels --channel $ch 2>$null | Out-Null }
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

    Expand-Runtime $pack $runtimeDest

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

# ------------------------------------------------- reconcile dist\ contents
# PyInstaller's COLLECT step DELETES the whole dist\Auto-MFA output folder on
# every GUI build (that's what --noconfirm agrees to). The conda-packed MFA
# runtime lives at dist\Auto-MFA\runtime -- i.e. inside that same folder --
# so building the GUI shell silently destroys the runtime, and the app then
# fails at BEGIN with "No module named montreal_forced_aligner" (the exe
# falls back to running ITSELF as the worker, and the frozen exe deliberately
# excludes MFA). Rebuilding the 655 MB runtime from scratch just to undo that
# would be absurd, so restore it from the tarball we already have instead.
#
# This runs unconditionally, whatever flags were passed, which also makes
# `.\build.ps1 -SkipGui -SkipRuntime` a fast "repair dist\ and refresh the
# app code" command.
$runtimePyExe = Join-Path $runtimeDest "python.exe"
if (-not (Test-Path $runtimePyExe)) {
    $packedRuntime = Join-Path $build "runtime.tar.gz"
    if (Test-Path $packedRuntime) {
        Write-Step "Runtime is missing from dist (a GUI build wipes it) -- restoring"
        Expand-Runtime $packedRuntime $runtimeDest
    }
}
if (-not (Test-Path $runtimePyExe)) {
    throw ("No MFA runtime at '$runtimeDest' and no build\runtime.tar.gz to restore " +
        "from. Re-run this script without -SkipRuntime to build it (large download).")
}

# Always ship current app code inside the runtime -- see Sync-AppIntoRuntime.
Sync-AppIntoRuntime $runtimeDest

Write-Step "Done"
Write-Host "App:     $dist\Auto-MFA.exe" -ForegroundColor Green
Write-Host "Runtime: $runtimeDest" -ForegroundColor Green
Write-Host "Ship the whole '$dist' folder." -ForegroundColor Green
