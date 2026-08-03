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
