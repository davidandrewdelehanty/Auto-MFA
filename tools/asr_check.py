"""Find text that isn't in the recording, by listening to the recording.

WHY THIS EXISTS
---------------
MFA is a forced aligner. Handed a transcript and an utterance, it decides
WHERE each word goes; it has no way to say "that word is not in here". So a
book whose text contains more than the tape does cannot fail loudly. It
fails quietly, by squeezing the surplus in, and the reader watches the
highlighting pull away from the voice.

The 1977 October speech is the clean example. Its closing peroration runs
to 146 words in the book. The tape, once the ovations are taken out, has
forty seconds of voice left there -- about 67 words at the pace he keeps
for the rest of the speech. None of that is visible in the alignment: the
run reported 100% of words matched, because every word did find a home.
They were just the wrong homes, from the last third onward.

No amount of skipping applause can fix a surplus of text, and no measure
taken from the alignment can even detect one -- the alignment is where the
lie is. The only way to know what is on a tape is to listen to it. So this
transcribes the audio (faster-whisper, which unlike MFA is free to hear
nothing) and diffs that against the book.

    python3 tools/asr_check.py \\
        --audio "/mnt/c/.../01.mp3" \\
        --fb2 "/mnt/c/.../speech.fb2"

It prints every run of book text with no counterpart in the audio, with the
clock position so it can be checked by ear, and two ways to act on it:

  --drop 268-346      pass to make_book_run.py. The words stay in the book
                      and simply carry no timing, so they show unhighlighted
                      and the highlighting resumes after them.

  --trim-fb2 OUT.fb2  write a copy of the FB2 with those sentences removed,
                      for a recording that is frankly an abridgement and
                      should be published as one.

WHAT IT WILL AND WON'T CATCH
Speech recognition mishears; a couple of wrong words in a row prove
nothing, so only runs of --min-run words or more are reported. That is
deliberate: this is for finding passages the recording does not contain,
not for proofreading. Long verbatim repetition in the text is the one place
the diff can pick the wrong occurrence, so every reported run prints its
surrounding text and its timestamps -- read them before acting.

The transcription is the slow part, so it is cached: --asr-json writes it,
and a second run with the same path reuses it instantly. Iterate on
--min-run against the cache rather than re-transcribing.
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import fb2 as fb2_mod  # noqa: E402
from app.govorim import split_sentences  # noqa: E402
from find_nonspeech import measure  # noqa: E402

MIN_RUN = 5             # words; shorter runs are mishearings, not omissions
CONTEXT = 8             # words of context printed either side
SENTENCE_DROP = 0.6     # a sentence this far dropped is removed by --trim-fb2
VOICE_DB = -40.0        # above this a window counts as somebody making a noise
ROOM = 0.8              # this much of the room needed means the words fit

# FB2 elements that hold a line of readable text. Kept in step with
# fb2._LEAF_TAGS: a trimmer that edited a different set of elements from the
# one the aligner reads would produce a file whose chapters no longer match.
_LEAF_TAGS = ("p", "v", "subtitle", "text-author", "th", "td")


def clock(seconds):
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


# ---------------------------------------------------------------- listening

def transcribe(audio, model_size, device, compute_type, language):
    """[{"word", "start", "end"}, ...] as heard, in order."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit(
            "faster-whisper is not installed in this environment.\n"
            "    pip install faster-whisper\n"
            "It downloads its model on first use, so run it somewhere with "
            "internet access.")
    print(f"Transcribing {audio.name} with the '{model_size}' model "
          f"(first run downloads it) ...", flush=True)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    # vad_filter keeps it from hallucinating text over applause and silence,
    # which is exactly where this file has trouble.
    segments, info = model.transcribe(
        str(audio), language=language, word_timestamps=True,
        vad_filter=True, beam_size=5)
    heard = []
    for seg in segments:
        for w in (seg.words or []):
            heard.append({"word": w.word.strip(),
                          "start": float(w.start), "end": float(w.end)})
    print(f"Heard {len(heard)} words over "
          f"{clock(heard[-1]['end']) if heard else '0:00'}.")
    return heard


# ---------------------------------------------------------------- comparing

def index_text(text):
    """(norm_words, sentences, owner) for one chapter's text.

    *sentences* is the chapter split into sentences; *owner[i]* is the index
    of the sentence normalized word i came from. Sentence granularity is
    what --trim-fb2 edits at, so the diff is carried in those terms.
    """
    sentences = split_sentences(text)
    norm, owner = [], []
    for si, sentence in enumerate(sentences):
        for word in fb2_mod.transcript_words(sentence):
            norm.append(word)
            owner.append(si)
    return norm, sentences, owner


def missing_runs(book_norm, heard, min_run):
    """Runs of book words with nothing matching in *heard*.

    Returns [(lo, hi, before_time, after_time), ...] as half-open index
    ranges into *book_norm*.

    difflib rather than a walk in step: the two sequences disagree in both
    directions at once (the reader adds an ad-lib, the recogniser mishears a
    name, the book has a paragraph the tape doesn't), and a walk re-syncs on
    the first coincidentally equal function word and carries the skew
    forward. autojunk is off for the usual reason -- it would discard exactly
    the common Russian words that anchor a match.
    """
    asr_norm, asr_time = [], []
    for w in heard:
        for piece in fb2_mod.transcript_words(w["word"]):
            asr_norm.append(piece)
            asr_time.append(w)

    sm = difflib.SequenceMatcher(None, book_norm, asr_norm, autojunk=False)
    marks = [None] * len(book_norm)
    for i, j, size in sm.get_matching_blocks():
        for k in range(size):
            marks[i + k] = asr_time[j + k]

    runs = []
    start = None
    for i in range(len(marks) + 1):
        mark = marks[i] if i < len(marks) else object()
        if mark is None:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_run:
                prev = next((m for m in reversed(marks[:start]) if m), None)
                nxt = next((m for m in marks[i:] if m), None)
                runs.append((start, i,
                             prev["end"] if prev else None,
                             nxt["start"] if nxt else None))
            start = None
    return runs, marks


# ------------------------------------------------------------------ judging

def audio_room(audio, ffmpeg="ffmpeg"):
    """(voiced_seconds(lo, hi), total_duration) for *audio*.

    Measures how much of a window anybody is making a noise in, using the
    same ffmpeg astats pass find_nonspeech.py uses. Applause counts as
    noise, deliberately: the question this answers is "could the missing
    words have been spoken in here", and a window full of ovation is a
    window where the recogniser had every reason to miss them.
    """
    frames = measure(Path(audio), ffmpeg)
    if not frames:
        return None, 0.0
    step = frames[1][0] - frames[0][0] if len(frames) > 1 else 0.5
    times = [f[0] for f in frames]
    loud = [f[1] > VOICE_DB for f in frames]
    # Running total, so each lookup is O(log n) rather than a scan: a
    # 58-hour book has 400,000 windows and a novel has thousands of runs.
    import bisect
    cum = [0.0]
    for ok in loud:
        cum.append(cum[-1] + (step if ok else 0.0))

    def voiced(lo, hi):
        i = bisect.bisect_left(times, lo)
        j = bisect.bisect_right(times, hi)
        return cum[min(j, len(cum) - 1)] - cum[min(i, len(cum) - 1)]

    return voiced, times[-1] + step


def local_pace(heard):
    """Seconds per word, from the recogniser's own consecutive words.

    Taken locally -- successive words less than 1.5s apart -- rather than
    from the whole file divided by the word count, because the file is a
    third applause and that would report the speaker as far slower than he
    is, which in turn would make every hole look too big to be a mishearing.
    """
    steps = sorted(b["start"] - a["start"] for a, b in zip(heard, heard[1:])
                   if 0.0 < b["start"] - a["start"] <= 1.5)
    if not steps:
        return 0.6
    return steps[len(steps) // 2]


def judge(runs, owner, heard, voiced, duration):
    """Label each run ABSENT (the tape hasn't got it) or MISHEARD (it has).

    Two independent tests have to agree before any of the author's words are
    called absent, because speech recognition over an ovation fails exactly
    where this file is hardest and a wrong call here deletes real text.

    ROOM -- is there enough noise between the words either side to have
    said them? The recogniser missed «Дорогие товарищи! Уважаемые
    зарубежные гости!» entirely, and those words are on the tape, 26
    seconds in, under the opening ovation. Forty seconds of audio for
    eleven words is not a tape that lacks them.

    SHAPE -- does the hole start or end where a sentence does? An edit to a
    tape removes whole phrases; it does not excise a clause from the middle
    of one and leave the grammar intact on both sides. The recogniser
    dropped «рабочие и крестьяне нашей страны в отведенный им историей»
    from the middle of a sentence whose head and tail it heard, and gave
    its neighbours timestamps a second apart -- no room by the clock, but
    the clock is what it got wrong.
    """
    spw = local_pace(heard)
    out = []
    for lo, hi, before, after in runs:
        window = (before if before is not None else 0.0,
                  after if after is not None else duration)
        seconds = voiced(*window) if voiced else None
        room = (seconds / spw) if seconds is not None else None
        n = hi - lo
        fits = room is not None and room >= ROOM * n
        starts_sentence = lo == 0 or owner[lo] != owner[lo - 1]
        ends_sentence = hi >= len(owner) or owner[hi - 1] != owner[hi]
        shaped = starts_sentence or ends_sentence
        # With no measurement, fall back to the shape test alone rather than
        # calling everything a mishearing: --no-audio-check exists so a run
        # can be judged when the audio is unreadable, and a flag that
        # silently declines to find anything is worse than no flag.
        absent = shaped and not fits
        out.append((lo, hi, before, after, room, absent, shaped))
    return out, spw


# ----------------------------------------------------------------- trimming

def trim_fb2(src, dst, dropped_sentences, log=print):
    """Write *src* to *dst* with *dropped_sentences* removed.

    Works at the level of the leaf text elements the aligner itself reads,
    re-splitting each into sentences the same way, so the two views of the
    file cannot drift apart. An element left with nothing is removed; an
    element with markup inside it is left alone and reported, because
    rewriting its text would throw the markup away.
    """
    import xml.etree.ElementTree as ET

    tree = ET.ElementTree(ET.fromstring(src.read_text(encoding="utf-8")))
    root = tree.getroot()

    def local(tag):
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    body = next((e for e in root if local(e.tag) == "body"), None)
    if body is None:
        raise SystemExit(f"{src.name}: no <body>.")

    want = set(dropped_sentences)
    state = {"seen": 0, "elems": 0, "sentences": 0}
    skipped_markup = []

    def visit(parent):
        for elem in list(parent):
            if local(elem.tag) in _LEAF_TAGS:
                text = "".join(elem.itertext()).strip()
                if not text:
                    continue
                sentences = split_sentences(text)
                first = state["seen"]
                state["seen"] += len(sentences)
                hits = [k for k in range(first, state["seen"]) if k in want]
                if not hits:
                    continue
                if len(elem) or (elem.text or "").strip() != text:
                    skipped_markup.append(text[:60])
                    continue
                keep = [s for k, s in enumerate(sentences, start=first)
                        if k not in want]
                state["sentences"] += len(hits)
                if keep:
                    elem.text = " ".join(keep)
                else:
                    parent.remove(elem)
                    state["elems"] += 1
            else:
                visit(elem)

    visit(body)

    # FB2's default namespace has to stay the default, or every reader that
    # looks for {...}body by name stops finding anything.
    ET.register_namespace("", "http://www.gribuser.ru/xml/fictionbook/2.0")
    ET.register_namespace("l", "http://www.w3.org/1999/xlink")
    dst.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                   + ET.tostring(root, encoding="unicode"),
                   encoding="utf-8")
    log(f"\nWrote {dst}")
    log(f"  {state['sentences']} sentence(s) removed, "
        f"{state['elems']} paragraph(s) emptied and dropped.")
    for text in skipped_markup:
        log(f"  Left alone (has markup inside): {text}...")


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--fb2", required=True)
    ap.add_argument("--chapter", type=int, default=0,
                    help="1-based chapter this audio file holds "
                         "(default: the only one)")
    ap.add_argument("--asr-json", default="",
                    help="cache the transcription here, and reuse it if the "
                         "file already exists")
    ap.add_argument("--model", default="medium",
                    help="faster-whisper model: tiny/base/small/medium/"
                         "large-v3 (default medium)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--language", default="ru")
    ap.add_argument("--min-run", type=int, default=MIN_RUN)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--no-audio-check", action="store_true",
                    help="skip the room measurement and report "
                         "every hole the recogniser left. Every "
                         "verdict is then unconfirmed -- check "
                         "each one by ear before trimming.")
    ap.add_argument("--trim-fb2", default="",
                    help="write a copy of the FB2 with the unspoken "
                         "sentences removed")
    a = ap.parse_args()

    fb2_path = Path(a.fb2)
    chapters = fb2_mod.extract_chapters(fb2_path)
    if a.chapter:
        chapter = chapters[a.chapter - 1]
    elif len(chapters) == 1:
        chapter = chapters[0]
    else:
        raise SystemExit(f"{fb2_path.name} has {len(chapters)} chapters; "
                         f"say which one with --chapter N.")

    cache = Path(a.asr_json) if a.asr_json else None
    if cache and cache.exists():
        heard = json.loads(cache.read_text(encoding="utf-8"))
        print(f"Reusing {len(heard)} transcribed words from {cache.name}.")
    else:
        heard = transcribe(Path(a.audio), a.model, a.device,
                           a.compute_type, a.language)
        if cache:
            cache.write_text(json.dumps(heard, ensure_ascii=False),
                             encoding="utf-8")

    book_norm, sentences, owner = index_text(chapter["text"])
    runs, marks = missing_runs(book_norm, heard, a.min_run)

    timed = sum(1 for m in marks if m)
    print(f"\n{chapter['title'][:60] or fb2_path.stem}: "
          f"{len(book_norm)} words in the book, {timed} of them heard "
          f"({100.0 * timed / len(book_norm):.0f}%).")

    if not runs:
        print("\nNothing missing. Every run of the text has a counterpart in "
              "the audio, so the whole text can be aligned.")
        return 0

    voiced, duration = (None, 0.0)
    if not a.no_audio_check:
        voiced, duration = audio_room(Path(a.audio), a.ffmpeg)
        if voiced is None:
            print("\nCould not measure the audio; reporting without the "
                  "room check, so treat every verdict as unconfirmed.")
    verdicts, spw = judge(runs, owner, heard, voiced, duration)

    print(f"\n{len(runs)} passage(s) the recogniser did not hear "
          f"(speaking at about {spw:.2f}s a word):\n")
    absent_runs = []
    touched = set()
    for lo, hi, before, after, room, absent, shaped in verdicts:
        n = hi - lo
        if absent:
            label = "ABSENT" if room is not None else "ABSENT?"
            why = (f"room for only ~{room:.0f} of {n} words"
                   if room is not None
                   else "audio not measured -- UNCONFIRMED, check by ear")
        else:
            label = "MISHEARD"
            if room is not None and room >= ROOM * n:
                why = f"room for ~{room:.0f} words, so the tape has time for it"
            elif not shaped:
                why = "starts and ends mid-sentence, which a tape edit does not"
            else:
                why = "not confirmed"
        print(f"  [{label}] {n} words at {clock(before)} -- {why}")
        print(f"     before: ...{' '.join(book_norm[max(0, lo - CONTEXT):lo])}")
        print(f"     ---->   {' '.join(book_norm[lo:hi])[:400]}")
        print(f"     after:  {' '.join(book_norm[hi:hi + CONTEXT])}...")
        print()
        if absent:
            absent_runs.append((lo, hi))
            for i in range(lo, hi):
                touched.add(owner[i])

    if not absent_runs:
        print("Nothing is missing from the recording -- every hole is the "
              "recogniser's, and the text should be aligned whole.")
        return 0

    print("Pass to make_book_run.py as:")
    print("   --drop " + ",".join(f"{lo}-{hi}" for lo, hi in absent_runs))

    # Whole sentences only: a sentence with most of its words unheard is one
    # the recording does not have, and half a sentence highlighted then
    # abandoned reads worse than none of it.
    sizes, unheard = {}, {}
    for i, si in enumerate(owner):
        sizes[si] = sizes.get(si, 0) + 1
        if marks[i] is None:
            unheard[si] = unheard.get(si, 0) + 1
    whole = sorted(si for si in touched
                   if unheard.get(si, 0) >= SENTENCE_DROP * sizes[si])
    print(f"\n{len(whole)} whole sentence(s) are absent from the recording:")
    for si in whole:
        print(f"   - {sentences[si][:110]}")

    if a.trim_fb2:
        trim_fb2(fb2_path, Path(a.trim_fb2), whole)
        print("\nCheck the trimmed file, then use it in place of the "
              "original and re-run WITHOUT --drop.")
    else:
        print("\nRe-run with --trim-fb2 OUT.fb2 to write a copy of the book "
              "with those sentences removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
