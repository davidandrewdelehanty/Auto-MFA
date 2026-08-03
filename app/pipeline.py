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
from . import segment as segment_mod
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
    duration: float  # ground truth: exactly what we told ffmpeg to cut, in
                      # seconds. Used to place this job's alignment at the
                      # right offset in postprocess() -- see the comment there
                      # for why this must NOT be inferred from MFA's own
                      # output instead.


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
    # G2P model: lets alignment cover words missing from the base dictionary
    # (character names, foreign phrases, anything the dictionary's author
    # didn't include) by generating a pronunciation for them on the fly,
    # instead of silently failing to align those words at all. Best-effort:
    # alignment still works without it, just worse on OOV-heavy text.
    if not model_present("g2p", dictionary):
        try:
            download_model("g2p", dictionary, log)
        except Exception as exc:  # noqa: BLE001
            log(f"Note: could not fetch optional g2p model ({exc}). "
                f"Alignment will proceed, but words missing from the "
                f"dictionary (character names, etc.) will not align.")


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


def prepare_corpus(pairs: List[Pair], work_dir: Path, log: LogFn,
                   progress: ProgressFn, ffmpeg: Optional[str] = None,
                   target_seconds: float = segment_mod.DEFAULT_TARGET,
                   max_seconds: float = segment_mod.DEFAULT_MAX) -> List[CorpusJob]:
    """Convert audio to 16 kHz WAV and split every chapter into short,
    silence-snapped utterances (see segment.py for why this always happens,
    not just for oversized files).

    Returns the list of corpus jobs (one per utterance / per pair).
    """
    ffmpeg = ffmpeg or audio_mod.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg was not found. Install it and put ffmpeg.exe on PATH, or "
            "re-run the build script so it is bundled next to the app."
        )

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

        duration = audio_mod.probe_duration(str(raw_wav))
        if duration <= 0:
            log(f"  Warning: '{pair.audio.name}' has no measurable duration; skipping.")
            progress((idx + 1) / total, "skipped unreadable audio")
            continue

        silences = segment_mod.detect_silences(str(raw_wav), ffmpeg, duration)
        segs = segment_mod.plan_segments(duration, silences, target_seconds, max_seconds)
        if len(segs) <= 1:
            log(f"  {duration:.1f}s, one utterance (under the {max_seconds:.0f}s cap).")
        else:
            snapped = sum(1 for s, e in silences
                          for seg in segs if abs(seg.end - (s + e) / 2.0) < 0.02)
            log(f"  {duration:.1f}s -> {len(segs)} utterances "
                f"(~{target_seconds:.0f}s target, {snapped} silence-snapped cuts).")

        weights = [s.duration for s in segs]
        parts = chunking.partition_words_by_weights(words, weights)

        for c_i, (seg, part_words) in enumerate(zip(segs, parts)):
            seg_stem = f"{stem}_{c_i:03d}"
            seg_wav = corpus_dir / f"{seg_stem}.wav"
            segment_mod.cut_segment(ffmpeg, str(raw_wav), str(seg_wav), seg.start, seg.duration)
            txt = corpus_dir / f"{seg_stem}.txt"
            txt.write_text(words_to_text(part_words), encoding="utf-8")
            jobs.append(CorpusJob(idx, c_i, str(seg_wav), str(txt), seg.duration))
        progress((idx + 1) / total, f"prepared {pair.audio.name}")
    return jobs


def run_alignment(corpus_dir: Path, output_dir: Path, temp_dir: Path,
                  dictionary: str, acoustic: str, log: LogFn,
                  num_jobs: int = 2) -> None:
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
        "--single_speaker",   # one narrator; also disables speaker adaptation
        "--num_jobs", str(max(1, num_jobs)),
    ]
    if model_present("g2p", dictionary):
        # Cover words missing from the base dictionary (names, foreign
        # phrases, ...) instead of leaving them unaligned. See ensure_models.
        args += ["--g2p_model_path", dictionary]
    else:
        log("Note: no g2p model available for this dictionary; words "
            "missing from it will not align.")
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
    """Combine per-segment TextGrids back into one JSON dict per audio pair."""
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
            shifted = _shift(parsed["tiers"], offset)
            for name, intervals in shifted.items():
                tiers.setdefault(name, []).extend(intervals)
            # Use the PLANNED segment duration -- what we told ffmpeg to cut,
            # known exactly -- as the offset increment for the next segment.
            # Deliberately NOT inferred from MFA's own TextGrid (e.g. the last
            # interval's end time): MFA regularly trims trailing silence off
            # the end of its aligned output, which is shorter than the
            # segment's true audio duration. Offsetting from that trimmed
            # figure would make every later segment in the chapter start a
            # little early, and with dozens of segments per chapter that
            # drift compounds into audibly wrong highlighting well before the
            # end of a long chapter.
            offset += job.duration
            duration += job.duration
            log(f"  Combined '{Path(job.wav).stem}' ({job.duration:.2f}s)")

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
                 output_dir: Path,
                 zip_name: Optional[str] = None,
                 auto_download: bool = True,
                 keep_temp: bool = False,
                 num_jobs: int = 2,
                 target_seconds: float = segment_mod.DEFAULT_TARGET,
                 max_seconds: float = segment_mod.DEFAULT_MAX,
                 log: LogFn = _null_log,
                 progress: ProgressFn = _null_progress) -> Path:
    """Full pipeline; returns the produced zip path."""
    ffmpeg = audio_mod.find_ffmpeg()
    work_dir = Path(tempfile.mkdtemp(prefix="auto_mfa_"))
    try:
        ensure_models(acoustic, dictionary, auto_download, log)
        jobs = prepare_corpus(
            pairs, work_dir, log, progress, ffmpeg,
            target_seconds=target_seconds, max_seconds=max_seconds,
        )
        if not jobs:
            raise RuntimeError("No valid audio/text pairs to align.")
        corpus_dir = work_dir / "corpus"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        alignment_dir = work_dir / "alignment"
        temp_dir = work_dir / "mfa_temp"
        progress(0.0, "aligning")
        run_alignment(corpus_dir, alignment_dir, temp_dir, dictionary, acoustic,
                     log, num_jobs=num_jobs)
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
