"""FFmpeg helpers: normalize audio to 16 kHz mono WAV and split large WAVs.

FFmpeg is located either next to the frozen executable (bundled by the build
script) or on PATH.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

WAV_SR = 16000

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(filename: str) -> str:
    """Turn a filename into a filesystem-safe stem (keeps a-z0-9 . _ -)."""
    stem = Path(filename).stem
    cleaned = _SAFE_RE.sub("_", stem).strip("._")
    return cleaned or "audio"


def _candidate_dirs() -> List[Path]:
    """Directories that may contain bundled ffmpeg/ffprobe."""
    dirs: List[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        dirs.append(exe_dir / "runtime" / "Library" / "bin")
        dirs.append(exe_dir / "ffmpeg" / "bin")
        dirs.append(exe_dir / "tools")
        dirs.append(exe_dir)
    return dirs


def _find_bin(name: str) -> Optional[str]:
    for d in _candidate_dirs():
        candidate = d / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    return found


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg: bundled copy first, then PATH."""
    return _find_bin("ffmpeg")


def find_ffprobe() -> Optional[str]:
    """Locate ffprobe: bundled copy first, then PATH."""
    return _find_bin("ffprobe")


def run_ffmpeg(args: List[str], ffmpeg: Optional[str] = None) -> None:
    ff = ffmpeg or find_ffmpeg()
    if not ff:
        raise RuntimeError(
            "FFmpeg was not found. Install it and put ffmpeg.exe on PATH, or "
            "re-run the build script so it is bundled next to the app."
        )
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"] + args
    subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def probe_duration(path: str) -> float:
    """Return audio duration in seconds using ffprobe."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RuntimeError("ffprobe was not found.")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
         "default=noprint_wrappers=1:nokey=1", path],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return float(out.stdout.strip())


def convert_to_wav(src: str, dst: str, ffmpeg: Optional[str] = None) -> str:
    """Convert *src* to 16 kHz mono WAV at *dst*; returns *dst*."""
    run_ffmpeg(
        ["-i", src, "-ac", "1", "-ar", str(WAV_SR), "-c:a", "pcm_s16le", dst],
        ffmpeg,
    )
    return dst


def split_wav_by_duration(src: str, out_dir: str, stem: str, chunk_seconds: float,
                          ffmpeg: Optional[str] = None) -> List[str]:
    """Split *src* into chunks of ~*chunk_seconds* and return output paths.

    Chunks are written as `<out_dir>/<stem>_NNN.wav`, ordered by start time.
    """
    pattern = str(Path(out_dir) / f"{stem}_%03d.wav")
    run_ffmpeg(
        [
            "-i", src,
            "-f", "segment",
            "-segment_time", f"{chunk_seconds:.6f}",
            "-reset_timestamps", "1",
            "-c:a", "pcm_s16le",
            "-ar", str(WAV_SR),
            "-ac", "1",
            pattern,
        ],
        ffmpeg,
    )
    chunks = sorted(Path(out_dir).glob(f"{stem}_[0-9][0-9][0-9].wav"))
    return [str(p) for p in chunks]
