"""Registry of Meera's published LinkedIn posts, used as few-shot examples.

Only the 4 LinkedIn posts (LI1-LI4) are ever used as drafting examples - see
docs/voice_extraction.md, header note: newsletters are a different format
(greeting/sign-off, relationship close) and are explicitly out of scope for
LinkedIn drafting. The newsletters still live in data/published/ for
completeness but nothing in app/ reads them.

Piece "type" here matches the categories in Section 2 of the voice file
("Structure by piece type"), restricted to the LinkedIn pieces:
  - explainer            (LI1: Ingredient and formulation explainers)
  - founder_data         (LI2, LI3: Founder and company data)
  - industry_transparency (LI4: Industry transparency)

A note is tagged with the closest of these three during cleaning (see
llm.py / prompts/clean_tag.txt), and drafting.py uses the tag to pick
which LinkedIn posts go into the prompt as examples.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = ROOT / "data" / "published"

PIECE_TYPES = ("explainer", "founder_data", "industry_transparency")


@dataclass(frozen=True)
class LinkedInPost:
    id: str  # LI1..LI4
    filename: str
    piece_type: str


LINKEDIN_POSTS: list[LinkedInPost] = [
    LinkedInPost("LI1", "linkedin_post_001.txt", "explainer"),
    LinkedInPost("LI2", "linkedin_post_002.txt", "founder_data"),
    LinkedInPost("LI3", "linkedin_post_003.txt", "founder_data"),
    LinkedInPost("LI4", "linkedin_post_004.txt", "industry_transparency"),
]


def load_post_text(post: LinkedInPost) -> str:
    path = PUBLISHED_DIR / post.filename
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python scripts/split_published.py <pdf>` "
            "to extract the published pieces first."
        )
    text = path.read_text(encoding="utf-8")
    # Strip the "Category: ..." line the extractor keeps at the top; it's
    # metadata for us, not something to feed to the model as content.
    lines = text.splitlines()
    if lines and lines[0].startswith("Category:"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def examples_for(piece_type: str, *, count: int = 3) -> list[LinkedInPost]:
    """Pick 2-3 LinkedIn posts as few-shot examples: all posts of the note's
    matching type, then fill out to `count` with the remaining LI posts in
    their fixed order, so the selection is deterministic.
    """
    matching = [p for p in LINKEDIN_POSTS if p.piece_type == piece_type]
    rest = [p for p in LINKEDIN_POSTS if p.piece_type != piece_type]
    chosen = (matching + rest)[:count]
    return chosen if len(chosen) >= 2 else LINKEDIN_POSTS[:2]
