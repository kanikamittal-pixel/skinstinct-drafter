"""Writes the extracted drafting-only slice of the voice file to
docs/voice_prompt.txt, so you can read exactly what gets sent to the LLM.

Usage: python scripts/build_voice_prompt.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.voice import build_voice_prompt  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "voice_prompt.txt"


def main() -> None:
    prompt = build_voice_prompt()
    OUT.write_text(prompt, encoding="utf-8")
    print(f"wrote {OUT} ({len(prompt)} chars)")


if __name__ == "__main__":
    main()
