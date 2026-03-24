import os

# ---------------------------------------------------------------------------
# Account type — XAUUSD contract multiplier
# ---------------------------------------------------------------------------
ACCOUNT_TYPE       = os.getenv("ACCOUNT_TYPE", "CENTS").upper()
# MetaApi returns balance in USC for CENTS accounts (e.g. $5 USD = 500 USC).
# 1 cent-lot XAUUSD = $1 per $1 gold move = 100 USC per $1 gold move.
# STANDARD: balance in USD, 1 lot = $100/pt → POINT_VALUE = 100
# CENTS:    balance in USC, 1 lot = 100 USC/pt → POINT_VALUE = 100 (same)
XAUUSD_POINT_VALUE = 100

# HFM CENTS accounts use "XAUUSDc" symbol; STANDARD uses "XAUUSD"
SYMBOL = "XAUUSDc" if ACCOUNT_TYPE == "CENTS" else "XAUUSD"

# Risk parameters
RISK_PER_TRADE_PCT            = 0.20   # Model A standard risk (trades 2+ per day)
RISK_PER_TRADE_FIRST_DAILY    = 0.50   # Model A first trade of the day only
MAX_DRAWDOWN_PCT         = 0.50
MAX_CONSECUTIVE_LOSSES   = 4
NEWS_BLACKOUT_MINUTES    = 30
MAX_SPREAD_DOLLARS       = 4.0
MAX_SIMULTANEOUS_TRADES  = 10

# Trading sessions (GMT, 24h "HH:MM" strings)
SESSIONS = {
    "LONDON_OPEN":     ("07:00", "10:00"),
    "NY_OVERLAP":      ("12:00", "16:00"),
    "NY_CLOSE":        ("19:00", "21:00"),
    "LONDON_BREAKOUT": ("07:00", "07:30"),
}
ACTIVE_SESSIONS  = ["LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"]
BLACKOUT_START   = "21:00"
BLACKOUT_END     = "07:00"

# ─────────────────────────────────────────────────────────────────────────────
# 6-State Regime Matrix (computed on H1)
# ─────────────────────────────────────────────────────────────────────────────
REGIME_BULLISH_GRIND   = "BULLISH_GRIND"    # Z>1,  ATRP≤75: slow uptrend
REGIME_BULLISH_BLOWOFF = "BULLISH_BLOWOFF"  # Z>1,  ATRP>75: violent upside
REGIME_BEARISH_GRIND   = "BEARISH_GRIND"    # Z<-1, ATRP≤75: slow downtrend
REGIME_BEARISH_PANIC   = "BEARISH_PANIC"    # Z<-1, ATRP>75: freefall
REGIME_TIGHT_RANGE     = "TIGHT_RANGE"      # |Z|≤1, ATRP≤75: compression
REGIME_TOXIC_CHOP      = "TOXIC_CHOP"       # |Z|≤1, ATRP>75: whipsaw

NO_TRADE_REGIMES  = {REGIME_TOXIC_CHOP, REGIME_TIGHT_RANGE}  # confirmed anti-edge (32% WR on 339 trades)

REGIME_Z_SCORE_WINDOW            = 50    # rolling mean/std window on H1 typical price
REGIME_Z_SCORE_THRESHOLD         = 1.0   # |Z| > 1.0 → directional
REGIME_MEAN_SLOPE_BARS           = 10    # bars to measure rolling mean slope direction
REGIME_ATR_PERIOD                = 14    # ATR14 on H1
REGIME_ATR_LOOKBACK              = 200   # percentile lookback
REGIME_ATR_SMOOTH_BARS           = 3     # smooth ATR over N bars (pseudo-hysteresis)
REGIME_ATR_PERCENTILE_THRESHOLD  = 75    # >75 = high volatility
REGIME_PERSISTENCE_BARS          = 6     # min consecutive H1 bars in same regime
REGIME_H1_BARS_NEEDED            = 200   # enough for ATR percentile lookback

# Structural Break Override
STRUCTURAL_BREAK_ATR_MULT        = 3.0   # single H1 candle > 3×ATR → halt
STRUCTURAL_BREAK_WARN_ATR_MULT   = 2.0   # sustained vol: 2 consecutive bars > 2×ATR
STRUCTURAL_BREAK_COOLDOWN_HOURS  = 4     # hours to cool down


# ─────────────────────────────────────────────────────────────────────────────
# Exit System: Fixed 1:2 RR + Breakeven Protection
# ─────────────────────────────────────────────────────────────────────────────
RR_RATIO                     = 2.0    # TP = SL × 2
BREAKEVEN_ATR_THRESHOLD      = 1.5    # opt 2: Move SL to entry at +1.5 ATR (was 1.0 — reduces whipsaw BEs)
MODEL_A_TIME_KILL_MINUTES    = 60     # Model A fallback (no half_life) — 60 min
MODEL_B_TIME_KILL_MINUTES    = 120    # Model B London reversal — reversals need more time
TIME_KILL_MINUTES            = 60     # Legacy alias (used by Model C / fallback)
MAX_TRADE_DURATION_MINUTES   = 240    # Hard cap: 4 hours

# ─────────────────────────────────────────────────────────────────────────────
# OU Model Parameters (Model A)
# ─────────────────────────────────────────────────────────────────────────────
OU_ZSCORE_ENTRY_THRESHOLD = 1.3   # |ou_z| > 1.3 to enter (EMA21 fallback)
OU_ZSCORE_GRIND_THRESHOLD = 0.9   # |ou_z| > 0.9 for Model A primary (EMA50)
OU_MIN_HALF_LIFE          = 3     # bars minimum half-life
OU_MAX_HALF_LIFE          = 50    # bars
OU_LOOKBACK               = 80    # bars for OU window (EMA50 detrend)
OU_LOOKBACK_MID           = 60    # mid detrend window (EMA34 residuals)
OU_LOOKBACK_SHORT         = 40    # short detrend window (EMA21 residuals)
OU_ZSCORE_EMA34_THRESHOLD  = 1.0   # Model A EMA34 z-score (between EMA50=0.9 and EMA21=1.3)
OU_ZSCORE_BLOWOFF_THRESHOLD = 1.0  # opt 6: tightened — only enter on deep dips in blowoff
OU_MAX_HALF_LIFE_BLOWOFF    = 25   # tighter half-life cap — blowoff dips snap back fast
OU_TIME_KILL_HALF_LIFE_MULT = 3   # gives OU process more time to mean-revert
OU_SL_ATR_MULT            = 1.5   # SL = 1.5 × ATR14
OU_SL_MIN                 = 6.0   # $6 minimum SL distance
OU_SL_MAX                 = 12.0  # $12 maximum SL distance
ADF_P_VALUE_THRESHOLD     = 0.10  # ADF significance threshold
M5_MAX_TRADES_PER_SESSION = 20    # Model A session limit
NY_OVERLAP_DEAD_HOUR_START = 14   # 14:00 UTC — dead zone start (US midday)
NY_OVERLAP_DEAD_HOUR_END   = 15   # 15:00 UTC — dead zone end

# ─────────────────────────────────────────────────────────────────────────────
# Model name constants
# ─────────────────────────────────────────────────────────────────────────────
MODEL_A = "OU_GRIND"
MODEL_B = "LONDON_REVERSAL"   # Kalman + CUSUM + N-bar + volume climax, London Open only
MODEL_C = "LONDON_TREND"      # Asian range breakout + Kalman continuation, London Phase 1+2

LONDON_REVERSAL_MAX_TRADES_PER_SESSION = 3
LONDON_TREND_MAX_TRADES_PER_SESSION    = 20  # max 20 London trend entries per day

# Model C risk — isolated from Model A to prevent anti-martingale cross-contamination
MODEL_C_RISK_PCT                  = 0.05   # 5% of balance (vs 15% for A)
LONDON_TREND_TIME_KILL_MINUTES    = 150    # 2.5h — exits by ~11:30 from latest 09:00 entry
LONDON_TREND_ENTRY_CUTOFF_HOUR    = 9      # no new entries at/after 09:00 UTC
LONDON_TREND_SL_MIN               = 5.0   # $5 floor
LONDON_TREND_SL_MAX               = 20.0  # $20 cap (raised — must survive panic ATR spikes)
LONDON_TREND_SL_ATR_MULT          = 1.5   # SL = max(asian_range/3, 1.5×ATR) — ATR floor ensures room
LONDON_TREND_SL_RANGE_FRACTION    = 3.0   # SL = asian_range / 3 (then take max with ATR floor)
LONDON_TREND_MIN_ASIAN_RANGE      = 5.0   # minimum $5 overnight compression
LONDON_TREND_ATR_MOMENTUM_MULT    = 1.1   # ATR must be >= 1.1× baseline (real momentum)
LONDON_TREND_KALMAN_VEL_THRESHOLD = 0.015 # velocity must exceed this in breakout direction
LONDON_TREND_MAX_KALMAN_VEL       = 3.0   # block if |velocity| > 3.0 (panic/free-fall chase)
LONDON_TREND_MAX_BREAK_ATR_MULT   = 2.0   # block if break distance > 2×ATR (peak extension)

MODEL_SESSION_LIMITS = {
    MODEL_A: M5_MAX_TRADES_PER_SESSION,
    MODEL_B: LONDON_REVERSAL_MAX_TRADES_PER_SESSION,
    MODEL_C: LONDON_TREND_MAX_TRADES_PER_SESSION,
}

MODEL_SESSIONS = {
    MODEL_A: ["NY_OVERLAP", "NY_CLOSE"],  # NY_CLOSE capped at 20:00 UTC entry cutoff
    MODEL_B: ["LONDON_OPEN"],
    MODEL_C: ["LONDON_OPEN"],             # entry window 07:00–09:00 UTC (gated internally)
}

# ─────────────────────────────────────────────────────────────────────────────
# MAHORAGA thresholds
# ─────────────────────────────────────────────────────────────────────────────
MIN_TRADES_FOR_ANALYSIS         = 30
UNDERPERFORM_WIN_RATE_THRESHOLD = 0.30
OUTPERFORM_WIN_RATE_THRESHOLD   = 0.55
MAHORAGA_DAILY_RUN_TIME         = "21:30"
MAHORAGA_WEEKLY_RUN_DAY         = "Sunday"

# ─────────────────────────────────────────────────────────────────────────────
# Poll intervals (seconds)
# ─────────────────────────────────────────────────────────────────────────────
NANAMI_POLL_ACTIVE    = 60
NANAMI_POLL_BLACKOUT  = 300
TOJI_MONITOR_INTERVAL = 30
ALERT_POLL_INTERVAL   = 60

# ─────────────────────────────────────────────────────────────────────────────
# stat_tests.py / indicator_engine.py constants
# ─────────────────────────────────────────────────────────────────────────────
HURST_WINDOW             = 200
ATR_VOLATILE_MULTIPLIER  = 2.0
HURST_TRENDING_THRESHOLD = 0.53  # Hurst trending threshold
HURST_RANGING_THRESHOLD  = 0.35
MODEL_A_KALMAN_Q_SCALE   = 0.01
MODEL_A_ZSCORE_LOOKBACK  = 50
