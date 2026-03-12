#!/usr/bin/env python3
"""
HONORED System Health Check
Run: python /opt/honored/scripts/health_check.py
Exit code 0 = all clear | 1 = warnings | 2 = critical failures
"""

import os
import sys
import sqlite3
import subprocess
import importlib.util
from datetime import datetime, timezone, timedelta

# ─── Config ──────────────────────────────────────────────────────────────────
PROJECT_DIR  = "/opt/honored"
DB_PATH      = os.getenv("HONORED_DB", "/opt/honored/honored.db")
LOG_DIR      = "/var/log/honored"
VENV_PYTHON  = f"{PROJECT_DIR}/.venv/bin/python"
LOG_TAIL     = 60   # lines to scan per log for errors

# Expected constants (update these whenever constants.py changes)
EXPECTED = {
    "OU_ZSCORE_GRIND_THRESHOLD": 0.9,
    "OU_ZSCORE_ENTRY_THRESHOLD": 1.3,
    "OU_MIN_HALF_LIFE": 3,
    "OU_MAX_HALF_LIFE": 50,
    "OU_LOOKBACK": 80,
    "OU_SL_MIN": 6.0,
    "OU_SL_MAX": 12.0,
    "RISK_PER_TRADE_PCT": 0.20,
    "MAX_DRAWDOWN_PCT": 0.50,
    "MAX_CONSECUTIVE_LOSSES": 3,
    "NY_OVERLAP_DEAD_HOUR_START": 14,
    "NY_OVERLAP_DEAD_HOUR_END": 15,
    "MODEL_SESSIONS_A": ["NY_OVERLAP"],
}

REQUIRED_TABLES = [
    "system_state", "account", "trading_state",
    "session_trades", "trades", "alert_queue",
    "mahoraga_state", "session_info",
]

AGENTS = ["nanami", "geto", "toji", "mahoraga"]

ERROR_PATTERNS = ["Traceback", "ERROR", "Exception", "CRITICAL", "error:"]
LOG_ERROR_IGNORE = [
    "TimeoutException",          # MetaApi SDK noise — expected on reconnect
    "Failed to subscribe",       # MetaApi transient — auto-recovers
]

# ─── ANSI colours ────────────────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
B  = "\033[94m"   # blue/cyan
W  = "\033[97m"   # white bold
DIM = "\033[2m"
RST = "\033[0m"

passes = 0
warnings = 0
failures = 0


def ok(label, detail=""):
    global passes
    passes += 1
    suffix = f"  {DIM}{detail}{RST}" if detail else ""
    print(f"  {G}✓{RST} {label}{suffix}")


def warn(label, detail=""):
    global warnings
    warnings += 1
    suffix = f"  {Y}{detail}{RST}" if detail else ""
    print(f"  {Y}⚠{RST} {label}{suffix}")


def fail(label, detail=""):
    global failures
    failures += 1
    suffix = f"  {R}{detail}{RST}" if detail else ""
    print(f"  {R}✗{RST} {label}{suffix}")


def section(title):
    print(f"\n{B}{'─'*60}{RST}")
    print(f"{W}  {title}{RST}")
    print(f"{B}{'─'*60}{RST}")


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), 1


# ─── 1. Process Health ────────────────────────────────────────────────────────
section("1. PROCESS HEALTH")

stdout, _, _ = run("supervisorctl status honored:*")
for agent in AGENTS:
    line = next((l for l in stdout.splitlines() if f"honored:{agent}" in l), "")
    if "RUNNING" in line:
        uptime = line.split("uptime")[-1].strip() if "uptime" in line else "?"
        ok(f"honored:{agent}", f"RUNNING  uptime {uptime}")
    elif line:
        fail(f"honored:{agent}", line.split()[-1] if line.split() else "NOT RUNNING")
    else:
        fail(f"honored:{agent}", "not found in supervisorctl output")

# OpenClaw / GOJO
stdout, _, rc = run("systemctl --user is-active openclaw-gateway.service 2>/dev/null || openclaw gateway status 2>/dev/null | grep Service")
if "active" in stdout or "systemd" in stdout:
    ok("GOJO (openclaw-gateway)", "active")
else:
    # try pid check
    pids, _, _ = run("pgrep -f 'openclaw.*gateway'")
    if pids:
        ok("GOJO (openclaw-gateway)", f"pid {pids.split()[0]}")
    else:
        fail("GOJO (openclaw-gateway)", "not running")


# ─── 2. Constants Sanity Check ────────────────────────────────────────────────
section("2. CONSTANTS SANITY CHECK")

sys.path.insert(0, PROJECT_DIR)
try:
    import importlib
    spec = importlib.util.spec_from_file_location("constants", f"{PROJECT_DIR}/core/constants.py")
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)

    def chk(key, expected, actual):
        if actual == expected:
            ok(key, str(actual))
        else:
            fail(key, f"expected {expected!r}  got {actual!r}")

    chk("OU_ZSCORE_GRIND_THRESHOLD", EXPECTED["OU_ZSCORE_GRIND_THRESHOLD"], c.OU_ZSCORE_GRIND_THRESHOLD)
    chk("OU_ZSCORE_ENTRY_THRESHOLD", EXPECTED["OU_ZSCORE_ENTRY_THRESHOLD"], c.OU_ZSCORE_ENTRY_THRESHOLD)
    chk("OU_MIN_HALF_LIFE",          EXPECTED["OU_MIN_HALF_LIFE"],          c.OU_MIN_HALF_LIFE)
    chk("OU_MAX_HALF_LIFE",          EXPECTED["OU_MAX_HALF_LIFE"],          c.OU_MAX_HALF_LIFE)
    chk("OU_LOOKBACK",               EXPECTED["OU_LOOKBACK"],               c.OU_LOOKBACK)
    chk("OU_SL_MIN",                 EXPECTED["OU_SL_MIN"],                 c.OU_SL_MIN)
    chk("OU_SL_MAX",                 EXPECTED["OU_SL_MAX"],                 c.OU_SL_MAX)
    chk("RISK_PER_TRADE_PCT",        EXPECTED["RISK_PER_TRADE_PCT"],        c.RISK_PER_TRADE_PCT)
    chk("MAX_DRAWDOWN_PCT",          EXPECTED["MAX_DRAWDOWN_PCT"],          c.MAX_DRAWDOWN_PCT)
    chk("MAX_CONSECUTIVE_LOSSES",    EXPECTED["MAX_CONSECUTIVE_LOSSES"],    c.MAX_CONSECUTIVE_LOSSES)
    chk("NY_OVERLAP_DEAD_HOUR_START",EXPECTED["NY_OVERLAP_DEAD_HOUR_START"],c.NY_OVERLAP_DEAD_HOUR_START)
    chk("NY_OVERLAP_DEAD_HOUR_END",  EXPECTED["NY_OVERLAP_DEAD_HOUR_END"],  c.NY_OVERLAP_DEAD_HOUR_END)

    model_a_sessions = c.MODEL_SESSIONS.get(c.MODEL_A, [])
    if model_a_sessions == EXPECTED["MODEL_SESSIONS_A"]:
        ok("MODEL_SESSIONS[MODEL_A]", str(model_a_sessions))
    else:
        fail("MODEL_SESSIONS[MODEL_A]", f"expected {EXPECTED['MODEL_SESSIONS_A']}  got {model_a_sessions}")

except Exception as e:
    fail("constants.py load", str(e))


# ─── 3. Database Health ───────────────────────────────────────────────────────
section("3. DATABASE HEALTH")

if not os.path.exists(DB_PATH):
    fail("DB file exists", DB_PATH)
else:
    size_kb = os.path.getsize(DB_PATH) // 1024
    ok("DB file exists", f"{DB_PATH}  ({size_kb} KB)")

    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {r[0] for r in cur.fetchall()}
        for t in REQUIRED_TABLES:
            if t in existing:
                ok(f"table: {t}")
            else:
                fail(f"table: {t}", "MISSING")

        # Account state
        cur.execute("SELECT * FROM account WHERE id=1")
        row = cur.fetchone()
        if row:
            balance = row["balance"] or 0
            dd      = row["current_dd_pct"] or 0
            open_p  = row["open_positions"] or 0
            peak    = row["peak_balance"] or 0
            ok("account row", f"balance=${balance:.2f}  peak=${peak:.2f}  DD={dd:.1f}%  open={open_p}")
            if dd >= 40:
                warn("Drawdown elevated", f"{dd:.1f}% (halt at 50%)")
        else:
            warn("account row", "missing — run init_db.py?")

        # System flags
        cur.execute("SELECT key, value FROM system_state")
        flags = dict(cur.fetchall())
        for flag, label in [
            ("pause_flag",          "pause_flag"),
            ("halt_flag",           "halt_flag"),
            ("emergency_halt_flag", "emergency_halt_flag"),
        ]:
            val = flags.get(flag, "missing")
            if val is None or val.lower() in ("false", "0", ""):
                ok(f"flag: {label}", "False (normal)")
            elif val.lower() in ("true", "1"):
                warn(f"flag: {label}", "TRUE — system halted")
            else:
                warn(f"flag: {label}", f"value={val!r}")

        # Consecutive losses
        cur.execute("SELECT value FROM trading_state WHERE key='consecutive_losses'")
        row = cur.fetchone()
        consec = int(row[0]) if row else 0
        if consec == 0:
            ok("consecutive_losses", "0")
        elif consec < 3:
            warn("consecutive_losses", f"{consec} (halt at 3)")
        else:
            fail("consecutive_losses", f"{consec} — soft halt triggered")

        # Signal freshness
        cur.execute("SELECT value, updated_at FROM trading_state WHERE key='last_signal'")
        row = cur.fetchone()
        if row and row[0]:
            updated = row[1] or "unknown"
            ok("last_signal", f"present  updated {updated}")
        else:
            ok("last_signal", "none yet (normal during blackout/startup)")

        # Unsent alerts
        cur.execute("SELECT COUNT(*) FROM alert_queue WHERE sent=0")
        unsent = cur.fetchone()[0]
        if unsent == 0:
            ok("alert_queue", "no pending alerts")
        else:
            warn("alert_queue", f"{unsent} unsent alert(s) — GOJO heartbeat may be stuck")

        # Session info freshness
        cur.execute("SELECT key, value FROM session_info")
        sinfo = dict(cur.fetchall())
        regime  = sinfo.get("current_regime", "?")
        session = sinfo.get("current_session", "?")
        spread  = sinfo.get("current_spread", "?")
        bid     = sinfo.get("current_bid", "?")
        ok("session_info", f"regime={regime}  session={session}  bid={bid}  spread={spread}")

        # Recent trades summary
        cur.execute("""
            SELECT result, COUNT(*) as n, SUM(pnl) as total_pnl
            FROM trades
            WHERE date >= date('now', '-7 days') AND paper=0
            GROUP BY result
        """)
        trade_rows = cur.fetchall()
        if trade_rows:
            parts = [f"{r['result']}:{r['n']}({'+' if (r['total_pnl'] or 0)>=0 else ''}{(r['total_pnl'] or 0):.2f})" for r in trade_rows]
            ok("trades last 7d", "  ".join(parts))
        else:
            ok("trades last 7d", "none yet (normal on new deployment)")

        con.close()

    except Exception as e:
        fail("database read", str(e))


# ─── 4. Python Import Health ──────────────────────────────────────────────────
section("4. PYTHON IMPORT HEALTH")

modules_to_check = [
    ("core.constants",         f"{PROJECT_DIR}/core/constants.py"),
    ("core.state_manager",     f"{PROJECT_DIR}/core/state_manager.py"),
    ("core.news_fetcher",      f"{PROJECT_DIR}/core/news_fetcher.py"),
    ("agents.nanami.agent",    f"{PROJECT_DIR}/agents/nanami/agent.py"),
    ("agents.geto.agent",      f"{PROJECT_DIR}/agents/geto/agent.py"),
    ("agents.toji.agent",      f"{PROJECT_DIR}/agents/toji/agent.py"),
    ("agents.mahoraga.agent",  f"{PROJECT_DIR}/agents/mahoraga/agent.py"),
]

for mod_name, mod_path in modules_to_check:
    if not os.path.exists(mod_path):
        fail(f"import {mod_name}", "file not found")
        continue
    stdout, stderr, rc = run(f"{VENV_PYTHON} -c \"import importlib.util; s=importlib.util.spec_from_file_location('m','{mod_path}'); m=importlib.util.module_from_spec(s)\" 2>&1")
    # simpler: just check syntax
    stdout2, stderr2, rc2 = run(f"{VENV_PYTHON} -m py_compile '{mod_path}' 2>&1")
    if rc2 == 0:
        ok(f"syntax: {mod_name}")
    else:
        fail(f"syntax: {mod_name}", stderr2[:120])


# ─── 5. Log Error Scan ────────────────────────────────────────────────────────
section("5. RECENT LOG ERRORS  (last 60 lines each)")

for agent in AGENTS:
    for log_type in ["err", "out"]:
        log_file = f"{LOG_DIR}/{agent}.{log_type}.log"
        if not os.path.exists(log_file):
            continue
        stdout, _, _ = run(f"tail -{LOG_TAIL} '{log_file}'")
        if not stdout:
            continue
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H")
        error_lines = [
            l.strip() for l in stdout.splitlines()
            if any(p in l for p in ERROR_PATTERNS)
            and not any(ig in l for ig in LOG_ERROR_IGNORE)
            and (l[:14] >= cutoff[:14] if l[:4].isdigit() else True)
        ]
        if not error_lines:
            ok(f"{agent}.{log_type}.log", "clean")
        else:
            # Show last 3 unique errors
            seen = []
            for l in reversed(error_lines):
                if l not in seen:
                    seen.append(l)
                if len(seen) >= 3:
                    break
            warn(f"{agent}.{log_type}.log", f"{len(error_lines)} error line(s)")
            for l in reversed(seen):
                print(f"    {DIM}{l[:120]}{RST}")

# OpenClaw log
openclaw_log = f"/tmp/openclaw/openclaw-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
if os.path.exists(openclaw_log):
    stdout, _, _ = run(f"tail -{LOG_TAIL} '{openclaw_log}'")
    error_lines = [l for l in stdout.splitlines() if any(p in l for p in ["ERROR", "error", "WARN", "exception"])]
    if not error_lines:
        ok("openclaw.log", "clean")
    else:
        warn("openclaw.log", f"{len(error_lines)} line(s)")
        for l in error_lines[-3:]:
            print(f"    {DIM}{l[:120]}{RST}")
else:
    warn("openclaw.log", f"not found at {openclaw_log}")


# ─── 6. File Integrity ────────────────────────────────────────────────────────
section("6. KEY FILE INTEGRITY")

key_files = [
    f"{PROJECT_DIR}/core/constants.py",
    f"{PROJECT_DIR}/core/state_manager.py",
    f"{PROJECT_DIR}/agents/nanami/agent.py",
    f"{PROJECT_DIR}/agents/nanami/skills/ou_grind.py",
    f"{PROJECT_DIR}/agents/geto/agent.py",
    f"{PROJECT_DIR}/agents/toji/agent.py",
    f"{PROJECT_DIR}/agents/mahoraga/agent.py",
    f"{PROJECT_DIR}/.env",
    DB_PATH,
]

for f in key_files:
    if os.path.exists(f):
        mtime = datetime.fromtimestamp(os.path.getmtime(f), tz=timezone.utc)
        age   = datetime.now(timezone.utc) - mtime
        age_str = f"{int(age.total_seconds()//3600)}h ago" if age.total_seconds() > 3600 else f"{int(age.total_seconds()//60)}m ago"
        ok(os.path.relpath(f, PROJECT_DIR), age_str)
    else:
        fail(os.path.relpath(f, PROJECT_DIR), "MISSING")


# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'═'*62}")
total = passes + warnings + failures
print(f"  {G}{passes} passed{RST}   {Y}{warnings} warnings{RST}   {R}{failures} failed{RST}   ({total} checks)")
print(f"  Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")

if failures > 0:
    print(f"\n  {R}SYSTEM STATUS: CRITICAL — {failures} check(s) failed{RST}")
    sys.exit(2)
elif warnings > 0:
    print(f"\n  {Y}SYSTEM STATUS: WARNING — {warnings} item(s) need attention{RST}")
    sys.exit(1)
else:
    print(f"\n  {G}SYSTEM STATUS: ALL CLEAR{RST}")
    sys.exit(0)
