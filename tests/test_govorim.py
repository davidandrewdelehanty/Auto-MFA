"""Tests for Govorim-format output and WSL script generation.

The expected schema here was verified against a REAL file in the govorim-app
repo (public/books/audio/chekhov-dama-ch1.json), not inferred: top-level
keys are audio_url / narrator / fragments / word_timings; fragments carry
text / begin / end / words; word entries carry word / begin / end. Note
`begin` (not `start`) and `word` (not `text`) -- both differ from what the
rest of this app uses internally, which is the whole point of govorim.py.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import govorim
from app.pipeline import CorpusJob, Pair, run_pipeline, write_govorim
from app.scriptgen import (build_script, run_command_for, shell_quote,
                           slugify, to_wsl_path)


def aligned(*pairs):
    """Build a pipeline-shaped aligned word list: (text, start, end)."""
    return [{"text": t, "start": s, "end": e} for t, s, e in pairs]


class AttachTimingsTest(unittest.TestCase):
    def test_restores_original_capitalization_and_punctuation(self):
        text = "Говорили, что на набережной."
        words = govorim.attach_timings(text, aligned(
            ("говорили", 0.291, 1.493),
            ("что", 1.553, 1.713),
            ("на", 1.753, 1.833),
            ("набережной", 1.873, 2.500),
        ))
        self.assertEqual([w["word"] for w in words],
                         ["Говорили,", "что", "на", "набережной."])
        self.assertEqual(words[0], {"word": "Говорили,", "begin": 0.291, "end": 1.493})

    def test_hyphenated_token_spans_both_aligned_pieces(self):
        # transcript_words splits on the hyphen, so MFA saw two words --
        # but Govorim should show ONE highlightable token covering both.
        words = govorim.attach_timings("какого-то веселого", aligned(
            ("какого", 1.0, 1.4),
            ("то", 1.4, 1.6),
            ("веселого", 1.7, 2.3),
        ))
        self.assertEqual([w["word"] for w in words], ["какого-то", "веселого"])
        self.assertEqual(words[0]["begin"], 1.0)
        self.assertEqual(words[0]["end"], 1.6)

    def test_tokens_with_no_alignment_are_skipped(self):
        # A standalone number and a bare dash never reach MFA (transcript_words
        # strips both), so there is no honest timing for them.
        words = govorim.attach_timings("Глава 1 — начало", aligned(
            ("глава", 0.0, 0.5),
            ("начало", 0.6, 1.2),
        ))
        self.assertEqual([w["word"] for w in words], ["Глава", "начало"])

    def test_resyncs_when_aligner_dropped_a_word(self):
        # MFA failed to align "на" entirely. Without a resync every later
        # word would land on the wrong token and the whole file would be
        # silently skewed.
        words = govorim.attach_timings("Говорили что на набережной появилось", aligned(
            ("говорили", 0.0, 1.0),
            ("что", 1.0, 1.2),
            # "на" missing
            ("набережной", 1.5, 2.0),
            ("появилось", 2.0, 2.6),
        ))
        by_word = {w["word"]: w for w in words}
        self.assertEqual(by_word["набережной"]["begin"], 1.5)
        self.assertEqual(by_word["появилось"]["begin"], 2.0)

    def test_extra_tokens_beyond_alignment_are_dropped_not_crashed(self):
        words = govorim.attach_timings("один два три четыре", aligned(
            ("один", 0.0, 0.5),
            ("два", 0.5, 1.0),
        ))
        self.assertEqual([w["word"] for w in words], ["один", "два"])


class BuildChapterTest(unittest.TestCase):
    def test_matches_the_real_govorim_schema(self):
        text = "Первое предложение. Второе предложение!"
        doc = govorim.build_chapter(
            text,
            aligned(("первое", 0.0, 0.5), ("предложение", 0.5, 1.2),
                    ("второе", 1.5, 2.0), ("предложение", 2.0, 2.7)),
            audio_url="https://example.test/book/01.mp3",
        )
        self.assertEqual(sorted(doc.keys()),
                         ["audio_url", "fragments", "narrator", "word_timings"])
        self.assertEqual(doc["narrator"], "audiobook")
        self.assertEqual(doc["audio_url"], "https://example.test/book/01.mp3")
        self.assertEqual(sorted(doc["fragments"][0].keys()),
                         ["begin", "end", "text", "words"])
        self.assertEqual(sorted(doc["word_timings"][0].keys()),
                         ["begin", "end", "word"])
        # No `start` key anywhere -- Govorim reads `begin`.
        blob = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn('"start"', blob)
        self.assertNotIn('"phones"', blob)

    def test_splits_into_sentence_fragments_with_correct_spans(self):
        text = "Первое предложение. Второе предложение!"
        doc = govorim.build_chapter(
            text,
            aligned(("первое", 0.0, 0.5), ("предложение", 0.5, 1.2),
                    ("второе", 1.5, 2.0), ("предложение", 2.0, 2.7)),
            audio_url="x.mp3",
        )
        self.assertEqual(len(doc["fragments"]), 2)
        first, second = doc["fragments"]
        self.assertEqual(first["text"], "Первое предложение.")
        self.assertEqual((first["begin"], first["end"]), (0.0, 1.2))
        self.assertEqual(second["text"], "Второе предложение!")
        self.assertEqual((second["begin"], second["end"]), (1.5, 2.7))
        # Every word appears exactly once across fragments, and the flat
        # word_timings list is the same sequence.
        flat = [w for f in doc["fragments"] for w in f["words"]]
        self.assertEqual(flat, doc["word_timings"])

    def test_handles_russian_sentence_terminators(self):
        text = "Он сказал: «Да…» А потом ушёл."
        doc = govorim.build_chapter(
            text,
            aligned(("он", 0.0, 0.2), ("сказал", 0.2, 0.7), ("да", 0.8, 1.0),
                    ("а", 1.2, 1.3), ("потом", 1.3, 1.7), ("ушёл", 1.7, 2.0)),
            audio_url="x.mp3",
        )
        self.assertGreaterEqual(len(doc["fragments"]), 2)
        self.assertEqual(len(doc["word_timings"]), 6)


class AudioUrlTest(unittest.TestCase):
    def test_builds_r2_url_from_folder(self):
        self.assertEqual(
            govorim.audio_url_for("01.mp3", "marble-head"),
            f"{govorim.DEFAULT_R2_BASE}/marble-head/01.mp3",
        )

    def test_blank_folder_leaves_bare_filename(self):
        self.assertEqual(govorim.audio_url_for("01.mp3", ""), "01.mp3")

    def test_tolerates_slashes_around_folder(self):
        self.assertEqual(
            govorim.audio_url_for("01.mp3", "/dama/"),
            f"{govorim.DEFAULT_R2_BASE}/dama/01.mp3",
        )

    def test_chapter_filename_is_zero_padded(self):
        self.assertEqual(govorim.chapter_filename("chekhov-dama", 1),
                         "chekhov-dama-ch01.json")
        self.assertEqual(govorim.chapter_filename("chekhov-dama", 12),
                         "chekhov-dama-ch12.json")


class WriteGovorimTest(unittest.TestCase):
    def test_writes_one_numbered_file_per_result(self):
        results = [
            {"audio_file": "01.mp3", "words": aligned(("привет", 0.0, 0.5)),
             "_source_text": "Привет."},
            {"audio_file": "02.mp3", "words": aligned(("пока", 0.0, 0.4)),
             "_source_text": "Пока."},
        ]
        with tempfile.TemporaryDirectory() as d:
            written = write_govorim(results, Path(d), "test-book", "tb",
                                    log=lambda m: None)
            self.assertEqual([p.name for p in written],
                             ["test-book-ch01.json", "test-book-ch02.json"])
            doc = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(doc["audio_url"],
                             f"{govorim.DEFAULT_R2_BASE}/tb/01.mp3")
            self.assertEqual(doc["word_timings"][0]["word"], "Привет.")

    def test_internal_underscore_keys_never_reach_the_file(self):
        results = [{"audio_file": "01.mp3", "words": aligned(("привет", 0.0, 0.5)),
                    "_source_text": "Привет.", "_audio_path": "/tmp/x.mp3"}]
        with tempfile.TemporaryDirectory() as d:
            written = write_govorim(results, Path(d), "b", "", log=lambda m: None)
            blob = written[0].read_text(encoding="utf-8")
            self.assertNotIn("_source_text", blob)
            self.assertNotIn("_audio_path", blob)


class PipelineGovorimModeTest(unittest.TestCase):
    """run_pipeline should emit loose Govorim JSONs (not a zip) when a slug
    is set, and the text carried through must be the ORIGINAL punctuated
    chapter text -- not the normalized transcript MFA aligned against."""

    def test_emits_govorim_files_instead_of_zip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work = root / "work"
            work.mkdir()
            out = root / "out"
            jobs = [CorpusJob(0, 0, str(work / "000_a_000.wav"),
                              str(work / "000_a_000.txt"), duration=2.0)]
            (work / "000_a_000.txt").write_text("привет мир", encoding="utf-8")

            def fake_run_alignment(corpus_dir, alignment_dir, temp_dir,
                                   dictionary, acoustic, log, num_jobs=2,
                                   disable_mp=False):
                Path(alignment_dir).mkdir(parents=True, exist_ok=True)
                tg = Path(alignment_dir) / "000_a_000.TextGrid"
                tg.write_text(
                    'File type = "ooTextFile"\n'
                    'Object class = "TextGrid"\n\n'
                    'xmin = 0\nxmax = 2\ntiers? <exists>\nsize = 1\nitem []:\n'
                    '    item [1]:\n'
                    '        class = "IntervalTier"\n'
                    '        name = "words"\n'
                    '        xmin = 0\n        xmax = 2\n        intervals: size = 2\n'
                    '        intervals [1]:\n'
                    '            xmin = 0\n            xmax = 0.5\n            text = "привет"\n'
                    '        intervals [2]:\n'
                    '            xmin = 0.5\n            xmax = 1.0\n            text = "мир"\n',
                    encoding="utf-8")

            with mock.patch("app.pipeline.ensure_models"), \
                 mock.patch("app.pipeline.prepare_corpus", return_value=jobs), \
                 mock.patch("app.pipeline.run_alignment", side_effect=fake_run_alignment):
                result = run_pipeline(
                    pairs=[Pair(work / "a.mp3", "Глава 1", "Привет, мир!")],
                    acoustic="russian_mfa", dictionary="russian_mfa",
                    output_dir=out, num_jobs=1,
                    govorim_slug="my-book", r2_folder="mb",
                    log=lambda m: None,
                )

            self.assertEqual(result, out)
            produced = sorted(p.name for p in out.glob("*.json"))
            self.assertEqual(produced, ["my-book-ch01.json"])
            doc = json.loads((out / "my-book-ch01.json").read_text(encoding="utf-8"))
            # Original punctuation/case preserved, NOT the normalized form.
            self.assertEqual([w["word"] for w in doc["word_timings"]],
                             ["Привет,", "мир!"])
            self.assertEqual(doc["audio_url"],
                             f"{govorim.DEFAULT_R2_BASE}/mb/a.mp3")
            self.assertFalse(list(out.glob("*.zip")))


class WslPathTest(unittest.TestCase):
    def test_translates_drive_letter_paths(self):
        self.assertEqual(to_wsl_path(r"C:\Users\dave\books"),
                         "/mnt/c/Users/dave/books")
        self.assertEqual(to_wsl_path(r"D:\Audio\Book 1"),
                         "/mnt/d/Audio/Book 1")

    def test_lowercases_only_the_drive_letter(self):
        self.assertEqual(to_wsl_path(r"C:\Users\David\Projects"),
                         "/mnt/c/Users/David/Projects")

    def test_passes_through_posix_paths(self):
        self.assertEqual(to_wsl_path("/home/dave/books"), "/home/dave/books")

    def test_handles_cyrillic_folder_names(self):
        self.assertEqual(to_wsl_path(r"C:\Users\dave\Мраморная головка"),
                         "/mnt/c/Users/dave/Мраморная головка")


class SlugifyTest(unittest.TestCase):
    def test_basic_slugs(self):
        self.assertEqual(slugify("Marble Head"), "marble-head")
        self.assertEqual(slugify("chekhov_dama!!"), "chekhov-dama")

    def test_cyrillic_falls_back_rather_than_transliterating(self):
        # The slug names every output file, so a guessed transliteration
        # would be worse than an obvious placeholder to replace.
        self.assertEqual(slugify("Мраморная головка"), "book")

    def test_empty_input(self):
        self.assertEqual(slugify(""), "book")


class BuildScriptTest(unittest.TestCase):
    def test_script_is_runnable_bash(self):
        script = build_script("my-book", "/mnt/c/books/align_my-book.job.json",
                              "/mnt/c/projects/Auto-MFA", "1.5.0")
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", script)
        # Must activate the env, not just call its python by path -- MFA
        # looks up fstcompile and friends on PATH.
        self.assertIn("conda activate", script)
        self.assertIn("--worker align", script)
        self.assertIn("PYTHONUTF8=1", script)

    def test_paths_are_quoted(self):
        script = build_script("b", "/mnt/c/a b/j.json", "/mnt/c/p q", "1.0")
        self.assertIn("JOB='/mnt/c/a b/j.json'", script)
        self.assertIn("PROJECT='/mnt/c/p q'", script)

    def test_single_quotes_in_paths_cannot_break_out(self):
        script = build_script("b", "/mnt/c/it's/j.json", "/mnt/c/p", "1.0")
        self.assertIn(r"'/mnt/c/it'\''s/j.json'", script)

    def test_shell_quote_escapes_embedded_quote(self):
        self.assertEqual(shell_quote("it's"), r"'it'\''s'")

    def test_fails_loudly_if_env_missing(self):
        script = build_script("b", "/j.json", "/p", "1.0")
        self.assertIn('if [ ! -x "$ENV_DIR/bin/python" ]; then', script)
        self.assertIn("setup_wsl.sh", script)


class RunCommandTest(unittest.TestCase):
    """Book folders routinely have spaces in them ("marble head"). An
    unquoted path silently splits into two arguments and bash reports
    'No such file or directory' for the fragment before the space."""

    def test_quotes_paths_with_spaces(self):
        self.assertEqual(
            run_command_for("/mnt/c/Users/david/Downloads/marble head/align_mh.sh"),
            "bash '/mnt/c/Users/david/Downloads/marble head/align_mh.sh'",
        )

    def test_quotes_even_without_spaces(self):
        self.assertEqual(run_command_for("/mnt/c/books/a.sh"), "bash '/mnt/c/books/a.sh'")

    def test_script_header_shows_a_quoted_command(self):
        script = build_script("mh", "/mnt/c/marble head/align_mh.job.json",
                              "/mnt/c/p", "2.0.1")
        self.assertIn("bash '/mnt/c/marble head/align_mh.sh'", script)


if __name__ == "__main__":
    unittest.main()
