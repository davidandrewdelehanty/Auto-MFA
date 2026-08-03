"""Tkinter GUI for Auto-MFA."""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .fb2 import extract_chapters, find_audio_files, find_fb2, transcript_words
from .pipeline import default_zip_name

_MSG_PREFIXES = ("@STATUS|", "@PROGRESS|", "@DONE|", "@ERROR|")


class AutoMfaApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Auto-MFA v{__version__}")
        self.geometry("980x720")
        self.minsize(820, 600)

        self.book_folder = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.acoustic_model = tk.StringVar(value="russian_mfa")
        self.dictionary = tk.StringVar(value="russian_mfa")
        self.target_seconds = tk.StringVar(value="30")
        # Default to 1: on Windows, MFA has been observed to silently drop
        # most of its own output (e.g. 5 TextGrids written out of 29
        # expected) while still reporting success, regardless of
        # --num_jobs -- this is a Windows multiprocessing-pool issue, not
        # specifically about parallel export (see run_pipeline's automatic
        # detect + escalating retry in pipeline.py, which stays in place as
        # a safety net either way). Raise this back to 2+ in the field below
        # if you want to try it -- alignment itself is faster with more
        # jobs when it works, the auto-retry will still catch it if it
        # doesn't.
        self.num_jobs = tk.StringVar(value="1")
        self.auto_download = tk.BooleanVar(value=True)
        self.keep_temp = tk.BooleanVar(value=False)

        self.chapters = []
        self.audio_files: list[Path] = []
        # Each pair is (audio_idx, chapter_idxs) where chapter_idxs is a
        # tuple of one or more chapter indices -- more than one means this
        # single audio file spans that whole (contiguous) run of chapters,
        # e.g. a "whole book" recording; see _pair_selected / _build_job.
        self.pairs: list[tuple[int, tuple[int, ...]]] = []
        self._busy = False
        self._proc = None
        self._msg_queue: "queue.Queue[str]" = queue.Queue()

        self._build_widgets()
        self.after(100, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # Book folder row
        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Book folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.book_folder).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse…", command=self._browse_folder).grid(
            row=0, column=2)

        # This build runs MFA through Windows-native conda/kaldi binaries,
        # which are meaningfully slower than the same alignment on Linux
        # (Windows lacks fork(), pays CreateProcess overhead on every one of
        # Kaldi's many short-lived subprocess calls, and unsigned freshly
        # extracted binaries get real-time-scanned by Defender). Nudge
        # people toward WSL, where this same app runs directly against a
        # normal Linux conda env with none of that overhead -- only shown
        # when actually running natively on Windows, not inside WSL itself.
        if sys.platform == "win32":
            wsl_hint = ttk.Label(
                top,
                text=("Running on Windows? Alignment is noticeably faster "
                      "under WSL Ubuntu -- click for setup instructions"),
                foreground="#0645AD", cursor="hand2",
                font=("Segoe UI", 9, "underline"),
            )
            wsl_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
            wsl_hint.bind(
                "<Button-1>",
                lambda e: webbrowser.open("https://learn.microsoft.com/en-us/windows/wsl/install"),
            )

        # Pairing panes
        pairing = ttk.Frame(root)
        pairing.grid(row=1, column=0, sticky="nsew")
        pairing.columnconfigure(0, weight=3)
        pairing.columnconfigure(1, weight=0)
        pairing.columnconfigure(2, weight=5)
        pairing.rowconfigure(0, weight=1)
        root.rowconfigure(1, weight=2)

        self.audio_list = self._make_listbox(
            pairing, "Audio files (in folder)", 0)
        middle = ttk.Frame(pairing)
        middle.grid(row=0, column=1, padx=6, pady=30)
        ttk.Button(middle, text="Pair ▶", command=self._pair_selected).pack(pady=4)
        ttk.Button(middle, text="◀ Unpair", command=self._remove_selected_pair).pack(pady=4)
        ttk.Button(middle, text="Auto-pair\nin order", command=self._auto_pair).pack(pady=4)
        self.chapter_list = self._make_listbox(
            pairing, "FB2 chapters (select a range to pair many to one file)", 2,
            selectmode="extended")

        # Pairs summary
        pairs_frame = ttk.LabelFrame(root, text="Pairs (audio ⇄ chapter)", padding=4)
        pairs_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        pairs_frame.columnconfigure(0, weight=1)
        self.pairs_list = tk.Listbox(pairs_frame, height=4)
        self.pairs_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(pairs_frame, command=self.pairs_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.pairs_list.configure(yscrollcommand=sb.set)
        pairs_frame.rowconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # Options
        opts = ttk.LabelFrame(root, text="Alignment options", padding=6)
        opts.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(3, weight=1)
        ttk.Label(opts, text="Acoustic model:").grid(row=0, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.acoustic_model).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Label(opts, text="Dictionary:").grid(row=0, column=2, sticky="w")
        ttk.Entry(opts, textvariable=self.dictionary).grid(
            row=0, column=3, sticky="ew", padx=4)
        ttk.Label(opts, text="Target utterance length (s):").grid(row=1, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.target_seconds, width=10).grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(opts, text="Parallel jobs:").grid(row=1, column=2, sticky="w")
        ttk.Entry(opts, textvariable=self.num_jobs, width=10).grid(
            row=1, column=3, sticky="w", padx=4)
        ttk.Label(opts, text="Output folder:").grid(row=2, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.output_folder).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=4)
        ttk.Button(opts, text="Browse…", command=self._browse_output).grid(
            row=2, column=3, sticky="w")
        ttk.Checkbutton(opts, text="Auto-download missing models",
                        variable=self.auto_download).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(opts, text="Keep temporary files",
                        variable=self.keep_temp).grid(
            row=3, column=2, columnspan=2, sticky="w", pady=(4, 0))

        # Buttons
        btns = ttk.Frame(root)
        btns.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.btn_download = ttk.Button(
            btns, text="Download models", command=self._download_models)
        self.btn_download.pack(side="left")
        self.btn_begin = ttk.Button(
            btns, text="BEGIN", command=self._begin, style="Accent.TButton")
        self.btn_begin.pack(side="right")

        self.progress = ttk.Progressbar(root, maximum=100, mode="determinate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        log_frame = ttk.LabelFrame(root, text="Log", padding=4)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        # Left in "normal" state permanently (never toggled to "disabled")
        # so mouse selection and Ctrl+C always work like any ordinary text
        # box; a <Key> binding blocks actual edits instead, which is a more
        # reliable way to get a read-only-but-selectable log than relying on
        # Tk's disabled-state selection behavior (inconsistent across
        # platforms/Tk builds).
        self.log_text = tk.Text(log_frame, height=10, wrap="word", cursor="xterm")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.bind("<Key>", self._log_key_guard)
        log_btns = ttk.Frame(log_frame)
        log_btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(log_btns, text="Select all", command=self._select_all_log).pack(
            side="right", padx=(4, 0))
        ttk.Button(log_btns, text="Copy log", command=self._copy_log).pack(
            side="right")
        root.rowconfigure(6, weight=3)

    def _make_listbox(self, parent: ttk.Frame, label: str, col: int,
                      selectmode: str = "browse") -> tk.Listbox:
        frame = ttk.LabelFrame(parent, text=label, padding=4)
        frame.grid(row=0, column=col, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        lb = tk.Listbox(frame, height=8, exportselection=False, selectmode=selectmode)
        lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, command=lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        lb.configure(yscrollcommand=sb.set)
        return lb

    # ------------------------------------------------------------ actions
    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose folder with .fb2 and audio")
        if not chosen:
            return
        self.book_folder.set(chosen)
        self._load_folder(Path(chosen))

    def _load_folder(self, folder: Path) -> None:
        try:
            fb2_path = find_fb2(folder)
            self.chapters = extract_chapters(fb2_path)
            self.audio_files = find_audio_files(folder)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Could not load folder", str(exc))
            return
        self.pairs = []
        self.output_folder.set(str(folder))

        self.audio_list.delete(0, "end")
        for a in self.audio_files:
            self.audio_list.insert("end", a.name)
        self.chapter_list.delete(0, "end")
        for i, ch in enumerate(self.chapters, start=1):
            words = len(transcript_words(ch["text"]))
            self.chapter_list.insert("end", f"{i}. {ch['title']}  ({words} words)")
        self._refresh_pairs()
        self.log(f"Loaded {len(self.audio_files)} audio file(s), "
                 f"{len(self.chapters)} chapter(s) from {fb2_path.name}.")
        if not self.audio_files:
            messagebox.showwarning(
                "No audio", "No supported audio files found in this folder.")

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(title="Choose output folder for the zip")
        if chosen:
            self.output_folder.set(chosen)

    def _pair_selected(self) -> None:
        audio_sel = self.audio_list.curselection()
        chapter_sel = sorted(self.chapter_list.curselection())
        if not audio_sel or not chapter_sel:
            messagebox.showinfo(
                "Pair", "Select one audio file and one or more chapters first.")
            return
        audio_idx = audio_sel[0]
        if any(a == audio_idx for a, _ in self.pairs):
            messagebox.showinfo(
                "Already paired",
                f"'{self.audio_files[audio_idx].name}' is already paired. "
                "Unpair it first to change the mapping.")
            return
        # A single audio file can be paired to a whole RUN of chapters (one
        # big recording spanning several chapters at once), but only if that
        # run is contiguous and in order -- the pipeline figures out where
        # each chapter starts by how far through the audio its words land,
        # which only makes sense for chapters read in that same order.
        if chapter_sel != list(range(chapter_sel[0], chapter_sel[-1] + 1)):
            messagebox.showerror(
                "Non-contiguous selection",
                "When pairing one audio file to multiple chapters, the "
                "chapters must be a contiguous, in-order run (e.g. chapters "
                "5-12). Select a single unbroken range.")
            return
        already_paired = {c for _, cs in self.pairs for c in cs}
        if already_paired & set(chapter_sel):
            messagebox.showinfo(
                "Already paired",
                "One or more of the selected chapters is already paired to "
                "another audio file. Unpair it first to change the mapping.")
            return
        self.pairs.append((audio_idx, tuple(chapter_sel)))
        self._refresh_pairs()
        nxt = audio_idx + 1
        if nxt < len(self.audio_files):
            self.audio_list.selection_clear(0, "end")
            self.audio_list.selection_set(nxt)
            self.audio_list.see(nxt)

    def _auto_pair(self) -> None:
        """Pair audio[i] with chapter[i] positionally, for the common case of a
        multi-chapter book whose audio files and FB2 chapters are already in
        the same order -- avoids clicking Pair 300+ times for a long book."""
        if not self.audio_files or not self.chapters:
            messagebox.showinfo("Auto-pair", "Load a folder with audio and an FB2 first.")
            return
        n = min(len(self.audio_files), len(self.chapters))
        if len(self.audio_files) != len(self.chapters):
            if not messagebox.askyesno(
                "Count mismatch",
                f"{len(self.audio_files)} audio file(s) but {len(self.chapters)} chapter(s). "
                f"Only the first {n} will be paired positionally (audio[i] ⇄ chapter[i]). "
                "Continue?"):
                return
        self.pairs = [(i, (i,)) for i in range(n)]
        self._refresh_pairs()
        self.log(f"Auto-paired {n} audio file(s) to chapter(s) in order.")

    def _remove_selected_pair(self) -> None:
        sel = self.pairs_list.curselection()
        if not sel:
            return
        del self.pairs[sel[0]]
        self._refresh_pairs()

    def _refresh_pairs(self) -> None:
        self.pairs_list.delete(0, "end")
        for audio_idx, chapter_idxs in self.pairs:
            if len(chapter_idxs) == 1:
                label = self.chapters[chapter_idxs[0]]["title"]
            else:
                label = (f"{self.chapters[chapter_idxs[0]]['title']} .. "
                         f"{self.chapters[chapter_idxs[-1]]['title']} "
                         f"({len(chapter_idxs)} chapters)")
            self.pairs_list.insert(
                "end", f"{self.audio_files[audio_idx].name}  ⇄  {label}")

    # --------------------------------------------------------- pipeline
    def _build_job(self) -> Path:
        try:
            target_seconds = float(self.target_seconds.get())
            if target_seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Target utterance length",
                "Target utterance length must be a positive number of seconds.")
            raise
        try:
            num_jobs = int(self.num_jobs.get())
            if num_jobs <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Parallel jobs", "Parallel jobs must be a positive integer.")
            raise
        # Mirrors the 30s/45s target/max ratio proven out on the govorim
        # audiobook pipeline (see segment.py's module docstring).
        max_seconds = target_seconds * 1.5
        job = {
            "pairs": [
                {
                    "audio": str(self.audio_files[a]),
                    "title": (
                        self.chapters[cs[0]]["title"] if len(cs) == 1
                        else f"{self.chapters[cs[0]]['title']} - {self.chapters[cs[-1]]['title']}"
                    ),
                    "text": " ".join(self.chapters[c]["text"] for c in cs),
                    # Only set for a multi-chapter pairing: (title, word_count)
                    # per constituent chapter, in order -- lets the pipeline
                    # split the alignment back into one result per chapter.
                    # See Pair.sub_chapters in pipeline.py.
                    "sub_chapters": (
                        [[self.chapters[c]["title"], len(transcript_words(self.chapters[c]["text"]))]
                         for c in cs]
                        if len(cs) > 1 else None
                    ),
                }
                for a, cs in self.pairs
            ],
            "acoustic_model": self.acoustic_model.get().strip() or "russian_mfa",
            "dictionary": self.dictionary.get().strip() or "russian_mfa",
            "output_dir": self.output_folder.get() or self.book_folder.get(),
            "target_seconds": target_seconds,
            "max_seconds": max_seconds,
            "num_jobs": num_jobs,
            "zip_name": default_zip_name(Path(self.output_folder.get() or ".")),
            "auto_download": self.auto_download.get(),
            "keep_temp": self.keep_temp.get(),
        }
        fd, path = tempfile.mkstemp(prefix="auto_mfa_job_", suffix=".json")
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False)
        return Path(path)

    def _begin(self) -> None:
        if self._busy:
            return
        if not self.pairs:
            messagebox.showinfo("Nothing to do", "Pair at least one audio file with a chapter.")
            return
        unpaired = {i for i in range(len(self.audio_files))} - {a for a, _ in self.pairs}
        if unpaired:
            names = ", ".join(self.audio_files[i].name for i in sorted(unpaired))
            if not messagebox.askyesno(
                "Unpaired audio",
                f"These audio files are not paired and will be skipped:\n{names}\n\n"
                "Continue anyway?"):
                return
        try:
            job_path = self._build_job()
        except Exception:  # noqa: BLE001
            return
        self._set_busy(True)
        self._start_worker(["align", str(job_path)])

    def _download_models(self) -> None:
        if self._busy:
            return
        acoustic = self.acoustic_model.get().strip() or "russian_mfa"
        dictionary = self.dictionary.get().strip() or "russian_mfa"
        self._set_busy(True)
        self._start_worker(["download-models", acoustic, dictionary])

    # ------------------------------------------------------- worker
    def _runtime_python(self):
        """Path to the bundled MFA runtime's interpreter (if present).

        Prefers pythonw.exe (windowed) so no console window flashes while the
        worker runs; it still writes to the GUI's pipe (verified on Windows).
        """
        if not getattr(sys, "frozen", False):
            return None
        base = Path(sys.executable).parent / "runtime"
        for name in ("pythonw.exe", "python.exe"):
            candidate = base / name
            if candidate.is_file():
                return candidate
        return None

    def _base_env(self) -> dict:
        """Env vars every worker invocation needs, regardless of how it is
        launched.

        PYTHONUTF8 forces Python's UTF-8 mode, which is what decides the
        *default* text encoding used by `open()` calls that don't pass their
        own `encoding=` -- including ones deep inside MFA/kalpy that we don't
        control. Without it, Windows falls back to the system locale codepage
        (commonly cp1252), and writing/reading the Cyrillic corpus text
        crashes with "'charmap' codec can't encode characters...". This has
        to be an env var (not e.g. a `-X utf8` flag) because MFA spawns its
        own worker subprocesses for parallel jobs, and only env vars survive
        that re-exec automatically. PYTHONIOENCODING is set too as a
        belt-and-suspenders for stdio specifically.
        """
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _runtime_env(self, runtime_python):
        """Return an env dict with the runtime's bin dirs on PATH, so DLLs
        (kaldi, ffmpeg, MKL...) are found even though the env is not activated.
        """
        runtime = Path(runtime_python).parent
        bin_dirs = [
            runtime / "Library" / "mingw-w64" / "bin",
            runtime / "Library" / "bin",
            runtime / "Scripts",
            runtime,
        ]
        env = self._base_env()
        existing = env.get("PATH", "")
        prefix = os.pathsep.join(str(d) for d in bin_dirs)
        env["PATH"] = prefix + os.pathsep + existing
        return env

    def _worker_command(self, args: list[str]) -> tuple[list[str], dict | None]:
        runtime_py = self._runtime_python()
        if runtime_py is not None:
            return (
                [str(runtime_py), "-m", "app.main", "--worker", *args],
                self._runtime_env(runtime_py),
            )
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker", *args], self._base_env()
        # Source mode: invoke main.py as a script (it bootstraps the project
        # root onto sys.path itself), so cwd does not matter.
        script = Path(__file__).resolve().parent / "main.py"
        return [sys.executable, str(script), "--worker", *args], self._base_env()

    def _start_worker(self, args: list[str]) -> None:
        cmd, extra_env = self._worker_command(args)
        self.log("$ " + " ".join(str(c) for c in cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=extra_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Failed to start worker", str(exc))
            self._set_busy(False)
            return
        self._proc = proc
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self._msg_queue.put(line)
        code = proc.wait()
        self._msg_queue.put(f"__EXIT__{code}")

    def _drain_queue(self) -> None:
        exited = False
        while True:
            try:
                line = self._msg_queue.get_nowait()
            except queue.Empty:
                break
            if line.startswith("__EXIT__"):
                exited = True
                code = int(line[len("__EXIT__"):])
                if self._busy and not self._done_received:
                    if code != 0:
                        self.log(f"[worker exited with code {code}]")
                        messagebox.showerror(
                            "Alignment failed",
                            "The worker process failed. See the log for details.")
                    else:
                        self.log("[worker finished]")
                    self._set_busy(False)
            else:
                self._handle_line(line)
        if not exited:
            self.after(100, self._drain_queue)

    def _handle_line(self, line: str) -> None:
        line = line.rstrip("\n")
        if line.startswith("@STATUS|"):
            self.log(line[len("@STATUS|"):])
        elif line.startswith("@PROGRESS|"):
            _, frac, msg = line.split("|", 2)
            if msg == "aligning":
                # The actual `mfa align` run is one long synchronous call
                # with no progress callback back into this app -- it can
                # run for many minutes on a full chapter with nothing to
                # report in between. A determinate bar frozen at 0% for
                # that whole stretch reads as "hung", so switch to an
                # animated indeterminate bar for just this phase: an
                # honest "still working, no ETA available" signal instead
                # of a number we can't actually back up.
                self._set_progress_indeterminate(True)
            else:
                self._set_progress_indeterminate(False)
                try:
                    self.progress["value"] = float(frac) * 100
                except ValueError:
                    pass
            if msg:
                self.log(msg)
        elif line.startswith("@DONE|"):
            self._done_received = True
            self._set_progress_indeterminate(False)
            self.progress["value"] = 100
            result = line[len("@DONE|"):]
            self.log(f"Done: {result}")
            messagebox.showinfo("Complete", f"Alignment finished.\n\nZip saved to:\n{result}")
            self._set_busy(False)
        elif line.startswith("@ERROR|"):
            self._done_received = True
            self._set_progress_indeterminate(False)
            msg = line[len("@ERROR|"):]
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Error", msg)
            self._set_busy(False)
        elif line.strip():
            self.log(line)

    def _set_progress_indeterminate(self, on: bool) -> None:
        currently_on = str(self.progress.cget("mode")) == "indeterminate"
        if on and not currently_on:
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
        elif not on and currently_on:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._done_received = False
        state = "disabled" if busy else "normal"
        for widget in (self.btn_begin, self.btn_download):
            widget.configure(state=state)
        if busy:
            self._set_progress_indeterminate(False)
            self.progress["value"] = 0
            self.log("---")
            self.log("Working…")

    # ------------------------------------------------------------ logging
    def log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _log_key_guard(self, event: "tk.Event") -> "str | None":
        """Block typing/edits in the log box while leaving selection, Ctrl+C
        copy, and navigation (arrows/Home/End/Page Up/Down) working.
        """
        allowed_keysyms = {
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
        }
        ctrl_down = bool(event.state & 0x4)
        if ctrl_down and event.keysym.lower() in ("c", "a", "insert"):
            if event.keysym.lower() == "a":
                self._select_all_log()
                return "break"
            return None  # let Ctrl+C / Ctrl+Insert copy proceed normally
        if event.keysym in allowed_keysyms:
            return None
        return "break"

    def _select_all_log(self) -> None:
        self.log_text.tag_add("sel", "1.0", "end")
        self.log_text.mark_set("insert", "end")
        self.log_text.see("insert")

    def _copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)


def _configure_unicode_fonts(root: tk.Tk) -> None:
    """Point every Tk named font at a family with real Cyrillic (and broad
    Unicode) glyph coverage.

    Filenames/chapter titles/folder names in this app are routinely
    Cyrillic (Russian audiobooks). Tk's own default font on some setups --
    seen on WSL/Linux Tk installs in particular -- has no Cyrillic glyphs
    at all; the fontconfig fallback path some systems use for that case
    renders each missing glyph as a small box containing its hex Unicode
    codepoint, which reads as "random digits" instead of Cyrillic text.
    Reconfiguring the named fonts (rather than each widget individually)
    fixes every widget that uses them -- ttk widgets always reference named
    fonts, and so do classic Listbox/Text/Label/Button/Entry widgets unless
    given an explicit font tuple.
    """
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return

    def pick(preferred: list[str]) -> str | None:
        return next((f for f in preferred if f in available), None)

    ui_family = pick([
        "Segoe UI",        # Windows -- already has full Cyrillic coverage
        "Noto Sans",       # common on modern Linux, broad Unicode coverage
        "DejaVu Sans",     # near-universal on Linux, incl. most WSL images
        "Liberation Sans",
        "Arial",
        "Helvetica",
    ])
    mono_family = pick([
        "Consolas",
        "DejaVu Sans Mono",
        "Noto Sans Mono",
        "Liberation Mono",
        "Courier New",
    ])

    if ui_family:
        for name in (
            "TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
            "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=ui_family)
            except tk.TclError:
                pass
    if mono_family:
        try:
            tkfont.nametofont("TkFixedFont").configure(family=mono_family)
        except tk.TclError:
            pass


def launch() -> None:
    # Create the real root window FIRST. ttk.Style() with no widget passed
    # in looks for an existing Tk root and, if none exists yet, silently
    # creates one itself (Tkinter's "implicit default root" behavior) --
    # that phantom root is a second, empty window that never gets any
    # widgets or a title, which is exactly the blank window some users see
    # alongside the real app window on every launch. Building AutoMfaApp
    # (a tk.Tk subclass) first, then passing it explicitly to Style(),
    # avoids ever creating that phantom root.
    app = AutoMfaApp()
    _configure_unicode_fonts(app)
    style = ttk.Style(app)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    try:
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
    except tk.TclError:
        pass
    try:
        # On some Windows machines the "vista" ttk theme fails to set a
        # visible foreground color for Entry widgets specifically (Button
        # and Listbox render fine), so bound text is present but invisible.
        # Force explicit, known-good colors regardless of theme quirks.
        style.configure(
            "TEntry",
            foreground="black",
            fieldbackground="white",
            insertcolor="black",
        )
        style.map(
            "TEntry",
            foreground=[("disabled", "#666666")],
            fieldbackground=[("disabled", "#e0e0e0")],
        )
    except tk.TclError:
        pass
    app.mainloop()
