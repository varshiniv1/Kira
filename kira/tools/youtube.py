import base64
import json
import logging
import os
import pickle
import tempfile
import time as _time
import urllib.request

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .. import media as media_mod

log = logging.getLogger(__name__)

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "token.pickle")

_current_user_phone: str = ""
_OWNER_NUMBERS: set[str] = {
    "whatsapp:+919840733969",
    "whatsapp:+14132106772",
}


def configure(phone: str) -> None:
    global _current_user_phone
    _current_user_phone = phone


def _is_owner() -> bool:
    return not _current_user_phone or _current_user_phone in _OWNER_NUMBERS


def _has_youtube_creds() -> bool:
    if os.environ.get("YOUTUBE_TOKEN_JSON"):
        return True
    return os.path.isfile(TOKEN_FILE)


def _load_credentials():
    """Load YouTube OAuth credentials.

    On Cloud Run the filesystem is ephemeral, so the token is passed as
    a base64-encoded JSON string (the output of `token.json`, base64'd)
    via YOUTUBE_TOKEN_JSON. Locally, falls back to the token.pickle file
    produced by the OAuth flow.
    """
    token_json = os.environ.get("YOUTUBE_TOKEN_JSON")
    if token_json:
        info = json.loads(base64.b64decode(token_json))
        return Credentials.from_authorized_user_info(info)
    with open(TOKEN_FILE, "rb") as f:
        return pickle.load(f)


def _upload_to_youtube_inner(local_path: str, title: str, description: str) -> str:
    """Upload a local video file to YouTube. Returns video ID."""
    creds = _load_credentials()
    if creds.expired and creds.refresh_token:
        log.info("[YOUTUBE] Refreshing OAuth credentials...")
        creds.refresh(Request())
        if not os.environ.get("YOUTUBE_TOKEN_JSON"):
            with open(TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)

    youtube = build("youtube", "v3", credentials=creds)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "private",
                "containsSyntheticMedia": True,
            },
        },
        media_body=MediaFileUpload(local_path, resumable=True),
    )
    response = request.execute()
    return response["id"]


def publish_video(video_url: str, title: str, description: str) -> dict:
    """Upload the final video to cloud storage and optionally to YouTube.

    The video is always uploaded to Google Cloud Storage for a shareable
    public link. If YouTube credentials are configured, it is also
    uploaded to YouTube as a private Short.

    Args:
        video_url: URL or local path of the final video (from mux step).
        title: Video title, under 60 characters, include #Shorts.
        description: Video description with source citation.

    Returns: dict with keys:
        - gcs_url: Public GCS link (always present when GCS configured)
        - youtube_url: YouTube Shorts link (present when YT configured)
        - video_id: YouTube video ID (present when YT configured)
    """
    log.info("[PUBLISH] Starting | title=%s | source=%s", title, video_url[:80])
    t0 = _time.time()

    if os.path.isfile(video_url):
        local_path = video_url
    else:
        local_path = os.path.join(tempfile.gettempdir(), "kira_upload.mp4")
        log.info("[PUBLISH] Downloading video from URL...")
        urllib.request.urlretrieve(video_url, local_path)
        log.info("[PUBLISH] Download complete | elapsed=%.1fs", _time.time() - t0)

    result = {}

    if media_mod.is_enabled():
        gcs_url = media_mod.upload_video(local_path)
        if gcs_url:
            result["gcs_url"] = gcs_url
            log.info("[PUBLISH] GCS upload done | url=%s", gcs_url)

    if _has_youtube_creds() and _is_owner():
        try:
            video_id = _upload_to_youtube_inner(local_path, title, description)
            result["video_id"] = video_id
            result["youtube_url"] = f"https://youtube.com/shorts/{video_id}"
            log.info("[PUBLISH] YouTube upload done | video_id=%s | elapsed=%.1fs",
                     video_id, _time.time() - t0)
        except Exception as e:
            log.error("[PUBLISH] YouTube upload failed (GCS still available): %s", e)
            if "gcs_url" not in result:
                raise
    elif not _is_owner():
        log.info("[PUBLISH] Non-owner user — skipping YouTube upload | phone=%s",
                 _current_user_phone)
    elif not result.get("gcs_url"):
        log.warning("[PUBLISH] No cloud storage available — returning local path as fallback")
        result["gcs_url"] = f"file://{local_path}"

    log.info("[PUBLISH] Complete | result=%s | elapsed=%.1fs", result, _time.time() - t0)
    return result


# Keep backward-compat alias for any direct imports
upload_to_youtube = publish_video
