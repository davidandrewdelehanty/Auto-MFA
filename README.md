# Auto-MFA

A Windows GUI for setting up audiobook forced-alignment jobs against an FB2
(FictionBook) e-book, which then run under **WSL Ubuntu** using **Montreal
Forced Aligner (MFA)**. Output is one JSON file per chapter in the schema the
**Govorim** app reads (`<slug>-chNN.json`).

**The GUI does not run alignment.** You pick a folder, pair audio files with
chapters, set options, and press *Generate script*; it writes an
`align_<slug>.sh` plus a job file, and you run that one command in an Ubuntu
terminal. This split is deliberate — see *Why a script generator* below.

## Why a script generator

MFA wraps Kaldi, which is developed and tested on Linux. On native Windows it
is meaningfully slower: Kaldi spawns many short-lived subprocesses per job,
Windows has no `fork()` so each pays full `CreateProcess` overhead, and
Defender real-time-scans freshly extracted unsigned binaries.

Running the whole GUI under WSL instead fixes that but breaks the UI: under
WSLg, Tk sees only a **single** system font, so Cyrillic filenames and chapter
titles — and even this app's own UI symbols — render as literal `\uXXXX`
escapes. Locale settings, `FONTCONFIG_PATH`, installing font packages, and
plain Ubuntu's own `python3-tk` all failed to resolve it.

Generating a script gets both halves right: the GUI stays native Windows,
where Tk has real fonts and Cyrillic displays correctly, and the alignment
runs in Linux, MFA's own primary platform.

### The silent-missing-output failure (read this before debugging one)

For a long time this app reported errors like *"MFA reported success but 24/29
expected TextGrid files are missing"*, and that was misdiagnosed twice — first
as a parallel-export race, then as a Windows `spawn`-multiprocessing bug. Both
were wrong. The same 5-of-29 result reproduced on Linux, at every `--num_jobs`
including 1, which rules out anything to do with parallelism.

**What's actually happening:** an utterance MFA cannot align produces *no
output file at all*, and MFA still reports the run as fully successful. So
"fewer files than expected" is not a bug in MFA's exporter — it is MFA's
normal way of telling you that alignment failed for those utterances.

The dominant cause is **out-of-vocabulary words with no pronunciation**.
Russian audiobooks are dense with them (character names in every grammatical
case, French phrases, years), and those names *recur constantly* — so leaving
them unpronounceable doesn't lose a word here and there, it strips the
alignment of its most frequent anchors and takes out whole segments. See
*Dictionary handling* below for the fix.

Secondary causes, once the dictionary is right: a transcript that genuinely
doesn't match the audio (an FB2 preamble or endnotes section the narrator
never reads, an abridged recording, a mis-paired chapter), or ordinary local
divergence, which is why a small residue of unaligned segments is normal and
tolerated rather than treated as failure.

## Dictionary handling

Before aligning, the pipeline builds a **book-specific dictionary**: the base
`russian_mfa` dictionary plus g2p-generated pronunciations for every word in
*this* corpus it doesn't already cover.

```
mfa g2p <corpus> russian_mfa oov.dict --dictionary_path russian_mfa
cat russian_mfa.dict oov.dict > book.dict
mfa align <corpus> book.dict russian_mfa <out>
```

The obvious-looking alternative — passing `--g2p_model_path` to `mfa align`
and letting it handle OOV internally — is what this app used to do, and it is
worse in two ways. MFA has shipped g2p models whose phone inventory doesn't
match their own paired dictionary, so `align` refuses to run at all with
`PronunciationG2PMismatchError`; and the natural fallback (dropping g2p) lands
you in exactly the no-pronunciations state described above. Building the
dictionary up front avoids both.

If g2p is unavailable or fails, the run continues with the plain base
dictionary and says so — a degraded run beats no run — but expect segments
containing names to fail.

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
     guess), then physically cuts the audio into one clip per chapter.
3. Fill in the **Book slug** (names every output file, e.g. `chekhov-dama` →
   `chekhov-dama-ch01.json`) and, optionally, the **R2 folder** used to build
   each `audio_url`.
4. Press **GENERATE SCRIPT**. The GUI writes `align_<slug>.sh` and
   `align_<slug>.job.json` into the output folder and copies the run command
   to your clipboard.
5. Paste that command into an Ubuntu (WSL) terminal. From there the pipeline:
   - converts each audio file to a 16 kHz mono WAV (via `ffmpeg`),
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
   - writes one `<slug>-chNN.json` per chapter into the output folder.

### JSON format (Govorim)

Verified against a real file in the Govorim repo, not guessed:

```json
{
  "audio_url": "https://pub-....r2.dev/dama/ch1.mp3",
  "narrator": "audiobook",
  "fragments": [
    {"text": "Говорили, что на набережной появилось новое лицо.",
     "begin": 0.291, "end": 7.481,
     "words": [{"word": "Говорили,", "begin": 0.291, "end": 1.493}, "..."]}
  ],
  "word_timings": [{"word": "Говорили,", "begin": 0.291, "end": 1.493}, "..."]
}
```

Three things about this schema are easy to get wrong, and `app/govorim.py`
exists to handle them:

- **`begin`, not `start`.** MFA and the rest of this app use `start`
  internally; Govorim reads `begin`. Word entries likewise use `word`, not
  `text`. There are no `phones` — that tier is dropped.
- **Original surface forms.** MFA aligns a *normalized* transcript
  (lowercased, punctuation and digits stripped — that's what its
  pronunciation dictionary can look up), but Govorim displays real book
  text. So every aligned word is mapped back onto the original token it came
  from: `Говорили,` with its capital and comma, not `говорили`. A hyphenated
  token like `какого-то` normalizes to two words but stays one highlightable
  token spanning both.
- **Sentence fragments.** Words are regrouped into sentences using the same
  splitting rule Govorim's own sentence-level build script uses, so fragments
  chunk the way already-shipped books do.

One known difference from the older WhisperX-produced files: tokens that
normalize to nothing — a bare `—` or `–`, a standalone number — get **no**
entry here, because the aligner never saw them and there is no honest timing
to report. WhisperX did emit dash tokens. If Govorim's highlighting matches
`word_timings` to rendered spans strictly by index, verify that against a
chapter containing dashes before bulk-processing a library.

## How chapters are found in an FB2

Two layouts occur in practice and both are handled:

1. **One `<section>` per chapter**, usually nested inside a Part section.
   War and Peace does this — 361 leaf sections, one per chapter.
2. **One section per PART**, with each chapter marked by a `<subtitle>`
   holding a roman numeral. Anna Karenina and Crime and Punishment do this.
   Read as layout 1, Anna Karenina extracts as *8* enormous chapters
   instead of 239, and can't be paired against one audio file per chapter.

Splitting on `<subtitle>` is deliberately conservative, because the tag is
also used for things that are not chapters:

- **Roman numerals only.** Crime and Punishment's `ПРИМЕЧАНИЯ` section holds
  273 subtitles numbered `1, 2, 3…`; treating arabic numbers as chapters
  would bury the novel's 41 real ones.
- **At least two, and distinct.** One `Занавес` at the end of an act, or the
  single `Конец.` in War and Peace, must never fragment a section that was
  already right. Requiring distinct numerals also stops a run of lines
  opening with the Russian preposition `С ` — which transliterates to a
  valid roman `C` — from looking like a numbered sequence.
- **Cyrillic homoglyphs are normalised.** `ХІV` is often Cyrillic Х plus
  Ukrainian І plus Latin V.
- **The numeral is not left in the chapter text.** It names the chapter; the
  narrator doesn't read it aloud, and leaving it in feeds the aligner a
  stray `i` that isn't spoken.
- **The pieces must be chapter-sized.** Verse numbers its *stanzas* with
  roman numerals exactly as prose numbers its chapters — Eugene Onegin has
  391 such subtitles, and splitting on them gives 380 pieces averaging 60
  words. A median under 150 words means the markers were counting something
  other than chapters, so the split is abandoned.

A numeral may carry a name — Anna Karenina has both `XX` and `XX СМЕРТЬ`.

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

## One-time setup

**1. Install WSL** (skip if you already have Ubuntu). From an elevated
PowerShell prompt:

```powershell
wsl --install
```

**2. Create the MFA environment.** From an Ubuntu (WSL) terminal, in this
project's folder:

```bash
bash setup_wsl.sh
```

This installs Miniconda if needed and builds a conda-forge env with MFA,
Kaldi, OpenFST and ffmpeg in it (~1–2 GB, several minutes). Re-running it is
safe and fast once the env exists.

**3. Build the Windows GUI** (see *Building the GUI* below), or just run it
from source with any Python 3.10+ that has tkinter.

The GUI itself needs **only tkinter** — it no longer needs MFA, conda, or the
bundled runtime, because it doesn't run alignment.

**Where to keep your audio.** Paths under `/mnt/c/...` (the Windows
filesystem, mounted into WSL) work fine and are the simplest option, since
that's where your books already live — the GUI translates Windows paths to
`/mnt/c/...` automatically. Crossing that boundary is slower than the native
Linux filesystem, though, so if a book is large and you care about the last
bit of speed, copy it to e.g. `~/audiobooks` first and point the GUI's
*Output folder* there.

## Building the GUI

Prerequisites: Windows, PowerShell 5.1+.

```powershell
.\build.ps1 -SkipRuntime
```

`-SkipRuntime` is now the normal case: the GUI only needs tkinter, so the
conda-packed MFA runtime that used to ship next to it is no longer required.
A plain `.\build.ps1` still builds that runtime if you want a self-contained
Windows fallback, but nothing in the current workflow uses it.

## Running from source (development)

The GUI needs nothing but Python and tkinter:

```powershell
python app/main.py          # or: python -m app.main
```

The alignment half (`app/pipeline.py`, driven by `--worker align`) needs a
conda-forge MFA env, which is exactly what `setup_wsl.sh` creates on the
Linux side. To run the worker by hand:

```bash
conda activate ~/miniconda3/envs/auto-mfa
python app/main.py --worker align /path/to/align_<slug>.job.json
```

## Notes & tips

- **First run needs internet**: the acoustic model / dictionary / g2p model are
  downloaded once into `~/Documents/MFA` on the Linux side, then reused.
- **Target utterance length (s)**: every chapter is split into utterances
  around this long (default 30s), snapped to silence. The hard cap used if no
  silence is found in time is 1.5× this value (45s at the default). Lower it
  if you still see memory pressure; raise it (cautiously) if alignment seems
  too fragmented on very clean, pause-heavy narration.
- **Parallel jobs**: MFA workers to run concurrently (default 2). Raise it on
  a machine with more CPU cores/RAM to speed up alignment; lower it to 1 if
  you hit memory pressure. Note that peak memory is driven by the length of
  the *longest single utterance*, not by job count or corpus size — which is
  why every chapter is split into ~30s pieces regardless of size.
- **Unaligned segments**: a few are normal. The pipeline retries with a wider
  beam (`--beam 100 --retry_beam 400`, MFA's documented remedy), then
  single-job, and only fails the run if under 90% of segments aligned — at
  which point the problem is the transcript, not the beam. Segments that
  never align leave a gap; because offsets come from each segment's *planned*
  duration, that gap does not shift anything after it.
- **Out-of-dictionary words**: covered by the book-specific dictionary built
  before alignment (see *Dictionary handling*). Numbers and punctuation are
  stripped from the transcript before alignment.
- **Unpaired audio** is skipped; you are asked to confirm before generating.
- **Re-running a script is safe** — `align_<slug>.sh` and its job file stay in
  the output folder, so you can re-run a book without touching the GUI, and
  edit the job JSON by hand if you only need to tweak one setting.
- Keep temporary files on to debug: the temp corpus, converted WAVs and raw
  TextGrids are kept under `/tmp/auto_mfa_*` on the Linux side.

## Layout

```
app/
  main.py        entry point (GUI, or --worker mode)
  gui.py         Tkinter GUI: folder picker, pairing, options, script output
                 (three generators: align, upload to R2, install into Govorim)
  scriptgen.py   WSL path translation, slugs, generated bash script (no Tk,
                 so it is unit-testable without a display)
  govorim.py     Govorim JSON format: surface-form remapping, sentence
                 fragments, word_timings
  worker.py      headless worker process (align / download-models)
  pipeline.py    corpus prep -> MFA -> TextGrid -> JSON
  fb2.py         FB2 parsing (nested Part/Chapter sections AND parts that
                 mark chapters with <subtitle>) + transcript normalization
  segment.py     silence-aware utterance segmentation (the OOM fix)
  textgrid.py    Praat TextGrid parser
  chunking.py    transcript partitioning (even, and weighted by segment
                 duration)
  audio.py       ffmpeg wrappers (convert, probe, split)
tests/           unit + integration tests (integration needs ffmpeg)
Auto-MFA.spec    PyInstaller spec (GUI shell)
build.ps1        Windows build; use -SkipRuntime (the GUI needs only tkinter)
setup_wsl.sh     WSL/Linux setup: creates the conda env with MFA in it
align_<slug>.sh  generated per book by the GUI; what you actually run
upload_<f>.sh    generated per book; rclone-uploads the audio to R2
install_<s>.sh   generated per book; installs it into the Govorim app
```

## Installing into the Govorim app

**Generate install script** writes `install_<slug>.sh`, which puts a finished
book where the app expects it:

| | |
|---|---|
| FB2 | `public/books/novel/<slug>.fb2` |
| chapters | `public/books/audio/<slug>/001.json`, `002.json`, … |
| catalogue | an entry in `public/books/index.json` with an ordered `audiobook.chapters` list |

The catalogue entry is the part worth understanding: **the app does not glob
the audio folder.** `index.json` lists each chapter path explicitly, in order,
and a chapter that isn't listed doesn't exist as far as the reader is
concerned. That's also why the chapter files are renamed from this tool's
`<slug>-chNNN.json` to the app's own `NNN.json` convention on the way in.

The JSON edit is done in Python inside the script — a real parse-modify-write,
so every other book's entry and any hand-curated title/author survive. Chapter
ordering comes from numeric sorting, not shell glob collation, because `ch100`
collates before `ch99` as text and this library has books with 239 and 362
chapters.

Re-running replaces this book's files and entry and leaves everything else
alone, including clearing out chapters from a previous longer run so no orphan
files are left behind. `index.json` is backed up to `index.json.bak` before the
first write.

## Uploading audio to R2

**Generate upload script** writes `upload_<folder>.sh` plus a file list, and
`rclone copy`s exactly the paired audio files into
`govorim-audio/<R2 folder>/` — the same folder the JSONs' `audio_url` fields
point at, so the two can't drift apart. Only the listed audio is uploaded;
the FB2, generated scripts and output JSONs in the same folder are not.

No credentials are written into the generated script — it sits in a book
folder that gets copied around, so a leaked key there would be a real
problem. It expects an rclone remote (default name `r2`, overridable via
`AUTO_MFA_R2_REMOTE`) configured once on the machine, and prints the exact
`rclone config create` command, endpoint included, if it's missing.

### R2 quirks the script works around

R2 answers `501 NotImplemented` for S3 features it doesn't implement, and
which of those rclone sends depends on the saved remote config — so the
script forces them off per-run rather than trusting it: `--s3-provider
Cloudflare`, `--s3-acl ""` (R2 has no ACLs), `--s3-no-check-bucket` (needed
for object-scoped tokens), `--s3-disable-checksum`, and an upload cutoff
above any single audio file so the multipart API is never touched.

More awkward: **older rclone reports 501 for uploads that actually
succeeded.** Observed on v1.60 — the object lands, a subsequent `HEAD`
returns it with the right size, and rclone still counts the transfer as
failed. So the script never trusts rclone's exit status. It lists the
bucket, counts how many of *this book's* files are present, uploads, counts
again, and repeats until the count is complete or a pass makes no progress.
The closing `holds N / M` line is the real answer. Updating rclone
(`curl https://rclone.org/install.sh | sudo bash`) fixes the mis-reporting
at source.
