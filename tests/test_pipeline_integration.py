"""Integration test for prepare -> postprocess -> zip with a fake MFA output.

Requires ffmpeg on PATH.  Verifies that oversized audio is chunked, transcripts
are split proportionally, TextGrids are recombined with correct time offsets,
and the JSONs are zipped.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import audio as audio_mod
from app.chunking import DEFAULT_CHUNK_LIMIT_BYTES
from app.pipeline import Pair, postprocess, prepare_corpus, write_zip

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

            # Force chunking: limit of 64 KB => 128 KB wav -> 2 chunks.
            jobs = prepare_corpus(
                [pair], work, chunk_limit_bytes=64 * 1024, log=print,
                progress=lambda *_: None)
            self.assertGreaterEqual(len(jobs), 2)
            stems = [Path(j.wav).stem for j in jobs]
            self.assertEqual(stems, sorted(stems))

            # Fake MFA output: each chunk has the same words but shifted so we
            # can verify recombination offsets.
            align = work / "alignment"
            align.mkdir()
            for i, job in enumerate(jobs):
                (align / f"{Path(job.wav).stem}.TextGrid").write_text(
                    fake_textgrid(words), encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
