"""Minimal synchronous Telegram Bot API client for the webhook path.

api/webhook.py runs as a plain synchronous Flask handler (Vercel's Python
runtime), so it can't use python-telegram-bot's async Bot/Application (that
stays in app/main.py for the long-polling process). This wraps the handful
of calls the webhook needs, each with the configured outbound timeout.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramAPIError(RuntimeError):
    pass


def _call(token: str, method: str, *, timeout: float, **params: Any) -> dict:
    url = API_BASE.format(token=token, method=method)
    try:
        resp = httpx.post(url, json=params, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise TelegramAPIError(f"{method} timed out after {timeout}s") from exc
    data = resp.json()
    if not data.get("ok"):
        raise TelegramAPIError(f"{method} failed: {data}")
    return data["result"]


def send_message(
    token: str, *, chat_id: int, text: str, timeout: float,
    reply_markup: Optional[dict] = None, parse_mode: Optional[str] = None,
) -> dict:
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    if parse_mode is not None:
        params["parse_mode"] = parse_mode
    return _call(token, "sendMessage", timeout=timeout, **params)


def answer_callback_query(token: str, *, callback_query_id: str, text: str = "", timeout: float) -> dict:
    return _call(
        token, "answerCallbackQuery", timeout=timeout,
        callback_query_id=callback_query_id, text=text,
    )


def get_file_path(token: str, *, file_id: str, timeout: float) -> str:
    result = _call(token, "getFile", timeout=timeout, file_id=file_id)
    return result["file_path"]


def download_file(token: str, *, file_path: str, dest: Path, timeout: float) -> None:
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.TimeoutException as exc:
        raise TelegramAPIError(f"file download timed out after {timeout}s") from exc
