from app.published import LINKEDIN_POSTS, examples_for, load_post_text


def test_four_linkedin_posts_registered():
    assert len(LINKEDIN_POSTS) == 4
    assert {p.id for p in LINKEDIN_POSTS} == {"LI1", "LI2", "LI3", "LI4"}


def test_load_post_text_strips_category_line():
    text = load_post_text(LINKEDIN_POSTS[0])
    assert not text.startswith("Category:")
    assert len(text.split()) > 100


def test_examples_for_prioritises_matching_type():
    chosen = examples_for("founder_data")
    assert chosen[0].piece_type == "founder_data"
    assert 2 <= len(chosen) <= 3


def test_examples_for_unknown_type_still_returns_posts():
    chosen = examples_for("something_unexpected")
    assert len(chosen) >= 2
