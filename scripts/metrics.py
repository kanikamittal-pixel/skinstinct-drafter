"""Prints the three headline metrics from the brief:
  - posts approved per week
  - median/average time for Meera to approve each draft (first-version sent -> approved)
  - how many approved drafts needed little or no revision (version <= 1)

Usage: python scripts/metrics.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")


def main() -> None:
    config = load_config()
    with db.connect(config.db_path) as conn:
        approvals = conn.execute(
            """
            SELECT d.note_id, d.version, d.sent_at, dec.decided_at
            FROM decisions dec
            JOIN drafts d ON d.id = dec.draft_id
            WHERE dec.action = 'approve'
            ORDER BY dec.decided_at
            """
        ).fetchall()

        if not approvals:
            print("No approved drafts yet.")
            return

        by_week: dict[str, int] = {}
        wait_times_hours: list[float] = []
        low_revision = 0

        for row in approvals:
            decided = _parse(row["decided_at"])
            week_key = decided.strftime("%Y-W%W")
            by_week[week_key] = by_week.get(week_key, 0) + 1

            first_sent = conn.execute(
                "SELECT sent_at FROM drafts WHERE note_id = ? AND version = 0", (row["note_id"],)
            ).fetchone()
            if first_sent and first_sent["sent_at"]:
                delta = decided - _parse(first_sent["sent_at"])
                wait_times_hours.append(delta.total_seconds() / 3600)

            if row["version"] <= 1:
                low_revision += 1

        print("Posts approved per week:")
        for week, count in sorted(by_week.items()):
            print(f"  {week}: {count}")
        avg_per_week = mean(by_week.values())
        print(f"  average: {avg_per_week:.1f}/week (target: 2/week)\n")

        if wait_times_hours:
            print(
                f"Time to approve: median {median(wait_times_hours):.1f}h, "
                f"average {mean(wait_times_hours):.1f}h\n"
            )

        pct_low_revision = 100 * low_revision / len(approvals)
        print(
            f"Approved with little/no revision (<=1 version): "
            f"{low_revision}/{len(approvals)} ({pct_low_revision:.0f}%)"
        )


if __name__ == "__main__":
    main()
