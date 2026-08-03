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


def extract_chapters(path: Path) -> List[Dict[str, str]]:
    """Parse *path* and return [{title, text}, ...].

    Only the main `body` is used; footnotes/comments bodies are skipped.  Each
    top-level `section` becomes one chapter.
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
            title = _section_title(section)
            text = _collect_text(section).strip()
            if text:
                chapters.append({"title": title or f"Chapter {len(chapters) + 1}", "text": text})
    if not chapters:
        raise ValueError(f"No chapters found in {path}")
    return chapters


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
