"""FastAPI server wrapping Kira's ADK pipeline behind a REST + SSE API.

Run locally:  uvicorn kira.server:app --reload --port 8080
Or:           python -m kira.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape as xml_escape
from dotenv import load_dotenv
import requests as _requests

# ── Logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Load .env from the kira package directory (same as ADK does)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
    logging.info(f"Loaded .env from {_env_path}")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# ── Twilio outbound client (optional — sandbox or production) ────
_twilio_client = None
_twilio_from = os.environ.get("TWILIO_WHATSAPP_NUMBER", "")
_twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
_twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
if _twilio_sid and _twilio_token and _twilio_from:
    try:
        from twilio.rest import Client as TwilioClient
        _twilio_client = TwilioClient(_twilio_sid, _twilio_token)
        logging.info("Twilio client initialised for outbound WhatsApp messages")
    except Exception as e:
        logging.warning(f"Twilio client init failed: {e}")


# ── Legacy routing (proxy non-whitelisted numbers to old backend) ─
_LEGACY_BACKEND_URL = os.environ.get("LEGACY_BACKEND_URL", "").rstrip("/")
_whitelist_numbers: set[str] = set()
_seed = os.environ.get("WHITELIST_NUMBERS", "")
if _seed:
    for n in _seed.split(","):
        n = n.strip()
        if n:
            if not n.startswith("whatsapp:"):
                n = f"whatsapp:{n}"
            _whitelist_numbers.add(n)
    logging.info("Whitelist seeded with %d numbers: %s", len(_whitelist_numbers), _whitelist_numbers)
if _LEGACY_BACKEND_URL:
    logging.info("Legacy routing enabled → %s", _LEGACY_BACKEND_URL)

# ── Per-user content block mapping (phone → block_id) ──────────
# Format: USER_BLOCK_MAP="+919XXXXXXXXX:fascinating-history,+1YYYY:space-cosmos"
_user_block_map: dict[str, str] = {}
_ubm_seed = os.environ.get("USER_BLOCK_MAP", "")
if _ubm_seed:
    for pair in _ubm_seed.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        phone, block_id = pair.rsplit(":", 1)
        phone = phone.strip()
        block_id = block_id.strip()
        if phone and block_id:
            if not phone.startswith("whatsapp:"):
                phone = f"whatsapp:{phone}"
            _user_block_map[phone] = block_id
    logging.info("User block map: %s", _user_block_map)


def _proxy_to_legacy(form_data: dict) -> bytes:
    """Forward a webhook request to the legacy backend (sync, run in executor)."""
    resp = _requests.post(
        f"{_LEGACY_BACKEND_URL}/whatsapp",
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return resp.content


# ── WhatsApp simulator message queue ─────────────────────────────
# Holds push messages per phone number so the chat UI can poll them.
_wa_sim_queues: dict[str, list[str]] = {}

_WA_SIM_NUMBER = "whatsapp:+15550001234"


def _push_whatsapp(to: str, text: str):
    """Send a proactive WhatsApp message via the Twilio REST API.
    Also queues the message for the local WhatsApp simulator."""
    text = _format_for_whatsapp(text)
    chunks = [text[i:i+1600] for i in range(0, len(text), 1600)]

    # Always queue for the simulator so the chat UI can pick it up
    _wa_sim_queues.setdefault(to, []).extend(chunks)

    if not _twilio_client:
        logging.warning("[WHATSAPP] No Twilio client — message queued for simulator only")
        return
    logger.info("[WHATSAPP] Pushing %d chunk(s) to %s | total_len=%d", len(chunks), to, len(text))
    for i, chunk in enumerate(chunks):
        try:
            _twilio_client.messages.create(
                body=chunk, from_=_twilio_from, to=to,
            )
            logger.info("[WHATSAPP] Chunk %d/%d sent | len=%d", i + 1, len(chunks), len(chunk))
        except Exception as e:
            logger.error("[WHATSAPP] Failed to send chunk %d | error=%s", i + 1, e)

import anthropic
from anthropic.types import TextBlock

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from .agent import build_agents
from . import block_manager
from . import db
from .events import event_bus, ProductionEvent
from .tools.memory import read_memory, save_memory, write_memory
from .tools import memory as memory_mod
from .tools import youtube as youtube_mod

logger = logging.getLogger(__name__)

# ── Summarization client for WhatsApp responses ──────────────────

_anthropic_client = anthropic.Anthropic()

_WHATSAPP_SUMMARIZE_SYSTEM = (
    "You are a WhatsApp message formatter for an AI called Kira.\n\n"
    "Rules:\n"
    "1. Strip ALL production details — scripts, shot breakdowns, visual "
    "descriptions, voiceover text, style specs, camera directions. The "
    "user does not need to review those.\n"
    "2. Keep: topic names, reasons why they'll work, any URLs, and any "
    "questions or choices the user needs to answer.\n"
    "3. ALWAYS end with a clear action line — what should the user do "
    "next? Examples: 'Pick a number, or say more for different options.' "
    "or 'You don't need to do anything — I'll send the video when it's "
    "ready.' or 'Want me to look for different topics?'\n"
    "4. Use plain text, numbered lists, and line breaks. No markdown.\n"
    "5. Keep under 800 characters."
)


async def _summarize_for_whatsapp(text: str) -> str:
    """Use Claude to summarize a long agent response for WhatsApp.

    Keeps the original meaning but fits within WhatsApp's readability
    constraints (~1200 chars). Returns the original if it's already short."""
    if len(text) <= 600:
        return text
    logger.info("[SUMMARIZE] Summarizing response for WhatsApp | original_len=%d", len(text))
    t0 = time.time()
    try:
        response = _anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=_WHATSAPP_SUMMARIZE_SYSTEM,
            messages=[{"role": "user", "content": f"Original message:\n{text}"}],
        )
        text_block = next((b for b in response.content if isinstance(b, TextBlock)), None)
        summary = (text_block.text if text_block else response.content[0].text).strip()
        logger.info("[SUMMARIZE] Done | summary_len=%d | elapsed=%.1fs",
                    len(summary), time.time() - t0)
        return summary
    except Exception as e:
        logger.error("[SUMMARIZE] Failed, using original | error=%s", e)
        return text

# ── ADK tracing / observability ───────────────────────────────────

def _setup_tracing():
    """Enable ADK's built-in OpenTelemetry tracing.

    By default, traces go to console via the LoggingPlugin. To send
    traces to Google Cloud Trace, set GOOGLE_CLOUD_PROJECT and
    ADK_TRACE_TO_CLOUD=true. For a generic OTLP backend (Jaeger, etc.),
    set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT.
    """
    plugins = []
    try:
        from google.adk.plugins import LoggingPlugin
        plugins.append(LoggingPlugin())
        logger.info("[TRACING] LoggingPlugin enabled — all agent events will be logged")
    except ImportError:
        logger.warning("[TRACING] LoggingPlugin not available in this ADK version")

    if os.environ.get("ADK_TRACE_TO_CLOUD", "").lower() == "true":
        try:
            from google.adk.telemetry.google_cloud import get_gcp_exporters, get_gcp_resource
            from google.adk.telemetry.setup import maybe_set_otel_providers
            import google.auth

            credentials, project_id = google.auth.default()
            otel_hooks = get_gcp_exporters(
                enable_cloud_tracing=True,
                enable_cloud_metrics=False,
                enable_cloud_logging=True,
                google_auth=(credentials, project_id),
            )
            otel_resource = get_gcp_resource(project_id)
            maybe_set_otel_providers(
                otel_hooks_to_setup=[otel_hooks],
                otel_resource=otel_resource,
            )
            logger.info("[TRACING] Google Cloud Trace enabled | project=%s", project_id)
        except Exception as e:
            logger.warning("[TRACING] Cloud Trace setup failed: %s", e)
    elif os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        try:
            from google.adk.telemetry.setup import maybe_set_otel_providers
            maybe_set_otel_providers()
            logger.info("[TRACING] OTLP exporter enabled | endpoint=%s",
                       os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"])
        except Exception as e:
            logger.warning("[TRACING] OTLP setup failed: %s", e)

    return plugins

_adk_plugins = _setup_tracing()

# ── ADK runner setup ──────────────────────────────────────────────

APP_NAME = "kira"

_database_url = os.environ.get("DATABASE_URL", "")
session_service = None
if _database_url:
    # Normalise URL: handle both postgres:// and postgresql:// schemes
    _sa_url = _database_url
    if _sa_url.startswith("postgres://"):
        _sa_url = "postgresql://" + _sa_url[len("postgres://"):]
    _sqlalchemy_url = _sa_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    try:
        from google.adk.sessions import DatabaseSessionService
        db.run_migration_sync(_sqlalchemy_url)
        session_service = DatabaseSessionService(
            db_url=_sqlalchemy_url,
            connect_args={"sslmode": "require", "connect_timeout": 10},
        )
        logger.info("[INIT] Using DatabaseSessionService for persistent sessions")
    except Exception as e:
        logger.error("[INIT] DatabaseSessionService failed, falling back to in-memory: %s", e)
        session_service = None
if session_service is None:
    session_service = InMemorySessionService()
    if _database_url:
        logger.warning("[INIT] Using InMemorySessionService (DB init failed)")
    else:
        logger.info("[INIT] Using InMemorySessionService (no DATABASE_URL)")

# Initialize from active block
_active_config = block_manager.get_active_block()
_active_block_id = _active_config["id"]
_active_block_path = block_manager.get_block_path(_active_block_id)
_root_agent = build_agents(_active_config, _active_block_path)
logger.info("[INIT] Building runner | block=%s | agent=%s", _active_block_id, _root_agent.name)
runner = Runner(
    agent=_root_agent,
    app_name=APP_NAME,
    session_service=session_service,
    plugins=_adk_plugins,
)

# ── Per-block runner cache for multi-user routing ──────────────
_block_runners: dict[str, tuple] = {}
_block_runners[_active_block_id] = (runner, _active_config)


def _get_runner_for_user(phone: str) -> tuple:
    """Return (runner, config, block_id) for a WhatsApp user.
    Uses the per-user block map if configured, falls back to the
    global active block."""
    block_id = _user_block_map.get(phone)
    if not block_id:
        return runner, _active_config, _active_block_id

    if block_id in _block_runners:
        r, cfg = _block_runners[block_id]
        return r, cfg, block_id

    try:
        cfg = block_manager.get_block(block_id)
        path = block_manager.get_block_path(block_id)
        agent = build_agents(cfg, path)
        r = Runner(agent=agent, app_name=APP_NAME,
                    session_service=session_service, plugins=_adk_plugins)
        _block_runners[block_id] = (r, cfg)
        logger.info("[BLOCK] Built runner for user block=%s", block_id)
        return r, cfg, block_id
    except Exception as e:
        logger.error("[BLOCK] Failed to build runner for block=%s: %s", block_id, e)
        return runner, _active_config, _active_block_id


# Track active session and production state
_state = {
    "session_id": None,
    "user_id": "kira-web-user",
    "status": "idle",            # idle | proposing | proposed | producing | done | error
    "current_proposal": None,    # Parsed proposal dict
    "production_result": None,   # Final result after production
    "error": None,
}

# ── Tool-to-phase mapping (shared by /api/approve and /api/chat) ─

_TOOL_TO_PHASE = {
    "write_script": "script",
    "plan_production": "plan",
    "generate_image": "image_gen",
    "generate_video": "video_gen",
    "concat_videos": "concat",
    "generate_voiceover": "voiceover",
    "generate_background_music": "music",
    "fit_and_mux_audio": "mux",
    "mux_music_only": "mux",
    "publish_video": "upload",
    "write_memory": "memory",
}
_PRODUCTION_TOOLS = set(_TOOL_TO_PHASE) - {"write_memory"}


def _effective_tool_name(part) -> str:
    """For transfer_to_agent calls, return the target agent name."""
    name = part.function_call.name
    if name == "transfer_to_agent" and part.function_call.args:
        args = part.function_call.args
        if isinstance(args, dict):
            return args.get("agent_name", name)
    return name

# ── Rate limits (non-owner WhatsApp users) ─────────────────────
_OWNER_NUMBERS: set[str] = {
    "whatsapp:+919840733969",
    "whatsapp:+14132106772",
    "whatsapp:+919003065436",
    _WA_SIM_NUMBER,
}
_DAILY_VIDEO_LIMIT = 1
_TOTAL_VIDEO_LIMIT = 3


async def _check_rate_limit(phone: str) -> str | None:
    """Return a user-facing message if the phone is over its limit, else None."""
    if not phone or phone in _OWNER_NUMBERS:
        return None
    if not db.is_enabled():
        return None
    today = await db.count_user_productions_today(phone)
    if today >= _DAILY_VIDEO_LIMIT:
        return (
            "You've already made a video today! "
            "Come back tomorrow for another one."
        )
    total = await db.count_user_productions_total(phone)
    if total >= _TOTAL_VIDEO_LIMIT:
        return (
            "You've used all 3 of your free videos. "
            "Thanks for trying Kira!"
        )
    return None


# ── WhatsApp per-user sessions ───────────────────────────────────

SESSION_GAP_SECONDS = 2 * 3600  # 2 hours of silence → new session

_wa_sessions: dict[str, dict] = {}


async def _get_wa_session(phone: str):
    """Get or rotate an ADK session for a WhatsApp user.

    After a container restart _wa_sessions is empty, so we check the DB
    for the most recent wa_session and try to resume the persisted ADK
    session.  This preserves conversation context (including tool-call
    history) across restarts.
    """
    now = time.time()
    entry = _wa_sessions.get(phone)

    if entry and (now - entry["last_active"]) < SESSION_GAP_SECONDS:
        idle_secs = now - entry["last_active"]
        entry["last_active"] = now
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=entry["user_id"],
            session_id=entry["session_id"],
        )
        if session:
            logger.info("[SESSION] Reusing session | phone=%s | session=%s | idle=%.0fs",
                       phone, session.id, idle_secs)
            await db.touch_session(session.id)
            return session, entry

    user_id = f"wa_{phone.replace('whatsapp:', '').replace('+', '')}"

    if not entry and db.is_enabled():
        db_session = await db.get_latest_session(phone)
        if db_session:
            last_active_ts = db_session["last_active"].timestamp()
            idle_secs = now - last_active_ts
            if idle_secs < SESSION_GAP_SECONDS:
                old_sid = db_session["id"]
                session = await session_service.get_session(
                    app_name=APP_NAME, user_id=user_id, session_id=old_sid,
                )
                if session:
                    logger.info("[SESSION] Resumed persisted session | phone=%s | session=%s | idle=%.0fs",
                               phone, old_sid, idle_secs)
                    await db.touch_session(old_sid)
                    entry = {
                        "session_id": old_sid,
                        "user_id": user_id,
                        "last_active": now,
                        "status": "idle",
                        "production_result": None,
                        "is_new": False,
                        "processing": False,
                        "pending_reply": None,
                        "last_production_outcome": None,
                    }
                    _wa_sessions[phone] = entry
                    return session, entry

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id,
    )
    logger.info("[SESSION] New session | phone=%s | session=%s | user_id=%s",
               phone, session.id, user_id)

    await db.upsert_user(phone, user_id)
    await db.create_session(session.id, phone)

    entry = {
        "session_id": session.id,
        "user_id": user_id,
        "last_active": now,
        "status": "idle",
        "production_result": None,
        "is_new": True,
        "processing": False,
        "pending_reply": None,
        "last_production_outcome": None,
    }
    _wa_sessions[phone] = entry
    return session, entry


async def _load_user_memory(phone: str):
    """Load a user's memory from DB into the memory module cache."""
    if db.is_enabled():
        mem = await db.get_user_memory(phone)
        memory_mod.configure_user(phone, mem)


async def _flush_user_memory():
    """Flush the memory module cache back to DB."""
    if db.is_enabled():
        phone, mem = memory_mod.get_current_memory()
        if phone and mem:
            await db.update_user_memory(phone, mem)


def _format_for_whatsapp(text: str) -> str:
    """Convert markdown → WhatsApp-friendly plain text, respecting the
    1600-char Twilio limit."""
    text = re.sub(r'^#{1,4}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', text)
    text = re.sub(r'^\s*[-•]\s*', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    if len(text) > 1500:
        cut = text[:1497].rsplit('\n', 1)[0] or text[:1497].rsplit('. ', 1)[0]
        text = cut.rstrip() + "..."
    return text


# ── Helpers ───────────────────────────────────────────────────────


def _get_phases() -> list[tuple[str, str]]:
    """Build the production phase list from the active block config."""
    narration = _active_config.get("narration_enabled", True)
    phases = [
        ("script", "Writing script"),
        ("plan", "Planning shots"),
        ("image_gen", "Generating images"),
        ("video_gen", "Generating video"),
        ("concat", "Assembling clips"),
    ]
    if narration:
        phases.append(("voiceover", "Recording voiceover"))
    phases.append(("music", "Creating music"))
    phases.append(("mux", "Mixing audio" if narration else "Adding music"))
    phases.extend([
        ("upload", "Publishing video"),
        ("memory", "Saving to memory"),
    ])
    return phases


async def _activate_block(block_id: str):
    """Switch to a different content block — rebuilds the agent tree."""
    global runner, _root_agent, _active_config, _active_block_id, _active_block_path

    config = block_manager.get_block(block_id)
    block_path = block_manager.get_block_path(block_id)

    new_root = build_agents(config, block_path)
    logger.info("[BLOCK] Switching to block=%s | rebuilding runner", block_id)
    runner = Runner(agent=new_root, app_name=APP_NAME, session_service=session_service,
                    plugins=_adk_plugins)

    _root_agent = new_root
    _active_config = config
    _active_block_id = block_id
    _active_block_path = block_path
    _block_runners[block_id] = (runner, config)

    # Reset session (new block = new conversation context)
    _state["session_id"] = None
    _state["status"] = "idle"
    _state["current_proposal"] = None
    _state["production_result"] = None
    _state["error"] = None

    block_manager.set_active_block(block_id)


async def _get_or_create_session():
    """Get existing session or create a new one."""
    if _state["session_id"]:
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=_state["user_id"],
            session_id=_state["session_id"],
        )
        if session:
            return session
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=_state["user_id"],
    )
    _state["session_id"] = session.id
    return session


async def _send_message(text: str) -> str:
    """Send a message to the ADK agent and collect the full response."""
    session = await _get_or_create_session()
    logger.info("[AGENT] Sending message | session=%s | text=%s", session.id, text[:100])
    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=text)],
    )
    response_parts = []
    t0 = time.time()
    async for event in runner.run_async(
        user_id=_state["user_id"],
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_parts.append(part.text)
                    logger.debug("[AGENT] Text chunk | author=%s | text=%s",
                                getattr(event, 'author', '?'), part.text[:120])
                if hasattr(part, "function_call") and part.function_call:
                    logger.info("[AGENT] Tool call | tool=%s | args=%s",
                               part.function_call.name,
                               str(part.function_call.args)[:200] if part.function_call.args else "")
                if hasattr(part, "function_response") and part.function_response:
                    resp_str = str(part.function_response.response)[:200]
                    logger.info("[AGENT] Tool result | tool=%s | result=%s",
                               part.function_response.name, resp_str)
    result = "\n".join(response_parts)
    logger.info("[AGENT] Response complete | elapsed=%.1fs | len=%d", time.time() - t0, len(result))
    return result


def _clean_markdown(text: str) -> str:
    """Strip markdown formatting from text."""
    text = re.sub(r'^#{1,4}\s*', '', text)       # heading markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)     # italic
    text = re.sub(r'^\s*[\*\-]\s*', '', text)      # list bullets
    return text.strip()


def _parse_proposal(raw_text: str) -> dict:
    """Parse the agent's proposal text into structured data."""
    topic = ""
    why = ""
    visual = ""
    source = ""
    trending = ""

    # Strategy 1: Look for "I propose we cover: **Topic Name**" pattern
    propose_match = re.search(
        r'I propose (?:we cover|covering|this topic)[:\s]*\*\*([^*]+)\*\*',
        raw_text, re.IGNORECASE
    )
    if propose_match:
        topic = propose_match.group(1).strip().rstrip('.')

    # Strategy 2: Look for "### Proposal: Topic" pattern
    if not topic:
        heading_match = re.search(
            r'#{1,4}\s*(?:Proposal|Topic|My Proposal)[:\s—\-]*(.+)',
            raw_text, re.IGNORECASE
        )
        if heading_match:
            topic = _clean_markdown(heading_match.group(1))

    # Strategy 3: Look for "**Topic:**" or "**The Topic:**" pattern
    if not topic:
        bold_match = re.search(
            r'\*\*(?:The )?Topic[:\s]*\*\*[:\s]*(.+)',
            raw_text, re.IGNORECASE
        )
        if bold_match:
            topic = _clean_markdown(bold_match.group(1))

    # Parse sections from the proposal part of the text
    proposal_start = 0
    for marker in [r'I propose', r'###\s*Proposal', r'###\s*Why This Topic',
                   r'My proposal', r'I recommend']:
        m = re.search(marker, raw_text, re.IGNORECASE)
        if m:
            proposal_start = m.start()
            break

    proposal_text = raw_text[proposal_start:]
    lines = proposal_text.split("\n")

    current_section = None
    for line in lines:
        line_lower = line.lower().strip()
        if not line.strip():
            continue

        if any(kw in line_lower for kw in ["why this topic", "why it will perform",
                                            "why:", "the trend hook"]):
            current_section = "why"
            extracted = re.sub(r'(?i).*?:\s*', '', line, count=1).strip()
            if extracted and not why:
                why = _clean_markdown(extracted)
        elif any(kw in line_lower for kw in ["visual", "the visuals", "visual angle",
                                              "visual concept"]):
            current_section = "visual"
            extracted = re.sub(r'(?i).*?:\s*', '', line, count=1).strip()
            if extracted:
                visual = _clean_markdown(extracted)
        elif any(kw in line_lower for kw in ["source", "citation", "the source"]):
            current_section = "source"
            extracted = re.sub(r'(?i).*?:\s*', '', line, count=1).strip()
            if extracted:
                source = _clean_markdown(extracted)
        elif any(kw in line_lower for kw in ["hook:", "the hook", "core hook",
                                              "the fact", "the angle"]):
            current_section = "hook"
            if not why:
                extracted = re.sub(r'(?i).*?:\s*', '', line, count=1).strip()
                if extracted:
                    why = _clean_markdown(extracted)
        elif current_section == "why" and not why:
            cleaned = _clean_markdown(line)
            if len(cleaned) > 20:
                why = cleaned
        elif current_section == "hook" and not why:
            cleaned = _clean_markdown(line)
            if len(cleaned) > 20:
                why = cleaned

    # Fallback topic: first bold text in proposal area
    if not topic:
        bold_match = re.search(r'\*\*([^*]{8,80})\*\*', proposal_text)
        if bold_match:
            topic = bold_match.group(1).strip().rstrip('.')

    # Fallback topic: first heading in proposal area
    if not topic:
        for line in lines:
            cleaned = _clean_markdown(line)
            if cleaned and 8 < len(cleaned) < 100:
                topic = cleaned
                break

    # Fallback why
    if not why:
        for line in lines:
            cleaned = _clean_markdown(line)
            if len(cleaned) > 40 and cleaned != topic:
                why = cleaned
                break

    # Extract trending percentage from raw text
    pct_match = re.search(r'(?:up|↑|rising)\s*(?:a staggering\s*)?(\d[\d,.]*\s*%)', raw_text, re.IGNORECASE)
    if pct_match:
        trending = f"+{pct_match.group(1).strip()}"
    elif not trending:
        pct_match = re.search(r'(\d[\d,.]*%)', raw_text)
        if pct_match:
            trending = f"+{pct_match.group(1)}"

    # Final cleanup on all fields
    topic = re.sub(r'^(?:Topic|Proposal)\s*[:—\-]\s*', '', topic, flags=re.IGNORECASE).strip()
    why = re.sub(r'^\*\s*', '', why).strip()
    source = re.sub(r'^\*\s*', '', source).strip()
    visual = re.sub(r'^\*\s*', '', visual).strip()

    return {
        "topic": topic or "Untitled Proposal",
        "why": why or "Kira found this topic worth exploring.",
        "visual": visual,
        "source": source,
        "trending": trending or "Trending",
        "raw": raw_text,
    }


# ── Lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Kira server starting")
    await db.init()
    yield
    await db.close()
    logger.info("Kira server shutting down")


# ── FastAPI app ───────────────────────────────────────────────────

app = FastAPI(title="Kira", lifespan=lifespan)

# Serve static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC_DIR, "chat.html")) as f:
        return HTMLResponse(f.read())


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui():
    with open(os.path.join(STATIC_DIR, "chat.html")) as f:
        return HTMLResponse(f.read())


@app.get("/api/status")
async def get_status():
    return {
        "status": _state["status"],
        "proposals": _state["current_proposal"],
        "result": _state["production_result"],
        "error": _state["error"],
        "phases": [
            {"phase": e.phase, "status": e.status, "detail": e.detail,
             "progress": e.progress, "preview_url": e.preview_url,
             "error_message": e.error_message}
            for e in event_bus.history
        ],
    }


def _parse_multiple_proposals(raw_text: str) -> list[dict]:
    """Parse up to 6 proposals from agent response into a list of dicts."""
    splits = re.split(
        r'(?:^|\n)\s*(?:#{1,4}\s*)?(?:\*\*)?(?:Option|Proposal|Topic)\s*(\d)[\s.:—\-\)]+',
        raw_text, flags=re.IGNORECASE
    )

    proposals = []

    if len(splits) >= 3:
        for i in range(1, len(splits), 2):
            if i + 1 < len(splits):
                chunk = splits[i + 1]
                p = _parse_proposal(chunk)
                if p["topic"] != "Untitled Proposal":
                    proposals.append(p)

    # Fallback: try splitting by numbered bold items
    if len(proposals) < 2:
        proposals = []
        bold_topics = re.findall(
            r'(?:^|\n)\s*\d+[\.\)]\s*\*\*([^*]{5,80})\*\*',
            raw_text
        )
        if len(bold_topics) >= 2:
            for bt in bold_topics[:6]:
                topic_clean = bt.strip().rstrip('.')
                idx = raw_text.find(bt)
                remaining = raw_text[idx + len(bt):]
                next_num = re.search(r'\n\s*\d+[\.\)]\s*\*\*', remaining)
                chunk = remaining[:next_num.start()] if next_num else remaining[:500]

                why = ""
                source = ""
                trending = ""

                for line in chunk.split("\n"):
                    cl = _clean_markdown(line)
                    ll = line.lower()
                    if any(kw in ll for kw in ["why", "hook", "angle", "perform"]):
                        why = _clean_markdown(re.sub(r'(?i).*?:\s*', '', line, count=1))
                    elif any(kw in ll for kw in ["source", "citation"]):
                        source = _clean_markdown(re.sub(r'(?i).*?:\s*', '', line, count=1))
                    elif not why and len(cl) > 30:
                        why = cl

                pct = re.search(r'(\d[\d,.]*%)', chunk)
                if pct:
                    trending = f"+{pct.group(1)}"

                proposals.append({
                    "topic": topic_clean,
                    "why": why or "A compelling topic for this block.",
                    "visual": "",
                    "source": source,
                    "trending": trending or "Trending",
                    "raw": chunk,
                })

    # Final fallback: parse as a single proposal
    if len(proposals) < 1:
        single = _parse_proposal(raw_text)
        proposals = [single]

    return proposals[:6]


@app.get("/api/propose")
async def propose():
    """Ask Kira to research trends and propose 3 topics."""
    if _state["status"] == "producing":
        return JSONResponse(
            {"error": "Production in progress. Wait for it to finish."},
            status_code=409,
        )

    _state["status"] = "proposing"
    _state["current_proposal"] = None
    _state["production_result"] = None
    _state["error"] = None
    await event_bus.clear()

    logger.info("[PROPOSE] Starting trend research for proposals")
    try:
        response = await _send_message(
            "What should we post today? Research trends, check memory, "
            "and give me exactly 6 topic options to choose from. "
            "For each option, format as:\n"
            "1. **Topic Name** — one-line description of why this will work. "
            "(Source: citation)\n"
            "Keep each option to 2-3 lines max. I'll pick one."
        )
        proposals = _parse_multiple_proposals(response)
        logger.info("[PROPOSE] Got %d proposals", len(proposals))
        _state["current_proposal"] = proposals
        _state["status"] = "proposed"
        return {"proposals": proposals}
    except Exception as e:
        logger.error("[PROPOSE] Failed: %s\n%s", e, traceback.format_exc())
        _state["status"] = "error"
        _state["error"] = str(e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/skip")
async def skip():
    """Skip current proposals, ask for 3 new ones."""
    if _state["status"] not in ("proposed", "idle", "done", "error"):
        return JSONResponse(
            {"error": "Cannot skip right now."},
            status_code=409,
        )

    _state["status"] = "proposing"
    _state["current_proposal"] = None
    _state["error"] = None
    await event_bus.clear()

    try:
        response = await _send_message(
            "None of those grab me. Give me 6 completely different topic options. "
            "Same format: numbered, bold topic, one-line why, source."
        )
        proposals = _parse_multiple_proposals(response)
        _state["current_proposal"] = proposals
        _state["status"] = "proposed"
        return {"proposals": proposals}
    except Exception as e:
        logger.error(f"Skip failed: {e}\n{traceback.format_exc()}")
        _state["status"] = "error"
        _state["error"] = str(e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/chat")
async def chat(request: Request):
    """Conversational endpoint for WhatsApp / Twilio.
    Sends the user's message to Kira and returns the reply.
    If the conversation leads to a confirmed topic, production
    starts in the background and the hand-off reply is returned
    immediately."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)

    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "No message provided."}, status_code=400)

    if _state["status"] == "producing":
        return {"reply": "Still working on the current video — I'll message you when it's live!"}

    _state["error"] = None

    logger.info("[CHAT] Incoming | message=%s", message[:100])
    youtube_mod.configure("")
    session = await _get_or_create_session()
    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=message)],
    )

    reply_parts: list[str] = []
    all_text: list[str] = []
    reply_ready = asyncio.Event()
    production_launched = False

    async def _process():
        nonlocal production_launched

        phases = _get_phases()
        phase_order = [p[0] for p in phases]
        phase_labels = {p[0]: p[1] for p in phases}
        current_phase_idx = 0
        t0 = time.time()

        try:
            async for event in runner.run_async(
                user_id=_state["user_id"],
                session_id=session.id,
                new_message=content,
            ):
                if not event.content or not event.content.parts:
                    continue

                for part in event.content.parts:
                    if part.text:
                        all_text.append(part.text)
                        if not production_launched:
                            reply_parts.append(part.text)

                    if hasattr(part, "function_call") and part.function_call:
                        tool_name = part.function_call.name
                        effective_name = _effective_tool_name(part)
                        logger.info("[CHAT] Tool call | tool=%s | effective=%s | args=%s",
                                   tool_name, effective_name,
                                   str(part.function_call.args)[:200] if part.function_call.args else "")

                        if (effective_name in _PRODUCTION_TOOLS or effective_name == "execution_agent") and not production_launched:
                            production_launched = True
                            _state["status"] = "producing"
                            _state["production_result"] = None
                            await event_bus.clear()
                            for pid, pname in phases:
                                await event_bus.emit(ProductionEvent(
                                    phase=pid, status="pending", detail=pname,
                                ))
                            reply_ready.set()

                        if production_launched and effective_name in _TOOL_TO_PHASE:
                            phase = _TOOL_TO_PHASE[effective_name]
                            if phase in phase_order:
                                phase_idx = phase_order.index(phase)
                                for i in range(current_phase_idx, phase_idx):
                                    if phase_order[i] in phase_labels:
                                        await event_bus.emit(ProductionEvent(
                                            phase=phase_order[i],
                                            status="completed",
                                            detail=f"{phase_labels[phase_order[i]]} — done",
                                            progress=1.0,
                                        ))
                                await event_bus.emit(ProductionEvent(
                                    phase=phase,
                                    status="in_progress",
                                    detail=f"{phase_labels[phase]} ...",
                                    progress=0.5,
                                ))
                                current_phase_idx = phase_idx

                    if (
                        production_launched
                        and hasattr(part, "function_response")
                        and part.function_response
                    ):
                        resp_name = part.function_response.name
                        if resp_name in _TOOL_TO_PHASE:
                            phase = _TOOL_TO_PHASE[resp_name]
                            if phase in phase_order:
                                result_data = part.function_response.response
                                preview = None
                                if isinstance(result_data, dict):
                                    for v in result_data.values():
                                        if isinstance(v, str) and (
                                            v.startswith("http")
                                            and any(
                                                ext in v.lower()
                                                for ext in [".png", ".jpg", ".mp4", ".webm"]
                                            )
                                        ):
                                            preview = v
                                elif isinstance(result_data, str) and result_data.startswith("http"):
                                    preview = result_data

                                await event_bus.emit(ProductionEvent(
                                    phase=phase,
                                    status="completed",
                                    detail=f"{phase_labels[phase]} — done",
                                    progress=1.0,
                                    preview_url=preview,
                                ))
                                current_phase_idx = phase_order.index(phase) + 1

            if production_launched:
                for i in range(current_phase_idx, len(phase_order)):
                    await event_bus.emit(ProductionEvent(
                        phase=phase_order[i],
                        status="completed",
                        detail=f"{phase_labels[phase_order[i]]} — done",
                        progress=1.0,
                    ))

                full_response = "\n".join(all_text)
                video_id = None
                gcs_url = None
                yt_match = re.search(
                    r"(?:youtube\.com/shorts/|video[_ ]?(?:id|ID)[:\s]*)\s*([A-Za-z0-9_-]{11})",
                    full_response,
                )
                if yt_match:
                    video_id = yt_match.group(1)
                gcs_match = re.search(
                    r'(https://storage\.googleapis\.com/\S+\.mp4)', full_response,
                )
                if gcs_match:
                    gcs_url = gcs_match.group(1)

                preview = (f"https://youtube.com/shorts/{video_id}" if video_id
                           else gcs_url)

                _state["production_result"] = {
                    "response": full_response,
                    "video_id": video_id,
                    "youtube_url": f"https://youtube.com/shorts/{video_id}" if video_id else None,
                    "gcs_url": gcs_url,
                }
                _state["status"] = "done"
                await event_bus.emit(ProductionEvent(
                    phase="done",
                    status="completed",
                    detail="Production complete!",
                    progress=1.0,
                    preview_url=preview,
                ))

        except Exception as e:
            logger.error(f"Chat/production failed: {e}\n{traceback.format_exc()}")
            if production_launched:
                _state["status"] = "error"
                _state["error"] = str(e)
                await event_bus.emit(ProductionEvent(
                    phase="error",
                    status="error",
                    detail="Production failed",
                    error_message=str(e),
                ))
        finally:
            if not reply_ready.is_set():
                reply_ready.set()

    asyncio.create_task(_process())

    try:
        await asyncio.wait_for(reply_ready.wait(), timeout=120)
    except asyncio.TimeoutError:
        return {"reply": "Hmm, taking longer than expected. Try again in a bit."}

    reply = "\n".join(reply_parts)
    if not reply:
        reply = "Something went wrong on my end. Try again?"

    return {"reply": reply, "producing": production_launched}


# ── WhatsApp / Twilio webhook ────────────────────────────────────

async def _wa_background_send(entry: dict, session, text: str, from_number: str = "",
                              user_runner=None, user_block_id: str = ""):
    """Process a WhatsApp message in the background and push the reply
    proactively via the Twilio REST API (no second user message needed).

    Filters out intermediate production text (scripts, shot plans) so
    the user only sees: the conversational reply, progress updates, and
    the final video link."""
    _runner = user_runner or runner
    _block_id = user_block_id or _active_block_id
    entry["processing"] = True
    logger.info("[WA_BG] Starting background processing | session=%s | block=%s | text=%s",
               session.id, _block_id, text[:80])
    await _load_user_memory(from_number)
    youtube_mod.configure(from_number)

    limit_msg = await _check_rate_limit(from_number)
    if limit_msg:
        text = (
            f"{text}\n\n[SYSTEM: This user has reached their video limit. "
            f"Do NOT start production or transfer to execution_agent. "
            f"Instead tell them: {limit_msg}]"
        )

    t0 = time.time()

    _BG_RESEARCH_PROGRESS = {
        "search_youtube_trends": "\U0001f50d Looking up what's trending on YouTube...",
        "search_google_trends": "\U0001f4ca Checking Google Trends data...",
        "web_search": "\U0001f310 Researching the latest news...",
    }

    _BG_PROGRESS_MESSAGES = {
        "execution_agent": "\U0001f680 Production started!",
        "write_script": "✍️ Writing the script...",
        "plan_production": "\U0001f4cb Planning the shots...",
        "generate_image": "Generating visuals...",
        "generate_video": "Bringing visuals to life...",
        "concat_videos": "Assembling clips...",
        "generate_voiceover": "Recording voiceover...",
        "generate_background_music": "Composing music...",
        "fit_and_mux_audio": "Mixing audio and adding captions...",
        "mux_music_only": "Adding music...",
        "publish_video": "Almost done! Publishing...",
    }

    try:
        content = genai_types.Content(
            role="user", parts=[genai_types.Part(text=text)],
        )
        reply_parts: list[str] = []
        production_launched = False
        reply_sent = False
        progress_sent: set[str] = set()

        async for event in _runner.run_async(
            user_id=entry["user_id"],
            session_id=session.id,
            new_message=content,
        ):
            if not event.content or not event.content.parts:
                continue
            for part in event.content.parts:
                if part.text and not production_launched:
                    reply_parts.append(part.text)

                if hasattr(part, "function_call") and part.function_call:
                    tool_name = part.function_call.name
                    effective_name = _effective_tool_name(part)
                    logger.info("[WA_BG] Tool call | tool=%s | effective=%s", tool_name, effective_name)

                    if tool_name in _BG_RESEARCH_PROGRESS and tool_name not in progress_sent:
                        progress_sent.add(tool_name)
                        if from_number:
                            _push_whatsapp(from_number, _BG_RESEARCH_PROGRESS[tool_name])

                    if (effective_name in _PRODUCTION_TOOLS or effective_name == "execution_agent") and not production_launched:
                        production_launched = True
                        entry["status"] = "producing"
                        logger.info("[WA_BG] Production launched | session=%s", session.id)
                        await db.create_production(entry["session_id"], _block_id)
                        if reply_parts and from_number and not reply_sent:
                            reply = "\n".join(reply_parts)
                            summary = await _summarize_for_whatsapp(reply)
                            _push_whatsapp(from_number, summary)
                            await db.save_message(entry["session_id"], "assistant", summary)
                            reply_sent = True

                    if production_launched and effective_name in _BG_PROGRESS_MESSAGES:
                        if effective_name not in progress_sent:
                            progress_sent.add(effective_name)
                            if from_number and _twilio_client:
                                _push_whatsapp(from_number, _BG_PROGRESS_MESSAGES[effective_name])

                if hasattr(part, "function_response") and part.function_response:
                    resp_name = part.function_response.name
                    logger.info("[WA_BG] Tool result | tool=%s | result=%s",
                               resp_name,
                               str(part.function_response.response)[:150])
                    if production_launched and resp_name == "publish_video":
                        resp = part.function_response.response
                        if isinstance(resp, dict):
                            entry["production_result"] = {
                                "youtube_url": resp.get("youtube_url", ""),
                                "gcs_url": resp.get("gcs_url", ""),
                                "video_id": resp.get("video_id", ""),
                            }

        if production_launched:
            entry["status"] = "done"
            logger.info("[WA_BG] Production complete | elapsed=%.1fs | result=%s",
                       time.time() - t0, entry.get("production_result"))
            if entry.get("production_result") and from_number:
                result = entry["production_result"]
                url = result.get("youtube_url") or result.get("gcs_url", "")
                msg = f"Your video is ready!\n{url}" if url else "Video production is complete!"
                _push_whatsapp(from_number, msg)
                await db.save_message(entry["session_id"], "assistant", msg)
                entry["last_production_outcome"] = {"status": "success", "url": url, "time": time.time()}
                entry["production_result"] = None
            elif not entry.get("production_result") and from_number:
                entry["last_production_outcome"] = {"status": "failed", "reason": "no video link", "time": time.time()}
                _push_whatsapp(from_number,
                              "Something went wrong — I couldn't produce the video. "
                              "Want me to try again?")
        elif reply_parts and not reply_sent:
            reply = "\n".join(reply_parts)
            logger.info("[WA_BG] Agent replied | len=%d | elapsed=%.1fs", len(reply), time.time() - t0)
            if from_number:
                summary = await _summarize_for_whatsapp(reply)
                _push_whatsapp(from_number, summary)
                await db.save_message(entry["session_id"], "assistant", summary)
            else:
                entry["pending_reply"] = reply
        elif not reply_parts:
            logger.warning("[WA_BG] No reply from agent | elapsed=%.1fs", time.time() - t0)
    except Exception as e:
        logger.error("[WA_BG] Failed | error=%s | elapsed=%.1fs\n%s",
                    e, time.time() - t0, traceback.format_exc())
        entry["status"] = "idle"
        entry["last_production_outcome"] = {"status": "error", "reason": str(e)[:200], "time": time.time()}
        if from_number:
            _push_whatsapp(from_number,
                          "Something went wrong — I couldn't produce the video. "
                          "Want me to try again?")
    finally:
        await _flush_user_memory()
        entry["processing"] = False


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """Twilio WhatsApp webhook — receives form-encoded messages,
    returns TwiML XML responses.  Per-user sessions rotate after
    SESSION_GAP_SECONDS of silence."""
    form = await request.form()
    body = (form.get("Body") or "").strip()
    from_number = form.get("From") or ""

    if not body or not from_number:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    # ── Route non-whitelisted numbers to the legacy backend ──
    if _LEGACY_BACKEND_URL and from_number not in _whitelist_numbers:
        logger.info("[ROUTER] Proxying to legacy | from=%s | body=%s", from_number, body[:80])
        try:
            form_dict = {k: form[k] for k in form}
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, _proxy_to_legacy, form_dict)
            return Response(content=content, media_type="application/xml")
        except Exception as e:
            logger.error("[ROUTER] Legacy proxy failed | error=%s", e)
            return _twiml("Sorry, something went wrong. Please try again.")

    logger.info(f"WhatsApp from {from_number}: {body[:80]}")

    # Resolve per-user content block
    user_runner, user_config, user_block_id = _get_runner_for_user(from_number)

    session, entry = await _get_wa_session(from_number)

    await db.save_message(entry["session_id"], "user", body)

    # ── Brand-new session → instant greeting, process in background ──
    if entry.get("is_new"):
        entry["is_new"] = False
        logger.info("[WA] New session | phone=%s | block=%s | body=%r", from_number, user_block_id, body[:80])

        is_returning = False
        if db.is_enabled():
            existing_user = await db.get_user(from_number)
            if existing_user:
                is_returning = True
                await _load_user_memory(from_number)
        logger.info("[WA] User type | phone=%s | returning=%s", from_number, is_returning)

        _casual = {"hi", "hey", "hello", "yo", "sup", "what's up", "whats up",
                   "how are you", "hiya", "good morning", "good evening"}
        normalized = body.strip().lower().rstrip("!.?")
        is_casual = normalized in _casual
        logger.info("[WA] Greeting check | body=%r | normalized=%r | is_casual=%s",
                   body, normalized, is_casual)
        if is_casual:
            logger.info("[WA] Casual greeting — returning greeting, no agent call")
            block_name = user_config.get("name", "cool stuff")
            if is_returning:
                greeting = (
                    "Welcome back! Ready to make another video?\n\n"
                    "Just say \"let's go\" and we'll pick a topic!"
                )
            else:
                greeting = (
                    f"Hey! I'm Kira — I make YouTube Shorts about "
                    f"{block_name.lower()}, powered by AI.\n\n"
                    "Just say \"let's make a video\" and I'll pitch you "
                    "3 topic ideas and produce a finished Short in about "
                    "5 minutes. You just pick the topic!\n\n"
                    "Ready when you are!"
                )
            await db.save_message(entry["session_id"], "assistant", greeting)
            return _twiml(greeting)
        logger.info("[WA] Non-casual new session — launching background agent")
        asyncio.create_task(
            _wa_background_send(entry, session, body, from_number=from_number,
                                user_runner=user_runner, user_block_id=user_block_id)
        )
        if is_returning:
            ack = "Welcome back! On it — give me a few seconds..."
        else:
            block_name = user_config.get("name", "cool stuff")
            ack = (
                f"Hey! I'm Kira — I make YouTube Shorts about "
                f"{block_name.lower()}, powered by AI.\n\n"
                "On it! Give me a few seconds..."
            )
        await db.save_message(entry["session_id"], "assistant", ack)
        return _twiml(ack)

    # ── Deliver a pending reply from a previous background run ───
    if entry.get("pending_reply"):
        reply = entry.pop("pending_reply")
        reply = await _summarize_for_whatsapp(reply)
        return _twiml(reply)

    # ── Previous message still processing ────────────────────
    if entry.get("processing"):
        return _twiml("Still working on it — I'll message you as soon as I'm done!")

    # ── If production just finished, deliver the result ──────
    if entry["status"] == "done" and entry["production_result"]:
        result = entry["production_result"]
        entry["status"] = "idle"
        entry["production_result"] = None
        url = result.get("youtube_url") or result.get("gcs_url", "")
        reply = f"Your video is ready!\n{url}" if url else "Video production is complete!"
        return _twiml(reply)

    # ── If production is still running ───────────────────────
    if entry["status"] == "producing":
        return _twiml(
            "Still working on your video — I'll send it as soon as it's ready!"
        )

    # ── Recent production outcome (user asking about what happened) ──
    outcome = entry.get("last_production_outcome")
    if outcome and (time.time() - outcome.get("time", 0)) < 600:
        if outcome["status"] == "success":
            url = outcome.get("url", "")
            entry["last_production_outcome"] = None
            entry["status"] = "idle"
            if url:
                return _twiml(f"Your video was already sent! Here it is again:\n{url}")
            # fall through to agent for other questions
        elif outcome["status"] in ("failed", "error"):
            entry["last_production_outcome"] = None
            entry["status"] = "idle"
            return _twiml(
                "The last video didn't make it — something went wrong during production. "
                "Want me to try again with a new topic?"
            )

    # ── Normal conversational flow ───────────────────────────
    entry["status"] = "idle"
    logger.info("[WA] Normal flow | phone=%s | session=%s | body=%s",
               from_number, session.id, body[:80])
    await _load_user_memory(from_number)
    youtube_mod.configure(from_number)

    limit_msg = await _check_rate_limit(from_number)
    msg_text = body
    if limit_msg:
        msg_text = (
            f"{body}\n\n[SYSTEM: This user has reached their video limit. "
            f"Do NOT start production or transfer to execution_agent. "
            f"Instead tell them: {limit_msg}]"
        )

    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=msg_text)],
    )

    reply_parts: list[str] = []
    all_text: list[str] = []
    production_launched = False
    reply_ready = asyncio.Event()
    timed_out = False
    _wa_progress_sent: set[str] = set()

    _WA_RESEARCH_PROGRESS = {
        "search_youtube_trends": "\U0001f50d Looking up what's trending on YouTube...",
        "search_google_trends": "\U0001f4ca Checking Google Trends data...",
        "web_search": "\U0001f310 Researching the latest news...",
    }

    _WA_PROGRESS_MESSAGES = {
        "execution_agent": "\U0001f680 Production started!",
        "write_script": "✍️ Writing the script...",
        "plan_production": "\U0001f4cb Planning the shots...",
        "generate_image": "Generating visuals...",
        "generate_video": "Bringing visuals to life...",
        "concat_videos": "Assembling clips...",
        "generate_voiceover": "Recording voiceover...",
        "generate_background_music": "Visuals ready! Composing music now...",
        "fit_and_mux_audio": "Mixing audio and adding captions...",
        "mux_music_only": "Adding music to the video...",
        "publish_video": "Almost done! Publishing your video...",
    }

    async def _process():
        nonlocal production_launched
        entry["processing"] = True
        t0 = time.time()
        try:
            async for event in user_runner.run_async(
                user_id=entry["user_id"],
                session_id=session.id,
                new_message=content,
            ):
                if not event.content or not event.content.parts:
                    continue
                for part in event.content.parts:
                    if part.text:
                        all_text.append(part.text)
                        if not production_launched:
                            reply_parts.append(part.text)

                    if hasattr(part, "function_call") and part.function_call:
                        tool_name = part.function_call.name
                        effective_name = _effective_tool_name(part)
                        logger.info("[WA] Tool call | tool=%s | effective=%s | args=%s",
                                   tool_name, effective_name,
                                   str(part.function_call.args)[:200] if part.function_call.args else "")

                        if tool_name in _WA_RESEARCH_PROGRESS and tool_name not in _wa_progress_sent:
                            _wa_progress_sent.add(tool_name)
                            if from_number:
                                _push_whatsapp(from_number, _WA_RESEARCH_PROGRESS[tool_name])

                        if (effective_name in _PRODUCTION_TOOLS or effective_name == "execution_agent") and not production_launched:
                            production_launched = True
                            entry["status"] = "producing"
                            logger.info("[WA] Production launched | session=%s", session.id)
                            await db.create_production(entry["session_id"], user_block_id)
                            reply_ready.set()

                        if production_launched and effective_name in _WA_PROGRESS_MESSAGES:
                            if effective_name not in _wa_progress_sent:
                                _wa_progress_sent.add(effective_name)
                                progress_msg = _WA_PROGRESS_MESSAGES[effective_name]
                                logger.info("[WA] Sending progress | phase=%s | msg=%s",
                                           effective_name, progress_msg)
                                if from_number and _twilio_client:
                                    _push_whatsapp(from_number, progress_msg)

                    if hasattr(part, "function_response") and part.function_response:
                        resp_name = part.function_response.name
                        logger.info("[WA] Tool result | tool=%s | result=%s",
                                   resp_name,
                                   str(part.function_response.response)[:150])

                        if (
                            production_launched
                            and resp_name == "publish_video"
                        ):
                            resp = part.function_response.response
                            if isinstance(resp, dict):
                                entry["production_result"] = {
                                    "youtube_url": resp.get("youtube_url", ""),
                                    "gcs_url": resp.get("gcs_url", ""),
                                    "video_id": resp.get("video_id", ""),
                                }

            if production_launched:
                if not entry.get("production_result"):
                    full = "\n".join(all_text)
                    yt = re.search(r'youtube\.com/shorts/([A-Za-z0-9_-]{11})', full)
                    gcs = re.search(r'(https://storage\.googleapis\.com/\S+\.mp4)', full)
                    if yt or gcs:
                        entry["production_result"] = {
                            "youtube_url": f"https://youtube.com/shorts/{yt.group(1)}" if yt else "",
                            "gcs_url": gcs.group(1) if gcs else "",
                        }
                entry["status"] = "done"
                logger.info("[WA] Production complete | elapsed=%.1fs | result=%s",
                           time.time() - t0, entry.get("production_result"))
                if entry.get("production_result") and from_number:
                    result = entry["production_result"]
                    url = result.get("youtube_url") or result.get("gcs_url", "")
                    msg = f"Your video is ready!\n{url}" if url else "Video production is complete!"
                    _push_whatsapp(from_number, msg)
                    await db.save_message(entry["session_id"], "assistant", msg)
                    entry["last_production_outcome"] = {"status": "success", "url": url, "time": time.time()}
                    entry["production_result"] = None
                elif not entry.get("production_result"):
                    logger.error("[WA] Production finished but no video link found!")
                    entry["last_production_outcome"] = {"status": "failed", "reason": "no video link", "time": time.time()}
                    if from_number:
                        _push_whatsapp(from_number,
                                      "Something went wrong — I couldn't produce the video. "
                                      "Want me to try again?")

        except Exception as e:
            logger.error("[WA] Processing failed | error=%s | elapsed=%.1fs\n%s",
                        e, time.time() - t0, traceback.format_exc())
            entry["status"] = "idle"
            entry["last_production_outcome"] = {"status": "error", "reason": str(e)[:200], "time": time.time()}
            if from_number:
                _push_whatsapp(from_number,
                              "Something went wrong — I couldn't produce the video. "
                              "Want me to try again?")
        finally:
            if timed_out and reply_parts:
                reply = "\n".join(reply_parts)
                logger.info("[WA] Delivering timed-out reply | len=%d", len(reply))
                if from_number:
                    summary = await _summarize_for_whatsapp(reply)
                    _push_whatsapp(from_number, summary)
                    await db.save_message(entry["session_id"], "assistant", summary)
                else:
                    entry["pending_reply"] = reply
            entry["processing"] = False
            await _flush_user_memory()
            if not reply_ready.is_set():
                reply_ready.set()

    asyncio.create_task(_process())

    try:
        await asyncio.wait_for(reply_ready.wait(), timeout=14)
    except asyncio.TimeoutError:
        timed_out = True
        logger.info("[WA] Webhook timed out (14s) | will push reply later")
        return _twiml(
            "Still pulling that together — "
            "I'll send you the answer in a few seconds!"
        )

    reply = "\n".join(reply_parts) or "Hmm, let me think about that."

    if production_launched:
        reply = (
            reply.rstrip()
            + "\n\nStarting production now! I'll have your video ready "
            "in a few minutes. I'll keep you posted on progress."
        )

    reply = await _summarize_for_whatsapp(reply)
    await db.save_message(entry["session_id"], "assistant", reply)
    return _twiml(reply)


def _twiml(text: str) -> Response:
    """Wrap a plain-text reply in TwiML XML for Twilio."""
    text = _format_for_whatsapp(text)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{xml_escape(text)}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


@app.post("/api/approve")
async def approve(request: Request):
    """Approve a chosen topic and start async production."""
    if _state["status"] != "proposed" or not _state["current_proposal"]:
        return JSONResponse(
            {"error": "No proposal to approve."},
            status_code=409,
        )

    try:
        body = await request.json()
        chosen_idx = body.get("index", 0)
    except Exception:
        chosen_idx = 0

    proposals = _state["current_proposal"]
    if isinstance(proposals, list) and 0 <= chosen_idx < len(proposals):
        chosen = proposals[chosen_idx]
        _state["chosen_topic"] = chosen.get("topic", "")
    else:
        _state["chosen_topic"] = ""

    _state["status"] = "producing"
    _state["production_result"] = None
    _state["error"] = None
    await event_bus.clear()

    # Emit initial pending phases (dynamic based on active block)
    phases = _get_phases()
    for phase_id, phase_name in phases:
        await event_bus.emit(ProductionEvent(
            phase=phase_id,
            status="pending",
            detail=phase_name,
        ))

    asyncio.create_task(_run_production())
    return {"status": "producing", "message": "Production started."}


async def _run_production():
    """Run the full production pipeline via ADK, emitting events."""
    logger.info("[PRODUCTION] Starting production pipeline")
    youtube_mod.configure("")
    t0 = time.time()
    try:
        session = await _get_or_create_session()
        chosen = _state.get("chosen_topic", "")
        if chosen:
            approval_msg = f'I pick "{chosen}". Go ahead and make it.'
        else:
            approval_msg = "Looks great. Go ahead and make it."
        content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=approval_msg)],
        )

        phases = _get_phases()
        phase_order = [p[0] for p in phases]
        phase_labels = {p[0]: p[1] for p in phases}
        current_phase_idx = 0

        response_parts = []
        async for event in runner.run_async(
            user_id=_state["user_id"],
            session_id=session.id,
            new_message=content,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        response_parts.append(part.text)

                    if hasattr(part, 'function_call') and part.function_call:
                        tool_name = part.function_call.name
                        logger.info("[PRODUCTION] Tool call | tool=%s | args=%s",
                                   tool_name,
                                   str(part.function_call.args)[:200] if part.function_call.args else "")
                        if tool_name in _TOOL_TO_PHASE:
                            phase = _TOOL_TO_PHASE[tool_name]
                            if phase in phase_order:
                                phase_idx = phase_order.index(phase)
                                for i in range(current_phase_idx, phase_idx):
                                    if phase_order[i] in phase_labels:
                                        await event_bus.emit(ProductionEvent(
                                            phase=phase_order[i],
                                            status="completed",
                                            detail=f"{phase_labels[phase_order[i]]} — done",
                                            progress=1.0,
                                        ))
                                await event_bus.emit(ProductionEvent(
                                    phase=phase,
                                    status="in_progress",
                                    detail=f"{phase_labels[phase]} ...",
                                    progress=0.5,
                                ))
                                current_phase_idx = phase_idx

                    if hasattr(part, 'function_response') and part.function_response:
                        resp_name = part.function_response.name
                        logger.info("[PRODUCTION] Tool result | tool=%s | result=%s",
                                   resp_name,
                                   str(part.function_response.response)[:200])
                        if resp_name in _TOOL_TO_PHASE:
                            phase = _TOOL_TO_PHASE[resp_name]
                            if phase in phase_order:
                                result_data = part.function_response.response
                                preview = None
                                if isinstance(result_data, dict):
                                    for v in result_data.values():
                                        if isinstance(v, str) and (
                                            v.startswith("http") and
                                            any(ext in v.lower() for ext in [".png", ".jpg", ".mp4", ".webm"])
                                        ):
                                            preview = v
                                elif isinstance(result_data, str):
                                    if result_data.startswith("http"):
                                        preview = result_data

                                await event_bus.emit(ProductionEvent(
                                    phase=phase,
                                    status="completed",
                                    detail=f"{phase_labels[phase]} — done",
                                    progress=1.0,
                                    preview_url=preview,
                                ))
                                current_phase_idx = phase_order.index(phase) + 1

        # Mark all remaining phases as completed
        for i in range(current_phase_idx, len(phase_order)):
            await event_bus.emit(ProductionEvent(
                phase=phase_order[i],
                status="completed",
                detail=f"{phase_labels[phase_order[i]]} — done",
                progress=1.0,
            ))

        full_response = "\n".join(response_parts)

        video_id = None
        gcs_url = None
        yt_match = re.search(r'(?:youtube\.com/shorts/|video[_ ]?(?:id|ID)[:\s]*)\s*([A-Za-z0-9_-]{11})', full_response)
        if yt_match:
            video_id = yt_match.group(1)
        gcs_match = re.search(r'(https://storage\.googleapis\.com/\S+\.mp4)', full_response)
        if gcs_match:
            gcs_url = gcs_match.group(1)

        preview = (f"https://youtube.com/shorts/{video_id}" if video_id
                   else gcs_url)

        _state["production_result"] = {
            "response": full_response,
            "video_id": video_id,
            "youtube_url": f"https://youtube.com/shorts/{video_id}" if video_id else None,
            "gcs_url": gcs_url,
        }
        _state["status"] = "done"
        logger.info("[PRODUCTION] Complete | video_id=%s | gcs_url=%s | elapsed=%.1fs",
                   video_id, gcs_url, time.time() - t0)

        await event_bus.emit(ProductionEvent(
            phase="done",
            status="completed",
            detail="Production complete!",
            progress=1.0,
            preview_url=preview,
        ))

    except Exception as e:
        logger.error("[PRODUCTION] Failed | error=%s | elapsed=%.1fs\n%s",
                    e, time.time() - t0, traceback.format_exc())
        _state["status"] = "error"
        _state["error"] = str(e)
        await event_bus.emit(ProductionEvent(
            phase="error",
            status="error",
            detail="Production failed",
            error_message=str(e),
        ))


@app.get("/api/production/stream")
async def production_stream(request: Request):
    """SSE endpoint for live production progress."""
    queue = await event_bus.subscribe()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event.to_sse()
                    if event.phase == "done" or event.status == "error":
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await event_bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/history")
async def get_history():
    memory = read_memory()
    return {
        "topics": list(reversed(memory.get("topics", []))),
        "total": len(memory.get("topics", [])),
    }


@app.get("/api/productions")
async def list_productions(phone: str = ""):
    """List video productions, optionally filtered by phone number."""
    if phone:
        rows = await db.get_user_productions(phone)
    elif db.is_enabled():
        async with db._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM productions ORDER BY created_at DESC LIMIT 50"
            )
    else:
        return {"productions": []}
    return {
        "productions": [
            {
                "id": r["id"],
                "topic": r["topic"],
                "gcs_url": r["gcs_url"],
                "youtube_url": r["youtube_url"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@app.get("/api/taste")
async def get_taste():
    memory = read_memory()
    return {
        "standing": memory.get("standing", []),
        "next": memory.get("next"),
    }


@app.post("/api/taste")
async def update_taste(request: Request):
    body = await request.json()
    instruction = body.get("instruction", "").strip()
    instruction_type = body.get("type", "standing")

    if not instruction:
        return JSONResponse({"error": "No instruction provided."}, status_code=400)

    if instruction_type == "next":
        write_memory(next_instruction=instruction)
    else:
        write_memory(standing_instruction=instruction)

    try:
        await _send_message(
            f"User steering: {instruction}. "
            "Save this to memory and acknowledge."
        )
    except Exception:
        pass

    return {"status": "ok", "instruction": instruction, "type": instruction_type}


@app.delete("/api/taste/{index}")
async def remove_taste(index: int):
    """Remove a standing instruction by index."""
    memory = read_memory()
    standing = memory.get("standing", [])
    if 0 <= index < len(standing):
        removed = standing.pop(index)
        memory["standing"] = standing
        save_memory(memory)
        return {"status": "ok", "removed": removed}
    return JSONResponse({"error": "Invalid index."}, status_code=404)


# ── Block API ────────────────────────────────────────────────────

@app.get("/api/blocks")
async def list_blocks_route():
    """List all content blocks."""
    return {"blocks": block_manager.list_blocks()}


@app.get("/api/blocks/active")
async def get_active_block_route():
    """Return the currently active block's config."""
    return _active_config


@app.post("/api/blocks")
async def create_block_route(request: Request):
    """Create a new content block using the meta LLM."""
    if _state["status"] == "producing":
        return JSONResponse(
            {"error": "Cannot create a block while production is in progress."},
            status_code=409,
        )

    try:
        form_data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)

    name = form_data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Block name is required."}, status_code=400)
    if not form_data.get("description", "").strip():
        return JSONResponse({"error": "Block description is required."}, status_code=400)

    try:
        config = await block_manager.create_block(form_data)
        # Auto-activate the new block
        await _activate_block(config["id"])
        return {"block": config, "message": "Block created and activated."}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        logger.error(f"Block creation failed: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/blocks/{block_id}/activate")
async def activate_block_route(block_id: str):
    """Switch to a different content block."""
    if _state["status"] == "producing":
        return JSONResponse(
            {"error": "Cannot switch blocks while production is in progress."},
            status_code=409,
        )

    try:
        await _activate_block(block_id)
        return {"status": "ok", "active_block": _active_config["name"]}
    except FileNotFoundError:
        return JSONResponse({"error": f"Block '{block_id}' not found."}, status_code=404)


@app.delete("/api/blocks/{block_id}")
async def delete_block_route(block_id: str):
    """Delete a content block."""
    if _state["status"] == "producing":
        return JSONResponse(
            {"error": "Cannot delete a block while production is in progress."},
            status_code=409,
        )

    blocks = block_manager.list_blocks_raw()
    if len(blocks) <= 1:
        return JSONResponse({"error": "Cannot delete the last block."}, status_code=409)

    try:
        was_active = block_id == _active_block_id
        block_manager.delete_block(block_id)
        if was_active:
            new_active = block_manager.get_active_block_id()
            if new_active:
                await _activate_block(new_active)
        return {"status": "ok", "message": f"Block '{block_id}' deleted."}
    except FileNotFoundError:
        return JSONResponse({"error": f"Block '{block_id}' not found."}, status_code=404)


# ── WhatsApp simulator API ──────────────────────────────────────

@app.post("/api/wa-sim/send")
async def wa_sim_send(request: Request):
    """Simulate a WhatsApp message by calling the /whatsapp webhook
    with the simulator's phone number. Returns the TwiML response
    body as plain text (the immediate reply)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "No message."}, status_code=400)

    from starlette.datastructures import FormData, UploadFile
    from starlette.requests import Request as StarletteRequest

    class _FakeRequest:
        async def form(self_inner):
            return {"Body": message, "From": _WA_SIM_NUMBER}

    resp = await whatsapp_webhook(_FakeRequest())
    twiml_body = resp.body.decode() if hasattr(resp, "body") else ""
    import re as _re
    text_match = _re.search(r"<Message>(.*?)</Message>", twiml_body, _re.DOTALL)
    text = text_match.group(1) if text_match else ""
    from html import unescape
    text = unescape(text)
    return {"reply": text}


@app.get("/api/wa-sim/push")
async def wa_sim_poll():
    """Poll for proactive push messages queued for the simulator number.
    Returns and drains the queue."""
    msgs = _wa_sim_queues.pop(_WA_SIM_NUMBER, [])
    return {"messages": msgs}


@app.post("/api/wa-sim/reset")
async def wa_sim_reset():
    """Reset the simulator session (like a 2-hour idle timeout)."""
    _wa_sessions.pop(_WA_SIM_NUMBER, None)
    _wa_sim_queues.pop(_WA_SIM_NUMBER, None)
    return {"status": "ok"}


# ── Whitelist management API ──────────────────────────────────────

@app.get("/api/whitelist")
async def get_whitelist():
    """List all whitelisted numbers."""
    return {
        "numbers": sorted(_whitelist_numbers),
        "legacy_backend": _LEGACY_BACKEND_URL or None,
        "routing_enabled": bool(_LEGACY_BACKEND_URL),
    }


@app.post("/api/whitelist")
async def add_to_whitelist(request: Request):
    """Add a phone number to the whitelist. Body: {"number": "+1234567890"}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    number = body.get("number", "").strip()
    if not number:
        return JSONResponse({"error": "No number provided."}, status_code=400)
    if not number.startswith("whatsapp:"):
        number = f"whatsapp:{number}"
    _whitelist_numbers.add(number)
    logger.info("[WHITELIST] Added %s | total=%d", number, len(_whitelist_numbers))
    return {"status": "added", "number": number, "total": len(_whitelist_numbers)}


@app.delete("/api/whitelist")
async def remove_from_whitelist(request: Request):
    """Remove a phone number from the whitelist. Body: {"number": "+1234567890"}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    number = body.get("number", "").strip()
    if not number:
        return JSONResponse({"error": "No number provided."}, status_code=400)
    if not number.startswith("whatsapp:"):
        number = f"whatsapp:{number}"
    _whitelist_numbers.discard(number)
    logger.info("[WHITELIST] Removed %s | total=%d", number, len(_whitelist_numbers))
    return {"status": "removed", "number": number, "total": len(_whitelist_numbers)}


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
