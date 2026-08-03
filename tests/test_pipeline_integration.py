"""Integration tests for prepare -> postprocess -> zip with a fake MFA output.

test_chunked_combine_and_zip requires ffmpeg on PATH: it drives the real
silence-aware segmentation (app.segment) through prepare_corpus on real
(silent) audio, verifies transcripts are split proportionally across
segments, and that TextGrids are recombined with correct time offsets before
being zipped.

test_postprocess_offset_uses_planned_duration_not_trimmed_textgrid does not
need ffmpeg (it drives postprocess() directly against hand-built CorpusJobs
and TextGrids) and specifically guards against a regression: MFA's own
aligned output can trim trailing silence off the end of a segment, so the
per-segment offset used to place the NEXT segment must come from the
*planned* segment duration (job.duration, known exactly -- it's what we told
ffmpeg to cut) and not be inferred from the TextGrid's own last interval end.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.pipeline import CorpusJob, Pair, postprocess, prepare_corpus, write_zip

BYTES_PER_SEC = 32000  # 16 kHz 16-bit mono WAV


def make_tone_wav(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=%f" % seconds,
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
    )


def fake_textgrid(words, start=0.0, step=0.5) -> str:
    lines = ['File type = "ooTextFile"', 'object class = "TextGrid"',
             "xmin = 0", "xmax = %f" % (start + len(words) * step + step),
             "tiers? <exists>", "size = 2",
             "item []", "    item [1]:", '        class = "IntervalTier"',
             '        name = "words"', "        xmin = 0",
             "        xmax = %f" % (len(words) * step + step),
             "        intervals: size = %d" % (len(words) + 1)]
    t = start
    lines.append(f"        intervals [1]:")
    lines.append(f"            xmin = {t:.6f}")
    lines.append(f"            xmax = {t + step:.6f}")
    lines.append('            text = ""')
    for i, w in enumerate(words, start=1):
        t += step
        lines.append(f"        intervals [{i + 1}]:")
        lines.append(f"            xmin = {t:.6f}")
        lines.append(f"            xmax = {t + step:.6f}")
        lines.append(f'            text = "{w}"')
    # minimal phones tier
    lines += ["item []", "    item [2]:", '        class = "IntervalTier"',
              '        name = "phones"', "        xmin = 0",
              "        xmax = 1", "        intervals: size = 1",
              "        intervals [1]:", "            xmin = 0",
              "            xmax = 1", '            text = ""']
    return "\n".join(lines) + "\n"


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class PipelineIntegrationTest(unittest.TestCase):
    def test_chunked_combine_and_zip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            audio = root / "01.wav"
            make_tone_wav(audio, 4.0)

            work = root / "work"
            work.mkdir()
            words = ["один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь"]
            pair = Pair(audio=audio, title="Глава 1", text=" ".join(words))

            # A pure tone has no detected silence, so plan_segments falls back
            # to hard time-based cuts. Force multiple segments out of a 4s
            # clip with a tiny target/max.
            jobs = prepare_corpus(
                [pair], work, log=print, progress=lambda *_: None,
                target_seconds=1.0, max_seconds=1.5)
            self.assertGreaterEqual(len(jobs), 2)
            stems = [Path(j.wav).stem for j in jobs]
            self.assertEqual(stems, sorted(stems))
            # Planned durations must account for the whole 4s clip.
            self.assertAlmostEqual(sum(j.duration for j in jobs), 4.0, places=3)

            # Fake MFA output: each chunk has the same words but shifted so we
            # can verify recombination offsets.
            align = work / "alignment"
            align.mkdir()
            # A real TextGrid can never claim words beyond the physical
            # length of the audio segment it was aligned from; use a small
            # step so the fake words comfortably fit inside even the
            # shortest planned segment (avoids a spurious overlap that has
            # nothing to do with what this test is actually checking).
            step = min(j.duration for j in jobs) / (len(words) + 2)
            for job in jobs:
                (align / f"{Path(job.wav).stem}.TextGrid").write_text(
                    fake_textgrid(words, step=step), encoding="utf-8")

            results = postprocess(align, [pair], jobs, log=print)
            self.assertEqual(len(results), 1)
            merged = results[0]["words"]
            self.assertEqual(len([w for w in merged if w["text"]]), len(jobs) * len(words))
            # Word N of chunk 1 must start later than the end of chunk 0.
            chunk0_end = max(w["end"] for w in merged[:len(words)])
            chunk1_start = merged[len(words)]["start"]
            self.assertGreater(chunk1_start, chunk0_end)

            out_dir = root / "out"
            out_dir.mkdir()
            zip_path = out_dir / "test.zip"
            write_zip(results, out_dir, zip_path, log=print)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertEqual(names, ["01.json"])
                data = json.loads(zf.read("01.json").decode("utf-8"))
                self.assertEqual(data["audio_file"], "01.wav")
                self.assertEqual(len(data["words"]), len(jobs) * len(words))


class PostprocessOffsetTest(unittest.TestCase):
    """No ffmpeg needed: drives postprocess() directly against hand-built
    CorpusJobs/TextGrids, so it always runs regardless of environment."""

    def test_offset_uses_planned_duration_not_trimmed_textgrid(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            align = root / "alignment"
            align.mkdir()
            pair = Pair(audio=root / "01.wav", title="Глава 1", text="")

            # Three planned segments of DIFFERENT lengths. Each one's fake
            # TextGrid deliberately ends well before the segment's true
            # duration, simulating MFA trimming trailing silence off its
            # aligned output.
            planned_durations = [12.0, 9.5, 15.25]
            trim = 2.0  # seconds trimmed off the end of every fake TextGrid
            words_per_job = [["один", "два"], ["три"], ["четыре", "пять", "шесть"]]

            jobs = []
            for i, (dur, words) in enumerate(zip(planned_durations, words_per_job)):
                wav = root / f"000_book_{i:03d}.wav"
                jobs.append(CorpusJob(pair_index=0, chunk_index=i,
                                       wav=str(wav), txt=str(wav.with_suffix(".txt")),
                                       duration=dur))
                # Words packed near the start of the segment; the TextGrid's
                # own xmax (last interval end) is `dur - trim`, i.e. shorter
                # than the real, planned segment duration.
                step = (dur - trim) / (len(words) + 1)
                (align / f"{Path(wav).stem}.TextGrid").write_text(
                    fake_textgrid(words, start=0.0, step=step), encoding="utf-8")

            results = postprocess(align, [pair], jobs, log=print)
            self.assertEqual(len(results), 1)
            words = [w for w in results[0]["words"] if w["text"]]
            self.assertEqual(len(words), sum(len(w) for w in words_per_job))

            # The offset for segment i must be the cumulative sum of the
            # PLANNED durations, not the (trimmed, shorter) TextGrid extents.
            # Segment 0 starts at offset 0; its first word starts at
            # step (the fake TextGrid's own within-segment start).
            step0 = (planned_durations[0] - trim) / (len(words_per_job[0]) + 1)
            self.assertAlmostEqual(words[0]["start"], step0, places=3)

            # Segment 1's first word must be offset by exactly the FULL
            # planned duration of segment 0 (12.0), not by segment 0's
            # trimmed TextGrid extent (12.0 - 2.0 = 10.0). This is the crux
            # of the regression: under the old (buggy) inferred-offset logic,
            # this would land ~2s too early.
            expected_offset_1 = planned_durations[0]
            step1 = (planned_durations[1] - trim) / (len(words_per_job[1]) + 1)
            seg1_first_word = words[len(words_per_job[0])]
            self.assertAlmostEqual(seg1_first_word["start"], expected_offset_1 + step1, places=3)

            # Segment 2's first word must be offset by the sum of BOTH prior
            # planned durations (12.0 + 9.5 = 21.5), not their trimmed sum
            # (10.0 + 7.5 = 17.5).
            expected_offset_2 = planned_durations[0] + planned_durations[1]
            step2 = (planned_durations[2] - trim) / (len(words_per_job[2]) + 1)
            seg2_first_word = words[len(words_per_job[0]) + len(words_per_job[1])]
            self.assertAlmostEqual(seg2_first_word["start"], expected_offset_2 + step2, places=3)

            # Total reported duration is the sum of planned durations too.
            self.assertAlmostEqual(results[0]["duration"], sum(planned_durations), places=3)


if __name__ == "__main__":
    unittest.main()
