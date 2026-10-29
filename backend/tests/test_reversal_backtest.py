"""Economic invariants of the research-only daily proxy account."""
from dataclasses import replace

import exchange_calendars as xcals
import pandas as pd
import pytest

from trading.reversal_backtest import ReversalBacktestConfig, run_backtest


def dataset(start="2023-01-03", end="2023-02-28", count=1, price=100.0):
    sessions = [day.date().isoformat() for day in xcals.get_calendar("XNYS").sessions_in_range(start, end)]
    records = []
    for session in sessions:
        for i in range(count):
            symbol = f"S{i:02d}"
            records.append({"session": session, "symbol": symbol, "security_id": symbol, "issuer_id": symbol,
                            "industry": f"I{i//5:02d}", "open": price, "close": price, "volume": 1_000_000,
                            "tr_close": price, "adj_open": price, "split_close": price, "eligible": True})
    return pd.DataFrame(records), sessions


def signals(sessions, symbols=("S00",)):
    return pd.DataFrame([{"signal_session": session, "security_id": symbol, "score": 1.0 - i / 1000}
                         for session in sessions for i, symbol in enumerate(symbols)])


def test_exit_is_five_exchange_intervals_after_entry_and_never_signal_close():
    panel, days = dataset(start="2023-01-09")  # MLK holiday is excluded.
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0))
    buy, sell = result["fills"]
    assert buy["session"] == days[1]
    assert sell["session"] == days[6] == "2023-01-18"
    assert result["trades"][0]["holding_intervals"] == 5
    assert result["metrics"]["total_return"] == 0


def test_daily_and_portfolio_caps_integer_shares_and_no_replenishment():
    panel, days = dataset(count=40)
    result = run_backtest(panel, signals(days[:10], tuple(f"S{i:02d}" for i in range(40))),
                          ReversalBacktestConfig(cost_bps=0))
    purchases = pd.DataFrame([fill for fill in result["fills"] if fill["side"] == "buy"])
    assert purchases.groupby("session").size().max() <= 4
    assert purchases.groupby("session")["notional"].sum().max() <= 20_000
    assert max(day["position_count"] for day in result["equity"]) <= 20
    assert all(day["settled_cash"] >= 0 for day in result["equity"])
    assert any(row["reason"] == "ALREADY_HELD" for row in result["rejections"])
    assert any(row["reason"] == "SOLD_TODAY" for row in result["rejections"])
    for fill in result["fills"]:
        if fill["side"] == "buy":
            assert fill["quantity"] == int(fill["quantity"])


def test_issuer_and_industry_limits_apply_to_existing_holdings():
    panel, days = dataset(count=10)
    panel.loc[panel.symbol.isin(["S00", "S01"]), "issuer_id"] = "SAME"
    panel["industry"] = "ALL"
    result = run_backtest(panel, signals(days[:2], tuple(f"S{i:02d}" for i in range(10))),
                          ReversalBacktestConfig(cost_bps=0))
    buys = [fill for fill in result["fills"] if fill["side"] == "buy"]
    assert len(buys) == 5
    assert "S01" not in [fill["security_id"] for fill in buys]
    assert sum(fill["notional"] for fill in buys) == 25_000
    reasons = {row["reason"] for row in result["rejections"]}
    assert "INSUFFICIENT_ISSUER_BUDGET_FOR_ONE_SHARE" in reasons
    assert "INSUFFICIENT_INDUSTRY_BUDGET_FOR_ONE_SHARE" in reasons


@pytest.mark.parametrize("start,expected_lag", [("2017-08-01", 3), ("2017-09-05", 2), ("2024-05-28", 1)])
def test_historical_sale_settlement_keeps_proceeds_unspendable(start, expected_lag):
    panel, days = dataset(start=start, end=(pd.Timestamp(start)+pd.Timedelta(days=40)).date().isoformat())
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0))
    sell = result["fills"][1]
    assert sell["settlement_session"] == days[6+expected_lag]
    series = {row["session"]: row for row in result["equity"]}
    assert series[days[6]]["unsettled_cash"] == 5000
    assert series[days[6+expected_lag]]["unsettled_cash"] == 0


def test_missing_exit_keeps_position_and_late_exit_without_restarting_clock():
    panel, days = dataset()
    panel = panel[~panel.session.isin(days[6:8])]
    result = run_backtest(panel, signals(days[:8]), ReversalBacktestConfig(cost_bps=0))
    assert result["trades"][0]["exit_session"] == days[8]
    assert result["trades"][0]["holding_intervals"] == 7
    assert result["metrics"]["overdue_position_days"] == 2
    assert result["metrics"]["stale_position_days"] == 2
    assert result["fills"][0]["planned_exit_session"] == days[6]


def test_missing_next_session_does_not_defer_old_signal_to_next_available_bar():
    panel, days = dataset()
    panel = panel[panel.session != days[1]]
    result = run_backtest(panel, signals(days[:1]))
    assert result["fills"] == []
    assert result["rejections"][0]["session"] == days[1]
    assert result["rejections"][0]["reason"] == "ENTRY_UNAVAILABLE"


def test_ending_holdings_not_liquidated_and_loss_is_marked():
    panel, days = dataset()
    panel.loc[panel.session == days[3], ["close", "tr_close", "split_close"]] = 80.0
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0), end=days[3])
    assert len(result["fills"]) == 1
    assert result["trades"] == []
    assert result["ending_positions"][0]["quantity"] == 50
    assert result["metrics"]["final_equity"] == 99_000
    assert result["metrics"]["total_return"] == pytest.approx(-0.01)


def test_split_changes_share_quantity_without_phantom_profit_or_loss():
    panel, days = dataset()
    panel.loc[panel.session < days[3], ["split_close", "tr_close", "adj_open"]] = 50.0
    panel.loc[panel.session >= days[3], ["open", "close", "split_close", "tr_close", "adj_open"]] = 50.0
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0))
    assert result["fills"][0]["quantity"] == 50
    assert result["fills"][1]["quantity"] == 100
    assert result["metrics"]["total_return"] == 0
    assert [row["type"] for row in result["corporate_actions"]] == ["inferred_split"]


def test_factor_distribution_is_nonspendable_and_not_double_counted():
    panel, days = dataset()
    # Price drops $1 on ex-date; adjusted TR is flat, so $50 is receivable.
    panel.loc[panel.session >= days[3], ["open", "close", "split_close"]] = 99.0
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0), end=days[8])
    assert result["metrics"]["final_equity"] == pytest.approx(100_000)
    assert result["metrics"]["ending_distribution_receivables"] == pytest.approx(50)
    assert result["metrics"]["ending_settled_cash"] == pytest.approx(99_950)
    assert result["trades"][0]["net_pnl"] == pytest.approx(0)
    assert len([a for a in result["corporate_actions"] if a["type"] == "synthetic_distribution"]) == 1


def test_ex_date_open_exit_retains_distribution_entitlement():
    panel, days = dataset()
    panel.loc[panel.session >= days[6], ["open", "close", "split_close"]] = 99.0
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0), end=days[8])
    assert result["metrics"]["final_equity"] == pytest.approx(100_000)
    assert result["trades"][0]["distributions"] == pytest.approx(50)


def test_explicit_dividend_takes_precedence_and_actual_pay_day_releases_cash():
    panel, days = dataset()
    panel["dividend"] = 0.0
    panel["dividend_pay_session"] = None
    panel.loc[panel.session == days[3], "dividend"] = 1.0
    panel.loc[panel.session == days[3], "dividend_pay_session"] = days[5]
    panel.loc[panel.session >= days[3], ["open", "close", "split_close"]] = 99.0
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0), end=days[5])
    assert result["metrics"]["final_equity"] == pytest.approx(100_000)
    assert result["metrics"]["ending_distribution_receivables"] == 0
    assert result["metrics"]["ending_settled_cash"] == 95_050
    assert not any(a["type"] == "synthetic_distribution" for a in result["corporate_actions"])


def test_future_close_cannot_change_current_open_entry_quantities():
    panel, days = dataset(count=8)
    changed = panel.copy()
    changed.loc[changed.session == days[2], "close"] *= 10
    # Adjusted factors remain consistent; no corporate action is invented.
    changed.loc[changed.session == days[2], ["tr_close", "split_close"]] *= 10
    candidates = signals(days[:2], tuple(f"S{i:02d}" for i in range(8)))
    a = run_backtest(panel, candidates)
    b = run_backtest(changed, candidates)
    assert [f for f in a["fills"] if f["session"] <= days[2]] == [f for f in b["fills"] if f["session"] <= days[2]]


def test_costs_both_sides_and_annual_slices_share_continuous_nav():
    panel, days = dataset(start="2022-12-28", end="2023-01-31")
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=10))
    assert result["metrics"]["costs"] == 10
    assert result["metrics"]["final_equity"] == 99_990
    assert result["annual"][0]["end_equity"] == result["annual"][1]["start_equity"]
    assert result["annual"][0]["return"] == pytest.approx(-0.00005)
    assert result["annual"][1]["return"] == pytest.approx(99_990 / 99_995 - 1)


def test_execution_day_universe_gate_does_not_force_exit_existing_holding():
    panel, days = dataset(count=2)
    panel.loc[(panel.symbol == "S01") & (panel.session == days[1]), "eligible"] = False
    panel.loc[(panel.symbol == "S00") & (panel.session > days[1]), "eligible"] = False
    result = run_backtest(panel, signals(days[:1], ("S00", "S01")), ReversalBacktestConfig(cost_bps=0))
    assert len(result["fills"]) == 2
    assert result["fills"][1]["session"] == days[6]
    assert result["rejections"][0]["reason"] == "NOT_IN_EXECUTION_UNIVERSE"


def test_cash_constraint_reserves_cost_before_rounding():
    panel, days = dataset(price=100.0)
    config = replace(ReversalBacktestConfig(), initial_cash=1000, max_security_weight=1,
                     max_issuer_weight=1, max_industry_weight=1, max_daily_buy_weight=1)
    result = run_backtest(panel, signals(days[:1]), config, end=days[1])
    assert result["fills"][0]["quantity"] == 9
    assert result["metrics"]["ending_settled_cash"] == pytest.approx(99.10)


def test_interpolated_or_zero_volume_bars_cannot_fill_or_refresh_holding_marks():
    panel, days = dataset(count=2)
    panel["interpolated"] = False
    panel.loc[(panel.symbol == "S01") & (panel.session == days[1]), "interpolated"] = True
    panel.loc[(panel.symbol == "S00") & (panel.session == days[6]), "volume"] = 0
    panel.loc[(panel.symbol == "S00") & (panel.session == days[6]), ["open", "close"]] = 999.0
    result = run_backtest(panel, signals(days[:1], ("S00", "S01")), ReversalBacktestConfig(cost_bps=0))
    assert result["fills"][0]["security_id"] == "S00"
    assert result["fills"][1]["session"] == days[7]
    assert result["equity"][6]["equity"] == 100_000
    assert result["metrics"]["stale_position_days"] == 1


def test_end_boundary_ignores_far_future_panel_and_candidates():
    panel, days = dataset()
    future, future_days = dataset(start="2026-01-05", end="2026-01-30")
    candidates = signals(days[:1])
    expected = run_backtest(panel, candidates, end=days[8])
    actual = run_backtest(pd.concat([panel, future]), pd.concat([candidates, signals(future_days)]), end=days[8])
    assert actual["metrics"] == expected["metrics"]
    assert actual["fills"] == expected["fills"]


def test_cent_rounding_does_not_invent_splits_or_distributions():
    panel, days = dataset()
    for i, session in enumerate(days):
        raw = 103.0 + i * 0.17
        mask = panel.session == session
        panel.loc[mask, ["open", "close"]] = raw
        panel.loc[mask, "split_close"] = round(raw / 4, 2)
        panel.loc[mask, ["tr_close", "adj_open"]] = round(raw * .23659, 2)
    result = run_backtest(panel, signals(days[:1]), ReversalBacktestConfig(cost_bps=0))
    assert result["corporate_actions"] == []
    assert result["fills"][0]["quantity"] == result["fills"][1]["quantity"]


def rebound_inputs(panel, days, gap=0.10):
    candidates = signals(days[:1])
    candidates["entry_gap"] = gap
    features = panel[["session", "security_id"]].copy()
    features["residual_log_return"] = 0.0
    features["fundamental_known"] = True
    features["fundamental_block"] = False
    config = ReversalBacktestConfig(exit_mode="rebound", cost_bps=0)
    return candidates, features, config


def test_rebound_uses_frozen_signal_anchor_and_exits_next_open():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    features.loc[features.session == days[1], "residual_log_return"] = .04
    features.loc[features.session == days[2], "residual_log_return"] = .04
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["entry_session"] == days[1]
    assert trade["exit_signal_session"] == days[2]
    assert trade["exit_signal_phase"] == "after_close"
    assert trade["exit_session"] == days[3]
    assert trade["holding_intervals"] == 2
    assert trade["entry_gap"] == .10
    assert trade["exit_signal_relative_log_return"] == pytest.approx(.08)
    assert trade["exit_reason"] == "REBOUND_TARGET"
    assert result["fills"][1]["exit_signal_session"] == days[2]


def test_expired_rolling_gap_and_repeat_signal_never_fake_repair_or_reset_timeout():
    panel, days = dataset()
    _, features, config = rebound_inputs(panel, days)
    candidates = signals(days[:10])
    candidates["entry_gap"] = [.10] + [.0001] * 9
    # A rolling 5-day score may vanish while the relative price remains flat.
    # The trade remains anchored to the original gap and exits only at max age.
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["entry_gap"] == .10
    assert trade["exit_reason"] == "MAX_HOLD"
    assert trade["exit_signal_session"] == days[10]
    assert trade["exit_session"] == days[11]
    assert trade["holding_intervals"] == 10
    assert any(row["reason"] == "ALREADY_HELD" for row in result["rejections"])


def test_current_and_future_close_features_cannot_change_same_day_open_decisions():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    changed = features.copy()
    changed.loc[changed.session >= days[2], "residual_log_return"] = .20
    changed.loc[changed.session >= days[2], "fundamental_block"] = True
    a = run_backtest(panel, candidates, config, exit_features=features)
    b = run_backtest(panel, candidates, config, exit_features=changed)
    assert [f for f in a["fills"] if f["session"] <= days[2]] == [f for f in b["fills"] if f["session"] <= days[2]]
    assert b["trades"][0]["exit_session"] == days[3]
    # A run ending before that trigger must also ignore all later features.
    aa = run_backtest(panel, candidates, config, end=days[1], exit_features=features)
    bb = run_backtest(panel, candidates, config, end=days[1], exit_features=changed)
    assert aa == bb


def test_missing_relative_path_cannot_be_mistaken_for_recovery():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    features = features[features.session != days[1]].copy()
    features.loc[features.session == days[2], "residual_log_return"] = .50
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["relative_path_complete"] is False
    assert trade["missing_residual_sessions"] == 1
    assert trade["exit_signal_relative_log_return"] is None
    assert trade["exit_reason"] == "MAX_HOLD"
    assert trade["exit_session"] == days[11]


def test_only_known_fundamental_risk_triggers_next_session_exit():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    features["fundamental_known"] = False
    features["fundamental_block"] = True
    features.loc[features.session == days[3], "fundamental_known"] = True
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["exit_signal_session"] == days[3]
    assert trade["exit_session"] == days[4]
    assert trade["exit_reason"] == "FUNDAMENTAL_RISK"


def test_pending_rebound_exit_retries_without_retracting_or_same_day_reentry():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    candidates = pd.concat([candidates, candidates.assign(signal_session=days[3])], ignore_index=True)
    features.loc[features.session == days[1], "residual_log_return"] = .09
    features.loc[features.session == days[2], "residual_log_return"] = -.20
    panel["tradable"] = True
    panel.loc[panel.session.isin(days[2:4]), "tradable"] = False
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["exit_signal_session"] == days[1]
    assert trade["planned_exit_session"] == days[2]
    assert trade["exit_session"] == days[4]
    assert trade["holding_intervals"] == 3
    assert trade["exit_reason"] == "REBOUND_TARGET"
    assert result["metrics"]["overdue_position_days"] == 2
    assert result["metrics"]["entry_count"] == 1
    assert any(row["reason"] == "SOLD_TODAY" for row in result["rejections"])


def test_relative_adverse_move_is_measured_from_original_signal_close():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    features.loc[features.session == days[1], "residual_log_return"] = -.11
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["exit_reason"] == "RELATIVE_STOP"
    assert trade["exit_signal_session"] == days[1]
    assert trade["exit_session"] == days[2]


def test_absolute_stop_accounts_for_splits_and_dividend_entitlements():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days)
    panel["dividend"] = 0.0
    panel["dividend_pay_session"] = None
    panel.loc[panel.session < days[3], ["split_close", "tr_close", "adj_open"]] = 50.0
    panel.loc[panel.session >= days[3], ["open", "close", "split_close", "tr_close", "adj_open"]] = 50.0
    panel.loc[panel.session >= days[5], ["open", "close", "split_close"]] = 49.0
    panel.loc[panel.session == days[5], "dividend"] = 1.0
    panel.loc[panel.session == days[5], "dividend_pay_session"] = days[10]
    panel.loc[panel.session >= days[6], ["open", "close", "split_close"]] = 46.0
    panel.loc[panel.session >= days[6], "tr_close"] = 47.0
    result = run_backtest(panel, candidates, config, exit_features=features)
    trade = result["trades"][0]
    assert trade["exit_reason"] == "ABSOLUTE_STOP"
    assert trade["exit_signal_session"] == days[6]
    assert trade["exit_session"] == days[7]
    assert trade["exit_signal_total_return"] == pytest.approx(-.06)
    assert trade["distributions"] == 100
    assert trade["quantity"] == 100


def test_rebound_rejects_invalid_frozen_gap_and_requires_feature_contract():
    panel, days = dataset()
    candidates, features, config = rebound_inputs(panel, days, gap=0)
    result = run_backtest(panel, candidates, config, exit_features=features)
    assert result["fills"] == []
    assert result["rejections"][0]["reason"] == "NONPOSITIVE_OR_INVALID_ENTRY_GAP"
    with pytest.raises(ValueError, match="exit_features"):
        run_backtest(panel, candidates, config)
