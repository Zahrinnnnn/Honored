import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
_DEFAULT_DB = os.environ.get("HONORED_DB_PATH", "paper.db" if PAPER_MODE else "honored.db")

_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    balance         REAL    DEFAULT 0.0,
    equity          REAL    DEFAULT 0.0,
    peak_balance    REAL    DEFAULT 0.0,
    current_dd_pct  REAL    DEFAULT 0.0,
    open_positions  INTEGER DEFAULT 0,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_trades (
    session TEXT NOT NULL,
    model   TEXT NOT NULL,
    date    TEXT NOT NULL,
    count   INTEGER DEFAULT 0,
    PRIMARY KEY (session, model, date)
);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT,
    time           TEXT,
    model          TEXT,
    direction      TEXT,
    entry_price    REAL,
    sl_price       REAL,
    tp_price       REAL,
    lot_size       REAL,
    sl_distance    REAL,
    risk_amount    REAL,
    result         TEXT,
    exit_price     REAL,
    pnl            REAL,
    balance_before REAL,
    balance_after  REAL,
    drawdown_pct   REAL,
    duration_mins  REAL,
    reason         TEXT,
    paper          INTEGER DEFAULT 0,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS alert_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    sent       INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mahoraga_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_info (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SYSTEM_STATE_DEFAULTS = {
    "status":              "ACTIVE",
    "pause_flag":          "false",
    "halt_flag":           "false",
    "emergency_halt_flag": "false",
}

_TRADING_STATE_DEFAULTS = {
    "consecutive_losses":   "0",
    "total_trades_today":   "0",
    "last_trade_result":    "",
    "last_trade_timestamp": "",
    "last_signal":          "",
    "last_risk_decision":   "",
}


class StateManager:
    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._setup_schema()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _setup_schema(self):
        await self._conn.executescript(_DDL)
        await self._conn.commit()
        await self._seed_defaults()

    async def _seed_defaults(self):
        now = _now()
        for key, value in _SYSTEM_STATE_DEFAULTS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        for key, value in _TRADING_STATE_DEFAULTS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO trading_state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        await self._conn.execute(
            "INSERT OR IGNORE INTO account (id, updated_at) VALUES (1, ?)", (now,)
        )
        await self._conn.commit()

    # ── system_state ────────────────────────────────────────────────────────

    async def get_system_flag(self, key: str) -> bool:
        row = await self._fetchone(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        return row["value"].lower() == "true" if row else False

    async def set_system_flag(self, key: str, value: bool):
        await self._upsert_kv("system_state", key, str(value).lower())

    async def get_system_status(self) -> str:
        row = await self._fetchone(
            "SELECT value FROM system_state WHERE key = 'status'"
        )
        return row["value"] if row else "UNKNOWN"

    async def set_system_status(self, status: str):
        await self._upsert_kv("system_state", "status", status)

    # ── account ─────────────────────────────────────────────────────────────

    async def get_account(self) -> dict:
        row = await self._fetchone("SELECT * FROM account WHERE id = 1")
        return dict(row) if row else {}

    async def update_account(self, **kwargs):
        if not kwargs:
            return
        kwargs["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        await self._conn.execute(
            f"UPDATE account SET {sets} WHERE id = 1", list(kwargs.values())
        )
        await self._conn.commit()

    # ── trading_state ────────────────────────────────────────────────────────

    async def get_trading_state(self, key: str) -> str:
        row = await self._fetchone(
            "SELECT value FROM trading_state WHERE key = ?", (key,)
        )
        return row["value"] if row else ""

    async def set_trading_state(self, key: str, value):
        await self._upsert_kv("trading_state", key, str(value))

    async def get_consecutive_losses(self) -> int:
        val = await self.get_trading_state("consecutive_losses")
        return int(val) if val.isdigit() else 0

    async def increment_consecutive_losses(self):
        current = await self.get_consecutive_losses()
        await self.set_trading_state("consecutive_losses", current + 1)

    async def reset_consecutive_losses(self):
        await self.set_trading_state("consecutive_losses", 0)

    # ── session_trades ───────────────────────────────────────────────────────

    async def get_session_trade_count(self, session: str, model: str) -> int:
        row = await self._fetchone(
            "SELECT count FROM session_trades WHERE session = ? AND model = ? AND date = ?",
            (session, model, _today()),
        )
        return row["count"] if row else 0

    async def increment_session_trade_count(self, session: str, model: str):
        await self._conn.execute(
            """
            INSERT INTO session_trades (session, model, date, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(session, model, date) DO UPDATE SET count = count + 1
            """,
            (session, model, _today()),
        )
        await self._conn.commit()

    async def reset_session_counts(self, session: Optional[str] = None):
        if session:
            await self._conn.execute(
                "DELETE FROM session_trades WHERE session = ? AND date = ?",
                (session, _today()),
            )
        else:
            await self._conn.execute(
                "DELETE FROM session_trades WHERE date = ?", (_today(),)
            )
        await self._conn.commit()

    # ── trades ───────────────────────────────────────────────────────────────

    async def log_trade(self, **kwargs) -> int:
        kwargs.setdefault("paper", 1 if PAPER_MODE else 0)
        kwargs.setdefault("created_at", _now())
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        cursor = await self._conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def update_trade(self, trade_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        await self._conn.execute(
            f"UPDATE trades SET {sets} WHERE id = ?",
            [*kwargs.values(), trade_id],
        )
        await self._conn.commit()

    async def get_trades(self, limit: int = 100) -> list:
        paper_filter = 1 if PAPER_MODE else 0
        rows = await self._fetchall(
            "SELECT * FROM trades WHERE paper = ? ORDER BY id DESC LIMIT ?",
            (paper_filter, limit),
        )
        return [dict(r) for r in rows]

    async def get_open_trades(self) -> list:
        """Return all currently open trades (result IS NULL) for the current mode."""
        paper_filter = 1 if PAPER_MODE else 0
        rows = await self._fetchall(
            "SELECT * FROM trades WHERE result IS NULL AND paper = ? ORDER BY id ASC",
            (paper_filter,),
        )
        return [dict(r) for r in rows]

    async def get_all_trades(self, limit: int = 500) -> list:
        rows = await self._fetchall(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    # ── alert_queue ──────────────────────────────────────────────────────────

    async def push_alert(self, alert_type: str, message: str):
        await self._conn.execute(
            "INSERT INTO alert_queue (alert_type, message, sent, created_at) VALUES (?, ?, 0, ?)",
            (alert_type, message, _now()),
        )
        await self._conn.commit()

    async def get_pending_alerts(self) -> list:
        rows = await self._fetchall(
            "SELECT * FROM alert_queue WHERE sent = 0 ORDER BY id ASC"
        )
        return [dict(r) for r in rows]

    async def mark_alert_sent(self, alert_id: int):
        await self._conn.execute(
            "UPDATE alert_queue SET sent = 1 WHERE id = ?", (alert_id,)
        )
        await self._conn.commit()

    # ── session_info ─────────────────────────────────────────────────────────

    async def get_session_info(self, key: str) -> str:
        row = await self._fetchone(
            "SELECT value FROM session_info WHERE key = ?", (key,)
        )
        return row["value"] if row else ""

    async def set_session_info(self, key: str, value):
        await self._upsert_kv("session_info", key, str(value))

    # ── mahoraga_state ───────────────────────────────────────────────────────

    async def get_mahoraga_state(self, key: str) -> str:
        row = await self._fetchone(
            "SELECT value FROM mahoraga_state WHERE key = ?", (key,)
        )
        return row["value"] if row else ""

    async def set_mahoraga_state(self, key: str, value):
        await self._upsert_kv("mahoraga_state", key, str(value))

    # ── internal helpers ─────────────────────────────────────────────────────

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchall()

    async def _upsert_kv(self, table: str, key: str, value: str):
        await self._conn.execute(
            f"INSERT INTO {table} (key, value, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )
        await self._conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
