import os

# ---------------------------------------------------------------------------
# Account type — XAUUSD contract multiplier
# ---------------------------------------------------------------------------
ACCOUNT_TYPE       = os.getenv("ACCOUNT_TYPE", "CENTS").upper()
XAUUSD_POINT_VALUE = 100 if ACCOUNT_TYPE == "STANDARD" else 1

# Risk parameters
RISK_PER_TRADE_PCT       = 0.05
MAX_DRAWDOWN_PCT         = 0.50
MAX_CONSECUTIVE_LOSSES   = 3
NEWS_BLACKOUT_MINUTES    = 30
MAX_SPREAD_DOLLARS       = 4.0

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

TRADEABLE_REGIMES = {REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_TIGHT_RANGE}
NO_TRADE_REGIMES  = {REGIME_BULLISH_BLOWOFF, REGIME_BEARISH_PANIC, REGIME_TOXIC_CHOP}

REGIME_Z_SCORE_WINDOW            = 50    # rolling mean/std window on H1 close
REGIME_Z_SCORE_THRESHOLD         = 1.0   # |Z| > 1.0 → directional
REGIME_ATR_PERIOD                = 14    # ATR14 on H1
REGIME_ATR_LOOKBACK              = 200   # percentile lookback
REGIME_ATR_PERCENTILE_THRESHOLD  = 75    # >75 = high volatility
REGIME_H1_BARS_NEEDED            = 200   # enough for ATR percentile lookback

# Structural Break Override
STRUCTURAL_BREAK_ATR_MULT       = 3.0   # single H1 candle > 3×ATR → halt
STRUCTURAL_BREAK_COOLDOWN_HOURS = 4     # hours to cool down

# HTF bias (retained for Model C GETO validation)
HTF_BIAS_BULLISH     = "BULLISH"
HTF_BIAS_BEARISH     = "BEARISH"
HTF_BIAS_NEUTRAL     = "NEUTRAL"
HTF_H4_EMA_PERIOD    = 21       # H4 EMA21 for Model C
HTF_H4_BARS_NEEDED   = 30

# ─────────────────────────────────────────────────────────────────────────────
# Exit System: Fixed 1:2 RR + Breakeven Protection
# ─────────────────────────────────────────────────────────────────────────────
RR_RATIO                     = 2.0    # TP = SL × 2
BREAKEVEN_ATR_THRESHOLD      = 1.0    # Move SL to entry at +1 ATR profit
TIME_KILL_MINUTES            = 60     # Close if not profitable after 60 min
MAX_TRADE_DURATION_MINUTES   = 240    # Hard cap: 4 hours

# ─────────────────────────────────────────────────────────────────────────────
# OU Model Parameters (shared by Model A and Model B)
# ─────────────────────────────────────────────────────────────────────────────
OU_ZSCORE_ENTRY_THRESHOLD = 1.3   # |ou_z| > 1.3 to enter (Model B default)
OU_ZSCORE_GRIND_THRESHOLD = 0.8   # |ou_z| > 0.8 for Model A (directional regime supports lower z)
OU_MIN_HALF_LIFE          = 3     # bars (relaxed from 5)
OU_MAX_HALF_LIFE          = 50    # bars (relaxed from 30)
OU_LOOKBACK               = 80    # bars for OU window (reduced from 100)
OU_LOOKBACK_SHORT         = 40    # short detrend window (EMA21 residuals)
OU_TIME_KILL_HALF_LIFE_MULT = 2   # close after 2 × half_life_bars M5 bars
OU_SL_ATR_MULT            = 1.5   # SL = 1.5 × ATR14
OU_SL_MIN                 = 6.0   # $6 minimum SL
OU_SL_MAX                 = 12.0  # $12 maximum SL
ADF_P_VALUE_THRESHOLD     = 0.10  # ADF significance (relaxed from 0.05)
M5_MAX_TRADES_PER_SESSION = 8     # Model A session limit
M1_MAX_TRADES_PER_SESSION = 8     # Model B session limit

# ─────────────────────────────────────────────────────────────────────────────
# Model C — London Open Breakout (refined with H4 filter)
# ─────────────────────────────────────────────────────────────────────────────
BREAKOUT_WINDOW_START        = "07:00"
BREAKOUT_WINDOW_END          = "07:30"
ASIAN_SESSION_START          = "00:00"
ASIAN_SESSION_END            = "07:00"
BREAKOUT_MAX_TRADES_PER_DAY  = 1
BREAKOUT_SL_MIN              = 5.0
BREAKOUT_SL_MAX              = 8.0
MODEL_C_MIN_RANGE            = 3.0

# ─────────────────────────────────────────────────────────────────────────────
# Model name constants
# ─────────────────────────────────────────────────────────────────────────────
MODEL_A = "OU_GRIND"
MODEL_B = "OU_RANGE"
MODEL_C = "ASIAN_BREAKOUT"

MODEL_SESSION_LIMITS = {
    MODEL_A: M5_MAX_TRADES_PER_SESSION,
    MODEL_B: M1_MAX_TRADES_PER_SESSION,
    MODEL_C: BREAKOUT_MAX_TRADES_PER_DAY,
}

MODEL_SESSIONS = {
    MODEL_A: ["LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"],
    MODEL_B: ["NY_OVERLAP"],
    MODEL_C: ["LONDON_BREAKOUT"],
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
# Legacy constants (kept for stat_tests.py / indicator_engine.py compatibility)
# ─────────────────────────────────────────────────────────────────────────────
HURST_WINDOW = 200
ATR_VOLATILE_MULTIPLIER = 2.0
HURST_TRENDING_THRESHOLD = 0.53
HURST_RANGING_THRESHOLD  = 0.35
MODEL_A_KALMAN_Q_SCALE   = 0.01
MODEL_A_ZSCORE_LOOKBACK  = 50
MODEL_B_ADF_SIGNIFICANCE = 0.10
MACRO_BIAS_WINDOW        = 1000
MACRO_BIAS_Q_SCALE       = 0.001
MACRO_BIAS_UP            = "UP"
MACRO_BIAS_DOWN          = "DOWN"
MACRO_BIAS_NEUTRAL       = "NEUTRAL"
