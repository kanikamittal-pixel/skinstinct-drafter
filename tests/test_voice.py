from app.voice import build_voice_prompt


def test_includes_required_sections():
    prompt = build_voice_prompt()
    for heading in [
        "## 3A. Language mechanics",
        "## 8. The Meera do-not-do list",
        "## 15. LinkedIn vs newsletter",
        "## 16. Style rulebook",
        "## 17. Style checklist",
        "## 18. Style fingerprint",
    ]:
        assert heading in prompt


def test_excludes_background_only_sections():
    prompt = build_voice_prompt()
    for heading in [
        "## 1. Voice DNA",
        "## 2. Writing formula",
        "## 4. Opening system",
        "## 9. Vocabulary",
        "## 12. Reader relationship",
    ]:
        assert heading not in prompt


def test_section_15_drops_email_column():
    prompt = build_voice_prompt()
    assert "Meera in email" not in prompt


def test_no_newsletter_signoff_rule():
    prompt = build_voice_prompt()
    assert "Warm sign-off formula" not in prompt


def test_no_email_opening_carveout_in_fingerprint():
    prompt = build_voice_prompt()
    assert "(in email)" not in prompt
