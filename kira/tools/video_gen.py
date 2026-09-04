import logging
import os
import tempfile
import time
import uuid

import fal_client
import requests

from .. import media as media_mod

log = logging.getLogger(__name__)


def generate_video(image_url: str, prompt: str, duration: int = 5) -> str:
    """Generate a video clip from a starting image using Minimax H3 Max
    Turbo image-to-video on fal.ai.

    Args:
        image_url: URL of the starting image (from generate_image).
            The video will animate from this image.
        prompt: Motion and visual prompt. Describe camera movement and
            subject action only — no audio, SFX, or narration (clip
            audio is discarded; TTS and background music are added
            later). Always specify 9:16 vertical.
        duration: Clip length in seconds (5 or 6). Use the value from
            the production plan.

    Returns: URL of the generated video clip."""
    duration_int = max(5, min(int(duration), 6))

    log.info("[VIDEO_GEN] Starting video generation | image=%s | duration=%ds | prompt=%s",
             image_url[:80], duration_int, prompt[:120])
    t0 = time.time()
    try:
        result = fal_client.subscribe(
            "minimax/h3-max/image-to-video",
            arguments={
                "image_url": image_url,
                "prompt": prompt,
                "duration": duration_int,
                "resolution": "768P",
                "prompt_expansion_mode": "quality",
            },
            with_logs=True,
            on_queue_update=lambda update: None,
        )
        url = result["video"]["url"]
        log.info("[VIDEO_GEN] Success | url=%s | elapsed=%.1fs", url[:80], time.time() - t0)

        # Persist to GCS so the URL survives after Fal.ai expires it
        if media_mod.is_enabled():
            try:
                gcs_url = _persist_to_gcs(url)
                log.info("[VIDEO_GEN] Persisted to GCS | gcs_url=%s", gcs_url)
            except Exception as e:
                log.warning("[VIDEO_GEN] GCS persist failed (using fal URL): %s", e)

        return url
    except Exception as e:
        log.error("[VIDEO_GEN] Failed | error=%s | elapsed=%.1fs", e, time.time() - t0)
        raise


def _persist_to_gcs(fal_url: str) -> str:
    """Download from Fal.ai and re-upload to GCS."""
    tmp = os.path.join(tempfile.gettempdir(), f"kira_clip_{uuid.uuid4().hex[:6]}.mp4")
    resp = requests.get(fal_url, timeout=120)
    resp.raise_for_status()
    with open(tmp, "wb") as f:
        f.write(resp.content)
    gcs_url = media_mod.upload_video(tmp)
    os.remove(tmp)
    return gcs_url
