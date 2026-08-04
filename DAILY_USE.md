# Auto-MFA — how to align a book

Everyday instructions. One-time setup is at the bottom; you should only ever
need it once per machine.

---

## Per book: 6 steps

### 1. Put the book in one folder

One folder containing:

- the `.fb2` file
- the audio files (`.mp3`, `.wav`, `.m4a`, …)

Nothing else needs to be in there. Cyrillic filenames are fine.

### 2. Open the GUI

Double-click:

```
C:\Users\david\projects\Auto-MFA\dist\Auto-MFA\Auto-MFA.exe
```

### 3. Load the folder and pair

1. **Browse…** → pick the book folder.
2. Pair audio to chapters:
   - **Same number of files and chapters, same order?** Click **Auto-pair in
     order**. Done.
   - **One big audio file covering many chapters?** Click the audio file, then
     click the first chapter and shift-click the last one, then **Pair ▶**.
   - **Otherwise**, pair them one at a time: click an audio file, click its
     chapter, **Pair ▶**.

Check the **Pairs** box at the bottom — it shows exactly what will be aligned.

### 4. Fill in the Govorim fields

- **Book slug** — names the output files, the FB2, the audio folder and the
  catalogue entry. `chekhov-dama` gives `public/books/audio/chekhov-dama/`.
- **R2 folder** — the folder in the `govorim-audio` bucket where this book's
  audio lives (e.g. `dama`). This builds each file's `audio_url`. Leave it
  blank if the audio isn't uploaded yet; you can fill the URLs in later.
- **Title / Author** — prefilled from the FB2's own metadata. These become the
  book's catalogue entry, so check they read the way you want them to appear
  in the picker.

### 5. Press GENERATE SCRIPT

It writes two files into the output folder and puts the run command on your
clipboard. Nothing is aligned yet — this step takes a second.

### 6. Run it in Ubuntu

Open an Ubuntu (WSL) terminal and paste the command (Ctrl+Shift+V, or
right-click):

```bash
# WSL
bash '/mnt/c/Users/david/.../align_<slug>.sh'
```

Keep the quotes — book folders often have spaces in the name, and without
them bash stops at the space and reports "No such file or directory". The
GUI's copied command already includes them.

Then leave it. Expect roughly **3–6 minutes per hour of audio**. It prints
progress as it goes and ends with a line for each file written.

When it finishes you'll have `<slug>-ch01.json`, `<slug>-ch02.json`, … in the
output folder.

---

## Uploading the audio to R2

Only needed if this book's audio isn't in the `govorim-audio` bucket yet.
Do it from the same GUI screen — it uses the **R2 folder** field, so the
uploaded audio lands exactly where the JSONs' `audio_url` fields point.

1. Press **Generate upload script**.
2. Paste the command into Ubuntu:

```bash
# WSL
bash '/mnt/c/Users/david/.../upload_<folder>.sh'
```

It uploads only the audio files (not the FB2, scripts, or JSONs), shows a
progress bar, and lists the bucket contents when done. Re-running is safe —
files already uploaded are skipped.

**First time only**, it will tell you to set up rclone. Install it and create
the remote:

```bash
# WSL
sudo apt-get install -y rclone
rclone config create r2 s3 provider=Cloudflare \
    access_key_id=YOUR_ACCESS_KEY_ID \
    secret_access_key=YOUR_SECRET_ACCESS_KEY \
    endpoint=https://34e5181838c8f719758264dbb7b02b46.r2.cloudflarestorage.com \
    region=auto
```

Your keys are in the Cloudflare dashboard under **R2 → Manage API tokens**.
They're stored once in rclone's own config — deliberately never written into
the generated scripts, since those sit in book folders that get copied around.

---

## Installing the book into Govorim

Press **Generate install script**, then run it:

```bash
# WSL
bash '/mnt/c/Users/david/.../install_<slug>.sh'
```

That does all three things a book needs to actually work:

1. copies the FB2 to `public/books/novel/<slug>.fb2`
2. copies the chapter JSONs to `public/books/audio/<slug>/001.json`, `002.json`, …
3. adds or updates the book's entry in `public/books/index.json`, including
   the ordered `audiobook.chapters` list

That third step is the one that's easy to miss by hand — the app does **not**
scan the audio folder. A chapter that isn't listed in `index.json` doesn't
exist as far as the reader is concerned.

Re-running is safe: it replaces this book's files and entry, leaves every
other book alone, preserves a title you've edited by hand in the repo, and
backs up `index.json` the first time.

Then commit:

```bash
# Git Bash
cd /c/Users/david/projects/govorim-app
git add public/books
git commit -m "Add <book> audiobook alignment"
git push
```

Vercel redeploys on push (1–2 minutes). **Hard-refresh the site afterwards**
(Ctrl+Shift+R) — the old chapter JSONs sit in the browser cache and will
happily keep showing you the previous behaviour.

If you left **R2 folder** blank when generating, the `audio_url` fields are
bare filenames — fill those in before committing, or the audio won't load.

---

## If something goes wrong

**"The Auto-MFA conda environment is missing"**
The one-time setup hasn't run, or the env got deleted:

```bash
# WSL
bash /mnt/c/Users/david/projects/Auto-MFA/setup_wsl.sh
```

**"Could not find 'fstcompile'"**
The env is incomplete. Rebuild it from scratch:

```bash
# WSL
conda env remove -p ~/miniconda3/envs/auto-mfa -y
bash /mnt/c/Users/david/projects/Auto-MFA/setup_wsl.sh
```

**"Only N/M segments aligned … too low to produce usable timings"**
Segments MFA can't align produce no output, and it reports success anyway.
The pipeline already retried with a wider beam. At this point the cause is
almost always the transcript not matching the audio:

- the FB2 has a section the narrator doesn't read (translator's preface,
  ПРИМЕЧАНИЯ / endnotes, a publisher's blurb) — pair around it, or use a
  cleaner FB2
- the recording is abridged, or a different edition from the text
- a chapter is paired to the wrong audio file — check the **Pairs** box

A handful of unaligned segments is normal and won't fail the run; it just
logs a note and leaves a small gap.

**Alignment looks shifted/wrong in the app**
Check the run's log for `alignment re-sync` warnings — those mean MFA failed
to align some words, and the surrounding timings may drift. Usually caused by
the FB2 text not matching what's actually read aloud (a LibriVox preamble, an
endnotes section, a different edition).

**"No rclone remote named 'r2' is configured"**
Run the `rclone config create` command in *Uploading the audio to R2* above.
The script prints it too, with your endpoint already filled in.

**Book installed but doesn't appear in the picker**
Check `public/books/index.json` has an entry whose `filename` matches the FB2
you installed. The install script writes one; if you moved or renamed the FB2
afterwards, the entry no longer matches.

**Book appears but has no audio / audio 404s**
The `audio_url` fields point at R2. Either the audio wasn't uploaded (run the
upload script), or **R2 folder** was blank when the JSONs were generated, so
the URLs are bare filenames.

**Book plays but highlighting is stuck or wrong**
Hard-refresh first (Ctrl+Shift+R) — cached JSONs are the usual culprit. If it
persists, the app reads the top-level `word_timings` array; check that it's
present and non-empty in one of the installed chapter files.

**Wrong chapters aligned**
The FB2's chapter list is what the GUI shows. If it doesn't match the audio
(extra preamble section, notes counted as a chapter), fix the pairing by hand
rather than using Auto-pair.

---

## One-time setup (already done on this machine)

**1. Install WSL** — elevated PowerShell:

```powershell
# PowerShell (as Administrator)
wsl --install
```

**2. Build the MFA environment** — takes several minutes, downloads ~1–2 GB:

```bash
# WSL
bash /mnt/c/Users/david/projects/Auto-MFA/setup_wsl.sh
```

**3. Build the GUI** — only needed after changing the app's code:

```powershell
# PowerShell
cd C:\Users\david\projects\Auto-MFA
.\build.ps1 -SkipRuntime
```

`-SkipRuntime` is correct: the GUI only needs tkinter now, since alignment
runs in WSL.
