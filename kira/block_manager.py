"""Content Block management — CRUD operations and meta-LLM generation.

A Content Block is a self-contained directory holding all theme-specific
prompts and config for a particular video niche (e.g. "Space & Cosmos").
The production pipeline is content-agnostic; blocks supply the *what*
while the pipeline handles the *how*.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone

BLOCKS_DIR = os.path.join(os.path.dirname(__file__), "..", "content_blocks")
ACTIVE_FILE = os.path.join(BLOCKS_DIR, ".active")

PROMPT_FILES = [
    "research_agent.md",
    "script_writer.md",
    "production_breakdown.md",
]


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "unnamed-block"


def _ensure_blocks_dir():
    os.makedirs(BLOCKS_DIR, exist_ok=True)


# ── Read operations ──────────────────────────────────────────────


def list_blocks() -> list[dict]:
    """Return a list of {id, name, description} for every block."""
    _ensure_blocks_dir()
    blocks = []
    active_id = get_active_block_id()
    for entry in sorted(os.listdir(BLOCKS_DIR)):
        config_path = os.path.join(BLOCKS_DIR, entry, "config.json")
        if os.path.isfile(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            blocks.append({
                "id": cfg["id"],
                "name": cfg["name"],
                "description": cfg.get("description", ""),
                "active": cfg["id"] == active_id,
            })
    return blocks


def get_block(block_id: str) -> dict:
    """Return the full config dict for a block, or raise FileNotFoundError."""
    config_path = os.path.join(BLOCKS_DIR, block_id, "config.json")
    with open(config_path) as f:
        return json.load(f)


def get_block_path(block_id: str) -> str:
    path = os.path.join(BLOCKS_DIR, block_id)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Block not found: {block_id}")
    return path


def get_active_block_id() -> str | None:
    if os.path.isfile(ACTIVE_FILE):
        with open(ACTIVE_FILE) as f:
            block_id = f.read().strip()
        if block_id and os.path.isdir(os.path.join(BLOCKS_DIR, block_id)):
            return block_id
    blocks = list_blocks_raw()
    return blocks[0] if blocks else None


def list_blocks_raw() -> list[str]:
    """Return block IDs (directory names) without loading configs."""
    _ensure_blocks_dir()
    return sorted(
        entry for entry in os.listdir(BLOCKS_DIR)
        if os.path.isfile(os.path.join(BLOCKS_DIR, entry, "config.json"))
    )


def get_active_block() -> dict:
    block_id = get_active_block_id()
    if not block_id:
        raise FileNotFoundError("No content blocks found.")
    return get_block(block_id)


# ── Write operations ─────────────────────────────────────────────


def set_active_block(block_id: str):
    if not os.path.isdir(os.path.join(BLOCKS_DIR, block_id)):
        raise FileNotFoundError(f"Block not found: {block_id}")
    _ensure_blocks_dir()
    with open(ACTIVE_FILE, "w") as f:
        f.write(block_id + "\n")


def delete_block(block_id: str):
    path = os.path.join(BLOCKS_DIR, block_id)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Block not found: {block_id}")
    shutil.rmtree(path)
    if get_active_block_id() == block_id:
        remaining = list_blocks_raw()
        if remaining:
            set_active_block(remaining[0])
        elif os.path.isfile(ACTIVE_FILE):
            os.remove(ACTIVE_FILE)


# ── Block memory (per-block memory.json) ─────────────────────────

_DEFAULT_MEMORY = {"topics": [], "standing": [], "next": None}


def read_block_memory(block_id: str) -> dict:
    mem_path = os.path.join(BLOCKS_DIR, block_id, "memory.json")
    if not os.path.isfile(mem_path):
        return dict(_DEFAULT_MEMORY)
    with open(mem_path) as f:
        return json.load(f)


def save_block_memory(block_id: str, data: dict):
    mem_path = os.path.join(BLOCKS_DIR, block_id, "memory.json")
    with open(mem_path, "w") as f:
        json.dump(data, f, indent=2)


# ── Meta-LLM block generation ───────────────────────────────────


def _load_space_examples() -> dict[str, str]:
    """Load the space-cosmos block .md files as examples for the meta prompt."""
    space_dir = os.path.join(BLOCKS_DIR, "space-cosmos")
    examples = {}
    for filename in PROMPT_FILES:
        path = os.path.join(space_dir, filename)
        if os.path.isfile(path):
            with open(path) as f:
                examples[filename] = f.read()
    return examples


def _build_meta_prompt(form_data: dict, examples: dict[str, str]) -> str:
    """Build the meta prompt that generates all block .md files."""

    name = form_data["name"]
    description = form_data["description"]
    style = form_data.get("style", "")
    duration_min = form_data.get("duration_min", 15)
    duration_max = form_data.get("duration_max", 20)
    example_topics = form_data.get("example_topics", [])
    narration = form_data.get("narration_enabled", True)
    captions = form_data.get("captions_enabled", True)
    trends = form_data.get("youtube_trends_enabled", False)
    seed_keywords = form_data.get("seed_keywords", [])

    topics_str = "\n".join(f"  - {t}" for t in example_topics) if example_topics else "  (none provided)"
    keywords_str = ", ".join(seed_keywords) if seed_keywords else "(none — YouTube trends disabled)"

    narration_note = ""
    if not narration:
        narration_note = (
            "\n\nIMPORTANT — NARRATION IS DISABLED for this block. "
            "The script_writer.md must NOT include NARRATION lines in beats. "
            "Instead, focus entirely on visual storytelling. Beats should have "
            "VISUAL and AUDIO (music/SFX) only. The production_breakdown.md "
            "should NOT include a VOICEOVER PROMPT section. Videos will rely "
            "purely on visuals and background music."
        )
    if not captions:
        narration_note += (
            "\n\nCAUTION — TEXT OVERLAY / CAPTIONS ARE DISABLED. "
            "Do not reference burned-in captions or text overlays in any prompt."
        )

    prompt = f"""You are an expert content strategist setting up an automated YouTube Shorts
creation pipeline for a specific niche. Your job is to generate the system
prompts (.md files) that will drive an AI agent pipeline.

Below are THREE example .md files from an existing "Space & Cosmos" block.
These demonstrate the EXACT structural format, level of detail, and quality
bar expected. You must produce equivalent files for a COMPLETELY DIFFERENT
content niche, adapting ALL content-specific references while keeping the
structural skeleton identical.

═══════════════════════════════════════════════════════════
EXAMPLE: research_agent.md (Space & Cosmos)
═══════════════════════════════════════════════════════════

{examples.get("research_agent.md", "(not available)")}

═══════════════════════════════════════════════════════════
EXAMPLE: script_writer.md (Space & Cosmos)
═══════════════════════════════════════════════════════════

{examples.get("script_writer.md", "(not available)")}

═══════════════════════════════════════════════════════════
EXAMPLE: production_breakdown.md (Space & Cosmos)
═══════════════════════════════════════════════════════════

{examples.get("production_breakdown.md", "(not available)")}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════

Generate the three .md files for this NEW content block:

  Block Name: {name}
  Description: {description}
  Visual Style: {style or "(use your best judgment based on the description)"}
  Video Duration: {duration_min}–{duration_max} seconds
  Example Topics:
{topics_str}
  YouTube Trends Enabled: {"Yes — seed keywords: " + keywords_str if trends else "No"}
  Narration Enabled: {"Yes" if narration else "No — visual-only storytelling"}
  Captions Enabled: {"Yes" if captions else "No"}{narration_note}

═══════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════

1. **Structural fidelity**: Each .md file must have the SAME section headings,
   the SAME output format blocks, and the SAME instructional structure as the
   Space example. You are replacing the CONTENT within that structure, not
   redesigning the structure itself.

2. **research_agent.md**:
   - Replace "space, cosmos, and the universe" with the new niche description.
   - Replace all space-specific topic examples with domain-appropriate ones.
   - Keep the 4-step flow (RESEARCH → PROPOSE → CONVERSE → HAND OFF) intact.
   - Adjust proposal criteria to match the new domain (e.g. replace "citable
     astronomical fact" with whatever the domain's equivalent anchor is).
   - The agent has 3 research tools: search_youtube_trends() (no args),
     search_google_trends(keywords=[...]) (1-4 keywords), and
     web_search(query="...") (natural language query). Plus read_memory()
     and write_memory(). Keep the research pipeline using these tools.
   - If YouTube trends are disabled, tell the agent to skip
     search_youtube_trends() and use web_search() for current events.
   - Replace the duration reference with {duration_min}-{duration_max} seconds.

3. **script_writer.md**:
   - Replace "educational space and cosmos content" with the new niche.
   - Replace ALL hook examples with domain-appropriate ones (at least 2 per
     hook type).
   - Replace visual storytelling principles with domain-appropriate ones.
   - Replace audio design palette with domain-appropriate music/sound.
   - Replace "{duration_min}-{duration_max} seconds" wherever "15-20 seconds"
     appears.
   - Update word count targets for the new duration range using ~145 WPM.
   - Keep the OUTPUT FORMAT block identical (TITLE, DESCRIPTION, BEATS, etc.).

4. **production_breakdown.md**:
   - Replace duration patterns to sum to {duration_min}-{duration_max} seconds.
   - Replace "space-specific visual language" section with domain-appropriate
     visual guidance (e.g. for rural India: "golden-hour paddy fields",
     "weathered hands preparing food", etc.).
   - Update style consistency examples for the new visual style: "{style}".
   - Keep ALL structural rules (shot duration, camera vocabulary, continuity,
     quality checklist) intact — these are universal.
   - Update voiceover timing table for the new duration range.

5. Also generate:
   - **voice_style**: A 1-2 sentence voice/delivery description for TTS
     (what kind of voice, tone, pacing, inspiration). Adapt to the niche.
   - **music_style**: A music generation prompt template. Must include
     "{{duration}} second" at the start (the pipeline replaces this with
     actual duration). Describe the musical style that fits the niche.
     End with "no vocals, no lyrics."

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return a JSON object with EXACTLY these keys:

{{
  "research_agent_md": "<full markdown content>",
  "script_writer_md": "<full markdown content>",
  "production_breakdown_md": "<full markdown content>",
  "voice_style": "<1-2 sentence voice description>",
  "music_style": "<music prompt template with {{duration}} placeholder>"
}}

Return ONLY the JSON object. No other text before or after it."""

    return prompt


async def create_block(form_data: dict) -> dict:
    """Create a new content block by generating .md files with Claude."""
    import anthropic

    block_id = _slugify(form_data["name"])

    if os.path.isdir(os.path.join(BLOCKS_DIR, block_id)):
        raise ValueError(f"Block '{block_id}' already exists.")

    block_path = os.path.join(BLOCKS_DIR, block_id)
    os.makedirs(block_path, exist_ok=True)

    try:
        examples = _load_space_examples()
        prompt = _build_meta_prompt(form_data, examples)

        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )

        result = json.loads(response.content[0].text)

        # Save .md files
        file_map = {
            "research_agent.md": result["research_agent_md"],
            "script_writer.md": result["script_writer_md"],
            "production_breakdown.md": result["production_breakdown_md"],
        }
        for filename, content in file_map.items():
            with open(os.path.join(block_path, filename), "w") as f:
                f.write(content)

        # Build and save config.json
        config = {
            "id": block_id,
            "name": form_data["name"],
            "description": form_data.get("description", ""),
            "style": form_data.get("style", ""),
            "duration_min": form_data.get("duration_min", 15),
            "duration_max": form_data.get("duration_max", 20),
            "example_topics": form_data.get("example_topics", []),
            "narration_enabled": form_data.get("narration_enabled", True),
            "captions_enabled": form_data.get("captions_enabled", True),
            "youtube_trends_enabled": form_data.get("youtube_trends_enabled", False),
            "seed_keywords": form_data.get("seed_keywords", []),
            "noise_terms": form_data.get("noise_terms", []),
            "voice_style": result.get("voice_style", ""),
            "music_style": result.get("music_style", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(block_path, "config.json"), "w") as f:
            json.dump(config, f, indent=2)

        # Initialize empty memory
        save_block_memory(block_id, dict(_DEFAULT_MEMORY))

        return config

    except Exception:
        if os.path.isdir(block_path):
            shutil.rmtree(block_path)
        raise
