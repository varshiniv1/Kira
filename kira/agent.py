import json
import logging
import os

import anthropic
from anthropic.types import TextBlock
from google.adk.agents import LlmAgent


def _extract_text(resp) -> str:
    """Extract the text content from a Claude response, skipping ThinkingBlocks."""
    for block in resp.content:
        if isinstance(block, TextBlock):
            return block.text
    return "".join(b.text for b in resp.content if hasattr(b, "text"))
from google.adk.models.llm_response import LlmResponse
from google.genai import types

log = logging.getLogger(__name__)

from .tools.memory import read_memory, write_memory
from .tools import memory as memory_mod
from .tools.trends import search_youtube_trends, search_google_trends, web_search
from .tools import trends
from .tools.image_gen import generate_image
from .tools.video_gen import generate_video
from .tools.concat_videos import concat_videos
from .tools.tts import generate_voiceover
from .tools import tts
from .tools.background_music import generate_background_music
from .tools import background_music
from .tools.mux_voiceover import fit_and_mux_audio, mux_music_only
from .tools.youtube import publish_video

ADK_MODEL = "anthropic/claude-sonnet-5"
CLAUDE_MODEL = "claude-sonnet-5"

_PRODUCTION_START_PHRASES = [
    "making your video now",
    "making your video",
    "i'll send the link when it's done",
    "you don't need to do anything",
    "starting production now",
    "i am making your video",
]


def _force_transfer_after_model(callback_context, llm_response):
    """Intercept kira's response: if it signals production start but forgot
    to call transfer_to_agent, inject the function call automatically."""
    if not llm_response or not llm_response.content or not llm_response.content.parts:
        return None

    parts = llm_response.content.parts
    has_production_signal = False
    has_transfer = False

    for p in parts:
        if hasattr(p, "text") and p.text:
            text_lower = p.text.lower()
            if any(phrase in text_lower for phrase in _PRODUCTION_START_PHRASES):
                has_production_signal = True
        if hasattr(p, "function_call") and p.function_call:
            if p.function_call.name == "transfer_to_agent":
                has_transfer = True

    if has_production_signal and not has_transfer:
        log.info("[CALLBACK] Model signaled production start without transfer — injecting transfer_to_agent")
        new_parts = list(parts) + [
            types.Part(function_call=types.FunctionCall(
                name="transfer_to_agent",
                args={"agent_name": "execution_agent"},
            ))
        ]
        return LlmResponse(
            content=types.Content(role="model", parts=new_parts),
        )

    return None

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(filename: str) -> str:
    with open(os.path.join(PROMPTS_DIR, filename)) as f:
        return f.read()


def _build_execution_prompt(block_config: dict) -> str:
    """Build execution agent prompt from the shared template, adjusted
    for the block's narration/caption/duration settings."""
    base = _load_prompt("execution_agent.md")

    dur_min = block_config.get("duration_min", 35)
    dur_max = block_config.get("duration_max", 45)
    base = base.replace("15-20 seconds", f"{dur_min}-{dur_max} seconds")
    base = base.replace("15-20 s", f"{dur_min}-{dur_max} s")

    narration = block_config.get("narration_enabled", True)
    captions = block_config.get("captions_enabled", True)

    if not narration:
        # Replace audio phase instructions for music-only
        base = base.replace(
            "## PHASE 5 — AUDIO\n"
            "\n"
            "1. Call generate_voiceover() with the VOICEOVER PROMPT from the\n"
            "   production plan (full narration only — the spoken words, nothing\n"
            "   else).\n"
            "\n"
            "2. Call generate_background_music() with:\n"
            "   - video_path: the concatenated video path from Phase 4\n"
            "\n"
            "   This generates ambient music matched to the video length (random\n"
            "   seed each run).\n"
            "\n"
            "3. Call fit_and_mux_audio() with:\n"
            "   - video_path: the concatenated video path from Phase 4\n"
            "   - voiceover_url: the MP3 URL from generate_voiceover()\n"
            "   - music_url: the MP3 URL from generate_background_music()\n"
            "   - script: the exact same VOICEOVER PROMPT text passed to\n"
            "     generate_voiceover() in step 1 — used to snap burned-in captions\n"
            "     to the approved narration instead of raw speech-to-text.\n"
            "\n"
            "   This discards clip audio, speed-fits TTS and music to the video\n"
            "   duration, burns in synced captions, and mixes the audio (VO\n"
            "   dominant, music quiet). Use the returned path as the final video.",
            "## PHASE 5 — AUDIO\n"
            "\n"
            "1. Call generate_background_music() with:\n"
            "   - video_path: the concatenated video path from Phase 4\n"
            "\n"
            "   This generates ambient music matched to the video length.\n"
            "\n"
            "2. Call mux_music_only() with:\n"
            "   - video_path: the concatenated video path from Phase 4\n"
            "   - music_url: the MP3 URL from generate_background_music()\n"
            "\n"
            "   This discards clip audio, speed-fits music to the video duration,\n"
            "   and mixes it in. Use the returned path as the final video.\n"
            "\n"
            "   There is NO voiceover or captions for this content block.",
        )

    if not captions and narration:
        base = base.replace(
            "burns in synced captions, and mixes the audio",
            "and mixes the audio (captions are disabled for this block)",
        )

    return base


def build_agents(block_config: dict, block_path: str) -> LlmAgent:
    """Build the full agent tree from a content block's config and prompts."""
    log.info("[AGENTS] Building agent tree | block=%s | narration=%s | trends=%s",
             block_config.get("name"), block_config.get("narration_enabled"),
             block_config.get("youtube_trends_enabled"))

    def load_block_prompt(filename: str) -> str:
        with open(os.path.join(block_path, filename)) as f:
            return f.read()

    # Configure tools with block-specific settings
    trends.configure(
        seed_keywords=block_config.get("seed_keywords", []),
        noise_terms=block_config.get("noise_terms", []),
        niche_description=block_config.get("description", block_config.get("name", "")),
        enabled=block_config.get("youtube_trends_enabled", True),
    )
    tts.configure(block_config.get("voice_style", ""))
    background_music.configure(block_config.get("music_style", ""))
    memory_mod.configure(block_path)

    narration_enabled = block_config.get("narration_enabled", True)

    # ── Script Writer & Production Planner as tool functions ──
    # These were previously sub-agents, but transfer_to_agent is a
    # one-way handoff in ADK — the execution_agent never resumes
    # after the sub-agent finishes.  By making them tool functions
    # that call the LLM internally, execution_agent stays in control.
    _script_prompt = load_block_prompt("script_writer.md")
    _planner_prompt = load_block_prompt("production_breakdown.md")
    _anthropic_client = anthropic.Anthropic()

    def write_script(creative_brief: str) -> str:
        """Write a production-ready script for a YouTube Short.
        Takes the full creative brief (topic, hook fact, trending reason,
        source) and returns a complete script with beats, narration,
        visuals, and audio design. Call this FIRST."""
        log.info("[TOOL] write_script | brief_len=%d", len(creative_brief))
        resp = _anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16384,
            system=_script_prompt,
            messages=[{"role": "user", "content": creative_brief}],
        )
        result = _extract_text(resp)
        log.info("[TOOL] write_script complete | result_len=%d", len(result))
        return result

    def plan_production(script: str) -> str:
        """Plan the shot-by-shot production breakdown for a finished script.
        Returns shot count (based on narration duration), starting image
        prompts, video prompts, continuity notes, and a single VOICEOVER
        PROMPT for TTS. Call AFTER write_script."""
        log.info("[TOOL] plan_production | script_len=%d", len(script))
        resp = _anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16384,
            system=_planner_prompt,
            messages=[{"role": "user", "content": script}],
        )
        result = _extract_text(resp)
        log.info("[TOOL] plan_production complete | result_len=%d", len(result))
        return result

    # ── Execution agent tools ────────────────────────────────
    exec_tools = [
        write_script,
        plan_production,
        generate_image,
        generate_video,
        concat_videos,
        generate_background_music,
        publish_video,
        write_memory,
    ]
    if narration_enabled:
        exec_tools.insert(5, generate_voiceover)
        exec_tools.append(fit_and_mux_audio)
    else:
        exec_tools.append(mux_music_only)

    execution_prompt = _build_execution_prompt(block_config)

    execution_agent = LlmAgent(
        name="execution_agent",
        model=ADK_MODEL,
        description=(
            "Production agent that takes a confirmed creative brief and "
            "autonomously produces the final video: writes a script, plans "
            "shots, generates starting images, generates multi-shot video, "
            "concatenates clips, "
            + ("generates TTS voiceover and " if narration_enabled else "")
            + "background music, muxes audio, publishes the video, and saves "
            "the result to memory. "
            "Transfer to this agent ONLY after the user has confirmed "
            "the topic and creative brief."
        ),
        instruction=execution_prompt,
        tools=exec_tools,
    )

    # ── Root agent tools ─────────────────────────────────────
    root_tools = [web_search, read_memory, write_memory]
    if block_config.get("youtube_trends_enabled", True):
        root_tools = [search_youtube_trends, search_google_trends] + root_tools

    root_agent = LlmAgent(
        name="kira",
        model=ADK_MODEL,
        description=f"Kira — autonomous content strategist for: {block_config['name']}.",
        instruction=load_block_prompt("research_agent.md"),
        tools=root_tools,
        sub_agents=[execution_agent],
        after_model_callback=_force_transfer_after_model,
    )

    log.info("[AGENTS] Agent tree built | root=%s | sub_agents=[execution_agent] "
             "| root_tools=%s | exec_tools=%d",
             root_agent.name, [t.__name__ for t in root_tools], len(exec_tools))
    return root_agent


# ── Default agent (built on import for backward compatibility) ───

def _load_default_agent() -> LlmAgent:
    from . import block_manager
    try:
        block_id = block_manager.get_active_block_id()
        if block_id:
            config = block_manager.get_block(block_id)
            path = block_manager.get_block_path(block_id)
            return build_agents(config, path)
    except Exception:
        pass

    # Fallback: build from legacy prompts dir (should not normally happen)
    from .tools.trends import configure as trends_configure
    trends_configure(
        seed_keywords=["black hole", "asteroid", "rocket launch", "james webb",
                       "supernova", "exoplanet", "mars planet", "meteorite",
                       "nebula", "dark matter", "space exploration", "moon landing",
                       "solar system"],
        noise_terms=["samsung", "mario", "game", "fortnite", "minecraft"],
        niche_description="space, cosmos, and the universe",
        enabled=True,
    )
    fallback_config = {
        "name": "Space & Cosmos",
        "narration_enabled": True,
        "captions_enabled": True,
        "youtube_trends_enabled": True,
        "duration_min": 35,
        "duration_max": 45,
    }
    # Use prompts dir as block path (has the same .md files)
    return build_agents(fallback_config, PROMPTS_DIR)


root_agent = _load_default_agent()
