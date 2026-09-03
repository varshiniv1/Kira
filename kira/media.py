"""Upload media files (videos, images) to GCS and return public URLs.

Reuses the GCS_BUCKET_NAME env var from storage.py. Files are stored
under a media/ prefix with unique names to avoid collisions.
"""

import logging
import os
import uuid
from datetime import timedelta

log = logging.getLogger(__name__)

_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
_bucket = None


def _get_bucket():
    global _bucket
    if _bucket is None:
        try:
            from google.cloud import storage
            _bucket = storage.Client().bucket(_BUCKET_NAME)
        except Exception as e:
            log.warning("[MEDIA] GCS client init failed: %s", e)
            return None
    return _bucket


def is_enabled() -> bool:
    return bool(_BUCKET_NAME)


def upload_video(local_path: str, filename: str = "") -> str:
    """Upload a video file to GCS and return its public URL.

    Args:
        local_path: Path to the local video file.
        filename: Optional filename override. Auto-generated if empty.

    Returns: Public HTTPS URL of the uploaded video.
    """
    if not _BUCKET_NAME:
        log.warning("[MEDIA] GCS_BUCKET_NAME not set — skipping upload")
        return ""

    if not filename:
        ext = os.path.splitext(local_path)[1] or ".mp4"
        filename = f"kira_{uuid.uuid4().hex[:8]}{ext}"

    blob_name = f"media/videos/{filename}"
    bucket = _get_bucket()
    if bucket is None:
        log.warning("[MEDIA] GCS unavailable — skipping video upload")
        return ""
    blob = bucket.blob(blob_name)

    log.info("[MEDIA] Uploading video | path=%s | blob=%s", local_path, blob_name)
    blob.upload_from_filename(local_path, content_type="video/mp4")
    blob.make_public()
    url = f"https://storage.googleapis.com/{_BUCKET_NAME}/{blob_name}"
    log.info("[MEDIA] Upload complete | url=%s", url)
    return url


def upload_image(local_path: str, filename: str = "") -> str:
    """Upload an image file to GCS and return its public URL."""
    if not _BUCKET_NAME:
        return ""

    if not filename:
        ext = os.path.splitext(local_path)[1] or ".png"
        filename = f"kira_{uuid.uuid4().hex[:8]}{ext}"

    blob_name = f"media/images/{filename}"
    bucket = _get_bucket()
    if bucket is None:
        log.warning("[MEDIA] GCS unavailable — skipping image upload")
        return ""
    blob = bucket.blob(blob_name)

    log.info("[MEDIA] Uploading image | path=%s | blob=%s", local_path, blob_name)
    blob.upload_from_filename(local_path)
    blob.make_public()
    url = f"https://storage.googleapis.com/{_BUCKET_NAME}/{blob_name}"
    log.info("[MEDIA] Upload complete | url=%s", url)
    return url
