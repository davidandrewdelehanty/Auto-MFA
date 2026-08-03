"""Tkinter GUI for Auto-MFA."""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
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
        self.chunk_mb = tk.StringVar(value="2048")
        self.auto_download = tk.BooleanVar(value=True)
        self.keep_temp = tk.BooleanVar(value=False)

        self.chapters = []
        self.audio_files: list[Path] = []
        self.pairs: list[tuple[int, int]] = []
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
        self.chapter_list = self._make_listbox(
            pairing, "FB2 chapters", 2)

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
        ttk.Label(opts, text="Max chunk size (MB):").grid(row=1, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.chunk_mb, width=10).grid(
            row=1, column=1, sticky="w", padx=4)
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
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_sb.set)
        root.rowconfigure(6, weight=3)

    def _make_listbox(self, parent: ttk.Frame, label: str, col: int) -> tk.Listbox:
        frame = ttk.LabelFrame(parent, text=label, padding=4)
        frame.grid(row=0, column=col, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        lb = tk.Listbox(frame, height=8, exportselection=False)
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
        chapter_sel = self.chapter_list.curselection()
        if not audio_sel or not chapter_sel:
            messagebox.showinfo("Pair", "Select one audio file and one chapter first.")
            return
        audio_idx, chapter_idx = audio_sel[0], chapter_sel[0]
        if any(a == audio_idx for a, _ in self.pairs):
            messagebox.showinfo(
                "Already paired",
                f"'{self.audio_files[audio_idx].name}' is already paired. "
                "Unpair it first to change the mapping.")
            return
        self.pairs.append((audio_idx, chapter_idx))
        self._refresh_pairs()
        nxt = audio_idx + 1
        if nxt < len(self.audio_files):
            self.audio_list.selection_clear(0, "end")
            self.audio_list.selection_set(nxt)
            self.audio_list.see(nxt)

    def _remove_selected_pair(self) -> None:
        sel = self.pairs_list.curselection()
        if not sel:
            return
        del self.pairs[sel[0]]
        self._refresh_pairs()

    def _refresh_pairs(self) -> None:
        self.pairs_list.delete(0, "end")
        for audio_idx, chapter_idx in self.pairs:
            self.pairs_list.insert(
                "end",
                f"{self.audio_files[audio_idx].name}  ⇄  "
                f"{self.chapters[chapter_idx]['title']}")

    # --------------------------------------------------------- pipeline
    def _build_job(self) -> Path:
        try:
            chunk_mb = int(self.chunk_mb.get())
            if chunk_mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Chunk size", "Max chunk size must be a positive integer (MB).")
            raise
        job = {
            "pairs": [
                {
                    "audio": str(self.audio_files[a]),
                    "title": self.chapters[c]["title"],
                    "text": self.chapters[c]["text"],
                }
                for a, c in self.pairs
            ],
            "acoustic_model": self.acoustic_model.get().strip() or "russian_mfa",
            "dictionary": self.dictionary.get().strip() or "russian_mfa",
            "output_dir": self.output_folder.get() or self.book_folder.get(),
            "chunk_limit_bytes": chunk_mb * 1024 * 1024,
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
        env = os.environ.copy()
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
            return [sys.executable, "--worker", *args], None
        # Source mode: invoke main.py as a script (it bootstraps the project
        # root onto sys.path itself), so cwd does not matter.
        script = Path(__file__).resolve().parent / "main.py"
        return [sys.executable, str(script), "--worker", *args], None

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
            try:
                self.progress["value"] = float(frac) * 100
            except ValueError:
                pass
            if msg:
                self.log(msg)
        elif line.startswith("@DONE|"):
            self._done_received = True
            self.progress["value"] = 100
            result = line[len("@DONE|"):]
            self.log(f"Done: {result}")
            messagebox.showinfo("Complete", f"Alignment finished.\n\nZip saved to:\n{result}")
            self._set_busy(False)
        elif line.startswith("@ERROR|"):
            self._done_received = True
            msg = line[len("@ERROR|"):]
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Error", msg)
            self._set_busy(False)
        elif line.strip():
            self.log(line)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._done_received = False
        state = "disabled" if busy else "normal"
        for widget in (self.btn_begin, self.btn_download):
            widget.configure(state=state)
        if busy:
            self.progress["value"] = 0
            self.log("---")
            self.log("Working…")

    # ------------------------------------------------------------ logging
    def log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def launch() -> None:
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    try:
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
    except tk.TclError:
        pass
    app = AutoMfaApp()
    app.mainloop()
