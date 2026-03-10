#!/usr/bin/env python3
"""
set_flag.py — GOJO tool script

Sets system flags in SQLite.

Usage:
    python3 set_flag.py --flag pause_flag --value true --json
    python3 set_flag.py --flag pause_flag --value false --json
    python3 set_flag.py --flag halt_flag --value false --json
    python3 set_flag.py --flag emergency_halt_flag --value false --json
    python3 set_flag.py --flag override --json   (clears halt_flag + resets consecutive_losses)

Reads HONORED_DB env var for SQLite path.
"""

import json
import os
import sqlite3
import sys

def _load_honored_env():
    try:
        with open("/opt/honored/.env") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _, _v = _line.partition('=')
                _k = _k.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v.strip()
    except OSError:
        pass

_load_honored_env()
from datetime import datetime, timezone


def db_path() -> str:
    path = os.getenv("HONORED_DB") or os.getenv("HONORED_DB_PATH")
    if path:
        return path
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"
    return "paper.db" if paper else "honored.db"


_VALID_FLAGS = {"pause_flag", "halt_flag", "emergency_halt_flag", "override"}


def upsert(conn: sqlite3.Connection, table: str, key: str, value: str):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"""INSERT INTO {table} (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, now),
    )


def main():
    flag  = None
    value = None
    for i, arg in enumerate(sys.argv):
        if arg == "--flag"  and i + 1 < len(sys.argv):
            flag  = sys.argv[i + 1]
        if arg == "--value" and i + 1 < len(sys.argv):
            value = sys.argv[i + 1].lower()

    if not flag:
        print("Error: --flag required", file=sys.stderr)
        sys.exit(1)

    if flag not in _VALID_FLAGS:
        print(f"Error: unknown flag '{flag}'. Valid: {sorted(_VALID_FLAGS)}", file=sys.stderr)
        sys.exit(1)

    if flag != "override" and value not in ("true", "false"):
        print("Error: --value must be 'true' or 'false'", file=sys.stderr)
        sys.exit(1)

    try:
        path = db_path()
        conn = sqlite3.connect(path)

        if flag == "override":
            # Clear soft halt + reset consecutive losses counter
            upsert(conn, "system_state",   "halt_flag",          "false")
            upsert(conn, "trading_state",  "consecutive_losses",  "0")
            conn.commit()
            conn.close()
            print(json.dumps({
                "status": "ok",
                "data": {
                    "action":  "override",
                    "cleared": ["halt_flag", "consecutive_losses"],
                    "message": "Soft halt cleared. Consecutive loss counter reset.",
                },
            }))
        else:
            upsert(conn, "system_state", flag, value)
            conn.commit()
            conn.close()
            print(json.dumps({
                "status": "ok",
                "data": {
                    "flag":    flag,
                    "value":   value,
                    "message": f"{flag} set to {value}.",
                },
            }))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
