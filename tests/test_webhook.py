"""Tests for the Vercel webhook entrypoint (api/webhook.py) against the
seven hardening requirements. Runs the Flask app's test client; no real
Telegram/OpenAI/network calls happen here (voice download and callback-query
paths that would need real HTTP are covered separately or left to manual
testing against a live deployment).
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_WEBHOOK_SECRET": "test-secret",
    "ALLOWED_CHAT_IDS": "111,-100222",
    "OPENAI_API_KEY": "x",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "webhook_test.db"))

    import api.webhook as webhook_module

    importlib.reload(webhook_module)
    webhook_module.app.config["TESTING"] = True
    return webhook_module.app.test_client()


def _post(client, body, secret="test-secret"):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return client.post("/api/webhook", json=body, headers=headers)


def _text_update(update_id=1, chat_id=111, text="hello", is_bot=False):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": is_bot},
            "text": text,
        },
    }


def test_missing_secret_is_401(client):
    resp = _post(client, _text_update(), secret=None)
    assert resp.status_code == 401


def test_wrong_secret_is_401(client):
    resp = _post(client, _text_update(), secret="wrong")
    assert resp.status_code == 401


def test_correct_secret_processes_and_returns_200(client):
    resp = _post(client, _text_update(update_id=1))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_disallowed_chat_is_ignored_but_200(client):
    resp = _post(client, _text_update(update_id=2, chat_id=999))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored_chat"


def test_bot_own_message_is_ignored_but_200(client):
    resp = _post(client, _text_update(update_id=3, is_bot=True))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored_self"


def test_duplicate_update_id_is_a_noop_but_200(client):
    first = _post(client, _text_update(update_id=4))
    assert first.status_code == 200
    assert first.get_json().get("status") != "duplicate"

    second = _post(client, _text_update(update_id=4))
    assert second.status_code == 200
    assert second.get_json()["status"] == "duplicate"


def test_channel_post_update_type_is_handled(client):
    update = {
        "update_id": 5,
        "channel_post": {
            "message_id": 2,
            "date": 1700000000,
            "chat": {"id": -100222, "type": "channel"},
            "text": "a note from the channel",
        },
    }
    resp = _post(client, update)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_note_actually_stored_for_allowed_chat(client, tmp_path):
    from app import db

    _post(client, _text_update(update_id=6, chat_id=111, text="a real note"))

    with db.connect(tmp_path / "webhook_test.db") as conn:
        rows = conn.execute("SELECT raw_text FROM notes").fetchall()
        assert any(r["raw_text"] == "a real note" for r in rows)


def test_bad_json_body_returns_200_not_500(client):
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret", "Content-Type": "application/json"}
    resp = client.post("/api/webhook", data="not json", headers=headers)
    assert resp.status_code == 200


def test_missing_env_config_returns_200_not_crash(monkeypatch, tmp_path):
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import api.webhook as webhook_module

    importlib.reload(webhook_module)
    webhook_module.app.config["TESTING"] = True
    c = webhook_module.app.test_client()

    resp = c.post("/api/webhook", json=_text_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "anything"})
    assert resp.status_code == 200
    assert resp.get_json()["error"] == "misconfigured"
