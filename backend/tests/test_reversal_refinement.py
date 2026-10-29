"""Causal entry screening and the economic contract for anchored exits."""
import json
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from trading.reversal_refinement import (
    peer_residuals, prepare_refinement, refined_candidates, screen_fundamentals,
)
from trading.reversal_signals import SignalConfig, prepare_signals


RULES = json.loads((Path(__file__).resolve().parents[2] / "config/reversal_research_v2.json").read_text())["entry_filters"]


def fundamentals(keys):
    result = keys[["session", "security_id"]].copy()
    result["fundamental_status"] = "known"
    result["filing_history_complete"] = True
    result["events_known"] = True
    result["revenue_yoy"] = .10
    result["net_income"] = 100.0
    result["net_income_yoy"] = .10
    result["earnings_age_sessions"] = np.nan
    result["risk_event_age_sessions"] = np.nan
    return result


def prices():
    days = [day.date().isoformat() for day in xcals.get_calendar("XNYS").sessions_in_range("2023-01-03", "2023-03-15")]
    rows = []
    for i, day in enumerate(days):
        for stock in range(6):
            sid = f"S{stock}"
            drift = -.01 if stock == 0 else (-.004 if stock == 1 else .001 * stock)
            price = 100 * np.exp(drift * i)
            rows.append({"session": day, "security_id": sid, "symbol": sid, "issuer_id": sid,
                         "industry": "35", "open": price, "close": price, "tr_close": price,
                         "adj_open": price, "split_close": price, "volume": 1_000_000})
    return pd.DataFrame(rows), days


def test_unknown_financials_or_event_history_fail_closed_for_entries():
    rows = fundamentals(pd.DataFrame({"session": ["2024-02-01"] * 4, "security_id": ["A", "B", "C", "D"]}))
    rows.loc[0, "fundamental_status"] = "unknown_no_comparable_revenue_income"
    rows.loc[1, "filing_history_complete"] = False
    rows.loc[2, "revenue_yoy"] = np.nan
    rows.loc[3, "net_income_yoy"] = np.inf
    screened = screen_fundamentals(rows, RULES)
    assert not screened.screen_pass.any()
    assert set(screened.risk_class) == {"unknown"}
    assert not screened.fundamental_block.any()


def test_known_risk_event_remains_actionable_with_unknown_financials_and_history():
    rows = fundamentals(pd.DataFrame({"session": ["2024-02-01"], "security_id": ["A"]}))
    rows.loc[0, "fundamental_status"] = "unknown_no_comparable_revenue_income"
    rows.loc[0, "filing_history_complete"] = False
    rows.loc[0, ["revenue_yoy", "net_income", "net_income_yoy"]] = np.nan
    rows.loc[0, "risk_event_age_sessions"] = 0
    screened = screen_fundamentals(rows, RULES).iloc[0]
    assert not screened.financial_known
    assert not screened.events_known
    assert screened.fundamental_known
    assert screened.fundamental_block
    assert not screened.screen_pass
    assert screened.risk_class == "disclosed_risk"


def test_unclassified_8k_blocks_entries_even_when_download_history_is_complete():
    rows = fundamentals(pd.DataFrame({"session": ["2024-02-01"], "security_id": ["A"]}))
    rows.loc[0, "events_known"] = False
    screened = screen_fundamentals(rows, RULES).iloc[0]
    assert screened.financial_known
    assert not screened.events_known
    assert not screened.screen_pass
    assert screened.risk_class == "unknown"


def test_normal_earnings_cooldown_only_blocks_entries_and_expires_after_five_sessions():
    rows = fundamentals(pd.DataFrame({"session": ["2024-02-01"] * 3, "security_id": ["A", "B", "C"]}))
    rows["earnings_age_sessions"] = [0, 4, 5]
    screened = screen_fundamentals(rows, RULES)
    assert screened.screen_pass.tolist() == [False, False, True]
    assert not screened.fundamental_block.any()
    assert screened.fundamental_known.all()


def test_screening_does_not_replace_rejected_original_decile_candidate():
    panel, _ = prices()
    prepared, scores, _ = prepare_signals(panel, SignalConfig(min_history_sessions=3, liquidity_sessions=3))
    f = fundamentals(prepared)
    f.loc[f.security_id == "S0", "net_income"] = -1.0
    refined, _ = prepare_refinement(prepared, scores, f, RULES)
    base = refined_candidates(refined)
    assert not base.empty
    assert set(base.security_id) == {"S0"}
    assert refined_candidates(refined, filtered=True).empty
    assert refined.loc[refined.security_id == "S1", "entry_rejection_reason"].eq("outside_original_top_decile").all()


def test_five_daily_peer_residuals_equal_the_original_five_day_log_gap():
    panel, _ = prices()
    prepared, scores, _ = prepare_signals(panel, SignalConfig(min_history_sessions=3, liquidity_sessions=3))
    residual = peer_residuals(prepared).sort_values(["security_id", "session"])
    residual["five_day_residual"] = residual.groupby("security_id").residual_log_return.transform(
        lambda values: values.rolling(5, min_periods=5).sum())
    joined = scores.merge(residual, on=["session", "security_id"], validate="one_to_one")
    joined = joined[joined.scorable]
    assert not joined.empty
    expected_gap = np.log1p(joined.peer_return) - np.log1p(joined.raw_return)
    np.testing.assert_allclose(-joined.five_day_residual, expected_gap, atol=1e-12, rtol=1e-12)


def test_future_prices_and_new_fundamental_rows_do_not_change_past_features_or_candidates():
    panel, days = prices()
    cutoff = "2023-02-28"
    altered = panel.copy()
    altered.loc[altered.session > cutoff, ["open", "close", "tr_close", "adj_open", "split_close"]] *= 10
    config = SignalConfig(min_history_sessions=3, liquidity_sessions=3)
    p1, scores1, _ = prepare_signals(panel, config)
    p2, scores2, _ = prepare_signals(altered, config)
    f1 = fundamentals(p1)
    f2 = f1.copy()
    f2.loc[f2.session > cutoff, "risk_event_age_sessions"] = 0
    a, x = prepare_refinement(p1, scores1, f1, RULES)
    b, y = prepare_refinement(p2, scores2, f2, RULES)
    pd.testing.assert_frame_equal(a[a.session <= cutoff].reset_index(drop=True), b[b.session <= cutoff].reset_index(drop=True))
    pd.testing.assert_frame_equal(x[x.session <= cutoff].reset_index(drop=True), y[y.session <= cutoff].reset_index(drop=True))


def test_missing_bar_makes_residual_path_missing_instead_of_forward_filling():
    panel, days = prices()
    prepared, _, _ = prepare_signals(panel, SignalConfig(min_history_sessions=3, liquidity_sessions=3))
    missing_day = "2023-02-15"
    next_day = days[days.index(missing_day) + 1]
    prepared = prepared[~((prepared.session == missing_day) & (prepared.security_id == "S0"))]
    residual = peer_residuals(prepared)
    own = residual[(residual.security_id == "S0") & residual.session.isin([missing_day, next_day])]
    assert len(own) == 2
    assert own.residual_log_return.isna().all()
    # Six-stock test pool has exactly five peers; the missing member also makes
    # everyone else's comparison too thin on both dates.
    assert residual.loc[residual.session.isin([missing_day, next_day]), "residual_log_return"].isna().all()
