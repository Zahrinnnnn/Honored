#!/usr/bin/env python3
"""
get_signal_reason.py — GOJO tool script

Returns the last signal and GETO's decision as JSON.

Usage:
    python3 get_signal_reason.py --json

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


def db_path() -> str:
    path = os.getenv("HONORED_DB") or os.getenv("HONORED_DB_PATH")
    if path:
        return path
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"
    return "paper.db" if paper else "honored.db"


def safe_get(conn, table, key, default=None):
    try:
        row = conn.execute(
            f"SELECT value FROM {table} WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def main():
    try:
        path = db_path()
        conn = sqlite3.connect(path)

        last_signal_raw = safe_get(conn, "trading_state", "last_signal",        None)
        last_decision   = safe_get(conn, "trading_state", "last_risk_decision",  None)
        conn.close()

        if not last_signal_raw:
            print(json.dumps({
                "status": "ok",
                "data": {"message": "No signal on record yet."},
            }))
            return

        try:
            signal = json.loads(last_signal_raw)
        except Exception:
            signal = {"raw": last_signal_raw}

        # Parse GETO decision if stored as JSON
        decision = None
        if last_decision:
            try:
                decision = json.loads(last_decision)
            except Exception:
                decision = {"raw": last_decision}

        data = {
            "signal":   signal,
            "decision": decision,
        }

        print(json.dumps({"status": "ok", "data": data}))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
