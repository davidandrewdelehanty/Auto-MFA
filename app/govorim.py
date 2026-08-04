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

import difflib
import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

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

# Below this share of the text carrying timings, the mapping is reported as
# suspect. It is not an error: a chapter where the narrator reads a preface
# that isn't in the book, or skips a footnote that is, legitimately yields
# partial timings, and partial highlighting beats none.
_LOW_COVERAGE = 0.60


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


def _normalized_index(text: str) -> Tuple[List[str], List[int], List[str]]:
    """Break *text* into the normalized word stream MFA would have seen.

    Returns ``(norm_words, owner, tokens)``: the normalized stream, the
    index of the surface token each normalized word came from, and the
    surface tokens themselves. A hyphenated token ("какого-то") contributes
    two normalized words that both point back at the one displayed token;
    punctuation-only tokens contribute none.
    """
    tokens = surface_tokens(text)
    norm: List[str] = []
    owner: List[int] = []
    for ti, token in enumerate(tokens):
        for word in _expected_norm(token):
            norm.append(word)
            owner.append(ti)
    return norm, owner, tokens


def _attach_indexed(text: str, aligned: Sequence[Dict],
                    log: Optional[callable] = None) -> List[Dict]:
    """Map aligned (normalized) words back onto *text*'s surface tokens.

    *aligned* is the pipeline's own word list: dicts with ``text``,
    ``start`` and ``end``, in the order MFA emitted them.

    The two sequences are matched with difflib rather than walked in
    lockstep, because in practice they are NOT the same sequence. Audio and
    book disagree in every direction: the narrator reads a title card, a
    dedication or a translator's note that is nowhere in the FB2; the FB2
    carries footnote text, an editor's preface or a different edition's
    wording that is nowhere in the audio; and MFA silently emits nothing at
    all for an utterance it could not align, leaving a hole of a hundred
    words in the middle. A lockstep walk with a small look-ahead window
    survives none of these -- past a hole it re-syncs on the first
    coincidentally equal word (in Russian text, some short function word a
    few sentences later), and everything after that inherits the skew,
    which is far worse than having no timing at all.

    difflib finds the longest matching blocks instead, so unmatched
    stretches on either side are simply left unmatched. Tokens with no
    alignment behind them get no entry: there is no honest timing to
    report, and the reader shows them unhighlighted rather than wrong.

    Returns Govorim-shaped word entries -- ``{"word", "begin", "end"}`` --
    each carrying an internal ``_token`` index so build_chapter can regroup
    them into sentences without assuming every token was matched.
    attach_timings() is the public form, without that index.
    """
    norm, owner, tokens = _normalized_index(text)
    mfa = [str(w.get("text", "")) for w in aligned]
    if not norm or not mfa:
        if log and norm:
            log("  Note: no aligned words for this chapter; it will have no "
                "timings.")
        return []

    # autojunk would treat any word appearing in more than 1% of a long
    # sequence as noise -- in Russian prose that is exactly the common
    # function words ("и", "в", "не") that anchor a match.
    matcher = difflib.SequenceMatcher(None, norm, mfa, autojunk=False)

    # Per surface token, the aligned entries that matched its pieces.
    spans: Dict[int, List[Dict]] = {}
    matched = 0
    for i, j, size in matcher.get_matching_blocks():
        for k in range(size):
            spans.setdefault(owner[i + k], []).append(aligned[j + k])
            matched += 1

    out: List[Dict] = []
    for ti in sorted(spans):
        entries = spans[ti]
        out.append({
            "word": tokens[ti],
            "begin": round(float(entries[0]["start"]), 3),
            "end": round(float(entries[-1]["end"]), 3),
            "_token": ti,
        })

    if log:
        coverage = matched / len(norm)
        unused = len(mfa) - matched
        note = (f"  Matched {matched}/{len(norm)} of the chapter's words to "
                f"the audio ({coverage:.0%})")
        if unused:
            note += f"; {unused} aligned word(s) had no counterpart in the text"
        log(note + ".")
        if coverage < _LOW_COVERAGE:
            log(f"  Warning: only {coverage:.0%} of this chapter has timings. "
                f"Usually the recording and this edition of the text differ "
                f"(an abridgement, a spoken preface, endnotes read aloud), or "
                f"the chapter is paired to the wrong audio file.")
    return out


def _public_word(entry: Dict) -> Dict:
    """The Govorim-visible fields of a word entry, without bookkeeping."""
    return {"word": entry["word"], "begin": entry["begin"], "end": entry["end"]}


def attach_timings(text: str, aligned: Sequence[Dict],
                   log: Optional[callable] = None) -> List[Dict]:
    """Timings for *text*'s surface tokens, one entry per token that has
    alignment behind it. See _attach_indexed for how the mapping is made."""
    return [_public_word(w) for w in _attach_indexed(text, aligned, log=log)]


def build_chapter(text: str, aligned: Sequence[Dict], audio_url: str,
                  narrator: str = DEFAULT_NARRATOR,
                  log: Optional[callable] = None) -> Dict:
    """Build one Govorim chapter document.

    *text* is the chapter's original (un-normalized) text -- the same text
    that was fed to the aligner, before fb2.transcript_words stripped it.
    *aligned* is the pipeline's word list for that chapter.
    """
    words = _attach_indexed(text, aligned, log=log)
    by_token = {w["_token"]: w for w in words}

    # Regroup into sentence fragments. Sentences are cut out of the SAME
    # text on whitespace only, so their tokens partition the token list in
    # order and each sentence owns a known index range. Indexing by range
    # rather than walking a counter is what lets a token with no timing be
    # missing from `words` entirely: with a counter, one unmatched token
    # would shift every later sentence onto the wrong words.
    fragments: List[Dict] = []
    cursor = 0
    for sentence in split_sentences(text):
        lo = cursor
        cursor += len(surface_tokens(sentence))
        chunk = [_public_word(by_token[t]) for t in range(lo, cursor)
                 if t in by_token]
        if not chunk:
            continue    # nothing in this sentence aligned; no honest timing
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
        "word_timings": [_public_word(w) for w in words],
    }


def audio_url_for(audio_name: str, r2_folder: str = "",
                  base: str = DEFAULT_R2_BASE) -> str:
    """Build the public audio URL for one track.

    The filename is percent-encoded. Russian audiobook files routinely
    arrive named in Cyrillic and with spaces ("мраморная головка
    аудиокнига.mp3"); dropped into a URL raw, that is not a valid URL, and
    whether it resolves depends on the client. Encoding it here means the
    stored URL is correct whatever the file is called. ASCII names like
    "44.mp3" are unaffected.

    With no *r2_folder*, returns just the filename unencoded -- it isn't a
    URL yet, and the install step rewrites it once the R2 folder is known.
    """
    name = str(audio_name).strip()
    folder = (r2_folder or "").strip().strip("/")
    if not folder:
        return name
    return f"{base.rstrip('/')}/{quote(folder)}/{quote(name)}"


def chapter_filename(slug: str, index: int) -> str:
    """``<slug>-ch001.json``.

    Three digits, always. Two would sort wrongly for any book with more
    than 99 chapters -- "ch100" collates before "ch99" -- and this library
    contains several (Anna Karenina 239, War and Peace 362). The install
    step renames these to the app's own ``NNN.json`` convention anyway.
    """
    return f"{slug}-ch{index:03d}.json"
