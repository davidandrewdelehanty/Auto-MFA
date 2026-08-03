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
    if (-not (Test-Path $venvPy)) {
        $py = (py -$PythonVersion -c "import sys; print(sys.executable)" 2>$null).Trim()
        if (-not $py) { throw "Python $PythonVersion not found. Run 'py -0p' to list versions." }
        & $py -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
    }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }
    & $venvPy -m PyInstaller Auto-MFA.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (see Auto-MFA.spec)" }
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
    & (Join-Path $runtimeDest "python.exe") -c "import montreal_forced_aligner, kalpy; print('runtime ok:', montreal_forced_aligner.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "runtime verification failed" }
}

Write-Step "Done"
Write-Host "App:     $dist\Auto-MFA.exe" -ForegroundColor Green
Write-Host "Runtime: $runtimeDest" -ForegroundColor Green
Write-Host "Ship the whole '$dist' folder." -ForegroundColor Green
