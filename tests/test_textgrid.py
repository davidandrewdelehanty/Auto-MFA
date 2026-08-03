import tempfile
import unittest
from pathlib import Path

from app.textgrid import parse_textgrid

TEXTGRID = """File type = "ooTextFile"
object class = "TextGrid"

xmin = 0
xmax = 3.0
tiers? <exists>
size = 2
item []
    item [1]:
        class = "IntervalTier"
        name = "words"
        xmin = 0
        xmax = 3.0
        intervals: size = 2
        intervals [1]:
            xmin = 0.0
            xmax = 0.64
            text = ""
        intervals [2]:
            xmin = 0.64
            xmax = 1.93
            text = "привет"
item []
    item [2]:
        class = "IntervalTier"
        name = "phones"
        xmin = 0
        xmax = 3.0
        intervals: size = 1
        intervals [1]:
            xmin = 0.64
            xmax = 1.93
            text = "p r i v j e t"
"""


class TextGridTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "01.TextGrid"
            p.write_text(TEXTGRID, encoding="utf-8")
            parsed = parse_textgrid(p)
        tiers = parsed["tiers"]
        self.assertIn("words", tiers)
        self.assertIn("phones", tiers)
        words = tiers["words"]
        self.assertEqual(words[0]["text"], "")
        self.assertEqual(words[1]["text"], "привет")
        self.assertAlmostEqual(words[1]["start"], 0.64)
        self.assertAlmostEqual(words[1]["end"], 1.93)


if __name__ == "__main__":
    unittest.main()
