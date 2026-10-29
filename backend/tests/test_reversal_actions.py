import pandas as pd
import pytest

from trading.reversal_actions import normalize_corporate_actions


def panel():
    return pd.DataFrame([
        {"session": "2023-01-03", "symbol": "ABC", "security_id": "ID", "close": 100., "split_close": 100., "tr_close": 99.},
        {"session": "2023-01-04", "symbol": "ABC", "security_id": "ID", "close": 99., "split_close": 99., "tr_close": 99.},
        {"session": "2023-01-05", "symbol": "ABC", "security_id": "ID", "close": 100., "split_close": 100., "tr_close": 100.}])


def dividend(**extra):
    return {"action_type": "cash_dividends", "symbol": "ABC", "ex_date": "2023-01-04", "rate": 1.,
            "id": "one", "process_date": "2023-01-04", **extra}


def test_event_only_mapping_does_not_turn_missing_into_zero_or_process_into_pay_date():
    result, audit = normalize_corporate_actions(panel(), [dividend()])
    assert pd.isna(result.dividend.iloc[0]) and pd.isna(result.dividend.iloc[2])
    assert result.dividend.iloc[1] == 1
    assert result.dividend_pay_session.iloc[1] is None
    assert audit["cash_missing_payment_date"] == 1


def test_duplicates_do_not_double_count_but_conflicting_records_stay_proxy():
    result, audit = normalize_corporate_actions(panel(), [dividend(), dividend(id="two")])
    assert result.dividend.iloc[1] == 1
    assert audit["identical_cash_terms_collapsed"] == 1
    result, audit = normalize_corporate_actions(panel(), [dividend(), dividend(id="two", rate=2)])
    assert result.dividend.isna().all()
    assert audit["fallbacks"][0]["reason"] == "multiple_differing_cash_records"


def test_adjusted_instead_of_raw_cash_amount_is_rejected():
    result, audit = normalize_corporate_actions(panel(), [dividend(rate=.25)])
    assert result.dividend.isna().all()
    assert audit["fallbacks"][0]["reason"] == "cash_rate_basis_incompatible_with_price_factors"


def test_payment_date_preserved_and_simultaneous_split_cash_basis_not_guessed():
    result, audit = normalize_corporate_actions(panel(), [dividend(payable_date="2023-01-10")])
    assert result.dividend_pay_session.iloc[1] == "2023-01-10"
    assert audit["cash_with_payment_date"] == 1
    p = panel()
    p.loc[p.session == "2023-01-03", "split_close"] = 50
    split = {"action_type": "forward_splits", "symbol": "ABC", "ex_date": "2023-01-04", "new_rate": 2, "old_rate": 1, "id": "s"}
    result, audit = normalize_corporate_actions(p, [dividend(), split])
    assert result.split_ratio.iloc[1] == 2
    assert pd.isna(result.dividend.iloc[1])
    assert audit["fallbacks"][0]["reason"] == "same_day_split_or_complex_distribution"


def test_unrelated_acquirer_merger_never_closes_or_changes_queried_stock():
    event = {"action_type": "cash_mergers", "acquirer_symbol": "ABC", "acquiree_symbol": "XYZ",
             "effective_date": "2023-01-04", "rate": 200, "id": "m"}
    result, audit = normalize_corporate_actions(panel(), [event])
    assert result.dividend.isna().all() and result.split_ratio.isna().all()
    assert audit["mapped_cash_days"] == 0
