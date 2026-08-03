"""End-to-end pipeline: build an MFA corpus from paired audio+text, run MFA,
convert the resulting TextGrids to JSON (recombining chunked files), and zip
the JSONs.
"""

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import audio as audio_mod
from . import chunking
from .fb2 import transcript_words, words_to_text
from .textgrid import parse_textgrid

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


@dataclass
class Pair:
    audio: Path
    title: str
    text: str


@dataclass
class CorpusJob:
    pair_index: int
    chunk_index: int
    wav: str
    txt: str


def _null_log(msg: str) -> None:
    pass


def _null_progress(frac: float, msg: str) -> None:
    pass


def mfa_data_dir() -> Path:
    for var in ("MFA_ROOT_DIR", "MFA_DATA_DIR", "MFA_USER_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    return Path.home() / "Documents" / "MFA"


def model_present(model_type: str, name: str) -> bool:
    """Check whether an MFA pretrained model is already downloaded."""
    base = mfa_data_dir() / "pretrained_models"
    if not base.is_dir():
        return False
    for candidate in (base / model_type / f"{name}.zip",
                      base / model_type / name):
        if candidate.exists():
            return True
    return False


def download_model(model_type: str, name: str, log: LogFn) -> None:
    log(f"Downloading {model_type} model '{name}' (one-time, needs internet)...")
    invoke_mfa(["model", "download", model_type, name], log)
    log(f"Model '{name}' ready.")


def ensure_models(acoustic: str, dictionary: str, auto_download: bool,
                  log: LogFn) -> None:
    for model_type, name in (("acoustic", acoustic), ("dictionary", dictionary)):
        if model_present(model_type, name):
            log(f"Using cached {model_type} model '{name}'.")
            continue
        if not auto_download:
            raise RuntimeError(
                f"Missing {model_type} model '{name}'. Enable 'auto-download "
                f"models' or run the Download models button."
            )
        download_model(model_type, name, log)
    # Optional G2P model helps with out-of-dictionary words. Best-effort only.
    if not model_present("g2p", dictionary):
        try:
            download_model("g2p", dictionary, log)
        except Exception as exc:  # noqa: BLE001
            log(f"Note: could not fetch optional g2p model ({exc}).")


def invoke_mfa(args: List[str], log: LogFn) -> int:
    """Invoke the MFA CLI inside the current (worker) process."""
    log("> mfa " + " ".join(args))
    try:
        from montreal_forced_aligner.command_line.mfa import mfa_cli
    except ImportError:
        from montreal_forced_aligner.command_line.mfa import main as mfa_cli
    try:
        mfa_cli(args)  # click.Group is callable with a list of argv
    except TypeError:
        mfa_cli.main(args, prog_name="mfa")
    return 0


def prepare_corpus(pairs: List[Pair], work_dir: Path, chunk_limit_bytes: int,
                   log: LogFn, progress: ProgressFn,
                   ffmpeg: Optional[str] = None) -> List[CorpusJob]:
    """Convert audio to 16 kHz WAV, chunk oversized files, write transcripts.

    Returns the list of corpus jobs (one per chunk / per pair).
    """
    raw_dir = work_dir / "raw"
    corpus_dir = work_dir / "corpus"
    raw_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    jobs: List[CorpusJob] = []
    total = len(pairs)
    for idx, pair in enumerate(pairs):
        log(f"[{idx + 1}/{total}] Preparing '{pair.audio.name}'")
        stem = f"{idx:03d}_{audio_mod.safe_stem(pair.audio.name)}"
        raw_wav = raw_dir / f"{stem}.wav"
        audio_mod.convert_to_wav(str(pair.audio), str(raw_wav), ffmpeg)

        words = transcript_words(pair.text)
        if not words:
            log(f"  Warning: no usable words for '{pair.audio.name}'; skipping.")
            progress((idx + 1) / total, "skipped empty chapter")
            continue

        size = raw_wav.stat().st_size
        duration = audio_mod.probe_duration(str(raw_wav))
        plan = chunking.plan_chunks(duration, size, chunk_limit_bytes)
        if plan.needs_chunking:
            log(f"  Chunking into {plan.num_chunks} parts (audio is "
                f"{size / (1024 ** 3):.2f} GiB).")
            chunks = audio_mod.split_wav_by_duration(
                str(raw_wav), str(corpus_dir), stem, plan.chunk_duration, ffmpeg
            )
            parts = chunking.partition_words(words, plan.num_chunks)
            for c_i, chunk_wav in enumerate(chunks):
                txt = corpus_dir / f"{Path(chunk_wav).stem}.txt"
                txt.write_text(words_to_text(parts[c_i]), encoding="utf-8")
                jobs.append(CorpusJob(idx, c_i, chunk_wav, str(txt)))
        else:
            wav = corpus_dir / f"{stem}.wav"
            shutil.copy2(raw_wav, wav)
            txt = corpus_dir / f"{stem}.txt"
            txt.write_text(words_to_text(words), encoding="utf-8")
            jobs.append(CorpusJob(idx, 0, str(wav), str(txt)))
        progress((idx + 1) / total, f"prepared {pair.audio.name}")
    return jobs


def run_alignment(corpus_dir: Path, output_dir: Path, temp_dir: Path,
                  dictionary: str, acoustic: str, log: LogFn) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "align",
        str(corpus_dir),
        dictionary,
        acoustic,
        str(output_dir),
        "--clean",
        "--overwrite",
        "--temp_directory", str(temp_dir),
    ]
    invoke_mfa(args, log)


def _shift(tiers: Dict[str, List[Dict]], offset: float) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for name, intervals in tiers.items():
        shifted = []
        for it in intervals:
            shifted.append({
                "start": round(it["start"] + offset, 6),
                "end": round(it["end"] + offset, 6),
                "text": it["text"],
            })
        out[name] = shifted
    return out


def postprocess(output_dir: Path, pairs: List[Pair], jobs: List[CorpusJob],
                log: LogFn) -> List[Dict]:
    """Combine per-chunk TextGrids back into one JSON dict per audio pair."""
    by_pair: Dict[int, List[CorpusJob]] = {}
    for job in jobs:
        by_pair.setdefault(job.pair_index, []).append(job)
    for key in by_pair:
        by_pair[key].sort(key=lambda j: j.chunk_index)

    results: List[Dict] = []
    for pair_idx, pair in enumerate(pairs):
        pair_jobs = by_pair.get(pair_idx, [])
        if not pair_jobs:
            continue
        tiers: Dict[str, List[Dict]] = {}
        offset = 0.0
        duration = 0.0
        for job in pair_jobs:
            tg_path = Path(output_dir) / f"{Path(job.wav).stem}.TextGrid"
            if not tg_path.exists():
                raise FileNotFoundError(f"Expected alignment output: {tg_path}")
            parsed = parse_textgrid(tg_path)
            chunk_dur = 0.0
            for name, intervals in parsed["tiers"].items():
                for it in intervals:
                    chunk_dur = max(chunk_dur, it["end"])
            shifted = _shift(parsed["tiers"], offset)
            for name, intervals in shifted.items():
                tiers.setdefault(name, []).extend(intervals)
            offset += chunk_dur
            duration += chunk_dur
            log(f"  Combined '{Path(job.wav).stem}' ({chunk_dur:.2f}s)")

        words = [i for i in tiers.get("words", []) if i["text"]]
        phones = [i for i in tiers.get("phones", []) if i["text"]]
        result = {
            "audio_file": pair.audio.name,
            "title": pair.title,
            "duration": round(duration, 6),
            "words": words,
            "phones": phones,
        }
        if len(tiers) > 2:
            result["tiers"] = {
                name: intervals for name, intervals in tiers.items()
                if name not in ("words", "phones")
            }
        results.append(result)
    return results


def write_zip(results: List[Dict], output_dir: Path, zip_path: Path,
              log: LogFn) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for result in results:
            stem = Path(result["audio_file"]).stem
            json_name = f"{stem}.json"
            zf.writestr(json_name, json.dumps(result, ensure_ascii=False, indent=2))
    log(f"Wrote {zip_path}")
    return zip_path


def run_pipeline(pairs: List[Pair], acoustic: str, dictionary: str,
                 output_dir: Path, chunk_limit_bytes: int,
                 zip_name: Optional[str] = None,
                 auto_download: bool = True,
                 keep_temp: bool = False,
                 log: LogFn = _null_log,
                 progress: ProgressFn = _null_progress) -> Path:
    """Full pipeline; returns the produced zip path."""
    ffmpeg = audio_mod.find_ffmpeg()
    work_dir = Path(tempfile.mkdtemp(prefix="auto_mfa_"))
    try:
        ensure_models(acoustic, dictionary, auto_download, log)
        jobs = prepare_corpus(
            pairs, work_dir, chunk_limit_bytes, log, progress, ffmpeg
        )
        if not jobs:
            raise RuntimeError("No valid audio/text pairs to align.")
        corpus_dir = work_dir / "corpus"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        alignment_dir = work_dir / "alignment"
        temp_dir = work_dir / "mfa_temp"
        progress(0.0, "aligning")
        run_alignment(corpus_dir, alignment_dir, temp_dir, dictionary, acoustic, log)
        progress(0.9, "building JSON")
        results = postprocess(alignment_dir, pairs, jobs, log)
        if not results:
            raise RuntimeError("Alignment produced no output.")
        zip_path = output_dir / (zip_name or default_zip_name(output_dir))
        write_zip(results, output_dir, zip_path, log)
        progress(1.0, "done")
        return zip_path
    finally:
        if not keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


def default_zip_name(output_dir: Path) -> str:
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"alignments_{output_dir.name or 'output'}_{stamp}.zip"
