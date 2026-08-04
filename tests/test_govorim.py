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

import shutil
import subprocess

from app import govorim
from app.govorim import attach_timings, build_chapter
from app.fb2 import extract_metadata
from app.pipeline import CorpusJob, Pair, run_pipeline, write_govorim
from app.scriptgen import (build_install_script, build_script,
                           build_upload_script, run_command_for, shell_quote,
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

    def test_percent_encodes_cyrillic_and_spaces(self):
        """Russian audiobook files arrive named like "мраморная головка
        аудиокнига.mp3". Raw in a URL that is not valid, and whether it
        resolves depends on the client."""
        url = govorim.audio_url_for("мраморная головка.mp3", "marble-head")
        self.assertNotIn(" ", url)
        self.assertIn("%20", url)
        self.assertTrue(url.startswith(f"{govorim.DEFAULT_R2_BASE}/marble-head/"))

    def test_ascii_names_are_left_readable(self):
        self.assertEqual(govorim.audio_url_for("44.mp3", "anna-karenina"),
                         f"{govorim.DEFAULT_R2_BASE}/anna-karenina/44.mp3")

    def test_chapter_filename_is_zero_padded(self):
        self.assertEqual(govorim.chapter_filename("chekhov-dama", 1),
                         "chekhov-dama-ch001.json")
        self.assertEqual(govorim.chapter_filename("chekhov-dama", 12),
                         "chekhov-dama-ch012.json")

    def test_chapter_filenames_sort_correctly_past_99(self):
        """Two digits would collate "ch100" before "ch99" -- silently
        reordering any book with more than 99 chapters (Anna Karenina has
        239, War and Peace 362)."""
        names = [govorim.chapter_filename("b", i) for i in range(1, 106)]
        self.assertEqual(sorted(names), names)


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
                             ["test-book-ch001.json", "test-book-ch002.json"])
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
                                   dictionary_path=None, beam=None,
                                   retry_beam=None):
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
                 mock.patch("app.pipeline.build_dictionary", return_value=None), \
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
            self.assertEqual(produced, ["my-book-ch001.json"])
            doc = json.loads((out / "my-book-ch001.json").read_text(encoding="utf-8"))
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


class UploadScriptTest(unittest.TestCase):
    def _script(self, **kw):
        params = dict(r2_folder="dama",
                      source_dir_wsl="/mnt/c/books/marble head",
                      list_path_wsl="/mnt/c/books/marble head/upload_dama.files.txt",
                      version="2.1.0")
        params.update(kw)
        return build_upload_script(**params)

    def test_never_embeds_credentials(self):
        """The script sits in a book folder that gets copied around; a
        leaked secret there is far worse than one extra setup step."""
        script = self._script()
        # The real key/secret must not appear, and no assignment of one either.
        self.assertNotIn("cc9d35405aadb9bc061516a40b0ff69b", script)
        self.assertNotIn("05e879c107847e543a4f3363b73b3de4179c8cb9420df96e0cd4ace05ba9ff5b", script)
        self.assertIn("YOUR_ACCESS_KEY_ID", script)
        self.assertIn("YOUR_SECRET_ACCESS_KEY", script)

    def test_uploads_only_the_listed_files(self):
        # A book folder also holds the FB2, generated scripts and output
        # JSONs -- none of which belong in the audio bucket.
        script = self._script()
        self.assertIn("--files-from", script)
        self.assertIn("rclone copy", script)

    def test_targets_the_same_folder_used_for_audio_url(self):
        script = self._script(r2_folder="marble-head")
        self.assertIn("FOLDER='marble-head'", script)
        self.assertIn("govorim-audio", script)

    def test_quotes_paths_with_spaces(self):
        script = self._script()
        self.assertIn("SRC='/mnt/c/books/marble head'", script)
        self.assertIn("LIST='/mnt/c/books/marble head/upload_dama.files.txt'",
                      script)

    def test_fails_loudly_when_rclone_or_remote_missing(self):
        script = self._script()
        self.assertIn("command -v rclone", script)
        self.assertIn("rclone listremotes", script)
        self.assertIn("rclone config create", script)

    def test_remote_name_is_overridable(self):
        script = self._script()
        self.assertIn("AUTO_MFA_R2_REMOTE", script)

    def test_disables_every_s3_feature_r2_lacks(self):
        """R2 returns 501 NotImplemented for S3 features it doesn't have,
        and which ones get sent depends on the saved remote config -- so
        force them off per-run instead of trusting the config."""
        script = self._script()
        for flag in ("--s3-provider Cloudflare", "--s3-acl", "--s3-no-check-bucket",
                     "--s3-disable-checksum"):
            self.assertIn(flag, script)

    def test_avoids_the_multipart_api_entirely(self):
        # Multipart is a common source of R2 501s and buys nothing for
        # audio files; a cutoff above any single file keeps every upload
        # a plain PutObject.
        script = self._script()
        self.assertIn("--s3-upload-cutoff 200M", script)
        self.assertIn("--s3-chunk-size 200M", script)

    def test_verifies_against_the_bucket_not_the_exit_status(self):
        """Older rclone reports 501 for uploads to R2 that actually
        succeeded, so 'did it work' has to be answered by listing the
        bucket, not by whether rclone returned non-zero."""
        script = self._script()
        self.assertIn("present_count", script)
        self.assertIn("rclone lsf", script)
        self.assertIn("set +e", script)          # a failing pass must not abort
        self.assertIn("for pass in", script)     # ...and must be retried

    def test_retry_loop_cannot_spin_forever(self):
        script = self._script()
        self.assertIn("No progress this pass", script)


FB2_WITH_META = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
 <description>
  <title-info>
   <author><first-name>Антон</first-name><last-name>Чехов</last-name></author>
   <book-title>Дама с собачкой</book-title>
  </title-info>
 </description>
 <body><section><title><p>Глава 1</p></title><p>Текст.</p></section></body>
</FictionBook>
"""


class Fb2MetadataTest(unittest.TestCase):
    """Title/author prefill the catalogue entry, so the book shows up in
    the app as "Дама с собачкой" rather than a slug-derived guess."""

    def _write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "b.fb2"
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_title_and_author(self):
        meta = extract_metadata(self._write(FB2_WITH_META))
        self.assertEqual(meta["title"], "Дама с собачкой")
        self.assertEqual(meta["author"], "Антон Чехов")

    def test_missing_metadata_is_not_an_error(self):
        meta = extract_metadata(self._write(
            '<?xml version="1.0"?><FictionBook><body><section>'
            '<p>x</p></section></body></FictionBook>'))
        self.assertEqual(meta, {"title": "", "author": ""})

    def test_unparseable_file_is_not_an_error(self):
        self.assertEqual(extract_metadata(self._write("not xml at all")),
                         {"title": "", "author": ""})


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"),
                     "needs bash and python3 to execute the generated script")
class InstallScriptTest(unittest.TestCase):
    """Runs the generated installer for real against a synthetic Govorim
    checkout. The install step has to get three things right at once (FB2
    location, chapter naming, catalogue entry) and a mistake in any of
    them produces a book that silently doesn't play -- so assert on the
    resulting files, not on the script text.
    """

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.repo = self.root / "repo"
        self.books = self.repo / "public" / "books"
        self.books.mkdir(parents=True)
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "book.fb2").write_text(FB2_WITH_META, encoding="utf-8")
        # A book already in the catalogue, which must survive untouched.
        self.index = self.books / "index.json"
        self.index.write_text(json.dumps([{
            "filename": "novel/other.fb2", "title": "Другая",
            "category": "Works",
            "audiobook": {"narrator": "audiobook", "source": "x",
                          "chapters": ["audio/other/001.json"]},
        }], ensure_ascii=False), encoding="utf-8")

    def _chapters(self, n):
        for i in range(1, n + 1):
            (self.src / govorim.chapter_filename("mh", i)).write_text(
                json.dumps({"audio_url": f"u{i}", "narrator": "audiobook",
                            "fragments": [], "word_timings": []}),
                encoding="utf-8")

    def _install(self, **kw):
        params = dict(slug="mh", repo_dir_wsl=str(self.repo),
                      fb2_src_wsl=str(self.src / "book.fb2"),
                      json_src_dir_wsl=str(self.src),
                      title="Дама с собачкой", author="Антон Чехов",
                      version="test")
        params.update(kw)
        script = self.root / "install.sh"
        script.write_text(build_install_script(**params), encoding="utf-8",
                          newline="\n")
        proc = subprocess.run(["bash", str(script)], capture_output=True,
                              text=True)
        return proc

    def _entry(self):
        data = json.loads(self.index.read_text(encoding="utf-8"))
        return next(e for e in data if e["filename"] == "novel/mh.fb2")

    def test_installs_fb2_chapters_and_catalogue_entry(self):
        self._chapters(3)
        proc = self._install()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue((self.books / "novel" / "mh.fb2").is_file())
        names = sorted(p.name for p in (self.books / "audio" / "mh").glob("*.json"))
        self.assertEqual(names, ["001.json", "002.json", "003.json"])

        entry = self._entry()
        self.assertEqual(entry["title"], "Дама с собачкой")
        self.assertEqual(entry["author"], "Антон Чехов")
        self.assertEqual(entry["category"], "Works")
        self.assertEqual(entry["audiobook"]["chapters"],
                         ["audio/mh/001.json", "audio/mh/002.json",
                          "audio/mh/003.json"])

    def test_leaves_other_books_alone(self):
        self._chapters(2)
        self._install()
        data = json.loads(self.index.read_text(encoding="utf-8"))
        other = next(e for e in data if e["filename"] == "novel/other.fb2")
        self.assertEqual(other["title"], "Другая")
        self.assertEqual(other["audiobook"]["chapters"], ["audio/other/001.json"])
        self.assertEqual(len(data), 2)

    def test_chapter_order_survives_past_99(self):
        """Lexicographic sorting would place ch100 before ch99, silently
        reordering the book. 105 chapters exercises that."""
        self._chapters(105)
        self.assertEqual(self._install().returncode, 0)
        chapters = self._entry()["audiobook"]["chapters"]
        self.assertEqual(len(chapters), 105)
        self.assertEqual(chapters[98], "audio/mh/099.json")
        self.assertEqual(chapters[99], "audio/mh/100.json")
        # ...and the CONTENT followed the number, not the collation order.
        got = json.loads((self.books / "audio" / "mh" / "100.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(got["audio_url"], "u100")

    def test_rerun_with_fewer_chapters_leaves_no_orphans(self):
        self._chapters(5)
        self._install()
        for p in self.src.glob("mh-ch00[4-5].json"):
            p.unlink()
        self._install()
        on_disk = sorted(p.name for p in (self.books / "audio" / "mh").glob("*.json"))
        self.assertEqual(len(on_disk), 3)
        self.assertEqual(len(self._entry()["audiobook"]["chapters"]), 3)

    def test_preserves_a_hand_edited_title(self):
        self._chapters(2)
        self._install()
        data = json.loads(self.index.read_text(encoding="utf-8"))
        for e in data:
            if e["filename"] == "novel/mh.fb2":
                e["title"] = "Отредактировано вручную"
        self.index.write_text(json.dumps(data, ensure_ascii=False),
                              encoding="utf-8")
        self._install()
        self.assertEqual(self._entry()["title"], "Отредактировано вручную")

    def test_backs_up_the_catalogue_before_first_write(self):
        self._chapters(1)
        self._install()
        self.assertTrue((self.books / "index.json.bak").is_file())

    def test_fails_clearly_when_no_chapters_exist(self):
        proc = self._install()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("run the alignment script first",
                      proc.stdout + proc.stderr)

    def test_fails_clearly_when_repo_path_is_wrong(self):
        self._chapters(1)
        proc = self._install(repo_dir_wsl=str(self.root / "nope"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Not a Govorim checkout", proc.stderr)


if __name__ == "__main__":
    unittest.main()


def _aligned(words, step=1.0, offset=0.0):
    """Fake MFA output: one word per `step` seconds."""
    return [{"text": w, "start": offset + i * step, "end": offset + i * step + 0.5}
            for i, w in enumerate(words)]


class PartialMatchTest(unittest.TestCase):
    """Audio and book disagree in every direction, and the mapping has to
    survive all of them: match what it can, leave the rest untimed, and
    never hand a token another token's timing.
    """

    def test_spoken_preamble_not_in_the_text_is_ignored(self):
        """Narrators announce the book and chapter before reading it. Those
        words reach the aligner from the audio side only."""
        text = "Мороз и солнце день чудесный"
        aligned = _aligned(["елена", "костюченко", "глава", "первая"]
                           + text.lower().split())
        words = attach_timings(text, aligned)
        self.assertEqual([w["word"] for w in words], text.split())
        # First real word takes the timing of the 5th aligned entry, not the 1st.
        self.assertEqual(words[0]["begin"], 4.0)

    def test_text_the_narrator_skipped_gets_no_timing(self):
        """A footnote, an editor's preface, a passage the recording cuts:
        present in the FB2, absent from the audio."""
        text = "Первое слово пропущенная вставка совсем лишняя второе слово"
        aligned = _aligned(["первое", "слово", "второе", "слово"])
        words = attach_timings(text, aligned)
        self.assertEqual([w["word"] for w in words],
                         ["Первое", "слово", "второе", "слово"])

    def test_a_long_hole_does_not_shift_later_words(self):
        """The failure this replaces: past a gap, a lockstep walk re-synced
        on the first coincidentally equal short word and skewed the rest of
        the chapter onto the wrong timings."""
        head = ["альфа", "бета", "гамма"]
        hole = ["и"] * 40
        tail = ["дельта", "эпсилон"]
        text = " ".join(head + hole + tail)
        # MFA aligned the head and the tail; the middle utterance produced
        # nothing at all.
        aligned = _aligned(head, offset=0.0) + _aligned(tail, offset=100.0)
        words = attach_timings(text, aligned)
        by_word = {w["word"]: w["begin"] for w in words}
        self.assertEqual(by_word["дельта"], 100.0)
        self.assertEqual(by_word["эпсилон"], 101.0)

    def test_timings_never_go_backwards(self):
        text = "один два три четыре пять шесть семь восемь"
        aligned = _aligned(["один", "два", "лишнее", "четыре", "пять",
                            "шесть", "восемь"])
        words = attach_timings(text, aligned)
        begins = [w["begin"] for w in words]
        self.assertEqual(begins, sorted(begins))

    def test_fragments_stay_on_their_own_sentence(self):
        """A token with no timing must not shift later sentences onto
        earlier words -- the reason fragments index tokens by position."""
        text = "Первое предложение здесь. Второе предложение тут."
        aligned = _aligned(["первое", "здесь", "второе", "предложение", "тут"])
        doc = build_chapter(text, aligned, "https://example/x.mp3")
        self.assertEqual([f["text"] for f in doc["fragments"]],
                         ["Первое предложение здесь.", "Второе предложение тут."])
        self.assertEqual([w["word"] for w in doc["fragments"][1]["words"]],
                         ["Второе", "предложение", "тут."])

    def test_nothing_aligned_yields_no_fragments_rather_than_wrong_ones(self):
        doc = build_chapter("Совершенно другой текст", _aligned(["ничего"]),
                            "https://example/x.mp3")
        self.assertEqual(doc["fragments"], [])
        self.assertEqual(doc["word_timings"], [])

    def test_low_coverage_is_reported(self):
        logged = []
        text = " ".join(["слово%d" % i for i in range(20)])
        attach_timings(text, _aligned(["слово0", "слово1"]), log=logged.append)
        self.assertTrue(any("only" in m.lower() for m in logged), logged)


class SentenceSplitTest(unittest.TestCase):
    """Fragments have to be whole sentences by the app's definition: the
    reader pairs each fragment against a sentence its own parser produced,
    and a fragment that is half a sentence there matches nothing.
    """

    def test_dialogue_dashes_do_not_end_a_sentence(self):
        """The em dash opens a line of Russian dialogue and separates
        clauses inside it -- one sentence can carry three."""
        line = "— Очень, — ответил сосед с готовностью, — и заметьте, это."
        self.assertEqual(govorim.split_sentences(line), [line])

    def test_closing_guillemet_ends_a_quotation_not_a_sentence(self):
        self.assertEqual(
            govorim.split_sentences("Он сказал: «Мастер и Маргарита». Потом ушёл."),
            ["Он сказал: «Мастер и Маргарита».", "Потом ушёл."])

    def test_terminator_inside_a_quotation_still_splits(self):
        """...and the closing quote stays on the sentence it belongs to."""
        self.assertEqual(govorim.split_sentences("«Привет!» Он обернулся."),
                         ["«Привет!»", "Он обернулся."])

    def test_all_four_terminators(self):
        self.assertEqual(
            govorim.split_sentences("Раз. Два! Три? Четыре…  Пять."),
            ["Раз.", "Два!", "Три?", "Четыре…", "Пять."])
