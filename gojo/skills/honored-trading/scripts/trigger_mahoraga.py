#!/usr/bin/env python3
"""
trigger_mahoraga.py — GOJO tool script

Queues a manual MAHORAGA analysis run.

Usage:
    python3 trigger_mahoraga.py --json

MAHORAGA picks up the manual_trigger flag on its next poll cycle and
runs a full performance analysis, then posts results to alert_queue.

Reads HONORED_DB env var for SQLite path.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def db_path() -> str:
    path = os.getenv("HONORED_DB")
    if path:
        return path
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"
    return "paper.db" if paper else "honored.db"


def main():
    try:
        path = db_path()
        conn = sqlite3.connect(path)
        now  = datetime.now(timezone.utc).isoformat()

        # Set manual_trigger flag — MAHORAGA polls for this
        conn.execute(
            """INSERT INTO mahoraga_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            ("manual_trigger", "true", now),
        )

        conn.commit()
        conn.close()

        print(json.dumps({
            "status": "ok",
            "data": {
                "message":   "MAHORAGA analysis queued. Report will arrive via alert when ready.",
                "triggered": now,
            },
        }))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
