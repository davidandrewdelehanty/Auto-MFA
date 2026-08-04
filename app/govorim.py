"""Convert Auto-MFA alignments into the JSON schema the Govorim app reads.

Govorim's audiobook files (``public/books/audio/<slug>-chNN.json``) look like
this -- verified against a real one in that repo, not guessed::

    {
      "audio_url": "https://pub-....r2.dev/dama/ch1.mp3",
      "narrator": "audiobook",
      "fragments": [
        {"text": "Говорили, что ...", "begin": 0.291, "end": 7.481,
         "words": [{"word": "Говорили,", "begin": 0.291, "end": 1.493}, ...]}
      ],
      "word_timings": [{"word": "Говорили,", "begin": 0.291, "end": 1.493}, ...]
    }

Three things differ from what the rest of this app produces internally and
are the whole reason this module exists:

1. **Key names.** Govorim uses ``begin``/``end``; MFA (and pipeline.py)
   use ``start``/``end``. Govorim uses ``word``; the TextGrid parser uses
   ``text``.
2. **Surface forms.** MFA aligns a *normalized* transcript -- lowercased,
   with punctuation and digits stripped (see fb2.transcript_words), because
   that is what its pronunciation dictionary can look up. Govorim displays
   real book text, so its word entries keep the original capitalization and
   trailing punctuation ("Говорили," -- not "говорили"). So every aligned
   word has to be mapped back onto the original token it came from.
3. **Sentences.** Govorim highlights by sentence *fragment*, not by raw
   word list, so the words have to be regrouped into sentences.

There are no ``phones`` in Govorim's schema; that tier is dropped here.
"""

import re
from typing import Dict, List, Optional, Sequence

from .fb2 import transcript_words

# The public R2 bucket the Govorim app serves audiobook audio from. Only
# used to build audio_url when a folder name is supplied; the caller can
# pass a full base of its own instead.
DEFAULT_R2_BASE = "https://pub-84adcd23e17e4925a0ac7eca17ea2556.r2.dev"

DEFAULT_NARRATOR = "audiobook"

# Sentence boundary: a terminator followed by whitespace. Deliberately the
# same rule Govorim's own sentence-level build script uses, so fragments
# here are chunked the way every already-shipped book was chunked. Includes
# the Russian-typography characters that end a sentence in these texts:
# ellipsis, closing guillemet, en/em dash.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…»–—])\s+")

# How far ahead to look for a re-sync when an aligned word doesn't match the
# token it was expected to. MFA can occasionally drop a word it failed to
# align at all; without a resync every later word in the chapter would be
# shifted onto the wrong token, silently corrupting the whole file.
_RESYNC_WINDOW = 8


def split_sentences(text: str) -> List[str]:
    """Split *text* into sentences, dropping empties."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def surface_tokens(text: str) -> List[str]:
    """Whitespace-delimited tokens of *text*, with punctuation intact.

    These are the tokens Govorim displays and highlights, so they keep
    their original form ("Говорили," including the comma).
    """
    return (text or "").split()


def _expected_norm(token: str) -> List[str]:
    """The normalized word(s) MFA would have seen for one surface token.

    Usually exactly one, but a hyphenated token ("какого-то") normalizes to
    two, because fb2.transcript_words treats the hyphen as punctuation and
    splits on it. Returning a list keeps the surface token whole (Govorim
    shows one highlightable token) while still consuming both of that
    token's aligned pieces, so the span covers the entire word.
    """
    return transcript_words(token)


def attach_timings(text: str, aligned: Sequence[Dict],
                   log: Optional[callable] = None) -> List[Dict]:
    """Map aligned (normalized) words back onto *text*'s surface tokens.

    *aligned* is the pipeline's own word list: dicts with ``text``,
    ``start`` and ``end``, in the order MFA emitted them -- which is the
    order of ``transcript_words(text)``, since that is exactly the
    transcript it was given.

    Returns Govorim-shaped word entries: ``{"word", "begin", "end"}``, one
    per surface token that actually has alignment behind it. Tokens that
    normalize to nothing (a bare dash, a standalone number) are skipped:
    MFA never saw them, so there is no honest timing to report.
    """
    out: List[Dict] = []
    i = 0
    n = len(aligned)
    resyncs = 0
    dropped = 0

    for token in surface_tokens(text):
        expected = _expected_norm(token)
        if not expected:
            continue  # punctuation/number-only: never went to MFA
        if i >= n:
            dropped += 1
            continue

        spans: List[Dict] = []
        for want in expected:
            if i >= n:
                break
            got = aligned[i]
            if str(got.get("text", "")) != want:
                # Look a short way ahead for the word we expected. If it's
                # there, the aligner dropped something in between, so skip
                # past the gap and carry on.
                found = None
                for k in range(i + 1, min(i + 1 + _RESYNC_WINDOW, n)):
                    if str(aligned[k].get("text", "")) == want:
                        found = k
                        break
                if found is None:
                    # This token has no alignment at all: the aligner never
                    # emitted it. Give up on THIS token without consuming an
                    # entry -- consuming one here would hand this token the
                    # next token's timing, and every later word in the
                    # chapter would inherit that one-word skew.
                    break
                i = found
                got = aligned[i]
                resyncs += 1
            spans.append(got)
            i += 1

        if not spans:
            dropped += 1
            continue
        out.append({
            "word": token,
            "begin": round(float(spans[0]["start"]), 3),
            "end": round(float(spans[-1]["end"]), 3),
        })

    if log and (resyncs or dropped):
        log(f"  Note: {resyncs} alignment re-sync(s), {dropped} token(s) "
            f"with no timing while building Govorim word list "
            f"({len(out)} words kept).")
    return out


def build_chapter(text: str, aligned: Sequence[Dict], audio_url: str,
                  narrator: str = DEFAULT_NARRATOR,
                  log: Optional[callable] = None) -> Dict:
    """Build one Govorim chapter document.

    *text* is the chapter's original (un-normalized) text -- the same text
    that was fed to the aligner, before fb2.transcript_words stripped it.
    *aligned* is the pipeline's word list for that chapter.
    """
    words = attach_timings(text, aligned, log=log)

    # Regroup into sentence fragments. Both the flat token list above and
    # the per-sentence lists below come from splitting the SAME text on
    # whitespace, and the sentence split only ever cuts at whitespace, so
    # walking the sentences in order consumes the flat list exactly.
    fragments: List[Dict] = []
    idx = 0
    for sentence in split_sentences(text):
        count = sum(1 for t in surface_tokens(sentence) if _expected_norm(t))
        if not count:
            continue
        chunk = words[idx:idx + count]
        idx += count
        if not chunk:
            continue
        fragments.append({
            "text": sentence,
            "begin": chunk[0]["begin"],
            "end": chunk[-1]["end"],
            "words": chunk,
        })

    return {
        "audio_url": audio_url,
        "narrator": narrator,
        "fragments": fragments,
        "word_timings": words,
    }


def audio_url_for(audio_name: str, r2_folder: str = "",
                  base: str = DEFAULT_R2_BASE) -> str:
    """Build the public audio URL for one track.

    With no *r2_folder*, returns just the filename -- the caller is
    expected to rewrite it when the audio actually gets uploaded.
    """
    name = str(audio_name).strip()
    folder = (r2_folder or "").strip().strip("/")
    if not folder:
        return name
    return f"{base.rstrip('/')}/{folder}/{name}"


def chapter_filename(slug: str, index: int) -> str:
    """``<slug>-ch001.json``.

    Three digits, always. Two would sort wrongly for any book with more
    than 99 chapters -- "ch100" collates before "ch99" -- and this library
    contains several (Anna Karenina 239, War and Peace 362). The install
    step renames these to the app's own ``NNN.json`` convention anyway.
    """
    return f"{slug}-ch{index:03d}.json"
