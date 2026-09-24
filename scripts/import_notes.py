"""One-off import of the 60 backlog notes from data/notes/ into the database.

Run once on setup: python scripts/import_notes.py

Each file in data/notes/ becomes one note with source='import', type='text'
(voice backlog notes should be audio files - see the --audio-ext handling
below - and are imported as type='voice' with transcription deferred to the
next batch, same as notes collected live).

Timestamp: uses the file's own modified time unless the filename starts
with an ISO date (YYYY-MM-DD), which takes precedence. This is a best
effort for backlog notes that never had a Telegram timestamp; if you know
the real capture dates, rename the files "YYYY-MM-DD_whatever.txt" first.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402

NOTES_DIR = Path(__file__).resolve().parent.parent / "data" / "notes"
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
TEXT_EXTS = {".txt", ".md"}
AUDIO_EXTS = {".ogg", ".mp3", ".m4a", ".wav"}


def _timestamp_for(path: Path) -> str:
    m = DATE_PREFIX_RE.match(path.stem)
    if m:
        return f"{m.group(1)}T00:00:00Z"
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return mtime.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    if not NOTES_DIR.exists():
        print(f"No {NOTES_DIR} directory found - nothing to import.")
        return

    config = load_config()
    db.init_db(config.db_path)

    files = sorted(p for p in NOTES_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        print(f"No files found in {NOTES_DIR}.")
        return

    imported = 0
    with db.connect(config.db_path) as conn:
        for path in files:
            ext = path.suffix.lower()
            created_at = _timestamp_for(path)
            if ext in TEXT_EXTS:
                text = path.read_text(encoding="utf-8").strip()
                if not text:
                    print(f"skip (empty): {path.name}")
                    continue
                db.insert_note(conn, source="import", type_="text", created_at=created_at, raw_text=text)
                imported += 1
            elif ext in AUDIO_EXTS:
                db.insert_note(conn, source="import", type_="voice", created_at=created_at, audio_path=str(path))
                imported += 1
            else:
                print(f"skip (unrecognised extension): {path.name}")
                continue
            print(f"imported: {path.name}")

    print(f"\nImported {imported} of {len(files)} files from {NOTES_DIR}.")


if __name__ == "__main__":
    main()
