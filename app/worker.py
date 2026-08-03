"""Headless worker subprocess.

The GUI spawns its own executable with ``--worker <mode> ...`` so that MFA runs
in a separate process; its output can be streamed live into the GUI log pane
without blocking the UI and without crashing the GUI if MFA dies.

Protocol (one line per message on stdout):
    @STATUS|<message>
    @PROGRESS|<fraction>|<message>
    @DONE|<result>
    @ERROR|<message>
All other lines (MFA / ffmpeg output) are passed through untouched.
"""

import json
import os
import sys
from pathlib import Path
from typing import List

from . import segment as segment_mod
from .pipeline import Pair, ensure_models, model_present, run_pipeline


def _ensure_stdout() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _status(msg: str) -> None:
    print(f"@STATUS|{msg}", flush=True)


def _progress(frac: float, msg: str) -> None:
    print(f"@PROGRESS|{frac}|{msg}", flush=True)


def _fail(msg: str) -> int:
    print(f"@ERROR|{msg}", flush=True)
    return 1


def cmd_align(args: List[str]) -> int:
    if len(args) < 1:
        return _fail("usage: worker align <jobfile.json>")
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    pairs = [
        Pair(Path(p["audio"]), p.get("title", ""), p["text"])
        for p in job["pairs"]
    ]
    if not pairs:
        return _fail("No pairs found in job file.")
    try:
        zip_path = run_pipeline(
            pairs,
            acoustic=job.get("acoustic_model", "russian_mfa"),
            dictionary=job.get("dictionary", "russian_mfa"),
            output_dir=Path(job["output_dir"]),
            zip_name=job.get("zip_name"),
            auto_download=job.get("auto_download", True),
            keep_temp=job.get("keep_temp", False),
            num_jobs=job.get("num_jobs", 2),
            target_seconds=job.get("target_seconds", segment_mod.DEFAULT_TARGET),
            max_seconds=job.get("max_seconds", segment_mod.DEFAULT_MAX),
            log=_status,
            progress=_progress,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))
    print(f"@DONE|{zip_path}", flush=True)
    return 0


def cmd_download_models(args: List[str]) -> int:
    acoustic = args[0] if args else "russian_mfa"
    dictionary = args[1] if len(args) > 1 else "russian_mfa"
    try:
        ensure_models(acoustic, dictionary, auto_download=True, log=_status)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))
    print("@DONE|models ready", flush=True)
    return 0


def cmd_check_models(args: List[str]) -> int:
    acoustic = args[0] if args else "russian_mfa"
    dictionary = args[1] if len(args) > 1 else "russian_mfa"
    acoustic_ok = model_present("acoustic", acoustic)
    dict_ok = model_present("dictionary", dictionary)
    print(f"@STATUS|acoustic={acoustic_ok} dictionary={dict_ok}", flush=True)
    print(f"@DONE|{acoustic_ok and dict_ok}", flush=True)
    return 0


def main(argv: List[str]) -> int:
    _ensure_stdout()
    if len(argv) >= 2 and argv[0] == "--worker":
        mode = argv[1]
        rest = argv[2:]
    elif len(argv) >= 1 and argv[0] == "--worker":
        return _fail("missing worker mode")
    else:
        return _fail("not a worker invocation (expected --worker <mode>)")

    if mode == "align":
        return cmd_align(rest)
    if mode == "download-models":
        return cmd_download_models(rest)
    if mode == "check-models":
        return cmd_check_models(rest)
    return _fail(f"unknown worker mode: {mode}")
