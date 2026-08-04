"""Parse FB2 (FictionBook) XML files into a list of chapter texts."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".wma", ".aac", ".opus", ".mp4",
}

# Characters MFA dictionaries generally contain: letters of the script, apostrophes.
# Everything else is stripped from the transcript (numbers, punctuation, markup).
_NUMBER_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^\w']+", flags=re.UNICODE)
_MULTI_WS_RE = re.compile(r"\s+")


# Tags that hold one readable passage and nothing further to descend into.
_LEAF_TAGS = ("p", "v", "subtitle", "text-author", "th", "td")
# Tags that normally WRAP the tags above (<title> holds <p>s, <stanza> holds
# <v> lines, <cite> holds <p>s) but are sometimes written with bare text
# instead. They are descended into first, and read directly only when that
# turned up nothing -- reading both levels used to duplicate every verse line
# and every heading, and reading only the wrapper ran the lines together
# ("морозсолнце") because FB2 puts no whitespace between them.
_TEXT_WRAPPERS = ("title", "stanza", "cite", "poem", "epigraph", "annotation")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_fb2(folder: Path) -> Path:
    """Return the first .fb2 file in *folder* (non-recursive)."""
    matches = sorted(folder.glob("*.fb2"))
    if not matches:
        raise FileNotFoundError(f"No .fb2 file found in {folder}")
    return matches[0]


def find_audio_files(folder: Path) -> List[Path]:
    """Return all supported audio files directly inside *folder*, sorted."""
    files: List[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(p)
    return sorted(files, key=lambda p: p.name.lower())


def _gather(elem: ET.Element, skip_sections: bool = False) -> str:
    """Readable text under *elem*, each passage counted exactly once.

    FB2 nests text-bearing tags inside text-bearing tags: a <poem> holds
    <stanza> elements holding <v> lines, a <title> holds its own <p>.
    Matching every level duplicated every verse line and every heading --
    Горе от ума came out at twice its real length, which fed the aligner a
    transcript saying everything twice and inflated the word counts the
    chapter-size guards rely on.

    With *skip_sections* the walk stops at nested <section> children, so a
    part's own preamble (an epigraph before its first chapter) can be read
    without also re-reading the chapters that follow it.
    """
    parts: List[str] = []

    def take(node: ET.Element) -> None:
        tag = _localname(node.tag)
        if tag in _LEAF_TAGS:
            text = "".join(node.itertext()).strip()
            if text:
                parts.append(text)
            return
        if skip_sections and tag == "section":
            return
        before = len(parts)
        for child in node:
            take(child)
        if len(parts) == before and tag in _TEXT_WRAPPERS:
            text = "".join(node.itertext()).strip()
            if text:
                parts.append(text)

    for child in elem:
        take(child)
    return " ".join(parts)


def _collect_text(elem: ET.Element) -> str:
    """All readable text under *elem*, including nested sections."""
    return _gather(elem)


def _section_title(section: ET.Element) -> str:
    for child in section:
        if _localname(child.tag) == "title":
            return "".join(child.itertext()).strip()
    return ""


def _direct_text(section: ET.Element) -> str:
    """Text from *section*'s own content only -- NOT descending into nested
    <section> children (but still descending into wrapper elements like
    <epigraph> or <poem> that hold their own <p>/<v> content).

    Used for a section that contains both its own content and nested
    subsections (e.g. a "Part One" section with an epigraph before its first
    nested chapter): that preamble becomes its own small chapter instead of
    being silently dropped, without also re-collecting the nested
    subsections' text (they are walked separately, as their own chapters).
    """
    holder = ET.Element("holder")
    for child in section:
        if _localname(child.tag) == "title":
            continue    # already captured separately by _section_title
        holder.append(child)   # ElementTree has no parent links, so this
                               # borrows the child rather than moving it
    return _gather(holder, skip_sections=True)


# Cyrillic look-alikes for Latin roman-numeral letters. Russian FB2s mix them
# freely -- "ХІV" can be Cyrillic Х + Ukrainian І + Latin V -- and a numeral
# read literally would not match anything.
_ROMAN_HOMOGLYPHS = str.maketrans({
    "Х": "X", "І": "I", "Ѵ": "V", "С": "C", "М": "M", "Д": "D",
})
# A chapter marker is a roman numeral, optionally followed by that chapter's
# own name -- Anna Karenina has both bare "XX" and "XX СМЕРТЬ".
_CHAPTER_MARK_RE = re.compile(r"^(?:глава\s+)?([ivxlcdm]+)\.?(?:\s+\S.*)?$",
                              re.IGNORECASE | re.DOTALL)

# A split is only believable if the pieces are chapter-sized. Verse works
# number their STANZAS with roman numerals in exactly the same way prose
# works number their chapters: Eugene Onegin has 391 such subtitles, and
# splitting on them yields 380 pieces with a median of 60 words -- stanzas,
# not chapters. Real chapters are far bigger (median 1149 words in Anna
# Karenina, 4172 in Crime and Punishment, minimum 212), so a median below
# this means the markers were numbering something else.
_MIN_MEDIAN_CHAPTER_WORDS = 150


# Endnote sections. FB2 normally puts these in <body name="notes">, which is
# skipped outright, but plenty of files leave them as an ordinary section of
# the main body -- Crime and Punishment ships 273 numbered notes that way.
# They are never recorded, so they must not become a chapter and shift every
# later chapter's pairing by one.
_NOTES_TITLE_RE = re.compile(
    r"^(сноски?|примечани[ея]|комментари[ий]|notes?|footnotes?|endnotes?)\W*$",
    re.IGNORECASE)


def _is_notes_title(title: str) -> bool:
    return bool(_NOTES_TITLE_RE.match((title or "").strip()))


def _chapter_marker(text: str) -> str:
    """Return the roman numeral if *text* opens like a chapter heading.

    Deliberately accepts ROMAN numerals only. Many Russian FB2s mark real
    chapters this way inside a part-level section, but they also use
    `<subtitle>` for things that are NOT chapters: scene breaks ("* * *"),
    stage directions ("Занавес"), an end marker ("Конец."), and -- the case
    that makes arabic numbers unsafe -- numbered endnotes. Crime and
    Punishment's ПРИМЕЧАНИЯ section carries 273 subtitles numbered 1, 2,
    3...; treating those as chapters would bury the novel's 41 real ones.
    """
    s = (text or "").strip().translate(_ROMAN_HOMOGLYPHS)
    match = _CHAPTER_MARK_RE.match(s) if s else None
    return match.group(1).upper() if match else ""


def _text_from(elements) -> str:
    """Readable text of *elements*, not descending into nested sections."""
    holder = ET.Element("holder")
    for elem in elements:
        holder.append(elem)   # borrowed, not moved -- ElementTree keeps no
                              # parent links, so the original tree is intact
    return _gather(holder, skip_sections=True)


def _split_leaf_on_subtitles(section: ET.Element, title: str,
                             chapters: List[Dict[str, str]]) -> bool:
    """Split one leaf section into a chapter per roman-numeral `<subtitle>`.

    Some books nest a `<section>` per chapter (War and Peace); others put a
    whole part in one section and mark each chapter with a `<subtitle>`
    (Anna Karenina, Crime and Punishment). Without this, the latter come out
    as a handful of enormous "chapters" -- Anna Karenina as 8 instead of
    239 -- which cannot be paired against one audio file per chapter.

    Returns False (splitting nothing) unless the section holds at least two
    chapter markers, so a lone "Занавес" or "Конец." can't fragment a
    section that was already correct.
    """
    marks = [c for c in section
             if _localname(c.tag) == "subtitle"
             and _chapter_marker("".join(c.itertext()))]
    # Two or more, and genuinely different numerals. Requiring distinct
    # values is what stops a run of subtitles that merely *start* with a
    # roman-numeral letter -- a Russian line opening with the preposition
    # "С ", which transliterates to a valid "C" -- from being mistaken for
    # a numbered chapter sequence.
    if len({_chapter_marker("".join(c.itertext())) for c in marks}) < 2:
        return False

    mark_ids = {id(m) for m in marks}
    groups: List = []                 # [(heading or None, [elements])]
    current = (None, [])
    for child in section:
        if _localname(child.tag) == "title":
            continue                  # captured separately as the part name
        if id(child) in mark_ids:
            groups.append(current)
            # Keep the subtitle's full text as the heading: Anna Karenina's
            # "XX СМЕРТЬ" names the chapter as well as numbering it.
            current = ("".join(child.itertext()).strip(), [])
            continue
        current[1].append(child)
    groups.append(current)

    candidates: List[Dict[str, str]] = []
    for marker, elems in groups:
        text = _text_from(elems).strip()
        if not text:
            continue                  # e.g. nothing between the part title
                                      # and its first chapter marker
        # The marker itself is the chapter's name, not part of what is read
        # aloud -- keeping it out of the text avoids feeding the aligner a
        # stray "i"/"ii" that the narrator never says.
        if marker and title:
            name = f"{title} — {marker}"
        else:
            name = marker or title or f"Chapter {len(chapters) + len(candidates) + 1}"
        candidates.append({"title": name, "text": text})

    if len(candidates) < 2:
        return False
    sizes = sorted(len(c["text"].split()) for c in candidates)
    median = sizes[len(sizes) // 2]
    if median < _MIN_MEDIAN_CHAPTER_WORDS:
        return False                  # stanzas or similar -- see the constant

    chapters.extend(candidates)
    return True


def _walk_sections(section: ET.Element, chapters: List[Dict[str, str]]) -> None:
    """Depth-first: append one chapter per LEAF <section> (no nested
    <section> children), in document order.

    Real FB2 files vary in how deep they nest: some (already flattened for a
    per-chapter audio pipeline) have one chapter per section directly under
    <body>; many "raw" FB2s downloaded from a library instead nest a "Part"
    section around each chapter's own section. Only descending into DIRECT
    children of <body> -- as earlier versions of this function did -- treats
    each Part as a single giant chapter, concatenating every chapter inside
    it into one blob via _collect_text's full-subtree walk. Recursing to
    leaves handles both shapes the same way, and changes nothing for a file
    that was already flat (a section with no nested sections behaves exactly
    as before).
    """
    nested = [c for c in section if _localname(c.tag) == "section"]
    title = _section_title(section)
    if _is_notes_title(title):
        return                        # endnotes left in the main body

    # Nesting only counts as chapter structure when the subsections are
    # chapters in their own right, which in practice means they are titled.
    # Where a large share of them are not, the nesting is internal division:
    # "Моя любимая страна" builds each of its 14 essays from an untitled
    # first-person opening plus the titled reportage piece it introduces, and
    # splitting there gives 28 half-chapters against 14 audio files. A stray
    # untitled section among many titled ones is just an untitled chapter
    # (Тихий Дон has three among 248), hence a share rather than a flat
    # "every one of them".
    #
    # The test only applies where the subsections are leaves. A subsection
    # holding subsections of its own is structural whatever its title says --
    # Тихий Дон's "КНИГА ТРЕТЬЯ" wraps two untitled parts holding 63 chapters
    # between them, and judging it by this rule would swallow all 63.
    if nested and not any(any(_localname(g.tag) == "section" for g in c) for c in nested):
        if sum(1 for c in nested if not _section_title(c)) * 3 > len(nested):
            nested = []

    if not nested:
        # A part-in-one-section book marks its chapters with <subtitle>;
        # split on those when present (see _split_leaf_on_subtitles).
        if _split_leaf_on_subtitles(section, title, chapters):
            return
        text = _collect_text(section).strip()
        if not text:
            return
        # An untitled scrap this short is front matter -- a dedication, an
        # epigraph, a colophon -- not a chapter anyone recorded.
        if not title and len(text.split()) < _MIN_MEDIAN_CHAPTER_WORDS:
            return
        chapters.append({"title": title or f"Chapter {len(chapters) + 1}", "text": text})
        return

    # Walk the subsections into a scratch list first, so their combined size
    # can be sanity-checked before they are accepted as chapters.
    sub: List[Dict[str, str]] = []
    preamble = _direct_text(section).strip()
    if preamble:
        sub.append({"title": title or f"Chapter {len(chapters) + 1}", "text": preamble})
    for child in nested:
        _walk_sections(child, sub)

    # Same size guard as the subtitle split, for the same reason: nesting a
    # <section> per unit is how one book marks its chapters and how another
    # marks something much smaller. Eugene Onegin wraps each of its 357
    # STANZAS in its own section inside the eight "Глава" sections, so
    # recursing to leaves turns an 8-chapter book into 357 fragments with a
    # median of 60 words. When the pieces come out that small the nesting was
    # not chapter structure, so this section is kept whole instead.
    if sub:
        sizes = sorted(len(c["text"].split()) for c in sub)
        if sizes[len(sizes) // 2] < _MIN_MEDIAN_CHAPTER_WORDS:
            text = _collect_text(section).strip()
            if text:
                chapters.append({"title": title or f"Chapter {len(chapters) + 1}",
                                 "text": text})
            return
    chapters.extend(sub)


def extract_chapters(path: Path) -> List[Dict[str, str]]:
    """Parse *path* and return [{title, text}, ...].

    Only the main `body` is used; footnotes/comments bodies are skipped. Each
    leaf `section` (recursively; see _walk_sections) becomes one chapter.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    chapters: List[Dict[str, str]] = []
    for body in root.iter():
        if _localname(body.tag) != "body":
            continue
        body_name = (body.get("name") or "").strip().lower()
        if body_name in ("notes", "comments", "footnotes"):
            continue
        for section in body:
            if _localname(section.tag) != "section":
                continue
            _walk_sections(section, chapters)
    if not chapters:
        raise ValueError(f"No chapters found in {path}")
    return chapters


def extract_metadata(path: Path) -> Dict[str, str]:
    """Return {"title", "author"} from an FB2's <title-info> block.

    Used to prefill the catalogue entry when installing a book into the
    Govorim app, so the title reads "Дама с собачкой" rather than a
    slug-derived guess like "Chekhov Dama". Both keys are always present
    and may be empty -- FB2 metadata is frequently incomplete, and a
    missing title is not worth failing over.
    """
    title = ""
    author = ""
    try:
        root = ET.parse(path).getroot()
    except Exception:  # noqa: BLE001
        return {"title": "", "author": ""}

    for info in root.iter():
        if _localname(info.tag) != "title-info":
            continue
        for child in info:
            name = _localname(child.tag)
            if name == "book-title" and not title:
                title = "".join(child.itertext()).strip()
            elif name == "author" and not author:
                parts = []
                for field in ("first-name", "middle-name", "last-name"):
                    for sub in child:
                        if _localname(sub.tag) == field:
                            value = "".join(sub.itertext()).strip()
                            if value:
                                parts.append(value)
                            break
                author = " ".join(parts)
        break
    return {"title": title, "author": author}


def transcript_words(text: str) -> List[str]:
    """Normalize chapter text into the word list MFA expects in a transcript.

    Lowercases, drops punctuation/numbers, collapses whitespace.
    """
    lowered = text.lower()
    cleaned = _NUMBER_RE.sub(" ", lowered)
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    cleaned = _MULTI_WS_RE.sub(" ", cleaned).strip()
    return [w for w in cleaned.split(" ") if w]


def words_to_text(words: List[str]) -> str:
    """Join a word list into a transcript string (MFA .txt format)."""
    return " ".join(words)
