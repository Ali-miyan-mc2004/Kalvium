"""Runtime settings, read from the environment and an optional .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LESSON_MAX_MINUTES = 45   # hard limit from the brief
LESSON_MIN_MINUTES = 35   # below this the lesson is probably too thin
READING_WPM = 180         # conservative pace for technical material
STUDY_FACTOR = 2          # time to reread and think, as a multiple of reading time


def user_agent() -> str:
    """Identify this tool honestly. Wikimedia's policy requires contact details."""
    contact = os.environ.get("LESSONFORGE_CONTACT", "").strip()
    return f"LessonForge/0.1 ({contact or 'local educational research tool'})"


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    qc_model: str
    tavily_api_key: str
    youtube_api_key: str
    db_path: Path


def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        llm_provider=env("LLM_PROVIDER", "anthropic").strip().lower(),
        anthropic_api_key=env("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=env("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        openai_api_key=env("OPENAI_API_KEY", "").strip(),
        openai_base_url=env("OPENAI_BASE_URL", "").strip(),
        openai_model=env("OPENAI_MODEL", "").strip(),
        qc_model=env("QC_MODEL", "").strip(),
        tavily_api_key=env("TAVILY_API_KEY", "").strip(),
        youtube_api_key=env("YOUTUBE_API_KEY", "").strip(),
        db_path=Path(env("LESSONFORGE_DB", str(ROOT / "data" / "lessonforge.db"))),
    )
