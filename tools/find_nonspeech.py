"""Find stretches of a recording that nobody is speaking through.

Applause, an ovation, a musical sting, an announcer over music: audio with
no counterpart in the text. MFA cannot decline to align there -- given an
utterance whose audio is applause and whose transcript is the next
sentence, it puts the words on the applause anyway, crushed into whatever
fraction of a second is left. On Брежнев's 1977 speech that produced 37
words in 11 seconds, from a man who averages 1.1 a second.

    python3 tools/find_nonspeech.py --audio "/mnt/c/.../01.mp3"

Prints ranges ready to paste into make_book_run.py's --skip.

HOW IT TELLS THEM APART
Applause is broadband noise: many zero crossings per sample, and a steady
level. Voiced speech has a pitch, so far fewer crossings, and a level that
moves with the syllables. The absolute numbers vary with the recording, so
the file's OWN speech is the reference -- the median of its loud windows --
and a span is called non-speech when it runs well above that for long
enough to matter. Measured on the 1977 speech: speech 0.025, applause
0.044, a clean 1.8x separation.

Features come from ffmpeg's astats rather than being computed here: a
58-hour book is 1.7 billion samples, which is fine in C and hopeless in
Python.
"""

import argparse
import re
import statistics
import subprocess
import sys
from pathlib import Path

WINDOW = 0.5        # seconds per measurement
ZCR_RATIO = 1.45    # times the file's own speech baseline
MIN_LENGTH = 2.5    # seconds; shorter than this is a pause, not an event
JOIN_GAP = 1.5      # merge spans separated by less than this


def measure(audio: Path, ffmpeg: str = "ffmpeg"):
    """[(time, rms_dB, zero_crossing_rate), ...] per WINDOW."""
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "8000",
         "-af", f"asetnsamples=n={int(8000 * WINDOW)},"
                f"astats=metadata=1:reset=1,ametadata=print:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True)
    out = []
    cur = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        m = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)", line)
        if m:
            if "t" in cur and "r" in cur and "z" in cur:
                out.append((cur["t"], cur["r"], cur["z"]))
            cur = {"t": float(m.group(1))}
            continue
        m = re.match(r"lavfi\.astats\.1\.RMS_level=(-?[\d.]+|-inf)", line)
        if m:
            cur["r"] = -99.0 if m.group(1) == "-inf" else float(m.group(1))
            continue
        m = re.match(r"lavfi\.astats\.1\.Zero_crossings_rate=([\d.]+)", line)
        if m:
            cur["z"] = float(m.group(1))
    if "t" in cur and "r" in cur and "z" in cur:
        out.append((cur["t"], cur["r"], cur["z"]))
    return out


def find(frames, zcr_ratio=ZCR_RATIO, min_length=MIN_LENGTH):
    loud = [f for f in frames if f[1] > -40]
    if len(loud) < 20:
        return [], None, None
    base_z = statistics.median(f[2] for f in loud)
    base_r = statistics.median(f[1] for f in loud)
    hits = [i for i, f in enumerate(frames)
            if f[2] > base_z * zcr_ratio and f[1] > base_r - 3]

    spans, cur = [], None
    for i in hits:
        if cur and (frames[i][0] - frames[cur[1]][0]) <= JOIN_GAP:
            cur[1] = i
        else:
            if cur:
                spans.append(cur)
            cur = [i, i]
    if cur:
        spans.append(cur)

    out = []
    for a, b in spans:
        lo, hi = frames[a][0], frames[min(b + 1, len(frames) - 1)][0]
        if hi - lo >= min_length:
            out.append((lo, hi))
    return out, base_z, base_r


def clock(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ratio", type=float, default=ZCR_RATIO)
    ap.add_argument("--min-length", type=float, default=MIN_LENGTH)
    a = ap.parse_args()

    frames = measure(Path(a.audio), a.ffmpeg)
    if not frames:
        print("Could not measure that file.", file=sys.stderr)
        return 1
    spans, base_z, base_r = find(frames, a.ratio, a.min_length)
    print(f"{len(frames)} windows of {WINDOW}s | this file's speech: "
          f"zcr={base_z:.4f}, {base_r:.0f} dB")
    if not spans:
        print("Nothing that looks like applause or music.")
        return 0
    total = sum(b - a_ for a_, b in spans)
    print(f"\n{len(spans)} non-speech span(s), {total:.0f}s in total:")
    for lo, hi in spans:
        print(f"   {clock(lo)} -> {clock(hi)}   ({hi - lo:.1f}s)")
    print("\nPass to make_book_run.py as:")
    print("   --skip " + ",".join(f"{lo:.1f}-{hi:.1f}" for lo, hi in spans))
    print("\nCheck a couple by ear first -- a loud crowd and a loud passage "
          "of speech are not that far apart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
