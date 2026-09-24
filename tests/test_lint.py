from app import lint


def test_autofix_replaces_em_dash_with_spaced_hyphen():
    assert lint.autofix("This is true—it is also useless.") == "This is true - it is also useless."


def test_autofix_replaces_en_dash():
    text = "pH 2.5–3.5 is the range."
    assert "–" not in lint.autofix(text)


def test_autofix_strips_hashtags_and_emoji():
    fixed = lint.autofix("Great point #skincare #glow \U0001F600")
    assert "#" not in fixed
    assert "\U0001F600" not in fixed


def test_autofix_british_spelling_case_preserving():
    assert lint.autofix("It will Oxidize quickly.") == "It will Oxidise quickly."
    assert lint.autofix("OXIDIZE") == "OXIDISE"
    assert lint.autofix("this can oxidize.") == "this can oxidise."


def test_check_flags_exclamation_and_semicolon():
    result = lint.check("This works! It is also true; trust it.")
    assert any("exclamation" in w for w in result.warnings)
    assert any("semicolon" in w for w in result.warnings)


def test_check_flags_banned_words():
    result = lint.check("This serum will transform your glow. " * 40)
    assert any("banned word" in w for w in result.warnings)


def test_check_flags_missing_verify_tag_on_scientific_claim():
    text = "The pH of this formula is 5.5. " * 30
    result = lint.check(text)
    assert any("VERIFY" in w for w in result.warnings)


def test_check_does_not_flag_verify_when_present():
    text = "[VERIFY]The pH of this formula is 5.5[/VERIFY]. " * 30
    result = lint.check(text)
    assert not any("no [VERIFY] tag" in w for w in result.warnings)


def test_word_count_bounds():
    short = lint.check("Too short. " * 5)
    assert any("below" in w for w in short.warnings)

    long_text = ("This is a moderately long sentence about formulation. " * 100)
    long_result = lint.check(long_text)
    assert any("above" in w for w in long_result.warnings)


def test_ends_on_question_flagged():
    result = lint.check("Some setup. " * 40 + "Is that not worth asking?")
    assert any("ends on a question" in w for w in result.warnings)


def test_strip_verify_tags_keeps_text():
    raw = "The [VERIFY]pH is 5.5[/VERIFY] and stable."
    assert lint.strip_verify_tags(raw) == "The pH is 5.5 and stable."


def test_count_verify_tags():
    raw = "[VERIFY]a[/VERIFY] and [VERIFY]b[/VERIFY]"
    assert lint.count_verify_tags(raw) == 2
