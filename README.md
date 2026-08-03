# Auto-MFA

A Windows GUI app that aligns audiobook audio files to the text of an FB2
(FictionBook) e-book using **Montreal Forced Aligner (MFA)**, then packages the
resulting word/phone alignments as one JSON file per audio track inside a zip.

## How it works

1. You pick a folder containing a `.fb2` file and audio files (mp3, wav, ogg,
   m4a, flac, wma, aac, opus).
2. The app lists the audio files and the FB2 chapters. You manually pair each
   audio file with its chapter (`Pair ▶`).
3. Press **BEGIN**.
4. The app:
   - converts each audio file to a 16 kHz mono WAV (via bundled `ffmpeg`),
   - splits any audio whose WAV would exceed the **max chunk size**
     (default 2 GB) into ≤ 2 GB chunks and splits the chapter text
     proportionally, so MFA never has to swallow an oversized file and crash,
   - runs `mfa align` (downloads the Russian `russian_mfa` acoustic model,
     dictionary and g2p model automatically on first run — internet required),
   - recombines chunk alignments (with correct time offsets),
   - writes `alignments_<folder>_<timestamp>.zip` with one `<track>.json` per
     audio file into the output folder.

### JSON format

```json
{
  "audio_file": "01.mp3",
  "title": "Глава 1",
  "duration": 1742.64,
  "words":   [ {"word": "привет", "start": 12.34, "end": 12.87}, ... ],
  "phones":  [ {"text": "p", "start": 12.34, "end": 12.45}, ... ]
}
```

Empty intervals (silence) are omitted.

## What is bundled

MFA 3.x depends on `kalpy`/`kaldi`, compiled native components that are **only
distributed through conda** (there are no pip wheels), so a pure
pip + PyInstaller bundle of MFA is impossible. Instead:

```
dist\Auto-MFA\
  Auto-MFA.exe   small GUI shell (Tkinter only, built with PyInstaller)
  runtime\       relocatable MFA runtime (conda-pack of a conda-forge env)
                 containing python + montreal-forced-aligner + kalpy + kaldi
                 + ffmpeg/ffprobe
```

The GUI runs MFA inside `runtime` as a separate windowed (`pythonw`) process,
so no console window flashes and a crash inside MFA never takes down the UI.
The whole `dist\Auto-MFA` folder is self-contained and can be moved anywhere.

## Building

Prerequisites: Windows, PowerShell 5.1+. The script auto-downloads Miniconda
into `build\miniconda` on first run (a conda install is required to build the
runtime).

```powershell
.\build.ps1
```

This:

1. builds the small GUI shell with PyInstaller (fast),
2. creates a conda env (`python=3.11 montreal-forced-aligner kaldi=*=cpu*
   ffmpeg` from conda-forge),
3. installs this project's `app` package into the env,
4. `conda-pack`s the env into `dist\Auto-MFA\runtime` so it is relocatable,
5. verifies `montreal_forced_aligner` imports in the packed runtime.

Flags: `-SkipGui` / `-SkipRuntime` to rebuild only one half, `-NoConda` to fail
instead of downloading Miniconda.

## Running from source (development)

You need a working MFA environment (conda-forge) on your machine:

```powershell
conda create -n aligner -c conda-forge montreal-forced-aligner kaldi=*=cpu* ffmpeg
conda activate aligner
python app/main.py          # or: python -m app.main
```

The GUI spawns the worker with the same interpreter it is running under, so
when launched from a conda env it uses that env's MFA. (For a frozen build it
uses the bundled `runtime` instead.)

## Notes & tips

- **First run needs internet**: the acoustic model / dictionary / g2p model are
  downloaded once into `%USERPROFILE%\Documents\MFA`. Use the
  *Download models* button to fetch them ahead of time.
- **Max chunk size**: audio is chunked into units no larger than this setting
  (MB). The default 2048 MB matches the Windows 2 GB practical file limit for
  the processing WAVs. Lower it if you still see memory pressure.
- **Out-of-dictionary words**: MFA auto-uses the Russian g2p model to cover
  words missing from the dictionary. Numbers and punctuation are stripped from
  the transcript before alignment.
- **Unpaired audio** is skipped; you are asked to confirm before BEGIN.
- Keep temporary files on to debug: the temp corpus, converted WAVs and raw
  TextGrids are kept under `%TEMP%\auto_mfa_*`.

## Layout

```
app/
  main.py        entry point (GUI, or --worker mode)
  gui.py         Tkinter GUI: folder picker, pairing, options, live log
  worker.py      headless worker process (align / download-models)
  pipeline.py    corpus prep -> MFA -> TextGrid->JSON -> zip
  fb2.py         FB2 parsing + transcript normalization
  textgrid.py    Praat TextGrid parser
  chunking.py    <= 2 GB chunk planning + transcript partitioning
  audio.py       ffmpeg wrappers (convert, probe, split)
tests/           unit + integration tests (integration needs ffmpeg)
Auto-MFA.spec    PyInstaller spec (GUI shell only)
build.ps1        one-command build: GUI shell + conda-pack MFA runtime
```
