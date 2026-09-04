import logging
import time

import fal_client

log = logging.getLogger(__name__)

# George: warm resonant British male, calm and captivating — ideal for NatGeo/Discovery documentary.
# Daniel: strong British broadcast voice — best authoritative alternative.
# Rachel: warm American female — best female alternative.
_DEFAULT_VOICE = "George"


def generate_voiceover(prompt: str) -> str:
    """Generate a full-video voiceover MP3 from narration text using
    fal-ai/elevenlabs/tts/eleven-v3 (George voice).

    Args:
        prompt: The complete spoken narration for the Short — all shots
            concatenated in order as one continuous VO script. Do NOT
            include stage directions, SFX notes, or shot labels; only
            the words that should be spoken.

    Returns: URL of the generated MP3. Pass to fit_and_mux_audio()
        along with the concatenated video and background music URL."""
    log.info("[TTS] Starting voiceover generation | words=%d | prompt=%s",
             len(prompt.split()), prompt[:100])
    t0 = time.time()
    try:
        result = fal_client.subscribe(
            "fal-ai/elevenlabs/tts/eleven-v3",
            arguments={
                "text": prompt,
                "voice": _DEFAULT_VOICE,
                "stability": 0.65,
                "language_code": "en",
                "apply_text_normalization": "auto",
            },
            with_logs=True,
            on_queue_update=lambda update: None,
        )
        url = result["audio"]["url"]
        log.info("[TTS] Success | url=%s | elapsed=%.1fs", url[:80], time.time() - t0)
        return url
    except Exception as e:
        log.error("[TTS] Failed | error=%s | elapsed=%.1fs", e, time.time() - t0)
        raise
