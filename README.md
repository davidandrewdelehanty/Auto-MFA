# Auto-MFA

A GUI app that aligns audiobook audio files to the text of an FB2
(FictionBook) e-book using **Montreal Forced Aligner (MFA)**, then packages the
resulting word/phone alignments as one JSON file per audio track inside a zip.

**Recommended: run it under WSL Ubuntu, not native Windows.** MFA wraps
Kaldi, which is developed and tested primarily on Linux; on native Windows,
alignment is meaningfully slower (no `fork()`, so every one of Kaldi's many
short-lived subprocesses pays full Windows process-creation overhead, plus
Windows Defender scanning freshly-extracted unsigned binaries) and a
`--num_jobs > 1` bug has been observed silently dropping most of the
TextGrid export output while still reporting success. Both go away running
the exact same app under WSL against a normal Linux conda env. See
[Running under WSL Ubuntu](#running-under-wsl-ubuntu-recommended) below —
if you don't have WSL yet, https://learn.microsoft.com/en-us/windows/wsl/install
covers installing it (the GUI itself also links there when running natively
on Windows). The native-Windows PyInstaller build further down still works
and is kept for anyone who can't use WSL, but WSL is the better default.

## How it works

1. You pick a folder containing a `.fb2` file and audio files (mp3, wav, ogg,
   m4a, flac, wma, aac, opus).
2. The app lists the audio files and the FB2 chapters. Pair each audio file
   with its chapter one at a time (`Pair ▶`), or use **Auto-pair in order**
   to positionally pair audio[i] with chapter[i] in one click — handy for a
   book with hundreds of chapters that are already in the same order as the
   audio files.
   - **One big audio file covering several chapters** (a "whole book" or
     "whole volume" recording, instead of one file per chapter) is also
     supported: select a *contiguous* range of chapters in the chapter list
     (click-drag, or click + shift-click) before pairing. The pipeline
     figures out exactly where each chapter starts by how far through that
     one audio file its words land (using the alignment's own output, not a
     guess), then physically cuts the audio into one clip per chapter. Those
     cut clips ship inside the output zip alongside their JSONs, since
     nothing like them exists elsewhere yet.
3. Press **BEGIN**.
4. The app:
   - converts each audio file to a 16 kHz mono WAV (via bundled `ffmpeg`),
   - splits **every** chapter's audio into short (~30s, configurable)
     utterances, snapped to detected silence so a cut never lands mid-word
     (falls back to a hard time-based cut if a stretch has no pause), and
     splits the chapter text proportionally across those utterances. This
     always happens, regardless of file size: MFA's peak memory is driven by
     the length of the *longest single utterance* it has to align, not by
     how big the file is, and a ~15-minute chapter fed to MFA whole reliably
     gets OOM-killed. See `app/segment.py` for the full rationale.
   - runs `mfa align --single_speaker` (single-narrator audiobooks skip
     unneeded speaker adaptation) with `--g2p_model_path` so words missing
     from the base dictionary (character names, foreign phrases) still get a
     generated pronunciation instead of silently failing to align (downloads
     the Russian `russian_mfa` acoustic model, dictionary and g2p model
     automatically on first run — internet required),
   - recombines segment alignments using each segment's *planned* duration
     (exactly what was cut from the audio) as the time offset — not MFA's own
     aligned output, which can trim trailing silence and would otherwise
     make every later segment in a chapter drift a little early,
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

## Multiple languages (French/German/English passages)

Russian classics (War and Peace especially) often have paragraphs or whole
pages of untranslated French, and sometimes German or English. MFA does
**not** support aligning genuinely mixed-language audio accurately in one
pass: alignment uses one acoustic model per run, trained on one language's
phone inventory, and there's no supported way to swap models mid-utterance.
MFA's "speaker dictionaries" feature maps different *speakers* to different
dictionaries (for multi-speaker corpora with dialect variation) — it doesn't
apply to a single narrator switching languages within their own speech, and
even MFA's own multilingual-IPA mode (approximating phone sets across
related languages/dialects to share one acoustic model) is documented by
MFA's author as giving only slight gains, for languages far more similar to
each other than Russian is to French. The realistic, supported path — align
each language's audio separately, once you already know where it falls in
the recording — is a chicken-and-egg problem for a first-time alignment:
you don't know where the French passage falls in the audio until you've
already aligned something.

What Auto-MFA does today: foreign-script (Latin-alphabet) words are kept as
literal tokens in the transcript and left to the Russian g2p model to guess
a pronunciation for, the same as any other out-of-dictionary word (see
*Out-of-dictionary words* below). This keeps a French/German/English passage
from derailing the words around it, but word-level timing *within* that
passage will be less accurate than for the surrounding Russian text — worth
knowing if a chapter is heavy with untranslated foreign dialogue. A proper
fix (coarse pass with the Russian model to locate the foreign span, then a
second refinement pass on just that span with the matching French/German/
English model) is a real, buildable feature, just a meaningfully bigger one
than a first cut — flag it if it's worth the effort for your books.

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

## Running under WSL Ubuntu (recommended)

Prerequisites: Windows 10/11 with WSL installed
(https://learn.microsoft.com/en-us/windows/wsl/install — `wsl --install` from
an elevated PowerShell prompt is normally all it takes, and it defaults to
Ubuntu) and WSLg, which ships with modern WSL and lets Linux GUI apps like
this one display as ordinary windows on your Windows desktop — no X server
to set up.

From an Ubuntu terminal, in this project's folder:

```bash
./setup_wsl.sh   # one-time: installs Miniconda (if needed) + creates the MFA env
./run.sh         # launches the GUI
```

`setup_wsl.sh` mirrors what `build.ps1` does for the Windows build (creates a
`python=3.11 montreal-forced-aligner kaldi=*=cpu* ffmpeg` conda-forge env) but
does *not* freeze anything into a standalone executable or relocate the
environment -- there's no need to, since you're not distributing this to a
different machine. It just sets up a normal conda env and generates `run.sh`
to launch the app straight from source against that env's own Python, using
the exact same code path already used for "Running from source" below.
Re-running `setup_wsl.sh` is safe and fast once the env already exists.

**Keep working files on the Linux side.** If this project folder lives under
`/mnt/c/...` (the Windows filesystem, mounted into WSL), that's fine for the
small app source itself, but point the GUI's *Book folder* and *Output
folder* at paths inside the Linux filesystem instead (e.g. `~/audiobooks`) --
crossing the Windows/Linux filesystem boundary for the audio files and MFA's
own working files is slow and cancels out much of the speed gain WSL is
otherwise giving you. `setup_wsl.sh` prints a reminder of this if it detects
itself running from `/mnt/...`.

## Building (native Windows)

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
- **Target utterance length (s)**: every chapter is split into utterances
  around this long (default 30s), snapped to silence. The hard cap used if no
  silence is found in time is 1.5× this value (45s at the default). Lower it
  if you still see memory pressure; raise it (cautiously) if alignment seems
  too fragmented on very clean, pause-heavy narration.
- **Parallel jobs**: MFA workers to run concurrently (default 2). Raise it on
  a machine with more CPU cores/RAM to speed up alignment; lower it to 1 if
  you're still hitting memory pressure even with short utterances.
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
  fb2.py         FB2 parsing (recurses nested Part/Chapter sections) +
                 transcript normalization
  segment.py     silence-aware utterance segmentation (the OOM fix)
  textgrid.py    Praat TextGrid parser
  chunking.py    transcript partitioning (even, and weighted by segment
                 duration)
  audio.py       ffmpeg wrappers (convert, probe, split)
tests/           unit + integration tests (integration needs ffmpeg)
Auto-MFA.spec    PyInstaller spec (GUI shell only, native-Windows build)
build.ps1        native-Windows build: GUI shell + conda-pack MFA runtime
setup_wsl.sh     WSL/Linux setup: creates the conda env + generates run.sh
run.sh           generated by setup_wsl.sh; launches the GUI under WSL/Linux
```
