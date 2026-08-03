"""Plan how to split long audio + transcript pairs into processable chunks.

MFA can crash on very large audio files (Kaldi/torch handle each file as a
whole, and Windows-side tools choke above ~2 GB).  We therefore chunk any
pair whose prepared 16 kHz mono WAV exceeds the configured limit (default
2 GB), splitting the transcript proportionally so each chunk still aligns to
its own text.
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

DEFAULT_CHUNK_LIMIT_BYTES = 2 * 1024 ** 3  # 2 GiB
BYTES_PER_SECOND_16K_MONO = 16000 * 2  # 16 kHz, 16-bit mono PCM


@dataclass
class ChunkPlan:
    num_chunks: int
    chunk_duration: float  # seconds, may be fractional

    @property
    def needs_chunking(self) -> bool:
        return self.num_chunks > 1


def plan_chunks(total_duration: float, total_bytes: int, limit_bytes: int) -> ChunkPlan:
    """Compute how many chunks are needed to keep each <= limit_bytes.

    *total_duration* is the duration in seconds of the 16 kHz mono WAV whose
    size is *total_bytes*.
    """
    if total_duration <= 0 or total_bytes <= 0:
        return ChunkPlan(num_chunks=1, chunk_duration=total_duration)
    if total_bytes <= limit_bytes:
        return ChunkPlan(num_chunks=1, chunk_duration=total_duration)
    num_chunks = math.ceil(total_bytes / limit_bytes)
    chunk_duration = total_duration / num_chunks
    return ChunkPlan(num_chunks=num_chunks, chunk_duration=chunk_duration)


def partition_words(words: List[str], num_chunks: int) -> List[List[str]]:
    """Split *words* into *num_chunks* roughly equal, non-empty parts."""
    if num_chunks <= 0:
        return [words]
    if num_chunks == 1:
        return [list(words)]
    if len(words) <= num_chunks:
        # Not enough words for one per chunk: distribute round-robin and
        # merge overflow so every part has at least one word.
        parts: List[List[str]] = [[] for _ in range(num_chunks)]
        for i, w in enumerate(words):
            parts[i % num_chunks].append(w)
        # Collapse empty trailing parts into the last non-empty part.
        merged: List[List[str]] = []
        for part in parts:
            if part:
                if merged and merged[-1] and len(merged) < num_chunks:
                    # keep simple: each part that has words stays separate
                    pass
                merged.append(part)
            else:
                if merged:
                    merged[-1].extend(part)
        return merged
    base = len(words) // num_chunks
    remainder = len(words) % num_chunks
    parts = []
    idx = 0
    for i in range(num_chunks):
        size = base + (1 if i < remainder else 0)
        parts.append(words[idx:idx + size])
        idx += size
    return parts


def partition_words_by_weights(words: List[str], weights: List[float]) -> List[List[str]]:
    """Split *words* across len(weights) parts, sized proportionally to *weights*.

    Used to divide a chapter's transcript across silence-snapped audio
    segments of unequal length (segment.plan_segments): a segment that is
    twice as long as another gets roughly twice as many words. This is an
    approximation -- it assumes a fairly steady narration pace -- but it only
    has to get each *segment's own* transcript slice close enough for MFA to
    align within that segment; a boundary that lands a word or two off just
    means that word aligns at the very edge of its segment instead of the
    next one, and the next real silence-snapped cut resets any drift.

    Word boundaries are chosen via cumulative rounding (each boundary is
    round(cumulative_weight_fraction * len(words))) so every word is used
    exactly once and rounding error cannot accumulate into the last part.
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [list(words)]
    total_weight = sum(weights)
    if total_weight <= 0:
        return partition_words(words, n)
    total = len(words)
    boundaries = [0]
    cum = 0.0
    for w in weights[:-1]:
        cum += max(0.0, w)
        boundaries.append(round(cum / total_weight * total))
    boundaries.append(total)  # force exact end regardless of float rounding
    # A weight of 0 (or heavy rounding) can produce a non-increasing boundary;
    # clamp so no part gets a negative-length slice.
    for i in range(1, len(boundaries)):
        if boundaries[i] < boundaries[i - 1]:
            boundaries[i] = boundaries[i - 1]
    return [words[boundaries[i]:boundaries[i + 1]] for i in range(n)]
