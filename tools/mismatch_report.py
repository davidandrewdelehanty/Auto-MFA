"""Show exactly where a book's text and its recording disagree.

Written for the radio productions, whose FB2s are reconstructions: a
WhisperX transcript of the broadcast, with the lines the production cut
removed and the actors' ad-libs written back in. That process leaves two
kinds of error, and an alignment run can find both -- it just doesn't
normally say where.

    python3 tools/mismatch_report.py \\
        --fb2 "/mnt/c/.../chekhov-chaika-radio-govorim.fb2" \\
        --json /mnt/c/.../\\_runs/chaika-radio/json

Every stretch of text with no timings sits between two words that DO have
them, so the clock says whether the narrator was speaking through it:

CUT FROM THE RECORDING -- the words are in the FB2 and the gap between the
surrounding timings is far too short for anyone to have read them. The
production dropped those lines, or they were invented. Fix: delete from the
FB2.

LOST BY THE ALIGNER -- the same, except the gap is about as long as reading
them would take. The actors do say it; MFA simply failed on those
utterances. Nothing to fix in the text, and editing it would make things
worse.

MISSING FROM THE TEXT -- a gap between two aligned words with no unmatched
text to account for it. Something is being said that the FB2 does not
contain: an ad-lib that was missed, or one written down differently from
how it is spoken. The timestamp is into that chapter's own audio file, so
it can be listened to directly. Fix: transcribe it in.

A gap can also be perfectly innocent -- applause, music, a pause held for
effect -- so this lists places to check, not defects.
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import fb2 as fb2_mod  # noqa: E402
from app.govorim import surface_tokens  # noqa: E402

MIN_TEXT_RUN = 4        # words; shorter runs are ordinary single-word misses
MIN_AUDIO_GAP = 5.0     # seconds; below this it is just a pause between lines
CONTEXT = 9             # words of context either side


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def chapter_files(json_dir: Path):
    """The run's chapter JSONs, in chapter order."""
    def n(p):
        m = re.search(r"-ch(\d+)\.json$", p.name) or re.search(r"(\d+)\.json$", p.name)
        return int(m.group(1)) if m else 0
    return sorted(json_dir.glob("*.json"), key=n)


def report(fb2_path: Path, json_dir: Path, min_run: int, min_gap: float) -> int:
    chapters = fb2_mod.extract_chapters(fb2_path)
    files = chapter_files(json_dir)
    if len(files) != len(chapters):
        print(f"note: {len(chapters)} chapter(s) in the FB2 but {len(files)} "
              f"JSON file(s); reporting the {min(len(files), len(chapters))} "
              f"that pair up.\n")

    findings = 0
    for idx, (chapter, path) in enumerate(zip(chapters, files), start=1):
        doc = json.loads(path.read_text(encoding="utf-8"))
        timed = doc.get("word_timings") or []
        tokens = surface_tokens(chapter["text"])

        # Which token each timing belongs to. Matched with difflib rather
        # than by walking both lists in step, because the obvious walk gets
        # this wrong exactly where it matters: Войницкий says "я обманут"
        # twice within one speech, so a walk that takes the first equal word
        # it sees pairs the timings of the SECOND with the position of the
        # first. That reports a 0.0s gap across 95 words the actors plainly
        # do say -- i.e. it tells you to delete real Chekhov.
        pairing = difflib.SequenceMatcher(
            None, tokens, [e["word"] for e in timed], autojunk=False)
        marks = [None] * len(tokens)
        for i, j, size in pairing.get_matching_blocks():
            for k in range(size):
                marks[i + k] = timed[j + k]

        counted = [t for t in tokens if fb2_mod.transcript_words(t)]
        pct = 100.0 * len(timed) / len(counted) if counted else 0.0
        header = (f"=== chapter {idx}: {chapter['title'][:48]} -- "
                  f"{len(timed)}/{len(counted)} words timed ({pct:.0f}%)")
        printed_header = False

        def head():
            nonlocal printed_header
            if not printed_header:
                print(header)
                printed_header = True

        # How fast this chapter is actually read, measured from the words
        # that did align -- used to judge whether the narrator could have
        # spoken an unmatched stretch in the time available.
        rate = 0.0
        if len(timed) > 1:
            span = float(timed[-1]["end"]) - float(timed[0]["begin"])
            if span > 0:
                rate = len(timed) / span

        # Runs of text with no timing, and the clock either side of each.
        runs = []
        run_start = None
        for i in range(len(marks) + 1):
            mark = marks[i] if i < len(marks) else object()
            if mark is None:
                if run_start is None:
                    run_start = i
            elif run_start is not None:
                words = [t for t in tokens[run_start:i]
                         if fb2_mod.transcript_words(t)]
                if len(words) >= min_run:
                    prev = next((m for m in reversed(marks[:run_start]) if m), None)
                    nxt = next((m for m in marks[i:] if m), None)
                    runs.append((run_start, i, len(words), prev, nxt))
                run_start = None

        # Every silent stretch in the chapter, for the cross-check below.
        gaps = [(float(a["end"]), float(b["begin"]))
                for a, b in zip(timed, timed[1:])
                if float(b["begin"]) - float(a["end"]) >= 1.0]

        explained = []      # (start, end) clock intervals a text run accounts for
        for lo, hi, n_words, prev, nxt in runs:
            head()
            findings += 1
            gap = (float(nxt["begin"]) - float(prev["end"])) if (prev and nxt) else None
            expected = n_words / rate if rate else None
            where = f" at {clock(prev['end'])}" if prev else ""

            # The words either side of a hole don't always carry trustworthy
            # times. Войницкий says "обманут" three times in one speech, and
            # the aligner gave the third occurrence a timestamp from the
            # first -- putting the word AFTER the hole 59 seconds BEFORE the
            # audio that fills it. Judging on those two timings alone said
            # "0.0s of audio", i.e. "delete it", for a speech plainly being
            # performed. So also look for silence near the hole big enough
            # to hold it: if the recording has the time, the text stays.
            nearby = 0.0
            if prev is not None and expected:
                window_lo = float(prev["end"]) - 2.0
                window_hi = float(prev["end"]) + 2.0 * expected + 10.0
                for g_start, g_end in gaps:
                    if g_start >= window_lo and g_start <= window_hi:
                        length = g_end - g_start
                        if length > nearby:
                            nearby = length
                            covered = (g_start, g_end)
                if nearby >= 0.5 * expected:
                    explained.append(covered)
                    gap = max(gap or 0.0, nearby)

            if gap is not None and expected and gap < 0.35 * expected:
                verdict = (f"CUT FROM THE RECORDING -- {n_words} words{where}; "
                           f"only {gap:.1f}s of audio for something that takes "
                           f"~{expected:.0f}s to read")
            elif gap is not None and expected:
                verdict = (f"LOST BY THE ALIGNER -- {n_words} words{where}; "
                           f"{gap:.1f}s of audio for ~{expected:.0f}s of reading, "
                           f"so it IS spoken -- leave the text alone")
            else:
                verdict = f"TEXT WITH NO AUDIO -- {n_words} words{where}"
            print(f"  {verdict}")
            print(f"     before: ...{' '.join(tokens[max(0, lo - CONTEXT):lo])}")
            print(f"     ---->   {' '.join(tokens[lo:hi])[:400]}")
            print(f"     after:  {' '.join(tokens[hi:hi + CONTEXT])}...")
            if prev and nxt:
                explained.append((float(prev["end"]), float(nxt["begin"])))

        # Gaps in the audio that no unmatched text accounts for.
        for a, b in zip(timed, timed[1:]):
            start_t, end_t = float(a["end"]), float(b["begin"])
            gap = end_t - start_t
            if gap < min_gap:
                continue
            if any(lo - 0.01 <= start_t and end_t <= hi + 0.01
                   for lo, hi in explained):
                continue        # already reported as unmatched text
            head()
            findings += 1
            ai = next((i for i, m in enumerate(marks) if m is a), None)
            before = " ".join(tokens[max(0, ai - CONTEXT):ai + 1]) if ai is not None else a["word"]
            after = " ".join(tokens[ai + 1:ai + 1 + CONTEXT]) if ai is not None else b["word"]
            print(f"  MISSING FROM THE TEXT -- {gap:.1f}s at "
                  f"{clock(start_t)} -> {clock(end_t)}")
            print(f"     after saying: ...{before}")
            print(f"     before:       {after}...")
        if printed_header:
            print()

    print(f"{findings} place(s) worth checking "
          f"(runs of {min_run}+ words, gaps of {min_gap:.0f}s+).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fb2", required=True)
    ap.add_argument("--json", required=True,
                    help="folder of chapter JSONs from the run")
    ap.add_argument("--min-run", type=int, default=MIN_TEXT_RUN)
    ap.add_argument("--min-gap", type=float, default=MIN_AUDIO_GAP)
    a = ap.parse_args()
    return report(Path(a.fb2), Path(a.json), a.min_run, a.min_gap)


if __name__ == "__main__":
    raise SystemExit(main())
