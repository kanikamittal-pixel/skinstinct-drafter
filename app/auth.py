"""Access control: only Meera's own Telegram account and her private notes
channel may interact with this bot. Everything else is silently ignored -
no reply, no error message, just a log line - so the bot doesn't advertise
its existence to anyone probing it.
"""
from __future__ import annotations

from telegram import Update

from app.config import Config


def is_from_meera(update: Update, config: Config) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.meera_user_id


def is_from_notes_channel(update: Update, config: Config) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == config.notes_channel_id


def is_meera_private_chat(update: Update, config: Config) -> bool:
    chat = update.effective_chat
    return (
        chat is not None
        and chat.type == "private"
        and is_from_meera(update, config)
    )
