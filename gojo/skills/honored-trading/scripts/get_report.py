#!/usr/bin/env python3
"""
get_report.py — GOJO tool script

Returns trade performance report as JSON.

Usage:
    python3 get_report.py --days 7 --json
    python3 get_report.py --days 30 --json

Reads HONORED_DB env var for SQLite path.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

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


def main():
    days = 7
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            try:
                days = int(sys.argv[i + 1])
            except ValueError:
                pass

    try:
        path = db_path()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        rows = conn.execute(
            """
            SELECT model, direction, result, pnl, entry_price, exit_price,
                   duration_mins, date, time
            FROM trades
            WHERE result IS NOT NULL
              AND date >= ?
            ORDER BY date ASC, time ASC
            """,
            (cutoff,),
        ).fetchall()

        # Current balance
        acct = conn.execute(
            "SELECT balance FROM account ORDER BY id DESC LIMIT 1"
        ).fetchone()
        current_balance = round(float(acct[0] or 0), 2) if acct else 0.0

        conn.close()

        if not rows:
            print(json.dumps({
                "status": "ok",
                "data": {
                    "period_days":     days,
                    "total_trades":    0,
                    "wins":            0,
                    "losses":          0,
                    "win_rate":        0.0,
                    "total_pnl":       0.0,
                    "avg_pnl":         0.0,
                    "best_trade":      0.0,
                    "worst_trade":     0.0,
                    "by_model":        {},
                    "current_balance": current_balance,
                    "message":         f"No completed trades in the last {days} days.",
                },
            }))
            return

        wins   = [r for r in rows if r["result"] == "WIN"]
        losses = [r for r in rows if r["result"] == "LOSS"]
        pnls   = [float(r["pnl"] or 0) for r in rows]

        # Per-model breakdown
        by_model = {}
        for r in rows:
            m = r["model"] or "UNKNOWN"
            if m not in by_model:
                by_model[m] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            by_model[m]["trades"] += 1
            by_model[m]["pnl"]    += float(r["pnl"] or 0)
            if r["result"] == "WIN":
                by_model[m]["wins"] += 1
            else:
                by_model[m]["losses"] += 1
        for m in by_model:
            t = by_model[m]["trades"]
            by_model[m]["win_rate"] = round(by_model[m]["wins"] / t, 3) if t else 0.0
            by_model[m]["pnl"]      = round(by_model[m]["pnl"], 2)

        data = {
            "period_days":     days,
            "total_trades":    len(rows),
            "wins":            len(wins),
            "losses":          len(losses),
            "win_rate":        round(len(wins) / len(rows), 3),
            "total_pnl":       round(sum(pnls), 2),
            "avg_pnl":         round(sum(pnls) / len(pnls), 2),
            "best_trade":      round(max(pnls), 2),
            "worst_trade":     round(min(pnls), 2),
            "by_model":        by_model,
            "current_balance": current_balance,
        }

        print(json.dumps({"status": "ok", "data": data}))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
