import logging
import time

import fal_client

log = logging.getLogger(__name__)

_DEFAULT_VOICE = "Kore"
_DEFAULT_STYLE = (
    "Voice & Tone: Calmly engaging, clear, and intellectually curious. "
    "Speaks with a steady, grounding warmth and an understated sense of wonder—"
    "fascinated by the topic without sounding overly hyped or sensational. "
    "Delivery & Pacing: "
    "Rhythm: Measured and deliberate. Natural, conversational cadence with brief, "
    "micro-pauses after key hooks or intriguing facts to let the idea land. "
    "Pitch & Articulation: Mid-tone pitch, crisp articulation, and a confident, "
    "grounded delivery. Avoid high-pitched exclamations or forced excitement. "
    "Energy: Intimate and storytelling-focused, as if sharing a profound insight "
    "or hidden historical detail one-on-one."
)

_active_style = _DEFAULT_STYLE


def configure(voice_style: str):
    global _active_style
    _active_style = voice_style or _DEFAULT_STYLE


def generate_voiceover(prompt: str) -> str:
    """Generate a full-video voiceover MP3 from narration text using
    fal-ai/gemini-3.1-flash-tts (Kore voice).

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
            "fal-ai/gemini-3.1-flash-tts",
            arguments={
                "voice": _DEFAULT_VOICE,
                "prompt": prompt,
                "temperature": 1,
                "language_code": "English (US)",
                "output_format": "mp3",
                "style_instructions": _active_style,
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
