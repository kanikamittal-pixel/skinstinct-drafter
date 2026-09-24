"""Extracts the drafting-relevant slice of docs/voice_extraction.md.

Per the file's own header note:
    Drafting instructions: Sections 3A, 8, 16, 17, 18, and the LinkedIn
    column of Section 15.
    Background only: everything else. Do not paste the whole file into a
    prompt.
    Voice target: LinkedIn only. Ignore newsletter-specific traits.

This module pulls exactly those sections out of the markdown file and drops
the newsletter-specific lines a naive extraction would otherwise carry over
into a LinkedIn-only prompt (rule 27 in Section 16, the newsletter half of
one checklist item in Section 17, and the email-only opening variant in
Section 18's fingerprint table).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VOICE_FILE_CandidatesA = ROOT / "docs" / "voice_extraction.md"
DEFAULT_VOICE_FILE_CandidatesB = ROOT / "docs" / "voice_extraction.txt"

SECTIONS_NEEDED = ["3A", "8", "15", "16", "17", "18"]

HEADING_RE = re.compile(r"^##\s+(\d+[A-Z]?)\.\s+(.+)$", re.MULTILINE)


def _default_voice_file() -> Path:
    if DEFAULT_VOICE_FILE_CandidatesA.exists():
        return DEFAULT_VOICE_FILE_CandidatesA
    return DEFAULT_VOICE_FILE_CandidatesB


def _split_sections(text: str) -> dict[str, str]:
    """Split the file into {section_number: body_text} by '## N. Title' headings."""
    matches = list(HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        num = m.group(1)
        title = m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[num] = f"## {num}. {title}\n" + text[start:end].strip()
    return sections


def _keep_linkedin_column(section_15: str) -> str:
    """Section 15 is a 3-column table (Dimension | LinkedIn | email). Keep only
    the Dimension and LinkedIn columns, since newsletter traits are out of scope.
    """
    out_lines = []
    for line in section_15.splitlines():
        if line.strip().startswith("|") and line.count("|") >= 4:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                out_lines.append(f"| {cells[0]} | {cells[1]} |")
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _drop_line_containing(text: str, *needles: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not any(needle in line for needle in needles)
    )


def _strip_email_opening_variant(section_18: str) -> str:
    # Row: "| Opening | ... or (in email) a first-person statement ... |"
    # Keep the LinkedIn-relevant openings, drop the parenthetical email carve-out.
    return re.sub(
        r"\s*, or \(in email\)[^|]*(?=\|)",
        "",
        section_18,
    )


def build_voice_prompt(voice_file: Path | None = None) -> str:
    """Returns the exact text handed to the LLM as drafting instructions:
    Sections 3A, 8, 16, 17, 18 in full, and Section 15 with only its
    Dimension / LinkedIn columns kept.
    """
    path = voice_file or _default_voice_file()
    if not path.exists():
        raise FileNotFoundError(
            f"Voice file not found at {path}. Place it at docs/voice_extraction.md."
        )
    raw = path.read_text(encoding="utf-8")
    sections = _split_sections(raw)

    missing = [n for n in SECTIONS_NEEDED if n not in sections]
    if missing:
        raise ValueError(
            f"Voice file is missing expected section(s) {missing}. "
            f"Found sections: {sorted(sections)}"
        )

    s15 = _keep_linkedin_column(sections["15"])
    s16 = _drop_line_containing(
        sections["16"], "Warm sign-off formula"
    )
    s17 = sections["17"].replace(
        "- [ ] Newsletter: \"Hi,\" / \"Meera\", no sign-off formula. LinkedIn: no greeting.",
        "- [ ] No greeting, no sign-off.",
    )
    s18 = _strip_email_opening_variant(sections["18"])

    header = (
        "# Meera Pillai - voice rules for LinkedIn drafting\n\n"
        "These are drafting instructions extracted from the full voice-extraction "
        "file. Follow them exactly. Write for LinkedIn only: no greeting, no "
        "sign-off, no hashtags, no emojis, no exclamation marks, British spelling.\n"
    )

    return "\n\n".join([
        header.strip(),
        sections["3A"],
        sections["8"],
        s15,
        s16,
        s17,
        s18,
    ]).strip() + "\n"
