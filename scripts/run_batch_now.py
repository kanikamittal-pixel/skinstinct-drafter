"""Manually triggers one batch run immediately, for testing without waiting
for Monday/Thursday 8am. Needs a real .env (Telegram + OpenAI credentials).

Usage: python scripts/run_batch_now.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram.ext import Application  # noqa: E402

from app import batch, db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.llm import LLMClient  # noqa: E402


async def main() -> None:
    config = load_config()
    db.init_db(config.db_path)
    conn = db.open_connection(config.db_path)
    llm = LLMClient(config.openai_api_key, config.openai_model, conn=conn, transcribe_model=config.openai_transcribe_model)

    application = Application.builder().token(config.telegram_bot_token).build()
    application.bot_data["config"] = config
    application.bot_data["conn"] = conn
    application.bot_data["llm"] = llm

    async with application:
        context = SimpleNamespace(bot=application.bot, bot_data=application.bot_data)
        await batch.run_batch(context)

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
