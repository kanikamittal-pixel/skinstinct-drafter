from types import SimpleNamespace

from app import auth
from app.config import Config


def _config() -> Config:
    return Config(
        telegram_bot_token="x", meera_user_id=111, notes_channel_id=-100222,
        openai_api_key="x", openai_model="x", openai_transcribe_model="x", db_path="x",
        batch_timezone="Asia/Kolkata", batch_time="08:00",
    )


def _update(user_id=None, chat_id=None, chat_type=None):
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    chat = SimpleNamespace(id=chat_id, type=chat_type) if chat_id is not None else None
    return SimpleNamespace(effective_user=user, effective_chat=chat)


def test_is_from_meera_true_for_her_id():
    config = _config()
    assert auth.is_from_meera(_update(user_id=111), config) is True


def test_is_from_meera_false_for_stranger():
    config = _config()
    assert auth.is_from_meera(_update(user_id=999), config) is False


def test_is_from_notes_channel():
    config = _config()
    assert auth.is_from_notes_channel(_update(chat_id=-100222), config) is True
    assert auth.is_from_notes_channel(_update(chat_id=-100333), config) is False


def test_is_meera_private_chat_requires_both():
    config = _config()
    assert auth.is_meera_private_chat(
        _update(user_id=111, chat_id=111, chat_type="private"), config
    ) is True
    assert auth.is_meera_private_chat(
        _update(user_id=999, chat_id=999, chat_type="private"), config
    ) is False
    assert auth.is_meera_private_chat(
        _update(user_id=111, chat_id=-100222, chat_type="channel"), config
    ) is False
