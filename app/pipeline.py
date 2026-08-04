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
from typing import Callable, Dict, List, Optional, Tuple

from . import audio as audio_mod
from . import chunking
from . import govorim
from . import segment as segment_mod
from .fb2 import transcript_words, words_to_text
from .textgrid import parse_textgrid

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]

# Beam widths used on the FIRST alignment pass, not just as a retry.
#
# MFA's defaults (beam 10 / retry_beam 40) assume a transcript that matches
# its audio closely. This app cuts a chapter at detected silences and then
# splits the text across those pieces *proportionally by duration* -- a
# principled guess, but a guess: a segment's transcript can start or end a
# few words off from what is actually spoken in it. That is exactly the
# condition a narrow beam rejects, and a rejected utterance produces no
# output file while MFA still reports success.
#
# Measured on a 29-segment chapter: default beam aligned 5/29; beam 100 /
# retry_beam 400 aligned 28/29. The wider beam costs roughly 4x on the
# alignment step alone, but a failed pass costs the whole run, so starting
# wide is cheaper in wall-clock as well as far more reliable.
DEFAULT_BEAM = 100
DEFAULT_RETRY_BEAM = 400

# Share of segments that must align for a run to be considered usable.
# MFA leaves a small number of utterances unaligned as a matter of course
# (beam failures where audio and transcript diverge locally) -- chasing
# those costs a full re-run for files that will fail again, and a gap in
# one 30s window is far less bad than no output at all. Anything well
# below this is not a beam problem but a corpus problem, and should be
# reported as such instead of quietly producing a mangled book.
MIN_ALIGNED_RATIO = 0.90

# Below this, stop and say so rather than shipping the result. A run this
# empty is not a text that merely disagrees with the recording in places --
# it is the wrong audio paired to the wrong chapter, a corrupt file, or a
# missing pronunciation dictionary, and the few "timings" it produces would
# be noise. Between this and MIN_ALIGNED_RATIO the run continues and warns:
# a narrator who reads a title card, an edition whose footnotes are absent
# from the recording, or a chapter the reader abridges all leave stretches
# with no alignment, and partial timings beat none.
ABORT_ALIGNED_RATIO = 0.10


@dataclass
class Pair:
    audio: Path
    title: str
    text: str
    # Set when one audio file spans several consecutive original chapters at
    # once (e.g. a single "whole book" or "whole volume" recording) instead
    # of the usual one-file-per-chapter case. `text` above is still the full
    # concatenated transcript for the whole `audio` file -- prepare_corpus
    # doesn't need to know anything changed. Each entry is (title, word_count)
    # for one constituent chapter, in the same order they were concatenated
    # into `text`; postprocess() uses the word counts to split the alignment
    # back into one result (and one cut audio clip) per original chapter. None
    # for the ordinary one-file-per-chapter case.
    sub_chapters: Optional[List[Tuple[str, int]]] = None
    # Parallel to sub_chapters: each constituent chapter's own ORIGINAL
    # (un-normalized) text. Only needed for the Govorim output format, which
    # reproduces real book text with its punctuation and so cannot work from
    # the normalized transcript. Optional -- everything else in the pipeline
    # works fine without it.
    sub_texts: Optional[List[str]] = None


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
    """Check whether an MFA pretrained model is already downloaded.

    MFA stores different model types under different filenames: acoustic
    and g2p models are `<name>.zip`, but dictionaries are a plain-text
    `<name>.dict` -- NOT a zip. Missing that extension meant this always
    reported dictionaries as absent (even once downloaded), so every run
    re-triggered a "Downloading dictionary model..." step that actually
    just asked MFA to re-check something already on disk.
    """
    base = mfa_data_dir() / "pretrained_models"
    if not base.is_dir():
        return False
    for candidate in (base / model_type / f"{name}.zip",
                      base / model_type / f"{name}.dict",
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


def _pair_stem(idx: int, pair: Pair) -> str:
    return f"{idx:03d}_{audio_mod.safe_stem(pair.audio.name)}"


def _corpus_dir(work_dir: Path) -> Path:
    """Where prepare_corpus writes the MFA corpus, and where run_alignment
    points `mfa align` at.

    MUST be named uniquely per run (hence including work_dir.name, itself a
    random tempfile.mkdtemp() suffix) -- MFA keeps its own persistent
    working state (a SQLite corpus.db plus scratch files) under
    ~/Documents/MFA/<corpus folder's NAME>/, keyed by that name alone, not
    by the full path we pass it. A literal "corpus" here (the old behavior)
    made every single run of every book collide on the exact same shared
    MFA-side state: a run interrupted/killed mid-alignment (e.g. by
    build.ps1's Stop-DistProcesses closing a still-running worker before a
    rebuild) could leave that shared database locked or inconsistent, and
    every subsequent run -- of any book -- would then hang or misbehave
    against that same stale state, regardless of using a fresh work_dir.
    """
    return work_dir / f"corpus_{work_dir.name}"


def _raw_wav_path(work_dir: Path, idx: int, pair: Pair) -> Path:
    """Where prepare_corpus put this pair's full-length converted WAV.

    postprocess() needs this (only for pairs with sub_chapters) to cut
    per-chapter audio clips after alignment; work_dir isn't cleaned up until
    after write_zip runs, so the file is still there when this is called.
    """
    return work_dir / "raw" / f"{_pair_stem(idx, pair)}.wav"


def invoke_mfa(args: List[str], log: LogFn) -> int:
    """Invoke the MFA CLI inside the current (worker) process.

    IMPORTANT: this must call ``mfa_cli.main(..., standalone_mode=False)``,
    never bare ``mfa_cli(args)``. click.Group/Command's default
    ``standalone_mode=True`` makes Click call ``sys.exit()`` itself after
    ANY command finishes -- success or failure. ``SystemExit`` is a
    BaseException, not an Exception, so it silently escapes the
    ``except Exception`` handler in worker.py's cmd_align() and kills the
    entire worker process right after the first ``mfa`` subcommand
    completes (e.g. right after a model download), with no error and no
    further pipeline steps ever running. standalone_mode=False makes Click
    return the command's result/exit code to us instead of exiting, so we
    can check it and keep going.
    """
    log("> mfa " + " ".join(args))
    try:
        from montreal_forced_aligner.command_line.mfa import mfa_cli
    except ImportError:
        from montreal_forced_aligner.command_line.mfa import main as mfa_cli
    result = mfa_cli.main(args, prog_name="mfa", standalone_mode=False)
    if isinstance(result, int) and result != 0:
        raise RuntimeError(f"mfa {' '.join(args)} failed (exit code {result})")
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
    corpus_dir = _corpus_dir(work_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    jobs: List[CorpusJob] = []
    total = len(pairs)
    for idx, pair in enumerate(pairs):
        log(f"[{idx + 1}/{total}] Preparing '{pair.audio.name}'")
        stem = _pair_stem(idx, pair)
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


def base_dictionary_path(dictionary: str) -> Optional[Path]:
    """On-disk path of a downloaded pretrained dictionary, if present."""
    candidate = (mfa_data_dir() / "pretrained_models" / "dictionary"
                 / f"{dictionary}.dict")
    return candidate if candidate.is_file() else None


def build_dictionary(corpus_dir: Path, dictionary: str, work_dir: Path,
                     log: LogFn) -> Optional[Path]:
    """Build a book-specific dictionary: the base one plus g2p-generated
    pronunciations for every word in THIS corpus that it doesn't cover.

    This is the single most important step for alignment quality, and
    getting it wrong is what makes MFA quietly fail to align most of a
    book. Russian audiobooks are full of out-of-vocabulary words --
    character names in every grammatical case, French phrases, years --
    and those names *recur constantly*, so leaving them unpronounceable
    doesn't just lose those words: it strips the alignment of its most
    frequent anchors, and whole utterances then fail beam search and
    produce no output at all. MFA reports that as a completely successful
    run with fewer files exported than it was given.

    The obvious-looking alternative, passing ``--g2p_model_path`` to
    ``mfa align`` and letting it do this internally, is what this app used
    to do. It is worse in two ways: MFA has shipped g2p models whose phone
    inventory doesn't match their own paired dictionary (align then refuses
    to run at all with PronunciationG2PMismatchError), and the natural
    fallback -- dropping g2p -- silently lands you in exactly the
    no-pronunciations state described above.

    Returns the merged dictionary's path, or None if it couldn't be built
    (in which case the caller should fall back to the plain base
    dictionary rather than failing the run).
    """
    base = base_dictionary_path(dictionary)
    if base is None:
        log(f"Note: no on-disk copy of dictionary '{dictionary}' to extend; "
            f"using it as-is. Out-of-vocabulary words (character names, "
            f"foreign phrases) may fail to align.")
        return None
    if not model_present("g2p", dictionary):
        log(f"Note: no g2p model for '{dictionary}', so out-of-vocabulary "
            f"words cannot be given pronunciations. Alignment will still "
            f"run, but expect segments containing names to fail.")
        return None

    oov_dict = work_dir / "oov.dict"
    merged = work_dir / "book.dict"
    log("Generating pronunciations for out-of-vocabulary words (names, "
        "foreign phrases)...")
    try:
        invoke_mfa(["g2p", str(corpus_dir), dictionary, str(oov_dict),
                    "--dictionary_path", dictionary], log)
    except Exception as exc:  # noqa: BLE001
        log(f"Warning: could not generate OOV pronunciations ({exc}). "
            f"Falling back to the base dictionary; segments containing "
            f"names may fail to align.")
        return None

    try:
        base_text = base.read_text(encoding="utf-8")
        oov_text = oov_dict.read_text(encoding="utf-8") if oov_dict.is_file() else ""
        if not oov_text.strip():
            log("  No out-of-vocabulary words found; using the base dictionary.")
            return None
        if not base_text.endswith("\n"):
            base_text += "\n"
        merged.write_text(base_text + oov_text, encoding="utf-8")
    except OSError as exc:
        log(f"Warning: could not merge the OOV dictionary ({exc}); "
            f"using the base dictionary.")
        return None

    n_oov = sum(1 for line in oov_text.splitlines() if line.strip())
    log(f"  Added {n_oov} pronunciation(s) for this book -> {merged.name}")
    return merged


def run_alignment(corpus_dir: Path, output_dir: Path, temp_dir: Path,
                  dictionary: str, acoustic: str, log: LogFn,
                  num_jobs: int = 2,
                  dictionary_path: Optional[Path] = None,
                  beam: Optional[int] = None,
                  retry_beam: Optional[int] = None) -> None:
    """Run one ``mfa align`` pass.

    *dictionary_path*, when given, is used INSTEAD of the named
    dictionary -- normally the merged book dictionary from
    build_dictionary(). No ``--g2p_model_path`` is passed: pronunciations
    are baked into that dictionary already (see build_dictionary for why
    that ordering matters).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "align",
        str(corpus_dir),
        str(dictionary_path) if dictionary_path else dictionary,
        acoustic,
        str(output_dir),
        "--clean",
        "--overwrite",
        "--temp_directory", str(temp_dir),
        "--single_speaker",   # one narrator; also disables speaker adaptation
        "--num_jobs", str(max(1, num_jobs)),
    ]
    # Widening the beam is MFA's documented remedy for utterances that fail
    # to align; a failed utterance produces no output file at all, which is
    # why this matters more than it sounds.
    if beam:
        args += ["--beam", str(beam)]
    if retry_beam:
        args += ["--retry_beam", str(retry_beam)]
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


def _split_pair_into_chapters(pair: Pair, pair_idx: int, words: List[Dict],
                              phones: List[Dict], duration: float,
                              work_dir: Optional[Path], ffmpeg: Optional[str],
                              log: LogFn) -> List[Dict]:
    """Split one multi-chapter pair's merged alignment into one result (and,
    if possible, one cut audio clip) per original chapter.

    Boundaries are chosen from the EXACT word counts of each constituent
    chapter (known precisely -- we concatenated the transcript from them
    ourselves), not estimated proportionally the way segment.py divides a
    single chapter's text across silence-snapped utterances. If the aligned
    word count doesn't match what was expected (MFA dropped or merged a
    word somewhere -- rare, but possible for pathological OOV cases), this
    falls back to a proportional split scaled to the actual count instead of
    crashing, and says so in the log.
    """
    sub_chapters = pair.sub_chapters or []
    expected_total = sum(wc for _, wc in sub_chapters)
    n = len(sub_chapters)

    bounds = [0]
    cum = 0
    if expected_total == len(words):
        for _, wc in sub_chapters[:-1]:
            cum += wc
            bounds.append(cum)
    else:
        log(f"  Warning: expected {expected_total} words across {n} chapters "
            f"but alignment produced {len(words)} for '{pair.audio.name}'; "
            f"falling back to a proportional split (chapter boundaries may "
            f"be slightly off).")
        for _, wc in sub_chapters[:-1]:
            cum += wc
            bounds.append(round(cum / expected_total * len(words)) if expected_total else 0)
    bounds.append(len(words))
    for i in range(1, len(bounds)):
        if bounds[i] < bounds[i - 1]:
            bounds[i] = bounds[i - 1]

    # Cut points in time: the midpoint between the last word of one chapter
    # and the first word of the next, so a cut lands in whatever pause is
    # there rather than mid-word. Falls back sensibly if a chapter ended up
    # with zero words (e.g. from a lopsided proportional split above).
    cut_points = [0.0]
    for k in range(1, n):
        w_before = words[bounds[k] - 1] if bounds[k] > 0 else None
        w_after = words[bounds[k]] if bounds[k] < len(words) else None
        if w_before and w_after:
            cut_points.append((w_before["end"] + w_after["start"]) / 2.0)
        elif w_after:
            cut_points.append(w_after["start"])
        elif w_before:
            cut_points.append(w_before["end"])
        else:
            cut_points.append(cut_points[-1])
    cut_points.append(duration)

    raw_wav = None
    if work_dir is not None and ffmpeg:
        candidate = _raw_wav_path(Path(work_dir), pair_idx, pair)
        if candidate.exists():
            raw_wav = candidate
        else:
            log(f"  Warning: source audio for '{pair.audio.name}' not found "
                f"at {candidate}; cannot cut per-chapter audio clips (JSON "
                f"timings will still be produced).")

    base_stem = audio_mod.safe_stem(pair.audio.stem)
    sub_texts = pair.sub_texts or []
    out: List[Dict] = []
    for k, (title, _wc) in enumerate(sub_chapters):
        w_slice = words[bounds[k]:bounds[k + 1]]
        start_t, end_t = cut_points[k], cut_points[k + 1]
        p_slice = [p for p in phones if start_t <= p["start"] < end_t]
        rebased_words = [
            {"start": round(w["start"] - start_t, 6),
             "end": round(w["end"] - start_t, 6), "text": w["text"]}
            for w in w_slice
        ]
        rebased_phones = [
            {"start": round(p["start"] - start_t, 6),
             "end": round(p["end"] - start_t, 6), "text": p["text"]}
            for p in p_slice
        ]
        chapter_audio_name = f"{base_stem}_ch{k + 1:03d}.mp3"
        result: Dict = {
            "audio_file": chapter_audio_name,
            "title": title,
            "duration": round(end_t - start_t, 6),
            "words": rebased_words,
            "phones": rebased_phones,
            # Underscore-prefixed keys are internal: stripped before this
            # dict is ever serialized (see write_zip). Carried so the
            # Govorim writer can reproduce this chapter's real text.
            "_source_text": sub_texts[k] if k < len(sub_texts) else "",
        }
        if raw_wav is not None:
            clip_path = Path(work_dir) / "chapters" / chapter_audio_name
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                segment_mod.cut_segment_mp3(
                    ffmpeg, str(raw_wav), str(clip_path), start_t, end_t - start_t)
                result["_audio_path"] = str(clip_path)
            except Exception as exc:  # noqa: BLE001
                log(f"  Warning: failed to cut chapter audio for '{title}' "
                    f"({exc}); JSON will still be produced.")
        out.append(result)
        log(f"  Split '{pair.audio.name}' chapter {k + 1}/{n} '{title}' "
            f"({end_t - start_t:.2f}s)")
    return out


def postprocess(output_dir: Path, pairs: List[Pair], jobs: List[CorpusJob],
                log: LogFn, work_dir: Optional[Path] = None,
                ffmpeg: Optional[str] = None) -> List[Dict]:
    """Combine per-segment TextGrids back into one JSON dict per audio pair
    (or, for a pair spanning multiple original chapters, one JSON dict --
    and one cut audio clip, if *work_dir*/*ffmpeg* are given -- per chapter).
    """
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
                # A segment MFA could not align produces no file at all.
                # That is a normal, low-rate occurrence (a stretch where
                # audio and transcript diverge), so don't fail the book
                # over it: skip its words but STILL advance the offset by
                # this segment's planned duration, so every later segment
                # in the chapter keeps its correct position. The result is
                # a gap in the timings rather than everything after it
                # drifting early.
                log(f"  Warning: no alignment for '{Path(job.wav).stem}' "
                    f"({job.duration:.1f}s) -- leaving a gap and continuing.")
                offset += job.duration
                duration += job.duration
                continue
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

        if pair.sub_chapters:
            results.extend(_split_pair_into_chapters(
                pair, pair_idx, words, phones, duration, work_dir, ffmpeg, log))
            continue

        result = {
            "audio_file": pair.audio.name,
            "title": pair.title,
            "duration": round(duration, 6),
            "words": words,
            "phones": phones,
            "_source_text": pair.text,   # internal; see write_zip
        }
        if len(tiers) > 2:
            result["tiers"] = {
                name: intervals for name, intervals in tiers.items()
                if name not in ("words", "phones")
            }
        results.append(result)
    return results


def _public_fields(result: Dict) -> Dict:
    """Drop internal, underscore-prefixed bookkeeping keys before writing.

    ``_audio_path`` and ``_source_text`` are carried on result dicts for
    later pipeline stages; neither belongs in a file handed to a user.
    """
    return {k: v for k, v in result.items() if not k.startswith("_")}


def write_zip(results: List[Dict], output_dir: Path, zip_path: Path,
              log: LogFn) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for result in results:
            stem = Path(result["audio_file"]).stem
            json_name = f"{stem}.json"
            # Only set for chapters split out of a multi-chapter pair (see
            # postprocess/_split_pair_into_chapters): those chapters have no
            # pre-existing standalone audio file anywhere else, so the cut
            # clip has to ship in this zip alongside its JSON. An ordinary
            # one-file-per-chapter pair has none of this -- its source audio
            # already exists as the user's own file, unchanged.
            audio_path = result.get("_audio_path")
            payload = _public_fields(result)
            zf.writestr(json_name, json.dumps(payload, ensure_ascii=False, indent=2))
            if audio_path and Path(audio_path).exists():
                zf.write(audio_path, arcname=result["audio_file"])
    log(f"Wrote {zip_path}")
    return zip_path


def write_govorim(results: List[Dict], output_dir: Path, slug: str,
                  r2_folder: str, log: LogFn) -> List[Path]:
    """Write one Govorim-format JSON per result, ready to drop into the
    app's ``public/books/audio/`` folder.

    Files are named ``<slug>-chNN.json``, numbered sequentially in pairing
    order -- matching how that repo already names its alignment files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for i, result in enumerate(results, start=1):
        doc = govorim.build_chapter(
            text=result.get("_source_text", ""),
            aligned=result.get("words", []),
            audio_url=govorim.audio_url_for(result["audio_file"], r2_folder),
            log=log,
        )
        path = Path(output_dir) / govorim.chapter_filename(slug, i)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        written.append(path)
        log(f"  Wrote {path.name} "
            f"({len(doc['fragments'])} fragments, "
            f"{len(doc['word_timings'])} words)")
    log(f"Wrote {len(written)} Govorim JSON file(s) to {output_dir}")
    return written


def export_chapter_audio(results: List[Dict], dest_dir: Path,
                        log: LogFn) -> List[Path]:
    """Copy out the per-chapter MP3 clips cut from a multi-chapter pair.

    Those clips are cut into the run's temp directory, which is deleted as
    soon as the run finishes. write_zip picks them up because it runs while
    the temp dir still exists; the Govorim path writes loose JSON files
    instead, and without this their audio_url would point at chapter audio
    that exists nowhere. A one-file-per-chapter pair has no clip -- its
    audio is the user's own file, already on disk.
    """
    written: List[Path] = []
    for result in results:
        src = result.get("_audio_path")
        if not src or not Path(src).exists():
            continue
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / result["audio_file"]
        shutil.copyfile(src, dest)
        written.append(dest)
    if written:
        log(f"Wrote {len(written)} chapter audio clip(s) to {dest_dir}")
    return written


def _missing_textgrids(alignment_dir: Path, jobs: List[CorpusJob]) -> List[CorpusJob]:
    return [j for j in jobs
            if not (Path(alignment_dir) / f"{Path(j.wav).stem}.TextGrid").exists()]


def _enough_aligned(aligned: int, total: int,
                    ratio: float = MIN_ALIGNED_RATIO) -> bool:
    """Whether *aligned* of *total* segments clears *ratio*.

    The epsilon is load-bearing: written the obvious way, one miss out of
    ten fails a 90% threshold, because 10 * (1 - 0.90) is 0.9999999999999998
    in binary floating point, not 1.0.
    """
    if total <= 0:
        return False
    return aligned >= total * ratio - 1e-9


def run_pipeline(pairs: List[Pair], acoustic: str, dictionary: str,
                 output_dir: Path,
                 zip_name: Optional[str] = None,
                 auto_download: bool = True,
                 keep_temp: bool = False,
                 num_jobs: int = 2,
                 target_seconds: float = segment_mod.DEFAULT_TARGET,
                 max_seconds: float = segment_mod.DEFAULT_MAX,
                 govorim_slug: str = "",
                 r2_folder: str = "",
                 log: LogFn = _null_log,
                 progress: ProgressFn = _null_progress) -> Path:
    """Full pipeline.

    Returns the produced zip path, or -- when *govorim_slug* is set -- the
    output directory holding the loose ``<slug>-chNN.json`` files written
    for the Govorim app instead of a zip.
    """
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
        corpus_dir = _corpus_dir(work_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        alignment_dir = work_dir / "alignment"
        temp_dir = work_dir / "mfa_temp"
        progress(0.0, "aligning")
        book_dict = build_dictionary(corpus_dir, dictionary, work_dir, log)
        run_alignment(corpus_dir, alignment_dir, temp_dir, dictionary, acoustic,
                     log, num_jobs=num_jobs, dictionary_path=book_dict,
                     beam=DEFAULT_BEAM, retry_beam=DEFAULT_RETRY_BEAM)
        # An utterance MFA cannot align produces NO output file, and MFA
        # still reports the run as fully successful -- so "fewer files than
        # expected" is the only signal that anything went wrong. A small
        # number of these is normal (a stretch where audio and transcript
        # diverge). A large number means something systematic: almost
        # always out-of-vocabulary words with no pronunciation, which
        # build_dictionary above exists to prevent.
        #
        # The first pass already runs wide (see DEFAULT_BEAM), so a
        # shortfall here means the leftovers need wider still.
        missing = _missing_textgrids(alignment_dir, jobs)
        tiers = [
            # Wider still, and single-job -- which also covers the
            # memory-pressure case, since an OOM-killed job likewise just
            # leaves output missing.
            {"beam": DEFAULT_BEAM * 2, "retry_beam": DEFAULT_RETRY_BEAM * 2,
             "num_jobs": 1},
        ]
        for tier in tiers:
            if _enough_aligned(len(jobs) - len(missing), len(jobs)):
                break
            log(f"Warning: {len(missing)}/{len(jobs)} segments produced no "
                f"alignment (MFA reports this as a successful run). Retrying "
                f"with --beam {tier['beam']} --retry_beam {tier['retry_beam']} "
                f"--num_jobs {tier['num_jobs']} -- this will take longer.")
            run_alignment(corpus_dir, alignment_dir, temp_dir, dictionary,
                         acoustic, log, num_jobs=tier["num_jobs"],
                         dictionary_path=book_dict, beam=tier["beam"],
                         retry_beam=tier["retry_beam"])
            missing = _missing_textgrids(alignment_dir, jobs)

        aligned = len(jobs) - len(missing)
        ratio = aligned / len(jobs) if jobs else 0.0
        if not _enough_aligned(aligned, len(jobs), ABORT_ALIGNED_RATIO):
            raise RuntimeError(
                f"Only {aligned}/{len(jobs)} segments aligned ({ratio:.0%}) "
                f"even after retrying with a wider beam. At that level there "
                f"is nothing usable to salvage.\n\n"
                f"This is not the ordinary case of a text that disagrees "
                f"with the recording here and there -- that still produces "
                f"timings for the parts that do match. A number this low "
                f"means the wrong audio is paired to the chapter, the file "
                f"is corrupt, or the pronunciation dictionary is missing."
            )
        if missing:
            level = "Warning" if not _enough_aligned(aligned, len(jobs)) else "Note"
            log(f"{level}: {len(missing)}/{len(jobs)} segments could not be "
                f"aligned ({ratio:.1%} aligned). Those stretches get no "
                f"timings and stay unhighlighted in the app; the segments "
                f"around them are unaffected.")
            if level == "Warning":
                log("  A shortfall this size usually means the recording and "
                    "this edition of the text genuinely differ -- a spoken "
                    "title card or dedication, endnotes read aloud (or not), "
                    "an abridgement. Continuing with what did align.")
        progress(0.9, "building JSON")
        results = postprocess(alignment_dir, pairs, jobs, log,
                              work_dir=work_dir, ffmpeg=ffmpeg)
        if not results:
            raise RuntimeError("Alignment produced no output.")
        if govorim_slug:
            write_govorim(results, output_dir, govorim_slug, r2_folder, log)
            # Must happen before the finally: below wipes work_dir.
            export_chapter_audio(results, output_dir / "audio", log)
            progress(1.0, "done")
            return output_dir
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
