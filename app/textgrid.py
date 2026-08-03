"""Parse Praat TextGrid files (long format) produced by Montreal Forced Aligner.

Only interval tiers are handled; point tiers are ignored.  MFA 3.x writes
interval tiers named "words" and "phones".
"""

import re
from pathlib import Path
from typing import List, Dict

_TIER_RE = re.compile(r'name = "([^"]*)"')


def _find_tier_blocks(lines: List[str]):
    """Split the raw text into blocks starting at 'item [n]:'."""
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("item [") and stripped.endswith("]:") or (
            stripped.startswith("item [") and ":" in stripped
        ):
            if current:
                blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def parse_textgrid(path) -> Dict[str, List[Dict]]:
    """Return {'tiers': {name: [{'start': float, 'end': float, 'text': str}, ...]}}.

    Empty intervals (silence) are kept; callers usually filter them.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tiers: Dict[str, List[Dict]] = {}
    for block in _find_tier_blocks(lines):
        tier_name = None
        intervals: List[Dict] = []
        for line in block:
            m = _TIER_RE.search(line)
            if m:
                tier_name = m.group(1)
        # Parse interval tuples: three consecutive numeric/text lines.
        xmin = xmax = text_val = None
        for line in block:
            stripped = line.strip()
            if stripped.startswith("xmin"):
                xmin = float(re.search(r"=\s*([-\d.eE]+)", stripped).group(1))
            elif stripped.startswith("xmax"):
                xmax = float(re.search(r"=\s*([-\d.eE]+)", stripped).group(1))
            elif stripped.startswith("text"):
                m = re.search(r'=\s*"([^"]*)"', stripped)
                text_val = m.group(1) if m else ""
                if xmin is not None and xmax is not None:
                    intervals.append({"start": xmin, "end": xmax, "text": text_val})
                xmin = xmax = text_val = None
        if tier_name is not None:
            tiers.setdefault(tier_name, []).extend(intervals)
    return {"tiers": tiers}
