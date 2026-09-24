"""Environment configuration for the Vercel webhook entrypoint (api/webhook.py).

Separate from app/config.py's Config: the long-polling bot (app/main.py)
uses two distinct values, MEERA_USER_ID and NOTES_CHANNEL_ID, because it
tells the two chat types apart to route messages differently. The webhook
instead uses one general allowlist, ALLOWED_CHAT_IDS, since both her
private chat and her notes channel just need "is this an ID we trust" -
the update type ("message" vs "channel_post") already tells the handler
which one it's looking at.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class WebhookConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WebhookConfigError(
            f"{name} is not set. Add it in your Vercel project's Environment Variables."
        )
    return value


@dataclass(frozen=True)
class WebhookConfig:
    bot_token: str
    webhook_secret: str
    allowed_chat_ids: frozenset[int]
    openai_api_key: str
    openai_model: str
    db_path: str
    # Per-outside-call timeout budget. Kept short and configurable because
    # the whole handler must finish inside Vercel's function duration limit -
    # see README's "Vercel webhook" section for the actual numbers per plan.
    outbound_timeout_seconds: float


def load_webhook_config() -> WebhookConfig:
    raw_ids = _require("ALLOWED_CHAT_IDS")
    try:
        allowed = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
    except ValueError as exc:
        raise WebhookConfigError(
            f"ALLOWED_CHAT_IDS must be comma-separated integers, got {raw_ids!r}"
        ) from exc
    if not allowed:
        raise WebhookConfigError("ALLOWED_CHAT_IDS is empty.")

    timeout_raw = os.environ.get("OUTBOUND_TIMEOUT_SECONDS", "12").strip()
    try:
        outbound_timeout = float(timeout_raw)
    except ValueError as exc:
        raise WebhookConfigError(
            f"OUTBOUND_TIMEOUT_SECONDS must be a number, got {timeout_raw!r}"
        ) from exc

    return WebhookConfig(
        bot_token=_require("TELEGRAM_BOT_TOKEN"),
        webhook_secret=_require("TELEGRAM_WEBHOOK_SECRET"),
        allowed_chat_ids=allowed,
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip(),
        # /tmp is the only writable path in a Vercel serverless function, and
        # it is NOT reliably persistent - see the README callout. Fine for
        # local `vercel dev` testing; production needs a hosted DB.
        db_path=os.environ.get("DB_PATH", "/tmp/skinstinct.db").strip(),
        outbound_timeout_seconds=outbound_timeout,
    )
