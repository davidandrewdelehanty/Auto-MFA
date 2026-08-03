"""Silence-aware utterance segmentation.

WHY THIS EXISTS
---------------
MFA's peak memory is set by the length of the LONGEST SINGLE UTTERANCE it is
asked to align, not by how many files are in the corpus or how large any one
file is on disk. A ~15-minute audiobook chapter handed to MFA as one utterance
builds an enormous alignment lattice and reliably gets OOM-killed on an
ordinary machine.

The previous chunking in this project (chunking.py's plan_chunks) only split
audio that exceeded a *file-size* ceiling (2 GB, the Windows practical file
limit) -- a 15-minute 16 kHz mono WAV is about 29 MB, so that limit was never
hit in practice and every chapter was fed to MFA whole. That is the exact bug
that was already tracked down and fixed, the hard way, in a sibling project's
audiobook-alignment pipeline; see that project's docs/mfa_playbook.md if it is
available to you -- "the one thing to know" there is this same fact.

This module splits every chapter into short (~30s) utterances *before* MFA
ever sees it, regardless of file size. Unlike a pipeline that is re-aligning
audio that already has approximate word timings (and can therefore cut in the
gaps between known words), Auto-MFA is doing a first-time alignment from a
bare FB2 + audio pair with no timing information at all. So instead of cutting
at existing fragment boundaries, this cuts at *detected silence* in the audio
(via ffmpeg's `silencedetect` filter), snapping each target boundary to the
nearest actual pause so a cut never lands mid-word. If a stretch of audio has
no detected pause within the maximum utterance length (a long unbroken
monologue), a hard time-based cut is used as a fallback -- worse for accuracy
at that one seam, but still far better than a 15-minute utterance.

The transcript is split across segments in proportion to each segment's share
of the chapter's total duration (see chunking.partition_words_by_weights),
which approximates the true word boundary well when narration pace is fairly
steady and self-corrects at the next real cut.
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_NOISE_DB = -30.0
DEFAULT_MIN_SILENCE = 0.3   # seconds; shorter pauses are not treated as cut points
DEFAULT_TARGET = 30.0       # aim for utterances about this long
DEFAULT_MAX = 45.0          # hard cap; forces a cut even with no silence nearby
DEFAULT_MIN_LAST = 3.0      # a trailing remainder shorter than this merges into
                             # the previous segment instead of standing alone

_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_silences(wav_path: str, ffmpeg: str, duration: float,
                    noise_db: float = DEFAULT_NOISE_DB,
                    min_silence: float = DEFAULT_MIN_SILENCE) -> List[Tuple[float, float]]:
    """Return [(start, end), ...] of silent stretches in *wav_path*.

    Uses ffmpeg's silencedetect filter, which writes its findings to stderr as
    the file streams past (no seeking, so this is a single fast decode pass).
    A silence that is still open at end-of-file (no matching silence_end line)
    is closed at *duration*.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", wav_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    silences: List[Tuple[float, float]] = []
    pending_start: Optional[float] = None
    # ffmpeg interleaves silence_start/silence_end lines with other log lines,
    # in chronological order, on stderr.
    for line in proc.stderr.splitlines():
        m = _SIL_START_RE.search(line)
        if m:
            pending_start = float(m.group(1))
            continue
        m = _SIL_END_RE.search(line)
        if m and pending_start is not None:
            silences.append((pending_start, float(m.group(1))))
            pending_start = None
    if pending_start is not None:
        silences.append((pending_start, duration))
    return silences


def plan_segments(duration: float, silences: List[Tuple[float, float]],
                  target: float = DEFAULT_TARGET, max_len: float = DEFAULT_MAX,
                  min_last: float = DEFAULT_MIN_LAST) -> List[Segment]:
    """Plan contiguous [0, duration] cut points, snapped to silence gaps.

    Greedy walk: from the current segment start, look for a detected silence
    whose midpoint falls in (start + target/2, start + max_len]. Prefer the
    one closest to start + target. If none exists in that window, force a
    hard cut at start + max_len so no utterance ever exceeds it.
    """
    if duration <= 0:
        return []
    if duration <= max_len:
        return [Segment(0.0, duration)]

    # Sort once; silences from detect_silences are already chronological, but
    # don't assume it.
    sils = sorted(silences)

    boundaries: List[float] = [0.0]
    pos = 0.0
    while pos < duration:
        desired = pos + target
        hard_cap = min(pos + max_len, duration)
        if hard_cap >= duration:
            break  # this is the last segment; close it at `duration` below
        earliest = pos + target / 2.0
        best_mid = None
        best_dist = None
        for s, e in sils:
            mid = (s + e) / 2.0
            if mid <= pos or mid > hard_cap:
                continue
            if mid < earliest:
                continue
            dist = abs(mid - desired)
            if best_dist is None or dist < best_dist:
                best_dist, best_mid = dist, mid
        cut = best_mid if best_mid is not None else hard_cap
        # Guard against float/silence weirdness producing a non-advancing cut.
        if cut <= pos + 0.01:
            cut = hard_cap
        boundaries.append(cut)
        pos = cut
    boundaries.append(duration)

    # Drop a degenerate trailing sliver by merging it into the previous cut.
    if len(boundaries) >= 3 and (boundaries[-1] - boundaries[-2]) < min_last:
        del boundaries[-2]

    return [Segment(a, b) for a, b in zip(boundaries, boundaries[1:]) if b > a]


def cut_segment(ffmpeg: str, src_wav: str, dst_wav: str, start: float, dur: float) -> None:
    """Extract [start, start+dur) from *src_wav* into *dst_wav* (16-bit PCM)."""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-i", src_wav, "-t", f"{dur:.6f}",
        "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1",
        dst_wav,
    ]
    subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def cut_segment_mp3(ffmpeg: str, src_wav: str, dst_mp3: str, start: float, dur: float,
                    bitrate: str = "96k") -> None:
    """Like cut_segment, but encodes to mp3.

    Used for final per-chapter audio deliverables (see pipeline.postprocess's
    multi-chapter split, where one audio file spanning several chapters gets
    physically cut back into one clip per chapter): a compact, shippable file
    matters more there than raw PCM.
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-i", src_wav, "-t", f"{dur:.6f}",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        dst_mp3,
    ]
    subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
