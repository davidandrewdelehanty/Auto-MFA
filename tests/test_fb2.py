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
