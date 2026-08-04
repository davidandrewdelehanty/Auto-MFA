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


def _collect_text(elem: ET.Element) -> str:
    parts: List[str] = []
    for node in elem.iter():
        tag = _localname(node.tag)
        if tag in ("p", "v", "subtitle", "stanza", "title", "cite", "text-author", "th", "td"):
            text = "".join(node.itertext()).strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _section_title(section: ET.Element) -> str:
    for child in section:
        if _localname(child.tag) == "title":
            return "".join(child.itertext()).strip()
    return ""


def _direct_text(section: ET.Element) -> str:
    """Text from *section*'s own content only -- NOT descending into nested
    <section> children (but still descending into wrapper elements like
    <epigraph> or <poem> that hold their own <p>/<v> content, the same way
    _collect_text does for a leaf section).

    Used for a section that contains both its own content and nested
    subsections (e.g. a "Part One" section with an epigraph before its first
    nested chapter): that preamble becomes its own small chapter instead of
    being silently dropped, without also re-collecting the nested
    subsections' text (they are walked separately, as their own chapters).
    """
    parts: List[str] = []

    def walk(elem: ET.Element) -> None:
        for child in elem:
            tag = _localname(child.tag)
            if tag in ("section", "title"):
                continue  # nested chapters, and the section's own title
                          # (already captured separately by _section_title),
                          # are excluded from the preamble text
            if tag in ("p", "v", "subtitle", "stanza", "cite", "text-author", "th", "td"):
                text = "".join(child.itertext()).strip()
                if text:
                    parts.append(text)
            else:
                walk(child)  # descend into wrapper containers (epigraph, poem, ...)

    walk(section)
    return " ".join(parts)


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

# Tags that carry readable text inside a section.
_TEXT_TAGS = ("p", "v", "subtitle", "stanza", "cite", "text-author", "th", "td")


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
    parts: List[str] = []

    def take(elem: ET.Element) -> None:
        tag = _localname(elem.tag)
        if tag == "section":
            return
        if tag in _TEXT_TAGS:
            text = "".join(elem.itertext()).strip()
            if text:
                parts.append(text)
            return
        for child in elem:
            take(child)

    for elem in elements:
        take(elem)
    return " ".join(parts)


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
            name = marker or title or f"Chapter {len(chapters) + 1}"
        chapters.append({"title": name, "text": text})
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
    if not nested:
        # A part-in-one-section book marks its chapters with <subtitle>;
        # split on those when present (see _split_leaf_on_subtitles).
        if _split_leaf_on_subtitles(section, title, chapters):
            return
        text = _collect_text(section).strip()
        if text:
            chapters.append({"title": title or f"Chapter {len(chapters) + 1}", "text": text})
        return
    preamble = _direct_text(section).strip()
    if preamble:
        chapters.append({"title": title or f"Chapter {len(chapters) + 1}", "text": preamble})
    for child in nested:
        _walk_sections(child, chapters)


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
