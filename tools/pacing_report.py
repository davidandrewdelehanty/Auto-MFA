"""Point at the places in a finished run where the timings can't be true.

Coverage does not measure correctness. Брежнев's 1977 speech reported
"Matched 388/388 (100%)" while the highlighting ran a minute ahead of the
voice for the last third: every word had found a home, they were simply the
wrong homes. Nothing in the alignment can report that, because the
alignment is what is wrong.

Physics can. A word takes time to say. So a stretch of words whose timings
imply a speaking rate nobody reaches did not happen that way, whatever the
aligner reported -- and a long stretch of audio with no words on it is the
same fault seen from the other side. This lists both, with the clock
position in that chapter's OWN audio file, so each one can be seeked to and
settled by ear.

    python3 tools/pacing_report.py --json /mnt/c/.../_runs/<slug>/json

WHAT THE NUMBERS MEAN
A single narrator reads at about one word a second (Брежнев averages 1.05,
an audiobook 1.5-2). Actors trading lines in a radio play average nearer
two. Above about 3 is fast but possible in a quick exchange; above 5 nobody
is speaking, and that is a defect. So --fast defaults to 5: the point is to
list what cannot be true, not to argue about what is merely brisk.

Adjacent bad blocks are merged, because one bad seam produces a run of them
and twenty lines describing one place is worse than one line describing it.
"""

import argparse
import json
import re
import statistics
from pathlib import Path

FAST = 5.0          # words a second; above this nobody is speaking
BLOCK = 10          # words per block measured
SILENT = 25.0       # seconds of audio with no words before it is worth a look
CONTEXT = 12        # words printed per finding


def clock(t):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def chapter_files(json_dir):
    def n(p):
        m = re.search(r"-ch(\d+)\.json$", p.name) or re.search(r"(\d+)\.json$", p.name)
        return int(m.group(1)) if m else 0
    return sorted(json_dir.glob("*.json"), key=n)


def report(json_dir, fast, silent):
    files = chapter_files(json_dir)
    if not files:
        raise SystemExit(f"No chapter JSONs in {json_dir}")
    grand = []
    for idx, path in enumerate(files, start=1):
        doc = json.loads(path.read_text(encoding="utf-8"))
        wt = doc.get("word_timings") or []
        if len(wt) < BLOCK + 1:
            print(f"=== {idx}. {path.name}: too few timed words to judge\n")
            continue

        blocks = []
        for k in range(0, len(wt) - BLOCK + 1):
            a = float(wt[k]["begin"])
            b = float(wt[k + BLOCK - 1]["end"])
            if b > a:
                blocks.append((k, a, b, BLOCK / (b - a)))
        rates = sorted(r for _, _, _, r in blocks)
        grand += rates
        n = len(rates)
        clip = Path(doc.get("audio_url", "")).name
        print(f"=== {idx}. {path.name}  ->  {clip}")
        print(f"    {len(wt)} timed words | median {statistics.median(rates):.2f} w/s"
              f" | p99 {rates[int(n * 0.99)]:.2f} | max {rates[-1]:.2f}"
              f" | {100.0 * sum(1 for r in rates if r > fast) / n:.1f}% impossible")

        # Merge overlapping bad blocks into one region each.
        regions = []
        for k, a, b, r in blocks:
            if r <= fast:
                continue
            if regions and k <= regions[-1][1] + BLOCK:
                regions[-1][1] = k + BLOCK - 1
                regions[-1][2] = max(regions[-1][2], r)
            else:
                regions.append([k, k + BLOCK - 1, r])
        if regions:
            print(f"    {len(regions)} place(s) faster than {fast:.0f} w/s:")
        for lo, hi, r in sorted(regions, key=lambda x: -x[2])[:12]:
            a = float(wt[lo]["begin"])
            b = float(wt[hi]["end"])
            words = " ".join(w["word"] for w in wt[lo:hi + 1])
            print(f"      {clock(a)} -> {clock(b)}  {hi - lo + 1} words in "
                  f"{b - a:.1f}s ({r:.1f} w/s peak)")
            print(f"         {words[:160]}")

        holes = [(float(x["end"]), float(y["begin"]), i)
                 for i, (x, y) in enumerate(zip(wt, wt[1:]))
                 if float(y["begin"]) - float(x["end"]) >= silent]
        if holes:
            print(f"    {len(holes)} silent stretch(es) of {silent:.0f}s+ "
                  f"(music, a pause, or words that lost their timing):")
        for s, e, i in sorted(holes, key=lambda h: h[0] - h[1])[:6]:
            after = " ".join(w["word"] for w in wt[max(0, i - CONTEXT):i + 1])
            print(f"      {clock(s)} -> {clock(e)}  ({e - s:.0f}s)")
            print(f"         last words before it: ...{after[-140:]}")
        print()

    grand.sort()
    print(f"WHOLE RUN: median {statistics.median(grand):.2f} w/s, "
          f"p99 {grand[int(len(grand) * 0.99)]:.2f}, "
          f"{100.0 * sum(1 for r in grand if r > fast) / len(grand):.1f}% of "
          f"blocks faster than {fast:.0f} w/s")
    print("\nEvery time above is into that chapter's own audio file, so it can "
          "be seeked to directly in the reader.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", required=True,
                    help="the run's json folder")
    ap.add_argument("--fast", type=float, default=FAST,
                    help=f"words a second above which nobody is speaking "
                         f"(default {FAST:.0f})")
    ap.add_argument("--silent", type=float, default=SILENT,
                    help=f"seconds of wordless audio worth listing "
                         f"(default {SILENT:.0f})")
    a = ap.parse_args()
    return report(Path(a.json), a.fast, a.silent)


if __name__ == "__main__":
    raise SystemExit(main())
