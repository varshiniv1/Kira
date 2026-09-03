import logging
import os
import subprocess
import tempfile
import time as _time
import uuid

import requests

log = logging.getLogger(__name__)


def concat_videos(video_urls: list[str]) -> str:
    """Download multiple video clips and concatenate them into a single
    video file using ffmpeg. Use this after generating all shots with
    generate_video() to assemble the final YouTube Short.

    Args:
        video_urls: List of video URLs in chronological shot order
            (from generate_video calls). Must have at least 2 URLs.

    Returns: Local file path of the concatenated video (e.g.
        /tmp/kira_final_abc123.mp4). Pass this path directly to
        publish_video()."""
    log.info("[CONCAT] Starting concatenation | clips=%d", len(video_urls))
    t0 = _time.time()
    _tmp = tempfile.gettempdir()

    if len(video_urls) == 1:
        path = os.path.join(_tmp, f"kira_final_{uuid.uuid4().hex[:6]}.mp4")
        _download(video_urls[0], path)
        return _enforce_max_duration(path, max_seconds=30)

    clip_paths = []
    for i, url in enumerate(video_urls):
        path = os.path.join(_tmp, f"kira_clip_{i}_{uuid.uuid4().hex[:6]}.mp4")
        _download(url, path)
        clip_paths.append(path)

    concat_list = os.path.join(_tmp, f"kira_concat_{uuid.uuid4().hex[:6]}.txt")
    with open(concat_list, "w") as f:
        for path in clip_paths:
            f.write(f"file '{path}'\n")

    output_path = os.path.join(_tmp, f"kira_final_{uuid.uuid4().hex[:6]}.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log.error("ffmpeg concat failed (exit %s):\n%s", result.returncode,
                  result.stderr.decode(errors="replace"))
        result.check_returncode()

    # Cleanup intermediates.
    for path in clip_paths:
        os.remove(path)
    os.remove(concat_list)

    output_path = _enforce_max_duration(output_path, max_seconds=30)

    log.info("[CONCAT] Success | output=%s | elapsed=%.1fs", output_path, _time.time() - t0)
    return output_path


def _get_duration(path: str) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    r = subprocess.run(cmd, capture_output=True)
    try:
        return float(r.stdout.decode().strip())
    except ValueError:
        return 0.0


def _enforce_max_duration(path: str, max_seconds: int) -> str:
    """Trim video to max_seconds if it exceeds that length. Returns path."""
    duration = _get_duration(path)
    if duration <= max_seconds:
        return path
    log.warning("[CONCAT] Duration %.1fs exceeds %ds cap — trimming", duration, max_seconds)
    trimmed = os.path.join(tempfile.gettempdir(), f"kira_trimmed_{uuid.uuid4().hex[:6]}.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", path,
        "-t", str(max_seconds),
        "-c", "copy",
        trimmed,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        log.error("[CONCAT] Trim failed, keeping original: %s", r.stderr.decode(errors="replace"))
        return path
    os.remove(path)
    return trimmed


def _download(url: str, dest: str) -> None:
    """Download a file from URL using requests (bundled SSL certs)."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)
