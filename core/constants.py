# Risk parameters
RISK_PER_TRADE_PCT       = 0.10
MAX_DRAWDOWN_PCT         = 0.50
MAX_CONSECUTIVE_LOSSES   = 3
MAX_OPEN_TRADES          = 2
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

# Regime detection
ADX_TRENDING_THRESHOLD  = 25
ADX_RANGING_THRESHOLD   = 20
ATR_VOLATILE_MULTIPLIER = 2.0

# Model A — M5 Momentum Scalp
M5_EMA_FAST               = 21
M5_EMA_SLOW               = 50
M5_EMA_HTF                = 50    # EMA period on M15 for higher timeframe bias
M5_RSI_MIN                = 40
M5_RSI_MAX                = 60
M5_MAX_TRADES_PER_SESSION = 3
M5_SL_MIN                 = 5.0
M5_SL_MAX                 = 8.0

# Model B — M1 Mean Reversion Scalp
M1_BB_PERIOD              = 20
M1_BB_STD                 = 2.0
M1_RSI_OVERBOUGHT         = 72
M1_RSI_OVERSOLD           = 28
M1_MAX_TRADES_PER_SESSION = 5
M1_SL_MIN                 = 3.0
M1_SL_MAX                 = 5.0

# Model C — London Open Breakout
BREAKOUT_WINDOW_START        = "07:00"
BREAKOUT_WINDOW_END          = "07:30"
ASIAN_SESSION_START          = "00:00"
ASIAN_SESSION_END            = "07:00"
BREAKOUT_MAX_TRADES_PER_DAY  = 1
BREAKOUT_SL_MIN              = 6.0
BREAKOUT_SL_MAX              = 8.0

# MAHORAGA thresholds
MIN_TRADES_FOR_ANALYSIS         = 30
UNDERPERFORM_WIN_RATE_THRESHOLD = 0.30
OUTPERFORM_WIN_RATE_THRESHOLD   = 0.55
MAHORAGA_DAILY_RUN_TIME         = "21:30"   # GMT, after NY close
MAHORAGA_WEEKLY_RUN_DAY         = "Sunday"

# Poll intervals (seconds)
NANAMI_POLL_ACTIVE    = 60
NANAMI_POLL_BLACKOUT  = 300
TOJI_MONITOR_INTERVAL = 30
ALERT_POLL_INTERVAL   = 60

# Model name constants
MODEL_A = "M5_MOMENTUM"
MODEL_B = "M1_MEANREV"
MODEL_C = "LONDON_BREAKOUT"

MODEL_SESSION_LIMITS = {
    MODEL_A: M5_MAX_TRADES_PER_SESSION,
    MODEL_B: M1_MAX_TRADES_PER_SESSION,
    MODEL_C: BREAKOUT_MAX_TRADES_PER_DAY,
}

MODEL_SESSIONS = {
    MODEL_A: ["LONDON_OPEN", "NY_OVERLAP"],
    MODEL_B: ACTIVE_SESSIONS,
    MODEL_C: ["LONDON_BREAKOUT"],
}
