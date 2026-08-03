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

### 4. Fill in the two Govorim fields

- **Book slug** — names every output file. `chekhov-dama` produces
  `chekhov-dama-ch01.json`, `chekhov-dama-ch02.json`, …
  Use the same slug pattern as the existing files in `public/books/audio/`.
- **R2 folder** — the folder in the `govorim-audio` bucket where this book's
  audio lives (e.g. `dama`). This builds each file's `audio_url`. Leave it
  blank if the audio isn't uploaded yet; you can fill the URLs in later.

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

## Getting the JSONs into Govorim

Copy them into the repo, then commit:

```bash
# Git Bash
cd /c/Users/david/projects/govorim-app
cp /c/Users/david/path/to/book/<slug>-ch*.json public/books/audio/
git add public/books/audio/
git commit -m "Add <book> alignment JSONs"
git push
```

Vercel picks up the push automatically; the deploy usually takes 1–2 minutes.

If the audio wasn't on R2 yet when you generated (you left **R2 folder**
blank), upload it and fix the `audio_url` fields before committing.

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

**"MFA did not produce N/N expected TextGrid files"**
This is the Windows bug that shouldn't happen on Linux. If it does appear,
send the log — it means something new. The pipeline already retries twice
(`--num_jobs 1`, then `--disable_mp`) before giving this up.

**Alignment looks shifted/wrong in the app**
Check the run's log for `alignment re-sync` warnings — those mean MFA failed
to align some words, and the surrounding timings may drift. Usually caused by
the FB2 text not matching what's actually read aloud (a LibriVox preamble, an
endnotes section, a different edition).

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
