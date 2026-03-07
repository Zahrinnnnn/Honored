"""
test_mahoraga_analysis.py — MAHORAGA skill unit tests

Tests cover:
  - performance_analyzer : compute_stats, Sharpe, max DD, recovery, profit factor,
                            by_model_stats, by_session_stats, rolling_stats
  - model_evaluator      : evaluate_model, binomial_significance, kelly_fraction,
                            detect_drift, score computation
  - parameter_optimizer  : analyze_sl_ranges, analyze_session_timing,
                            analyze_direction_bias, generate_parameter_suggestions
  - regime_validator     : validate_regime_accuracy, analyze_regime_stability,
                            generate_regime_suggestions
  - adaptation_reporter  : compile_report, format_whatsapp_summary,
                            recommendation building
"""

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.mahoraga.skills.performance_analyzer import (
    compute_stats,
    compute_sharpe,
    compute_max_drawdown,
    compute_recovery_factor,
    compute_profit_factor,
    avg_duration,
    by_model_stats,
    by_session_stats,
    rolling_stats,
    _empty_stats,
)
from agents.mahoraga.skills.model_evaluator import (
    evaluate_model,
    binomial_significance,
    kelly_fraction,
    detect_drift,
    ModelEvaluation,
)
from agents.mahoraga.skills.parameter_optimizer import (
    analyze_sl_ranges,
    analyze_session_timing,
    analyze_direction_bias,
    analyze_duration_vs_outcome,
    generate_parameter_suggestions,
)
from agents.mahoraga.skills.regime_validator import (
    validate_regime_accuracy,
    analyze_regime_stability,
    generate_regime_suggestions,
)
from agents.mahoraga.skills.adaptation_reporter import (
    compile_report,
    format_whatsapp_summary,
    Recommendation,
    _priority_order,
    _short_model_name,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _trade(result="WIN", pnl=3.0, model="OU_GRIND", direction="BUY",
           sl_distance=5.0, duration_mins=30.0, time="08:00:00", date="2024-01-01"):
    return {
        "result": result, "pnl": pnl, "model": model,
        "direction": direction, "sl_distance": sl_distance,
        "duration_mins": duration_mins, "time": time, "date": date,
    }


def _wins(n, **kw):
    return [_trade(result="WIN", pnl=3.0, **kw) for _ in range(n)]


def _losses(n, **kw):
    return [_trade(result="LOSS", pnl=-1.0, **kw) for _ in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# TestPerformanceAnalyzer
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeStats:
    def test_empty_returns_zero_stats(self):
        s = compute_stats([])
        assert s["total_trades"] == 0
        assert s["win_rate"] == 0.0

    def test_all_wins(self):
        s = compute_stats(_wins(10))
        assert s["total_trades"] == 10
        assert s["wins"] == 10
        assert s["losses"] == 0
        assert s["win_rate"] == 1.0
        assert s["total_pnl"] == pytest.approx(30.0)

    def test_mixed_winrate(self):
        trades = _wins(6) + _losses(4)
        s = compute_stats(trades)
        assert s["win_rate"] == pytest.approx(0.6)
        assert s["wins"] == 6
        assert s["losses"] == 4

    def test_total_pnl_correct(self):
        trades = _wins(3) + _losses(2)   # 3×3 + 2×(-1) = 7
        s = compute_stats(trades)
        assert s["total_pnl"] == pytest.approx(7.0)

    def test_best_worst_trade(self):
        trades = [_trade(pnl=10.0), _trade(pnl=-5.0), _trade(pnl=2.0)]
        s = compute_stats(trades)
        assert s["best_trade"] == pytest.approx(10.0)
        assert s["worst_trade"] == pytest.approx(-5.0)

    def test_all_required_keys_present(self):
        s = compute_stats(_wins(5))
        expected_keys = [
            "total_trades", "wins", "losses", "win_rate", "total_pnl",
            "avg_pnl", "best_trade", "worst_trade", "sharpe_ratio",
            "max_drawdown_pct", "recovery_factor", "profit_factor",
            "avg_duration_mins", "expectancy",
        ]
        for k in expected_keys:
            assert k in s, f"Missing key: {k}"


class TestSharpe:
    def test_positive_sharpe_for_positive_pnls(self):
        # Varied positive P&Ls — std > 0 so Sharpe is computable and positive
        pnls = [3.0, 2.0, 4.0, 3.5, 2.5] * 4
        assert compute_sharpe(pnls) > 0

    def test_zero_sharpe_for_single_trade(self):
        assert compute_sharpe([5.0]) == 0.0

    def test_zero_sharpe_for_empty(self):
        assert compute_sharpe([]) == 0.0

    def test_negative_sharpe_for_mixed_negative_pnls(self):
        # Varied negative P&Ls — std > 0, mean negative → Sharpe < 0
        pnls = [-1.0, -2.0, -1.5, -3.0, -0.5] * 4
        assert compute_sharpe(pnls) < 0


class TestMaxDrawdown:
    def test_no_drawdown_on_all_wins(self):
        assert compute_max_drawdown([3.0, 3.0, 3.0]) == 0.0

    def test_drawdown_detected(self):
        pnls = [10.0, -5.0, -3.0, 1.0]   # peak 10 → trough 2 → DD 80%
        dd = compute_max_drawdown(pnls)
        assert dd > 0.0

    def test_empty_returns_zero(self):
        assert compute_max_drawdown([]) == 0.0


class TestByModelStats:
    def test_splits_by_model(self):
        trades = _wins(5, model="OU_GRIND") + _losses(5, model="OU_RANGE")
        result = by_model_stats(trades)
        assert "OU_GRIND" in result
        assert "OU_RANGE"  in result
        assert result["OU_GRIND"]["win_rate"] == 1.0
        assert result["OU_RANGE"]["win_rate"]  == 0.0

    def test_unknown_model_grouped(self):
        trades = [_trade(model=None)]
        result = by_model_stats(trades)
        assert "UNKNOWN" in result


class TestBySessionStats:
    def test_london_open_identified(self):
        trades = [_trade(time="08:30:00")]   # hour 8 = LONDON_OPEN
        result = by_session_stats(trades)
        assert "LONDON_OPEN" in result

    def test_ny_overlap_identified(self):
        trades = [_trade(time="13:00:00")]   # hour 13 = NY_OVERLAP
        result = by_session_stats(trades)
        assert "NY_OVERLAP" in result

    def test_other_for_unknown_hour(self):
        trades = [_trade(time="03:00:00")]   # off-session
        result = by_session_stats(trades)
        assert "OTHER" in result


class TestRollingStats:
    def test_empty_returns_empty(self):
        s = rolling_stats([], 7)
        assert s["total_trades"] == 0

    def test_all_within_window(self):
        # All trades have today's date — all within 30-day window
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = [_trade(date=today) for _ in range(10)]
        s = rolling_stats(trades, 30)
        assert s["total_trades"] == 10


# ─────────────────────────────────────────────────────────────────────────────
# TestModelEvaluator
# ─────────────────────────────────────────────────────────────────────────────

class TestKellyFraction:
    def test_positive_edge_above_25pct_wr_with_3rr(self):
        # 1:3 RR: break-even WR = 25%, so WR=0.5 should be positive
        assert kelly_fraction(0.50, 3.0) > 0

    def test_break_even_at_25pct(self):
        # Kelly = 0.25 - 0.75/3 = 0.25 - 0.25 = 0.0
        assert kelly_fraction(0.25, 3.0) == pytest.approx(0.0, abs=0.001)

    def test_negative_edge_capped_at_zero(self):
        assert kelly_fraction(0.20, 3.0) == 0.0

    def test_max_cap_at_025(self):
        # Kelly at 100% WR should be capped
        assert kelly_fraction(1.0, 3.0) <= 0.25

    def test_zero_wr_returns_zero(self):
        assert kelly_fraction(0.0) == 0.0


class TestBinomialSignificance:
    def test_high_wr_significant(self):
        # 40 wins out of 50 is very significant
        result = binomial_significance(40, 50)
        assert result["significant"] is True
        assert result["p_value"] < 0.05

    def test_50pct_wr_not_significant(self):
        result = binomial_significance(5, 10)
        assert result["significant"] is False

    def test_zero_trades_returns_defaults(self):
        result = binomial_significance(0, 0)
        assert result["significant"] is False
        assert result["p_value"] == 1.0

    def test_returns_required_keys(self):
        result = binomial_significance(5, 10)
        assert "p_value" in result
        assert "significant" in result
        assert "test_used" in result


class TestDetectDrift:
    def test_no_drift_insufficient_data(self):
        trades = _wins(5) + _losses(5)
        drift = detect_drift(trades, lookback=20)
        assert drift["detected"] is False

    def test_drift_detected_when_recent_is_much_worse(self):
        historical = _wins(30)               # 100% historical WR
        recent     = _losses(20)             # 0% recent WR → delta = 1.0
        drift = detect_drift(historical + recent, lookback=20)
        assert drift["detected"] is True
        assert drift["delta"] > 0.15

    def test_no_drift_when_stable(self):
        # 60% win rate throughout
        trades = (_wins(6) + _losses(4)) * 5
        drift = detect_drift(trades, lookback=10)
        assert drift["delta"] < 0.15  # consistent, no significant drift


class TestEvaluateModel:
    def test_insufficient_data(self):
        ev = evaluate_model("OU_GRIND", _wins(5))   # < 10
        assert ev.status == "INSUFFICIENT_DATA"
        assert ev.total_trades == 5

    def test_healthy_model(self):
        trades = _wins(7) + _losses(3)   # 70% WR
        ev = evaluate_model("OU_GRIND", trades)
        assert ev.status in ("HEALTHY", "OUTPERFORM")
        assert ev.win_rate == pytest.approx(0.7)

    def test_underperform_detected(self):
        # 10% WR over 50 trades — clearly underperforming
        trades = _wins(5) + _losses(45)
        ev = evaluate_model("OU_GRIND", trades)
        assert ev.status in ("UNDERPERFORM", "DRIFT")

    def test_outperform_detected(self):
        # Uniform wins — no drift possible (recent_wr == historical_wr == 1.0)
        trades = _wins(35)
        ev = evaluate_model("OU_GRIND", trades)
        assert ev.status in ("OUTPERFORM", "HEALTHY")

    def test_score_range(self):
        trades = _wins(7) + _losses(3)
        ev = evaluate_model("OU_GRIND", trades)
        assert 0 <= ev.score <= 100

    def test_drift_takes_priority(self):
        historical = _wins(30)
        recent     = _losses(20)   # 0% recent
        trades = historical + recent
        ev = evaluate_model("OU_GRIND", trades)
        assert ev.status == "DRIFT"

    def test_model_evaluation_has_required_fields(self):
        ev = evaluate_model("OU_RANGE", _wins(10) + _losses(5))
        assert hasattr(ev, "win_rate")
        assert hasattr(ev, "kelly_fraction")
        assert hasattr(ev, "significance")
        assert hasattr(ev, "drift")
        assert hasattr(ev, "findings")
        assert isinstance(ev.findings, list)

    def test_recommendation_present_for_underperform(self):
        trades = _wins(3) + _losses(27)
        ev = evaluate_model("OU_GRIND", trades)
        if ev.status == "UNDERPERFORM":
            assert ev.recommendation is not None
            assert "z-score" in ev.recommendation.lower() or "threshold" in ev.recommendation.lower()


# ─────────────────────────────────────────────────────────────────────────────
# TestParameterOptimizer
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeSlRanges:
    def test_empty_returns_empty(self):
        assert analyze_sl_ranges([]) == []

    def test_buckets_are_ranked_by_avg_pnl(self):
        good = [_trade(sl_distance=5.0, result="WIN",  pnl=3.0) for _ in range(10)]
        bad  = [_trade(sl_distance=7.0, result="LOSS", pnl=-1.0) for _ in range(10)]
        result = analyze_sl_ranges(good + bad)
        if len(result) >= 2:
            assert result[0]["avg_pnl"] >= result[-1]["avg_pnl"]

    def test_bucket_below_min_excluded(self):
        # Only 3 trades with sl=5 — below _MIN_BUCKET of 5
        trades = [_trade(sl_distance=5.0) for _ in range(3)]
        result = analyze_sl_ranges(trades)
        assert result == []


class TestAnalyzeDirectionBias:
    def test_no_bias_when_equal(self):
        trades = _wins(10, direction="BUY") + _wins(10, direction="SELL")
        result = analyze_direction_bias(trades)
        assert result["BUY"]["win_rate"]  == 1.0
        assert result["SELL"]["win_rate"] == 1.0
        assert result["stronger"] == "BALANCED"

    def test_buy_stronger_detected(self):
        buys  = _wins(15, direction="BUY")
        sells = _losses(10, direction="SELL")
        result = analyze_direction_bias(buys + sells)
        assert result["BUY"]["win_rate"] > result["SELL"]["win_rate"]

    def test_empty_direction_returns_zero_trades(self):
        result = analyze_direction_bias([])
        assert result["BUY"]["trades"]  == 0
        assert result["SELL"]["trades"] == 0


class TestGenerateParameterSuggestions:
    def test_returns_list(self):
        result = generate_parameter_suggestions([], [], {
            "BUY":  {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0},
            "SELL": {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0},
            "bias_delta": 0, "stronger": "BALANCED",
        }, [])
        assert isinstance(result, list)
        assert len(result) > 0

    def test_no_data_returns_fallback_message(self):
        result = generate_parameter_suggestions([], [], {
            "BUY":  {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0},
            "SELL": {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0},
            "bias_delta": 0, "stronger": "BALANCED",
        }, [])
        assert any("no statistically" in s.lower() or "within expected" in s.lower() for s in result)


# ─────────────────────────────────────────────────────────────────────────────
# TestRegimeValidator
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateRegimeAccuracy:
    def test_returns_dict_keyed_by_model(self):
        trades = _wins(5, model="OU_GRIND") + _wins(5, model="OU_RANGE")
        result = validate_regime_accuracy(trades)
        assert "OU_GRIND" in result
        assert "OU_RANGE"  in result

    def test_good_verdict_on_high_wr(self):
        trades = _wins(10, model="OU_GRIND")
        result = validate_regime_accuracy(trades)
        assert result["OU_GRIND"]["verdict"] == "GOOD"

    def test_poor_verdict_on_low_wr(self):
        trades = _losses(10, model="OU_GRIND")
        result = validate_regime_accuracy(trades)
        assert result["OU_GRIND"]["verdict"] in ("POOR", "MARGINAL")

    def test_insufficient_data_verdict(self):
        trades = _wins(2, model="OU_GRIND")
        result = validate_regime_accuracy(trades)
        assert result["OU_GRIND"]["verdict"] == "INSUFFICIENT_DATA"


class TestAnalyzeRegimeStability:
    def test_stable_when_no_flips(self):
        regimes = ["TRENDING"] * 10
        result = analyze_regime_stability(regimes)
        assert result["flip_rate"] == 0.0
        assert result["stable"] is True

    def test_unstable_when_alternating(self):
        regimes = ["TRENDING", "RANGING"] * 5
        result = analyze_regime_stability(regimes)
        assert result["flip_rate"] == pytest.approx(1.0)
        assert result["stable"] is False

    def test_dominant_is_most_common(self):
        regimes = ["TRENDING"] * 7 + ["RANGING"] * 3
        result = analyze_regime_stability(regimes)
        assert result["dominant"] == "TRENDING"

    def test_distribution_counts_correct(self):
        regimes = ["TRENDING"] * 5 + ["VOLATILE"] * 3
        result = analyze_regime_stability(regimes)
        assert result["distribution"]["TRENDING"] == 5
        assert result["distribution"]["VOLATILE"] == 3

    def test_empty_list_handled(self):
        result = analyze_regime_stability([])
        assert result["dominant"] is None
        assert result["stable"] is True


class TestGenerateRegimeSuggestions:
    def test_good_verdict_no_suggestions(self):
        accuracy = {
            "OU_GRIND": {"verdict": "GOOD",    "accuracy_proxy": 0.70},
            "OU_RANGE":  {"verdict": "GOOD",    "accuracy_proxy": 0.65},
        }
        result = generate_regime_suggestions(accuracy)
        assert any("acceptable" in s.lower() for s in result)

    def test_poor_verdict_generates_suggestion(self):
        accuracy = {
            "OU_GRIND": {"verdict": "POOR", "accuracy_proxy": 0.30},
        }
        result = generate_regime_suggestions(accuracy)
        assert any("hurst" in s.lower() or "threshold" in s.lower() for s in result)


# ─────────────────────────────────────────────────────────────────────────────
# TestAdaptationReporter
# ─────────────────────────────────────────────────────────────────────────────

class TestCompileReport:
    def _make_eval(self, status="HEALTHY", wr=0.60, score=65):
        from agents.mahoraga.skills.model_evaluator import ModelEvaluation
        return ModelEvaluation(
            model="OU_GRIND", total_trades=20, win_rate=wr,
            total_pnl=12.0, kelly_fraction=0.08, status=status,
            significance={"p_value": 0.04, "significant": True, "test_used": "scipy"},
            drift={"detected": False, "recent_wr": wr, "historical_wr": wr, "delta": 0.0},
            score=score, findings=["Test finding."], recommendation="Test recommendation.",
        )

    def test_report_has_required_keys(self):
        ev = self._make_eval()
        report = compile_report(
            overall_stats=compute_stats(_wins(10)),
            model_evaluations=[ev],
            regime_accuracy={},
            param_suggestions=["No adjustment needed."],
            regime_suggestions=["Regime OK."],
        )
        assert "generated_at" in report
        assert "overall" in report
        assert "by_model" in report
        assert "recommendations" in report
        assert "summary_counts" in report

    def test_critical_for_drift(self):
        from agents.mahoraga.skills.model_evaluator import ModelEvaluation
        ev = ModelEvaluation(
            model="OU_GRIND", total_trades=30, win_rate=0.25,
            total_pnl=-5.0, kelly_fraction=0.0, status="DRIFT",
            significance={"p_value": 0.03, "significant": True, "test_used": "scipy"},
            drift={"detected": True, "recent_wr": 0.20, "historical_wr": 0.65, "delta": 0.45},
            score=15, findings=["Drift found."], recommendation="Pause model.",
        )
        report = compile_report(
            overall_stats=compute_stats(_wins(10)),
            model_evaluations=[ev],
            regime_accuracy={},
            param_suggestions=[],
            regime_suggestions=[],
        )
        assert report["summary_counts"]["CRITICAL"] >= 1

    def test_low_for_outperform(self):
        ev = self._make_eval(status="OUTPERFORM", wr=0.80, score=90)
        report = compile_report(
            overall_stats=compute_stats(_wins(10)),
            model_evaluations=[ev],
            regime_accuracy={},
            param_suggestions=[],
            regime_suggestions=[],
        )
        assert report["summary_counts"]["LOW"] >= 1

    def test_recommendations_sorted_by_priority(self):
        from agents.mahoraga.skills.model_evaluator import ModelEvaluation

        ev_drift = ModelEvaluation(
            model="OU_GRIND", total_trades=30, win_rate=0.20,
            total_pnl=-10.0, kelly_fraction=0.0, status="DRIFT",
            significance={"p_value": 0.01, "significant": True, "test_used": "scipy"},
            drift={"detected": True, "recent_wr": 0.10, "historical_wr": 0.60, "delta": 0.50},
            score=10, findings=["Drift."], recommendation="Pause.",
        )
        ev_ok = ModelEvaluation(
            model="OU_RANGE", total_trades=20, win_rate=0.60,
            total_pnl=8.0, kelly_fraction=0.08, status="OUTPERFORM",
            significance={"p_value": 0.04, "significant": True, "test_used": "scipy"},
            drift={"detected": False, "recent_wr": 0.60, "historical_wr": 0.60, "delta": 0.0},
            score=70, findings=["Good."], recommendation=None,
        )
        report = compile_report(
            overall_stats=compute_stats(_wins(10)),
            model_evaluations=[ev_drift, ev_ok],
            regime_accuracy={},
            param_suggestions=[],
            regime_suggestions=[],
        )
        recs = report["recommendations"]
        if len(recs) >= 2:
            assert _priority_order(recs[0]["priority"]) <= _priority_order(recs[1]["priority"])


class TestFormatWhatsappSummary:
    def test_returns_string(self):
        report = {
            "period_days": 30,
            "overall": {"total_trades": 20, "win_rate": 0.60, "total_pnl": 5.0,
                        "sharpe_ratio": 1.2, "max_drawdown_pct": 15.0, "profit_factor": 1.8},
            "by_model": {},
            "summary_counts": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 1, "LOW": 1},
            "recommendations": [],
        }
        result = format_whatsapp_summary(report)
        assert isinstance(result, str)
        assert "MAHORAGA" in result

    def test_weekly_label_appears(self):
        report = {
            "period_days": 90,
            "overall": {"total_trades": 50, "win_rate": 0.55, "total_pnl": 20.0,
                        "sharpe_ratio": 1.5, "max_drawdown_pct": 10.0, "profit_factor": 2.0},
            "by_model": {},
            "summary_counts": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
            "recommendations": [],
        }
        result = format_whatsapp_summary(report, is_weekly=True)
        assert "Weekly" in result

    def test_critical_count_mentioned(self):
        report = {
            "period_days": 30,
            "overall": {"total_trades": 20, "win_rate": 0.25, "total_pnl": -5.0,
                        "sharpe_ratio": -0.5, "max_drawdown_pct": 40.0, "profit_factor": 0.5},
            "by_model": {},
            "summary_counts": {"CRITICAL": 2, "HIGH": 1, "MEDIUM": 0, "LOW": 0},
            "recommendations": [],
        }
        result = format_whatsapp_summary(report)
        assert "CRITICAL" in result or "2" in result


class TestHelpers:
    def test_priority_order_sorted(self):
        assert _priority_order("CRITICAL") < _priority_order("HIGH")
        assert _priority_order("HIGH")     < _priority_order("MEDIUM")
        assert _priority_order("MEDIUM")   < _priority_order("LOW")

    def test_short_model_name(self):
        assert _short_model_name("OU_GRIND")    == "Model A"
        assert _short_model_name("OU_RANGE")     == "Model B"
        assert _short_model_name("ASIAN_BREAKOUT") == "Model C"
        assert _short_model_name("UNKNOWN")         == "UNKNOWN"
