"""Environment configuration. Fails fast and loudly if anything required is missing.

All secrets and IDs come from the environment (loaded from a .env file in
development). Nothing sensitive is ever hard-coded here or anywhere else in
the app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    meera_user_id: int
    notes_channel_id: int
    openai_api_key: str
    openai_model: str
    openai_transcribe_model: str
    db_path: Path
    batch_timezone: str
    batch_time: str  # "HH:MM"

    @property
    def batch_hour_minute(self) -> tuple[int, int]:
        hh, mm = self.batch_time.split(":")
        return int(hh), int(mm)


def load_config() -> Config:
    db_path = Path(os.environ.get("DB_PATH", "data/skinstinct.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        meera_user_id=_require_int("MEERA_USER_ID"),
        notes_channel_id=_require_int("NOTES_CHANNEL_ID"),
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o").strip(),
        openai_transcribe_model=os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip(),
        db_path=db_path,
        batch_timezone=os.environ.get("BATCH_TIMEZONE", "Asia/Kolkata").strip(),
        batch_time=os.environ.get("BATCH_TIME", "08:00").strip(),
    )
