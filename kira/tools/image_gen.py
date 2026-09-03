import logging
import os
import tempfile
import time
import uuid

import fal_client
import requests

from .. import media as media_mod

log = logging.getLogger(__name__)


def generate_image(prompt: str) -> str:
    """Generate a reference image from a detailed text prompt using
    Nano Banana Pro on fal.ai. Always include '9:16 vertical' in the
    prompt for YouTube Shorts format. Returns a URL that can be passed
    to generate_video()."""
    log.info("[IMAGE_GEN] Starting image generation | prompt=%s", prompt[:120])
    t0 = time.time()
    try:
        result = fal_client.subscribe(
            "fal-ai/nano-banana-pro",
            arguments={
                "prompt": prompt,
                "num_images": 1,
                "resolution": "1K",
                "aspect_ratio": "9:16",
                "output_format": "png",
            },
            with_logs=True,
            on_queue_update=lambda update: None,
        )

        images = result.get("images") or []
        if not images or not images[0].get("url"):
            log.warning("[IMAGE_GEN] No image returned | elapsed=%.1fs", time.time() - t0)
            return "ERROR: No image was generated. Try a different prompt."

        url = images[0]["url"]
        log.info("[IMAGE_GEN] Success | url=%s | elapsed=%.1fs", url[:80], time.time() - t0)

        # Persist to GCS so the URL survives after Fal.ai expires it
        if media_mod.is_enabled():
            try:
                gcs_url = _persist_to_gcs(url)
                log.info("[IMAGE_GEN] Persisted to GCS | gcs_url=%s", gcs_url)
            except Exception as e:
                log.warning("[IMAGE_GEN] GCS persist failed (using fal URL): %s", e)

        return url
    except Exception as e:
        log.error("[IMAGE_GEN] Failed | error=%s | elapsed=%.1fs", e, time.time() - t0)
        raise


def _persist_to_gcs(fal_url: str) -> str:
    """Download from Fal.ai and re-upload to GCS."""
    tmp = os.path.join(tempfile.gettempdir(), f"kira_img_{uuid.uuid4().hex[:6]}.png")
    resp = requests.get(fal_url, timeout=60)
    resp.raise_for_status()
    with open(tmp, "wb") as f:
        f.write(resp.content)
    gcs_url = media_mod.upload_image(tmp)
    os.remove(tmp)
    return gcs_url
