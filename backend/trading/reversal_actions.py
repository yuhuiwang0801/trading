"""Conservative normalization of Alpaca corporate-action research records.

The upstream symbol is not a permanent identity. Explicit cash amounts are used
only when their basis is compatible with the adjusted-price factors, allowing
for the provider's cent rounding. Complex/conflicting records remain disclosed
fallbacks. This is an accounting normalization, never a signal/event filter.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

import numpy as np
import pandas as pd


def normalize_corporate_actions(panel: pd.DataFrame, actions: list[dict], *, price_quantum: float = 0.01) -> tuple[pd.DataFrame, dict]:
    """Return event-only columns and an audit; missing events remain NaN.

    Rates on a simultaneous split or spin-off date are not assigned a guessed
    pre/post action share basis. Multiple differing cash records are not blindly
    summed. Payment dates use ``payable_date`` only, never ``process_date``.
    """
    needed = {"session", "symbol", "security_id", "close", "tr_close", "split_close"}
    if not needed <= set(panel):
        raise ValueError(f"Corporate-action normalization requires {sorted(needed)}")
    p = panel.copy().reset_index(drop=True)
    p["session"] = pd.to_datetime(p["session"]).dt.strftime("%Y-%m-%d")
    p["dividend"] = np.nan
    p["dividend_pay_session"] = None
    p["split_ratio"] = np.nan
    p["explicit_action_ids"] = None
    if p.empty:
        return p, {"input_events": len(actions), "mapped_cash_days": 0, "mapped_split_days": 0, "fallbacks": []}
    ordered = p.sort_values(["security_id", "session"])
    prior = ordered.groupby("security_id", sort=False)[["close", "split_close", "tr_close"]].shift()
    current = ordered[["close", "split_close", "tr_close"]]
    split_ratio = (current["split_close"] / current["close"]) / (prior["split_close"] / prior["close"])
    distribution_ratio = (current["tr_close"] / current["split_close"]) / (prior["tr_close"] / prior["split_close"])
    # For a standard backward dividend adjustment, F_new/F_old=C_prev/(C_prev-D).
    implied_rate = prior["close"] * (1 - 1 / distribution_ratio)
    half = price_quantum / 2
    factor_uncertainty = sum(half / frame[col].clip(lower=half + 1e-8)
                             for frame in (current, prior) for col in ("tr_close", "split_close"))
    rate_uncertainty = prior["close"] * factor_uncertainty * 1.1
    split_uncertainty = (half / current["split_close"].clip(lower=half + 1e-8)
                         + half / prior["split_close"].clip(lower=half + 1e-8)) * 1.1 + 0.000005
    lookup = defaultdict(list)
    for index, row in p.iterrows():
        lookup[row["symbol"], row["session"]].append(index)
    start, end = p["session"].min(), p["session"].max()
    counts = Counter(input_events=len(actions))
    type_counts = Counter(action.get("action_type", "unknown") for action in actions)
    grouped = defaultdict(list)
    seen_ids = set()
    complex_dates = set()
    for action in actions:
        identifier = action.get("id")
        if identifier and identifier in seen_ids:
            counts["duplicate_ids_ignored"] += 1
            continue
        if identifier:
            seen_ids.add(identifier)
        kind = action.get("action_type")
        if kind in {"cash_dividends", "forward_splits", "reverse_splits"}:
            symbol, date = action.get("symbol"), action.get("ex_date")
            if not symbol or not date:
                counts["missing_symbol_or_ex_date"] += 1
                continue
            date = pd.Timestamp(date).date().isoformat()
            if not start <= date <= end:
                counts["events_outside_panel_dates"] += 1
                continue
            grouped[symbol, date].append(action)
        elif kind in {"spin_offs", "rights_distributions"}:
            symbol, date = action.get("source_symbol"), action.get("ex_date")
            if symbol and date:
                complex_dates.add((symbol, date))
    fallbacks, mapped = [], []
    for key, events in sorted(grouped.items()):
        indices = lookup.get(key, [])
        if len(indices) != 1:
            counts["unmatched_symbol_exdate_events" if not indices else "ambiguous_security_mapping_events"] += len(events)
            continue
        index = indices[0]
        splits = [event for event in events if event["action_type"] in {"forward_splits", "reverse_splits"}]
        cash = [event for event in events if event["action_type"] == "cash_dividends"]
        applied_ids = []

        def fallback(kind, reason, **details):
            counts[f"{kind}_fallback_days"] += 1
            fallbacks.append({"symbol": key[0], "session": key[1], "kind": kind, "reason": reason, **details})

        if splits:
            ratios = {float(event["new_rate"]) / float(event["old_rate"]) for event in splits
                      if event.get("old_rate", 0) > 0 and event.get("new_rate", 0) > 0}
            observed = float(split_ratio.loc[index])
            noise = float(split_uncertainty.loc[index])
            if len(ratios) != 1:
                fallback("split", "conflicting_or_invalid_rates")
            else:
                ratio = next(iter(ratios))
                if not math.isfinite(observed) or abs(observed / ratio - 1) > max(noise, 0.001):
                    fallback("split", "explicit_ratio_incompatible_with_price_factors", explicit_ratio=ratio,
                             observed_ratio=observed if math.isfinite(observed) else None)
                else:
                    p.at[index, "split_ratio"] = ratio
                    applied_ids.extend(event.get("id") for event in splits)
                    counts["mapped_split_days"] += 1
                    mapped.append({"symbol": key[0], "session": key[1], "type": "split", "ratio": ratio})
        if cash:
            # Exact same economic terms with different IDs/CUSIPs occur in this
            # feed after legal reorganizations. Record this deduplication.
            terms = {}
            for event in cash:
                term = (float(event.get("rate", float("nan"))), event.get("payable_date"), bool(event.get("special", False)))
                terms.setdefault(term, event)
            counts["identical_cash_terms_collapsed"] += len(cash) - len(terms)
            if len(terms) != 1:
                fallback("cash", "multiple_differing_cash_records", rates=[float(event.get("rate", 0)) for event in cash])
            elif splits or key in complex_dates:
                fallback("cash", "same_day_split_or_complex_distribution")
            else:
                (rate, payment, _), event = next(iter(terms.items()))
                estimate = float(implied_rate.loc[index])
                uncertainty = float(rate_uncertainty.loc[index])
                allowed_error = max(0.03, abs(rate) * 0.10, uncertainty) if math.isfinite(uncertainty) else 0.03
                if not math.isfinite(rate) or rate <= 0:
                    fallback("cash", "invalid_cash_rate")
                elif not math.isfinite(estimate) or abs(estimate-rate) > allowed_error:
                    fallback("cash", "cash_rate_basis_incompatible_with_price_factors", explicit_rate=rate,
                             factor_implied_rate=estimate if math.isfinite(estimate) else None,
                             allowed_rounding_basis_error=allowed_error)
                elif payment and pd.Timestamp(payment) < pd.Timestamp(key[1]):
                    fallback("cash", "payment_precedes_ex_date")
                else:
                    p.at[index, "dividend"] = rate
                    p.at[index, "dividend_pay_session"] = payment
                    applied_ids.extend(item.get("id") for item in cash)
                    counts["mapped_cash_days"] += 1
                    counts["cash_with_payment_date" if payment else "cash_missing_payment_date"] += 1
                    mapped.append({"symbol": key[0], "session": key[1], "type": "cash_dividend", "rate": rate,
                                   "payment_session": payment, "factor_implied_rate": estimate})
        if applied_ids:
            p.at[index, "explicit_action_ids"] = ",".join(str(x) for x in applied_ids if x)
    summary = dict(counts)
    for key in ("mapped_cash_days", "mapped_split_days", "cash_with_payment_date", "cash_missing_payment_date",
                "cash_fallback_days", "split_fallback_days", "identical_cash_terms_collapsed"):
        summary.setdefault(key, 0)
    summary.update(source_type_counts=dict(type_counts), panel_start=start, panel_end=end,
                   fallbacks=fallbacks, mapped=mapped, price_quantum=price_quantum,
                   limitations=["Symbol/ex-date mapping is not a point-in-time permanent security identifier audit.",
                                "Explicit events are cross-checked against cent-rounded price factors; missing or conflicting actions retain synthetic-distribution fallback.",
                                "Cash amount compatibility uses the standard backward dividend-adjustment identity with a 10% basis allowance plus price-rounding uncertainty; it does not prove provider identity correctness.",
                                "Exact duplicate economic terms are collapsed; multiple differing cash records, same-day splits/spin-offs and incompatible rates are not guessed.",
                                "Missing payable dates remain missing for the ledger's disclosed 30-calendar-day delay proxy."])
    return p, summary
