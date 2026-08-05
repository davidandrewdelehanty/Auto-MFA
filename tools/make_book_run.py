"""Generate everything needed to align, upload and install ONE book.

The GUI does this interactively, a book at a time, with the pairing done by
hand. This does the same for a book whose pairing needs no judgement --
where the audio files and the FB2's chapters correspond one to one, in
order -- and chains the three generated scripts into a single command, so a
book can be run start to finish without sitting in front of it.

    python3 tools/make_book_run.py \\
        --folder "/mnt/c/Users/david/Downloads/audiobooks/<book>" \\
        --slug moya-strana --r2-folder moya-strana \\
        --repo /mnt/c/Users/david/projects/govorim-app \\
        --work /mnt/c/Users/david/Downloads/audiobooks/_runs

Writes into <work>/<slug>/ and prints the one command to run.

Audio is staged into <work>/<slug>/audio as 01.mp3, 02.mp3, ... rather than
being read from the book folder directly. The staged name is what ends up
in every chapter's audio_url and therefore in the R2 bucket, and the
originals are routinely named things like "chapter 01 Moya lyubimaya
strana.mp3" -- legal, but it turns every URL into a percent-encoded mouthful
and makes the bucket hard to read. Staging copies rather than renames: the
originals are the only copy of the recording and this tool does not touch
them.
"""

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import fb2, scriptgen  # noqa: E402
from app.fb2 import transcript_words  # noqa: E402
from app.govorim import DEFAULT_R2_BASE  # noqa: E402
from app import __version__  # noqa: E402


def build(folder: Path, slug: str, r2_folder: str, repo: str, work: Path,
          title: str = "", author: str = "", narrator: str = "audiobook",
          num_jobs: int = 2, as_folder: str = "",
          project: str = "", groups=None) -> Path:
    """Generate the run for the book in *folder*.

    *as_folder*, when given, is the path written INTO the generated scripts
    in place of *folder*: it lets the run be generated on one machine from
    a copy of the book folder and executed on another, where the same book
    lives at a different path. Everything else is read from *folder* as
    normal, so the chapter list and the file names still come from the real
    book rather than being taken on trust. *project* does the same for this
    Auto-MFA checkout's own path, which the alignment script has to cd into.
    """
    fb2_path = fb2.find_fb2(folder)
    chapters = fb2.extract_chapters(fb2_path)
    audio = fb2.find_audio_files(folder)
    src_dir = PurePosixPath(as_folder) if as_folder else folder

    if groups:
        if len(groups) != len(audio):
            raise SystemExit(
                f"{folder.name}: --groups lists {len(groups)} group(s) but "
                f"there are {len(audio)} audio file(s).")
        if sum(groups) != len(chapters):
            raise SystemExit(
                f"{folder.name}: --groups covers {sum(groups)} chapter(s) but "
                f"the FB2 has {len(chapters)}.")
    elif len(audio) != len(chapters):
        raise SystemExit(
            f"{folder.name}: {len(audio)} audio file(s) but {len(chapters)} "
            f"chapter(s).\n"
            f"Pass --groups to say how the chapters divide between the files "
            f"(e.g. --groups 16,12,10,12 for a novel recorded one file per "
            f"part), or pair this one in the GUI."
        )

    meta = fb2.extract_metadata(fb2_path)
    title = title or meta.get("title") or slug
    author = author or meta.get("author") or ""

    out = Path(work) / slug
    tmp_dir = Path(work) / "_tmp"
    staged_dir = out / "audio"
    json_dir = out / "json"
    out.mkdir(parents=True, exist_ok=True)

    # Staged names, in pairing order. Width follows the count so 100 sorts
    # after 99 in the bucket listing as well as in the pairing.
    width = max(2, len(str(len(audio))))
    staged = [f"{i:0{width}d}{src.suffix.lower()}"
              for i, src in enumerate(audio, start=1)]

    # One pair per audio file. With --groups a pair spans several chapters:
    # the pipeline aligns the file whole and then cuts it back into one
    # clip -- and one JSON -- per chapter, using each chapter's exact word
    # count to find the boundaries. That is the shape the GUI produces for a
    # multi-chapter pairing, built here from the group sizes instead of by
    # hand. See Pair.sub_chapters in pipeline.py.
    spans = []
    at = 0
    for size in (groups or [1] * len(chapters)):
        spans.append(chapters[at:at + size])
        at += size

    pairs = []
    for name, span in zip(staged, spans):
        pair = {"audio": str(staged_dir / name),
                "title": span[0]["title"] if len(span) == 1
                         else f"{span[0]['title']} - {span[-1]['title']}",
                "text": " ".join(c["text"] for c in span)}
        if len(span) > 1:
            pair["sub_chapters"] = [[c["title"], len(transcript_words(c["text"]))]
                                    for c in span]
            pair["sub_texts"] = [c["text"] for c in span]
        pairs.append(pair)

    job = {
        "pairs": pairs,
        "output_dir": str(json_dir),
        "govorim_slug": slug,
        "r2_folder": r2_folder,
        "num_jobs": num_jobs,
        "acoustic_model": "russian_mfa",
        "dictionary": "russian_mfa",
    }
    job_path = out / f"{slug}.job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # What gets uploaded differs by mode. One file per chapter: the staged
    # audio IS the chapter audio. Grouped: the staged files are whole parts,
    # and what the app needs is the per-chapter clips the pipeline cuts out
    # of them during alignment -- which don't exist until it has run, so the
    # list is built at that point rather than now.
    upload_src = staged_dir if not groups else (json_dir / "audio")
    list_path = out / "upload_list.txt"

    project = project or str(Path(__file__).resolve().parent.parent)
    (out / f"align_{slug}.sh").write_text(
        scriptgen.build_script(slug, str(job_path), project, __version__),
        encoding="utf-8")
    (out / f"upload_{slug}.sh").write_text(
        scriptgen.build_upload_script(r2_folder, str(upload_src),
                                      str(list_path), version=__version__),
        encoding="utf-8")
    (out / f"install_{slug}.sh").write_text(
        scriptgen.build_install_script(
            slug, repo, str(src_dir / fb2_path.name), str(json_dir), title=title,
            author=author, narrator=narrator, r2_folder=r2_folder,
            r2_base=DEFAULT_R2_BASE, version=__version__),
        encoding="utf-8")

    # Staging is a script step rather than something done here: it copies
    # gigabytes, and it belongs in the same log as everything else. The
    # source/target pairs go in their own tab-separated file instead of one
    # cp line per file -- War and Peace would otherwise put 361 copy
    # commands in the middle of the script.
    stage_list = out / "stage_list.txt"
    stage_list.write_text(
        "".join(f"{src_dir / src.name}\t{name}\n"
                for src, name in zip(audio, staged)),
        encoding="utf-8")

    run_path = out / f"run_{slug}.sh"
    run_path.write_text(f"""#!/usr/bin/env bash
# Auto-MFA -- run '{slug}' end to end
# Generated by Auto-MFA v{__version__}.
#
#   {scriptgen.run_command_for(str(run_path))}
#
# Four steps, in order: stage the audio under short names, align it against
# the FB2, upload the audio to R2, install the result into the Govorim app.
# Every step is safe to re-run; staging and uploading skip what's already
# there, and aligning simply overwrites its own output.
#
# Progress goes to the terminal AND to {out.name}/run.log, so a run left
# overnight can be read back in the morning.

set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a run.log) 2>&1

echo "=================================================================="
echo "{title}"
echo "{len(audio)} audio file(s) -> {len(chapters)} chapter(s)   slug={slug}"
echo "started $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================================="

STAGE={scriptgen.shell_quote(str(staged_dir))}
mkdir -p "$STAGE"

# Working space for the alignment. Every hour of audio becomes roughly
# 230 MB of temporary WAV -- a full-length decode plus the per-utterance
# cuts, both alive at once -- so a long book needs several GB. The default
# would be /tmp, which under WSL is the Linux VM's own disk rather than the
# drive the audio came from; Идиот's 27 hours filled it mid-run.
export TMPDIR="${{AUTO_MFA_TMPDIR:-{tmp_dir}}}"
mkdir -p "$TMPDIR"
echo "working space: $TMPDIR ($(df -h "$TMPDIR" | awk 'NR==2 {{print $4}}') free)"

echo
echo "--- 1/4  staging audio ------------------------------------------"
# Tested by size rather than with `cp -n`: coreutils 9 warns on every
# single -n invocation that its behaviour may change, which buries the
# actual progress under one warning per file. Comparing sizes rather than
# just testing existence matters because this step gets interrupted -- it
# copies gigabytes and it is the first thing that runs -- and a half-copied
# file left behind by a Ctrl-C would otherwise be skipped for good and
# aligned as a truncated chapter.
STAGE_LIST={scriptgen.shell_quote(str(stage_list))}
total=$(grep -c . "$STAGE_LIST")
n=0
while IFS=$'\t' read -r src dst; do
    n=$((n + 1))
    want=$(stat -c %s "$src")
    have=$(stat -c %s "$STAGE/$dst" 2>/dev/null || echo -1)
    if [ "$have" = "$want" ]; then
        printf '  [%d/%d] %s -- already staged\n' "$n" "$total" "$dst"
        continue
    fi
    if [ "$have" != "-1" ]; then
        printf '  [%d/%d] %s -- incomplete (%s of %s bytes), recopying ... ' \
            "$n" "$total" "$dst" "$have" "$want"
    else
        printf '  [%d/%d] %s ... ' "$n" "$total" "$dst"
    fi
    cp "$src" "$STAGE/$dst"
    printf 'done\n'
done < "$STAGE_LIST"
echo "staged $(ls -1 "$STAGE" | wc -l) file(s) in $STAGE"

echo
echo "--- 2/4  aligning (this is the long one) ------------------------"
echo "MFA is quiet for the first minute or two while it loads the acoustic"
echo "model and builds the corpus. That is normal -- it is not stuck."
bash {scriptgen.shell_quote(str(out / f'align_{slug}.sh'))}

echo
echo "--- 3/4  uploading audio to R2 ----------------------------------"
UPLOAD_SRC={scriptgen.shell_quote(str(upload_src))}
ls -1 "$UPLOAD_SRC" > {scriptgen.shell_quote(str(list_path))}
echo "uploading $(grep -c . {scriptgen.shell_quote(str(list_path))}) file(s) from $UPLOAD_SRC"
bash {scriptgen.shell_quote(str(out / f'upload_{slug}.sh'))}

echo
echo "--- 4/4  installing into Govorim --------------------------------"
bash {scriptgen.shell_quote(str(out / f'install_{slug}.sh'))}

echo
echo "=================================================================="
echo "'{slug}' done at $(date '+%Y-%m-%d %H:%M:%S')"
echo
echo "Changed in {repo}:"
echo "    public/books/novel/{slug}.fb2"
echo "    public/books/audio/{slug}/"
echo "    public/books/index.json"
echo "Review and publish those, then hard-refresh the site (Ctrl+Shift+R)."
echo "=================================================================="
""", encoding="utf-8")

    print(f"Book:     {title}" + (f" -- {author}" if author else ""))
    print(f"FB2:      {fb2_path.name}")
    if groups:
        print(f"Chapters: {len(chapters)}   Audio: {len(audio)}   "
              f"(grouped {','.join(str(g) for g in groups)})")
    else:
        print(f"Chapters: {len(chapters)}   Audio: {len(audio)}   (paired in order)")
    print(f"Written:  {out}")
    print()
    print("Run it with:")
    print(f"    {scriptgen.run_command_for(str(run_path))}")
    return run_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--r2-folder", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--narrator", default="audiobook")
    ap.add_argument("--num-jobs", type=int, default=2)
    ap.add_argument("--as-folder", default="",
                    help="path to write into the generated scripts instead "
                         "of --folder (for generating on one machine and "
                         "running on another)")
    ap.add_argument("--project", default="",
                    help="path of the Auto-MFA checkout on the machine that "
                         "will RUN the scripts (defaults to this one)")
    ap.add_argument("--groups", default="",
                    help="comma-separated chapter counts, one per audio file, "
                         "for a book recorded a part at a time "
                         "(e.g. 16,12,10,12). Omit when each file is one "
                         "chapter.")
    a = ap.parse_args()
    build(Path(a.folder), a.slug, a.r2_folder, a.repo, Path(a.work),
          title=a.title, author=a.author, narrator=a.narrator,
          num_jobs=a.num_jobs, as_folder=a.as_folder, project=a.project,
          groups=[int(x) for x in a.groups.split(",") if x.strip()] or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
