"""Split the published-corpus PDF into one text file per piece in data/published/.

Usage: python scripts/split_published.py "/path/to/meera writing style.pdf"

Files are named linkedin_post_001.txt ... newsletter_011.txt. The voice file cites
them as LI1-LI4 and NL1-NL11 (see app/published.py for the mapping).
"""
import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "published"

HEADER = re.compile(r"^[─\-–—]{1,3}\s*((?:linkedin_post|newsletter)_\d{3})\s*[─\-–—]{1,3}\s*$")
RULE = re.compile(r"^[━─—_]{5,}\s*$|^[━─—]\s*$")
SECTION_TITLES = {"LINKEDIN POSTS (4)", "EMAIL NEWSLETTERS (11)", "END OF DOCUMENT"}


def reflow(lines: list[str]) -> str:
    """Join PDF-wrapped lines into paragraphs. Blank lines mark paragraph breaks."""
    paras, cur = [], []
    for line in lines:
        if not line.strip():
            if cur:
                paras.append(" ".join(cur))
                cur = []
        else:
            cur.append(line.strip())
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras).strip() + "\n"


def main(pdf_path: str) -> None:
    text = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in PdfReader(pdf_path).pages)
    pieces: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = HEADER.match(line.strip())
        if m:
            current = m.group(1)
            pieces[current] = []
            continue
        if RULE.match(line.strip()) or line.strip() in SECTION_TITLES:
            current = None if line.strip() in SECTION_TITLES else current
            continue
        if current:
            pieces[current].append(line)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, lines in pieces.items():
        (OUT / f"{name}.txt").write_text(reflow(lines), encoding="utf-8")
        print(f"wrote {name}.txt")


if __name__ == "__main__":
    main(sys.argv[1])
