"""Thin wrapper around the OpenAI API: retries, JSON-schema output,
transcription, and logging. This replaces app/gemini.py - the project
switched providers from Gemini to OpenAI.

Every call is logged (component + event + short detail), but request/
response bodies containing note text are not dumped into logs.detail
beyond a short excerpt, and the API key itself is never logged.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from app import db

MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3.0
# 429 (rate limit) and 5xx ("overloaded"/"try again") need longer backoff
# than a generic transient error - short retries just hit the same window.
OVERLOAD_BACKOFF_SECONDS = 8.0

DEFAULT_TRANSCRIBE_MODEL = "whisper-1"


class LLMError(RuntimeError):
    pass


def _is_overloaded(exc: Exception) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in ("429", "rate_limit", "rate limit", "503", "overloaded", "server_error", "try again")
    )


def _backoff_seconds(attempt: int, exc: Exception) -> float:
    base = OVERLOAD_BACKOFF_SECONDS if _is_overloaded(exc) else RETRY_BACKOFF_SECONDS
    return base * attempt


def _strict_json_schema(schema: dict) -> dict:
    """Converts a plain JSON-schema dict into the shape OpenAI's strict
    structured-output mode requires: every object needs
    `additionalProperties: false` and every property listed in `required`
    (OpenAI's strict mode has no notion of an optional key). A property that
    was optional, or explicitly marked `"nullable": true`, becomes required
    but with `null` added to its `type` instead - same effective meaning,
    different spelling.
    """
    schema = copy.deepcopy(schema)

    def make_nullable(node: dict) -> None:
        node.pop("nullable", None)
        t = node.get("type")
        if isinstance(t, list):
            if "null" not in t:
                t.append("null")
        elif t is not None and t != "null":
            node["type"] = [t, "null"]

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if node.get("type") == "object" and "properties" in node:
            original_required = set(node.get("required", []))
            props = node["properties"]
            for key, sub in props.items():
                walk(sub)
                was_optional = key not in original_required
                is_nullable_flag = sub.get("nullable") is True
                if was_optional or is_nullable_flag:
                    make_nullable(sub)
            node["required"] = list(props.keys())
            node["additionalProperties"] = False
        elif node.get("type") == "array" and "items" in node:
            walk(node["items"])
        return node

    return walk(schema)


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        conn: Optional[sqlite3.Connection] = None,
        *,
        timeout_seconds: Optional[float] = None,
        transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
    ):
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self.model = model
        self.transcribe_model = transcribe_model
        self._conn = conn

    def _log(self, event: str, **detail: Any) -> None:
        if self._conn is not None:
            db.log(self._conn, "info", "llm", event, **detail)

    def generate_json(
        self,
        prompt: str,
        *,
        response_schema: dict,
        temperature: float = 0.4,
        schema_name: str = "response",
    ) -> dict:
        """Calls the model asking for JSON matching response_schema. Retries
        on transient failures and on invalid JSON.
        """
        schema = _strict_json_schema(response_schema)
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                    },
                )
                text = response.choices[0].message.content
                if not text:
                    raise LLMError("empty response from OpenAI")
                parsed = json.loads(text)
                self._log("generate_json.ok", attempt=attempt, chars=len(text))
                return parsed
            except Exception as exc:  # noqa: BLE001 - we want to retry broadly and log
                last_error = exc
                self._log("generate_json.error", attempt=attempt, error=str(exc))
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt, exc))
        raise LLMError(f"OpenAI call failed after {MAX_RETRIES} attempts: {last_error}")

    def transcribe_audio(self, audio_path: Path) -> str:
        """Transcribes a voice note via the Whisper API. Retries on
        transient failures.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with open(audio_path, "rb") as f:
                    response = self._client.audio.transcriptions.create(
                        model=self.transcribe_model, file=f,
                    )
                text = (getattr(response, "text", None) or str(response)).strip()
                if not text:
                    raise LLMError("empty transcript from OpenAI")
                self._log("transcribe.ok", attempt=attempt, path=str(audio_path))
                return text
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._log("transcribe.error", attempt=attempt, error=str(exc))
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt, exc))
        raise LLMError(f"Transcription failed after {MAX_RETRIES} attempts: {last_error}")
