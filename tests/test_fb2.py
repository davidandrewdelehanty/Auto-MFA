import tempfile
import unittest
from pathlib import Path

from app.fb2 import (
    extract_chapters,
    find_audio_files,
    transcript_words,
    words_to_text,
)

FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info><book-title>Test</book-title></title-info></description>
  <body>
    <section>
      <title><p>Глава 1</p></title>
      <p>Привет, мир! Это первая глава.</p>
      <p>Вторая строка с цифрой 42.</p>
    </section>
    <section>
      <title><p>Глава 2</p></title>
      <p>Hello world. This is chapter two.</p>
    </section>
  </body>
  <body name="notes">
    <section><p>Сноска, которая не должна попасть в главы.</p></section>
  </body>
</FictionBook>
"""

# Many "raw" library FB2s nest a Part section around each chapter's own
# section, instead of listing chapters as direct children of <body> (the
# shape FB2 above already uses). Part One also has its own preamble text (an
# epigraph) before its first nested chapter; Part Two has no content of its
# own at all. extract_chapters must recurse to leaf sections, capture a
# Part's own preamble as its own chapter when present, and must NOT leak the
# Part's <title> text into that preamble or emit a spurious empty chapter for
# a Part with no direct content.
NESTED_FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info><book-title>Nested Test</book-title></title-info></description>
  <body>
    <section>
      <title><p>Part One</p></title>
      <epigraph><p>An epigraph for the whole part.</p></epigraph>
      <section>
        <title><p>Chapter 1</p></title>
        <p>Chapter 1 Text of chapter one.</p>
      </section>
      <section>
        <title><p>Chapter 2</p></title>
        <p>Chapter 2 Text of chapter two.</p>
      </section>
    </section>
    <section>
      <title><p>Part Two</p></title>
      <section>
        <title><p>Chapter 3</p></title>
        <p>Chapter 3 Text of chapter three.</p>
      </section>
    </section>
  </body>
</FictionBook>
"""


class Fb2Test(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "book.fb2").write_text(FB2, encoding="utf-8")
        (self.root / "01.mp3").write_bytes(b"x")
        (self.root / "02.wav").write_bytes(b"x")
        (self.root / "cover.jpg").write_bytes(b"x")

    def tearDown(self):
        self.dir.cleanup()

    def test_extract_chapters(self):
        chapters = extract_chapters(self.root / "book.fb2")
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Глава 1")
        self.assertIn("первая глава", chapters[0]["text"])
        self.assertIn("Hello world", chapters[1]["text"])
        self.assertNotIn("Сноска", chapters[0]["text"] + chapters[1]["text"])

    def test_extract_chapters_nested_sections(self):
        (self.root / "nested.fb2").write_text(NESTED_FB2, encoding="utf-8")
        chapters = extract_chapters(self.root / "nested.fb2")
        # Part One's own preamble becomes its own chapter, then its two
        # nested chapters, then Part Two's single nested chapter (no
        # separate entry for Part Two itself -- it has no content of its own).
        self.assertEqual(len(chapters), 4)

        self.assertEqual(chapters[0]["title"], "Part One")
        self.assertEqual(chapters[0]["text"], "An epigraph for the whole part.")
        # The Part's own title text must not leak into its preamble text.
        self.assertNotIn("Part One", chapters[0]["text"])

        self.assertEqual(chapters[1]["title"], "Chapter 1")
        self.assertIn("chapter one", chapters[1]["text"])
        self.assertEqual(chapters[2]["title"], "Chapter 2")
        self.assertIn("chapter two", chapters[2]["text"])

        # Part Two contributes no chapter of its own (no direct content).
        self.assertEqual(chapters[3]["title"], "Chapter 3")
        self.assertIn("chapter three", chapters[3]["text"])
        self.assertNotIn("Part Two", " ".join(c["title"] + c["text"] for c in chapters))

    def test_find_audio_files(self):
        found = find_audio_files(self.root)
        self.assertEqual([p.name for p in found], ["01.mp3", "02.wav"])

    def test_transcript_words(self):
        words = transcript_words("Привет, мир! 42 и ещё-таки слова.")
        self.assertNotIn("42", words)
        self.assertNotIn(",", words)
        self.assertIn("привет", words)
        self.assertIn("мир", words)
        self.assertEqual(words_to_text(words), " ".join(words))


if __name__ == "__main__":
    unittest.main()


FB2_HEAD = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            '<body>')
FB2_TAIL = "</body></FictionBook>"


def _fb2(sections):
    return FB2_HEAD + sections + FB2_TAIL


class SubtitleChapterSplitTest(unittest.TestCase):
    """Some books nest a <section> per chapter; others put a whole PART in
    one section and mark each chapter with a <subtitle>. Without splitting
    on those, Anna Karenina extracts as 8 giant chapters instead of 239 and
    cannot be paired against one audio file per chapter.
    """

    def _write(self, xml):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "b.fb2"
        p.write_text(xml, encoding="utf-8")
        return p

    def test_splits_a_part_into_its_numbered_chapters(self):
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ ПЕРВАЯ</p></title>"
            "<subtitle>I</subtitle><p>Первая глава.</p>"
            "<subtitle>II</subtitle><p>Вторая глава.</p>"
            "<subtitle>III</subtitle><p>Третья глава.</p></section>"))
        ch = extract_chapters(p)
        self.assertEqual([c["title"] for c in ch],
                         ["ЧАСТЬ ПЕРВАЯ — I", "ЧАСТЬ ПЕРВАЯ — II",
                          "ЧАСТЬ ПЕРВАЯ — III"])
        self.assertEqual(ch[1]["text"], "Вторая глава.")

    def test_marker_is_not_left_in_the_chapter_text(self):
        """The numeral names the chapter; the narrator doesn't read it.
        Left in, it feeds the aligner a stray 'i' that isn't spoken."""
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ</p></title>"
            "<subtitle>I</subtitle><p>Текст один.</p>"
            "<subtitle>II</subtitle><p>Текст два.</p></section>"))
        for c in extract_chapters(p):
            self.assertNotIn("i", transcript_words(c["text"]))

    def test_numeral_may_carry_a_chapter_name(self):
        # Anna Karenina has both bare "XX" and "XX СМЕРТЬ".
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ ПЯТАЯ</p></title>"
            "<subtitle>XIX</subtitle><p>Раз.</p>"
            "<subtitle>XX СМЕРТЬ</subtitle><p>Два.</p></section>"))
        ch = extract_chapters(p)
        self.assertEqual(len(ch), 2)
        self.assertEqual(ch[1]["title"], "ЧАСТЬ ПЯТАЯ — XX СМЕРТЬ")

    def test_cyrillic_homoglyph_numerals_are_recognised(self):
        # "ХІV" can be Cyrillic Х + Ukrainian І + Latin V.
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ</p></title>"
            "<subtitle>ХIII</subtitle><p>Раз.</p>"
            "<subtitle>ХІV</subtitle><p>Два.</p></section>"))
        self.assertEqual(len(extract_chapters(p)), 2)

    def test_scene_breaks_do_not_split(self):
        # "* * *" is a scene break, not a chapter (Собачье сердце has 38).
        p = self._write(_fb2(
            "<section><title><p>Глава</p></title>"
            "<p>Раз.</p><subtitle>* * *</subtitle><p>Два.</p>"
            "<subtitle>* * *</subtitle><p>Три.</p></section>"))
        self.assertEqual(len(extract_chapters(p)), 1)

    def test_single_marker_does_not_split(self):
        """War and Peace has exactly one <subtitle> ('Конец.') across 361
        sections. One marker must never fragment a correct section."""
        p = self._write(_fb2(
            "<section><title><p>Глава</p></title>"
            "<p>Раз.</p><subtitle>I</subtitle><p>Два.</p></section>"))
        self.assertEqual(len(extract_chapters(p)), 1)

    def test_arabic_numbers_do_not_split(self):
        """Crime and Punishment's ПРИМЕЧАНИЯ has 273 subtitles numbered
        1, 2, 3... Treating those as chapters would bury the 41 real ones."""
        p = self._write(_fb2(
            "<section><title><p>ПРИМЕЧАНИЯ</p></title>"
            "<subtitle>1</subtitle><p>Прим один.</p>"
            "<subtitle>2</subtitle><p>Прим два.</p>"
            "<subtitle>3</subtitle><p>Прим три.</p></section>"))
        self.assertEqual(len(extract_chapters(p)), 1)

    def test_repeated_same_marker_does_not_split(self):
        """A line opening with the preposition 'С ' transliterates to a
        valid roman 'C'. Requiring DISTINCT numerals keeps a run of those
        from looking like a numbered sequence."""
        p = self._write(_fb2(
            "<section><title><p>Глава</p></title>"
            "<subtitle>С другой стороны</subtitle><p>Раз.</p>"
            "<subtitle>С третьей стороны</subtitle><p>Два.</p></section>"))
        self.assertEqual(len(extract_chapters(p)), 1)

    def test_text_before_the_first_marker_is_kept(self):
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ</p></title>"
            "<p>Вступление.</p>"
            "<subtitle>I</subtitle><p>Раз.</p>"
            "<subtitle>II</subtitle><p>Два.</p></section>"))
        ch = extract_chapters(p)
        self.assertEqual(len(ch), 3)
        self.assertEqual(ch[0]["text"], "Вступление.")

    def test_nested_sections_still_win(self):
        """A book that already nests one section per chapter must be
        untouched by any of this."""
        p = self._write(_fb2(
            "<section><title><p>ЧАСТЬ</p></title>"
            "<section><title><p>I</p></title><p>Раз.</p></section>"
            "<section><title><p>II</p></title><p>Два.</p></section>"
            "</section>"))
        ch = extract_chapters(p)
        self.assertEqual([c["title"] for c in ch], ["I", "II"])
