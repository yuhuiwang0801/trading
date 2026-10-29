"""Research-only, daily-open proxy ledger for the reversal protocol.

This module has no broker or order submission dependency. A daily open is not a
09:35 executable quote. Candidates must already be the fixed signal-day top
decile; this ledger never expands that list to fill the portfolio.

Raw prices and raw share quantities drive cash and valuation. When explicit
corporate actions are unavailable, split-adjusted/raw factors identify splits
and total-return/split factors identify *synthetic distributions*, including
possible spin-offs. These are an accounting proxy, not verified cash dividends.
The distribution is recognized only at that day's close and becomes spendable
after a disclosed delay, so a future closing price cannot fund an opening buy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

import exchange_calendars as xcals
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ReversalBacktestConfig:
    initial_cash: float = 100_000.0
    holding_sessions: int = 5
    cost_bps: float = 10.0
    max_positions: int = 20
    max_entries_per_day: int = 4
    max_security_weight: float = 0.05
    max_issuer_weight: float = 0.05
    max_industry_weight: float = 0.25
    max_daily_buy_weight: float = 0.20
    settlement: str = "historical"
    distribution_payment_delay_days: int = 30
    action_tolerance: float = 0.000005
    adjusted_price_quantum: float = 0.01
    exit_mode: str = "fixed"
    max_hold_sessions: int = 10
    rebound_fraction: float = 0.8
    relative_stop_multiple: float | None = 1.0
    absolute_stop_loss: float | None = 0.05

    def __post_init__(self):
        values = [self.initial_cash, self.cost_bps, self.action_tolerance, self.adjusted_price_quantum]
        if not all(math.isfinite(x) for x in values):
            raise ValueError("Configuration values must be finite")
        if self.initial_cash <= 0 or not 0 <= self.cost_bps < 10_000:
            raise ValueError("Positive initial cash and costs in [0, 10000) required")
        for name in ("holding_sessions", "max_positions", "max_entries_per_day", "max_hold_sessions"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_security_weight", "max_issuer_weight", "max_industry_weight", "max_daily_buy_weight"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.settlement not in {"historical", "T+0", "T+1", "T+2", "T+3"}:
            raise ValueError("Unknown settlement policy")
        if self.distribution_payment_delay_days < 0 or self.action_tolerance < 0 or self.adjusted_price_quantum < 0:
            raise ValueError("Action delay and tolerance must be nonnegative")
        if self.exit_mode not in {"fixed", "rebound"}:
            raise ValueError("exit_mode must be fixed or rebound")
        if not math.isfinite(self.rebound_fraction) or not 0 < self.rebound_fraction <= 1:
            raise ValueError("rebound_fraction must be in (0, 1]")
        if self.relative_stop_multiple is not None and (not math.isfinite(self.relative_stop_multiple) or self.relative_stop_multiple <= 0):
            raise ValueError("relative_stop_multiple must be positive or None")
        if self.absolute_stop_loss is not None and (not math.isfinite(self.absolute_stop_loss) or not 0 < self.absolute_stop_loss < 1):
            raise ValueError("absolute_stop_loss must be in (0, 1) or None")


def _valid(value) -> bool:
    return value is not None and pd.notna(value) and math.isfinite(float(value)) and float(value) > 0


def _day(value) -> str:
    return pd.Timestamp(value).date().isoformat()


def _flag(row: dict, key: str, default: bool = True) -> bool:
    value = row.get(key, default)
    return bool(value) if pd.notna(value) else False


def _observed_bar(row: dict | None) -> bool:
    """Reject provider interpolation/stubs; daily volume never sizes a fill."""
    return row is not None and not _flag(row, "interpolated", False) and _valid(row.get("volume"))


def _settlement_days(session: str, config: ReversalBacktestConfig) -> int:
    if config.settlement != "historical":
        return int(config.settlement[-1])
    if session < "2017-09-05":
        return 3
    return 2 if session < "2024-05-28" else 1


def _prepare_panel(panel: pd.DataFrame) -> pd.DataFrame:
    required = {"session", "security_id", "symbol", "issuer_id", "industry", "open", "close", "volume", "tr_close", "adj_open"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"Missing panel fields: {sorted(missing)}")
    p = panel.copy()
    p["session"] = p["session"].map(_day)
    for field in ("security_id", "symbol", "issuer_id", "industry"):
        if p[field].isna().any():
            raise ValueError(f"Missing identity/classification: {field}")
        p[field] = p[field].astype(str)
    if p.duplicated(["session", "security_id"]).any():
        raise ValueError("Duplicate panel session/security_id")
    p = p.sort_values(["security_id", "session"]).reset_index(drop=True)
    for field in ("open", "close", "volume", "tr_close", "adj_open", "split_close", "split_ratio", "dividend"):
        if field in p:
            p[field] = pd.to_numeric(p[field], errors="coerce")
    # Factors are used for ex-post accounting only, never for signal selection.
    p["_total_factor"] = p["tr_close"] / p["close"]
    if "split_close" in p:
        p["_split_factor"] = p["split_close"] / p["close"]
    else:
        p["_split_factor"] = np.nan
    return p


def run_backtest(
    panel: pd.DataFrame,
    candidates: pd.DataFrame,
    config: ReversalBacktestConfig | None = None,
    start: str | None = None,
    end: str | None = None,
    exit_features: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Run one continuous cash account, returning JSON-compatible records.

    ``candidates`` requires signal_session, security_id and score; every supplied
    row must belong to the prefiltered top decile. The next exchange session is
    used even when its bars are missing. ``panel.eligible`` gates new entries on
    the execution day. An invalid/missing opening price cannot execute an order.
    An optional ``tradable`` field gates both entries and exits. Daily volume is
    deliberately not a sizing/participation input: it is unknown at the open.

    ``exit_mode='rebound'`` additionally requires positive ``entry_gap`` on each
    candidate, frozen at its signal close, and ``exit_features`` keyed by session
    and security_id. Features contain close-to-close ``residual_log_return``
    (stock minus industry), ``fundamental_known`` and ``fundamental_block``.
    The residual path starts immediately after the signal close, not after the
    opening fill. Close-only triggers execute at the next session's valid open.
    A missing residual observation permanently disables relative-path triggers
    for that holding; known fundamental risk, total-return stop and maximum age
    remain active. Fundamental unknown/cooldown alone is not an exit trigger.
    The absolute stop uses raw marked holdings plus post-entry distribution
    entitlements relative to entry notional, excluding transaction costs.
    """
    cfg = config or ReversalBacktestConfig()
    p = _prepare_panel(panel)
    if p.empty:
        raise ValueError("Empty panel")
    start, end = _day(start or p["session"].min()), _day(end or p["session"].max())
    if start > end:
        raise ValueError("Start must not follow end")
    # A caller may reuse a full dataset for an earlier evaluation boundary.
    # Future bars must neither affect accounting nor fail calendar validation.
    p = p[p["session"] <= end]
    if p.empty:
        raise ValueError("No panel observations at or before the requested end")
    calendar_start = min(start, p["session"].min())
    cal = xcals.get_calendar("XNYS", start=pd.Timestamp(calendar_start) - pd.Timedelta(days=15),
                            end=pd.Timestamp(end) + pd.Timedelta(days=90))
    calendar = [_day(x) for x in cal.sessions]
    day_index = {day: i for i, day in enumerate(calendar)}
    sessions = [day for day in calendar if start <= day <= end]
    if not sessions:
        raise ValueError("No exchange sessions in requested range")
    if not p["session"].isin(calendar).all():
        raise ValueError("Panel contains non-XNYS sessions")
    c = candidates.copy()
    if not {"signal_session", "security_id", "score"} <= set(c):
        raise ValueError("Candidates require signal_session, security_id, score")
    c["signal_session"] = c["signal_session"].map(_day)
    c = c[(c["signal_session"] >= calendar[0]) & (c["signal_session"] <= end)].copy()
    c["security_id"] = c["security_id"].astype(str)
    c["score"] = pd.to_numeric(c["score"], errors="coerce")
    if cfg.exit_mode == "rebound":
        if "entry_gap" not in c:
            raise ValueError("Rebound candidates require a frozen entry_gap")
        c["entry_gap"] = pd.to_numeric(c["entry_gap"], errors="coerce")
    if c.duplicated(["signal_session", "security_id"]).any():
        raise ValueError("Duplicate candidate signal_session/security_id")
    if not c["signal_session"].isin(calendar).all():
        raise ValueError("Candidate signal date is not an exchange session")
    scheduled: dict[str, list[dict]] = {}
    for signal in c.sort_values(["signal_session", "score", "security_id"], ascending=[True, False, True]).to_dict("records"):
        idx = day_index[signal["signal_session"]]
        if idx + 1 < len(calendar):
            scheduled.setdefault(calendar[idx + 1], []).append(signal)
    by_day = {day: frame.set_index("security_id", drop=False).to_dict("index") for day, frame in p.groupby("session", sort=False)}
    exit_by_day: dict[str, dict[str, dict]] = {}
    if cfg.exit_mode == "rebound":
        feature_fields = {"session", "security_id", "residual_log_return", "fundamental_known", "fundamental_block"}
        if exit_features is None or not feature_fields <= set(exit_features):
            raise ValueError(f"Rebound exit_features require {sorted(feature_fields)}")
        features = exit_features[sorted(feature_fields)].copy()
        features["session"] = features["session"].map(_day)
        features = features[(features["session"] >= calendar[0]) & (features["session"] <= end)].copy()
        features["security_id"] = features["security_id"].astype(str)
        features["residual_log_return"] = pd.to_numeric(features["residual_log_return"], errors="coerce")
        if features.duplicated(["session", "security_id"]).any():
            raise ValueError("Duplicate exit feature session/security_id")
        if not features["session"].isin(calendar).all():
            raise ValueError("Exit feature date is not an exchange session")
        exit_by_day = {day: frame.set_index("security_id", drop=False).to_dict("index")
                       for day, frame in features.groupby("session", sort=False)}

    settled_cash = cfg.initial_cash
    unsettled: list[dict] = []
    receivables: list[dict] = []
    positions: dict[str, dict] = {}
    all_positions: list[dict] = []
    fills, rejections, actions, equity = [], [], [], []
    limitations = {
        "Daily open proxy; no 09:35 execution, quote spread/depth, queue, delay or partial-fill validation.",
        "Only settled cash can fund buys; sale proceeds settle on historical T+3/T+2/T+1 exchange-session schedules. Cash earns zero.",
        "All-inclusive per-side cost scenarios are research assumptions, not actual broker charges. Returns are before personal taxes.",
        "Terminal holdings are marked, not forcibly sold; missing quotes retain the last mark and exposure.",
        "Fixed-universe, classification, corporate-action, delisting and event-history quality must be audited separately.",
    }
    previous_nav = cfg.initial_cash
    peak_nav = cfg.initial_cash
    previous_factors: dict[str, tuple[float, float, float, float]] = {}
    # The latest observation before the run may anchor a corporate-action factor.
    before = p[p["session"] < start].groupby("security_id", sort=False).tail(1)
    for row in before.to_dict("records"):
        previous_factors[row["security_id"]] = (row["_split_factor"], row["_total_factor"], row.get("split_close"), row["tr_close"])
    closed_ids: set[str] = set()
    cost_rate = cfg.cost_bps / 10_000

    def next_session_after(day: str, intervals: int = 0) -> str:
        if day not in day_index:
            day = _day(cal.date_to_session(day, direction="next"))
        return calendar[day_index[day] + intervals]

    def add_distribution(position: dict, amount: float, session: str, kind: str, pay_session=None):
        if abs(amount) < 1e-8:
            return
        if pay_session is None or pd.isna(pay_session):
            date = _day(pd.Timestamp(session) + pd.Timedelta(days=cfg.distribution_payment_delay_days))
            pay_day = next_session_after(date)
            limitations.add(f"Distribution payment dates missing: payment is proxied at the first session on/after {cfg.distribution_payment_delay_days} calendar days after recognition.")
        else:
            pay_day = next_session_after(_day(pay_session))
            if pay_day < session:
                raise ValueError("Dividend payment date precedes entitlement date")
        position["distributions"] += amount
        receivables.append({"trade_id": position["trade_id"], "security_id": position["security_id"],
                            "amount": amount, "recognized_session": session, "payment_session": pay_day})
        actions.append({"session": session, "type": kind, "security_id": position["security_id"],
                        "trade_id": position["trade_id"], "amount": amount, "payment_session": pay_day})

    for session in sessions:
        idx = day_index[session]
        rows = by_day.get(session, {})
        due = [item for item in unsettled if item["settlement_session"] <= session]
        settled_cash += sum(item["amount"] for item in due)
        unsettled = [item for item in unsettled if item["settlement_session"] > session]
        due_dividends = [item for item in receivables if item["payment_session"] <= session]
        settled_cash += sum(item["amount"] for item in due_dividends)
        receivables = [item for item in receivables if item["payment_session"] > session]
        for item in due_dividends:
            actions.append({"session": session, "type": "distribution_payment", **item})
        # Retain references after an opening exit: yesterday's holders remain
        # entitled to the ex-date distribution recognized at today's close.
        overnight = dict(positions)
        synthetic_distributions: list[tuple[dict, float]] = []
        for security_id, row in rows.items():
            if not _observed_bar(row):
                continue
            split_factor, total_factor = row["_split_factor"], row["_total_factor"]
            split_price, total_price = row.get("split_close"), row["tr_close"]
            old_split, old_total, old_split_price, old_total_price = previous_factors.get(
                security_id, (split_factor, total_factor, split_price, total_price))
            half_quantum = cfg.adjusted_price_quantum / 2

            def rounding_bound(now, before):
                if not _valid(now) or not _valid(before):
                    return cfg.action_tolerance
                # A ratio of two cent-rounded adjusted prices can move even
                # with a constant adjustment factor. Bound both roundings.
                return cfg.action_tolerance + half_quantum / max(float(now)-half_quantum, 1e-8) + half_quantum / max(float(before)-half_quantum, 1e-8)

            split_noise = rounding_bound(split_price, old_split_price)
            total_noise = rounding_bound(total_price, old_total_price)
            explicit_split = row.get("split_ratio")
            if _valid(explicit_split):
                split_ratio = float(explicit_split)
            elif _valid(split_factor) and _valid(old_split):
                split_ratio = float(split_factor / old_split)
                if abs(split_ratio - 1) <= split_noise:
                    split_ratio = 1.0
            else:
                split_ratio = 1.0
                if security_id in overnight:
                    limitations.add("Split-adjusted prices and explicit split events are absent for at least one holding; its corporate-action accounting is incomplete.")
            if security_id in overnight:
                position = overnight[security_id]
                if abs(split_ratio - 1) > cfg.action_tolerance:
                    if not 0.001 <= split_ratio <= 1000:
                        raise ValueError("Invalid inferred split factor")
                    # Vendor factors are rounded; snap genuinely near common
                    # rational split ratios, preserving exact whole shares.
                    rationals = {a / b for a in range(1, 31) for b in range(1, 11)} | {0.01, 0.001, 100.0, 1000.0}
                    nearest = min(rationals, key=lambda value: abs(value - split_ratio))
                    if not _valid(explicit_split) and abs(nearest / split_ratio - 1) < max(0.00005, split_noise):
                        split_ratio = nearest
                    old_quantity = position["quantity"]
                    position["quantity"] *= split_ratio
                    position["last_price"] /= split_ratio
                    actions.append({"session": session, "type": "split" if _valid(explicit_split) else "inferred_split",
                                    "security_id": security_id, "trade_id": position["trade_id"],
                                    "ratio": split_ratio, "quantity_before": old_quantity, "quantity_after": position["quantity"]})
                    if abs(position["quantity"] - round(position["quantity"])) > 1e-7:
                        limitations.add("Fractional entitlements created by corporate actions are retained; actual cash-in-lieu terms are unavailable.")
                if "dividend" in row and pd.notna(row["dividend"]):
                    dividend = float(row["dividend"])
                    if dividend < 0:
                        raise ValueError("Negative explicit dividend")
                    add_distribution(position, position["quantity"] * dividend, session, "cash_dividend",
                                     row.get("dividend_pay_session"))
                elif _valid(total_factor) and _valid(old_total) and _valid(row["close"]):
                    ratio = total_factor / old_total / split_ratio
                    if abs(ratio - 1) > total_noise:
                        # Reproduce the vendor's total-return factor through a
                        # separate distribution, never through adjusted shares.
                        amount = position["quantity"] * float(row["close"]) * (ratio - 1)
                        synthetic_distributions.append((position, amount))
                        limitations.add("Synthetic distributions inferred from total-return/split adjustment factors may include dividends, spin-offs and vendor revisions; these are not verified cash entitlements.")
                        limitations.add(f"Adjustment-factor changes within the rounding-error bound of {cfg.adjusted_price_quantum:g}-dollar adjusted prices are ignored; small unsourced distributions may be missed.")
                        if amount < 0 or abs(ratio - 1) > 0.20:
                            limitations.add("Unusual/negative adjustment-factor distributions occurred; corporate-action results need manual review.")
                position["symbol"] = row["symbol"]
            previous_factors[security_id] = (split_factor if _valid(split_factor) else old_split,
                                             total_factor if _valid(total_factor) else old_total,
                                             split_price if _valid(split_price) else old_split_price,
                                             total_price if _valid(total_price) else old_total_price)

        def mark(position, field="open"):
            row = rows.get(position["security_id"], {})
            return float(row[field]) if _observed_bar(row) and _valid(row.get(field)) else position["last_price"]

        nav_open = settled_cash + sum(x["amount"] for x in unsettled) + sum(x["amount"] for x in receivables)
        nav_open += sum(pos["quantity"] * mark(pos) for pos in positions.values())
        if nav_open <= 0 or not math.isfinite(nav_open):
            raise ValueError("Nonpositive or invalid account equity")
        sold_today: set[str] = set()
        daily_buy = daily_sell = daily_cost = 0.0
        entries = 0

        def reject(signal: dict, reason: str, side="buy"):
            rejections.append({"session": session, "signal_session": signal.get("signal_session"),
                               "security_id": signal["security_id"], "symbol": signal.get("symbol"),
                               "score": signal.get("score"), "side": side, "reason": reason})

        for security_id, position in list(positions.items()):
            if idx < position["exit_index"]:
                continue
            row = rows.get(security_id)
            if not _observed_bar(row) or not _valid(row.get("open")) or not _flag(row, "tradable"):
                reject(position, "EXIT_UNAVAILABLE_RETAIN_POSITION", "sell")
                continue
            price = float(row["open"])
            notional = position["quantity"] * price
            cost = notional * cost_rate
            proceeds = notional - cost
            settlement_day = next_session_after(session, _settlement_days(session, cfg))
            if settlement_day == session:
                settled_cash += proceeds
            else:
                unsettled.append({"amount": proceeds, "settlement_session": settlement_day})
            fill = {"trade_id": position["trade_id"], "session": session, "side": "sell", "security_id": security_id,
                    "symbol": row["symbol"], "quantity": position["quantity"], "price": price, "notional": notional,
                    "cost": cost, "cash_flow": proceeds, "settlement_session": settlement_day,
                    "signal_session": position["signal_session"], "planned_exit_session": position["planned_exit_session"]}
            if cfg.exit_mode == "rebound":
                fill.update({key: position.get(key) for key in (
                    "exit_reason", "exit_signal_session", "exit_signal_phase", "entry_gap",
                    "max_exit_session", "exit_signal_relative_log_return", "exit_signal_total_return")})
                fill["holding_intervals"] = idx - position["entry_index"]
            fills.append(fill)
            position.update(exit_session=session, exit_price=price, exit_notional=notional, exit_cost=cost,
                            holding_intervals=idx-position["entry_index"])
            closed_ids.add(position["trade_id"])
            daily_sell += notional
            daily_cost += cost
            sold_today.add(security_id)
            del positions[security_id]

        for signal in scheduled.get(session, []):
            security_id = signal["security_id"]
            row = rows.get(security_id)
            reason = None
            if not _valid(signal["score"]):
                reason = "NONPOSITIVE_OR_INVALID_SCORE"
            elif cfg.exit_mode == "rebound" and not _valid(signal.get("entry_gap")):
                reason = "NONPOSITIVE_OR_INVALID_ENTRY_GAP"
            elif security_id in sold_today:
                reason = "SOLD_TODAY"
            elif security_id in positions:
                reason = "ALREADY_HELD"
            elif not _observed_bar(row) or not _valid(row.get("open")) or not _flag(row, "tradable"):
                reason = "ENTRY_UNAVAILABLE"
            elif not _flag(row, "eligible"):
                reason = "NOT_IN_EXECUTION_UNIVERSE"
            elif entries >= cfg.max_entries_per_day:
                reason = "DAILY_ENTRY_LIMIT"
            elif len(positions) >= cfg.max_positions:
                reason = "POSITION_LIMIT"
            if reason:
                reject(signal, reason)
                continue
            issuer = row["issuer_id"]
            industry = row["industry"]
            issuer_exposure = sum(pos["quantity"] * mark(pos) for pos in positions.values()
                                  if rows.get(pos["security_id"], {}).get("issuer_id", pos["issuer_id"]) == issuer)
            industry_exposure = sum(pos["quantity"] * mark(pos) for pos in positions.values()
                                    if rows.get(pos["security_id"], {}).get("industry", pos["industry"]) == industry)
            limits = {"SECURITY_BUDGET": cfg.max_security_weight * nav_open,
                      "ISSUER_BUDGET": cfg.max_issuer_weight * nav_open - issuer_exposure,
                      "INDUSTRY_BUDGET": cfg.max_industry_weight * nav_open - industry_exposure,
                      "DAILY_BUY_BUDGET": cfg.max_daily_buy_weight * nav_open - daily_buy,
                      "SETTLED_CASH": settled_cash / (1 + cost_rate)}
            limit_name = min(limits, key=limits.get)
            budget = limits[limit_name]
            price = float(row["open"])
            quantity = math.floor(max(0.0, budget) / price + 1e-10)
            if quantity < 1:
                reject(signal, f"INSUFFICIENT_{limit_name}_FOR_ONE_SHARE")
                continue
            notional = quantity * price
            cost = notional * cost_rate
            if notional + cost > settled_cash + 1e-7:
                raise AssertionError("Buy exceeded settled cash")
            settled_cash -= notional + cost
            settled_cash = max(0.0, settled_cash)
            holding_limit = cfg.holding_sessions if cfg.exit_mode == "fixed" else cfg.max_hold_sessions
            position = {"trade_id": f"R{len(all_positions)+1:07d}", "security_id": security_id, "symbol": row["symbol"],
                        "issuer_id": issuer, "industry": industry, "signal_session": signal["signal_session"],
                        "score": float(signal["score"]), "entry_session": session, "entry_index": idx,
                        "entry_price": price, "entry_quantity": quantity, "quantity": float(quantity),
                        "entry_notional": notional, "entry_cost": cost, "last_price": price, "last_mark_session": session,
                        "planned_exit_session": next_session_after(session, holding_limit),
                        "exit_index": idx + holding_limit, "distributions": 0.0}
            if cfg.exit_mode == "rebound":
                position.update(entry_gap=float(signal["entry_gap"]),
                                rebound_target_log_return=float(signal["entry_gap"]) * cfg.rebound_fraction,
                                cumulative_relative_log_return=0.0, relative_path_complete=True,
                                missing_residual_sessions=0, max_exit_session=position["planned_exit_session"],
                                exit_reason=None, exit_signal_session=None, exit_signal_phase=None)
            positions[security_id] = position
            all_positions.append(position)
            fills.append({"trade_id": position["trade_id"], "session": session, "side": "buy", "security_id": security_id,
                          "symbol": row["symbol"], "quantity": quantity, "price": price, "notional": notional,
                          "cost": cost, "cash_flow": -notional-cost, "settlement_session": session,
                          "signal_session": signal["signal_session"], "planned_exit_session": position["planned_exit_session"]})
            if cfg.exit_mode == "rebound":
                fills[-1].update(entry_gap=position["entry_gap"], signal_phase="after_close",
                                 max_exit_session=position["max_exit_session"],
                                 rebound_target_log_return=position["rebound_target_log_return"])
            entries += 1
            daily_buy += notional
            daily_cost += cost
        invested_open = sum(pos["quantity"] * mark(pos) for pos in positions.values())
        for position, amount in synthetic_distributions:
            add_distribution(position, amount, session, "synthetic_distribution")
        # Same-day known payment still only becomes spendable after today's
        # opening decisions; subsequent sessions can use it normally.
        newly_due = [item for item in receivables if item["payment_session"] <= session]
        settled_cash += sum(item["amount"] for item in newly_due)
        receivables = [item for item in receivables if item["payment_session"] > session]
        for item in newly_due:
            actions.append({"session": session, "type": "distribution_payment", **item})
        stale = overdue = 0
        for position in positions.values():
            row = rows.get(position["security_id"], {})
            if _observed_bar(row) and _valid(row.get("close")):
                position["last_price"] = float(row["close"])
                position["last_mark_session"] = session
            else:
                stale += 1
            if cfg.exit_mode == "rebound":
                feature = exit_by_day.get(session, {}).get(position["security_id"], {})
                residual = feature.get("residual_log_return")
                close_observed = _observed_bar(row) and _valid(row.get("close"))
                if close_observed and residual is not None and pd.notna(residual) and math.isfinite(float(residual)):
                    position["cumulative_relative_log_return"] += float(residual)
                else:
                    position["relative_path_complete"] = False
                    position["missing_residual_sessions"] += 1
                    limitations.add("Missing holding-day residual observations disable rebound and relative-stop triggers for that holding; partial paths are never treated as complete recovery.")
                total_return = ((position["quantity"] * float(row["close"]) + position["distributions"]) /
                                position["entry_notional"] - 1) if close_observed else None
                # Once an exit is pending, retain its original trigger and retry
                # the next valid open even if the subsequent signal disappears.
                if position["exit_reason"] is None:
                    reason = None
                    if _flag(feature, "fundamental_known", False) and _flag(feature, "fundamental_block", False):
                        reason = "FUNDAMENTAL_RISK"
                    elif cfg.absolute_stop_loss is not None and total_return is not None and total_return <= -cfg.absolute_stop_loss + 1e-12:
                        reason = "ABSOLUTE_STOP"
                    elif (position["relative_path_complete"] and cfg.relative_stop_multiple is not None and
                          position["cumulative_relative_log_return"] <= -position["entry_gap"] * cfg.relative_stop_multiple + 1e-12):
                        reason = "RELATIVE_STOP"
                    elif (position["relative_path_complete"] and
                          position["cumulative_relative_log_return"] >= position["rebound_target_log_return"] - 1e-12):
                        reason = "REBOUND_TARGET"
                    elif idx + 1 >= position["exit_index"]:
                        reason = "MAX_HOLD"
                    if reason:
                        position.update(exit_reason=reason, exit_signal_session=session, exit_signal_phase="after_close",
                                        exit_signal_relative_log_return=position["cumulative_relative_log_return"]
                                        if position["relative_path_complete"] else None,
                                        exit_signal_total_return=total_return,
                                        exit_index=idx + 1, planned_exit_session=next_session_after(session, 1))
            overdue += int(idx >= position["exit_index"])
        market_value = sum(pos["quantity"] * pos["last_price"] for pos in positions.values())
        unsettled_cash = sum(item["amount"] for item in unsettled)
        distribution_receivables = sum(item["amount"] for item in receivables)
        nav = settled_cash + unsettled_cash + distribution_receivables + market_value
        daily_return = nav / previous_nav - 1
        peak_nav = max(peak_nav, nav)
        equity.append({"session": session, "equity": nav, "nav": nav, "daily_return": daily_return,
                       "return": daily_return, "drawdown": nav / peak_nav - 1, "settled_cash": settled_cash,
                       "unsettled_cash": unsettled_cash, "cash": settled_cash + unsettled_cash,
                       "receivables": distribution_receivables, "market_value": market_value,
                       "exposure": market_value / nav, "open_exposure": invested_open / nav_open,
                       "position_count": len(positions), "stale_positions": stale, "overdue_positions": overdue,
                       "buy_notional": daily_buy, "sell_notional": daily_sell, "costs": daily_cost})
        previous_nav = nav

    trades = []
    for position in all_positions:
        if position["trade_id"] not in closed_ids:
            continue
        gross_pnl = position["exit_notional"] - position["entry_notional"] + position["distributions"]
        net_pnl = gross_pnl - position["entry_cost"] - position["exit_cost"]
        trades.append({key: value for key, value in position.items() if key not in {"last_price", "entry_index", "exit_index"}} |
                      {"gross_pnl": gross_pnl, "net_pnl": net_pnl, "net_return": net_pnl / (position["entry_notional"] + position["entry_cost"])})
    trades.sort(key=lambda trade: (trade["exit_session"], trade["trade_id"]))
    ending_positions = []
    for position in positions.values():
        value = position["quantity"] * position["last_price"]
        ending_positions.append({key: value for key, value in position.items() if key not in {"entry_index", "exit_index"}} |
                                {"market_value": value, "unrealized_pnl_before_exit_cost": value - position["entry_notional"] - position["entry_cost"] + position["distributions"],
                                 "overdue": day_index[sessions[-1]] >= position["exit_index"]})
    daily = pd.DataFrame(equity)
    returns = daily["daily_return"]
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    drawdown_duration = longest_drawdown = 0
    for drawdown in daily["drawdown"]:
        drawdown_duration = drawdown_duration + 1 if drawdown < -1e-12 else 0
        longest_drawdown = max(longest_drawdown, drawdown_duration)
    dated_returns = pd.Series(returns.to_numpy(), index=pd.to_datetime(daily["session"]))
    weekly = (1 + dated_returns).resample("W-FRI").prod() - 1
    monthly = (1 + dated_returns).resample("ME").prod() - 1
    annual = []
    year_start_nav = cfg.initial_cash
    for year, frame in daily.groupby(daily["session"].str[:4], sort=True):
        final = float(frame["equity"].iloc[-1])
        year_returns = frame["daily_return"]
        year_nav = np.concatenate(([year_start_nav], frame["equity"].to_numpy()))
        annual.append({"year": int(year), "start_equity": year_start_nav, "end_equity": final,
                       "return": final / year_start_nav - 1, "session_count": len(frame),
                       "max_drawdown": float(np.min(year_nav / np.maximum.accumulate(year_nav) - 1)),
                       "annualized_volatility": float(year_returns.std(ddof=1) * math.sqrt(252)) if len(frame) > 1 else None,
                       "costs": float(frame["costs"].sum()), "average_exposure": float(frame["exposure"].mean())})
        year_start_nav = final
    turnover_notional = float(daily["buy_notional"].sum() + daily["sell_notional"].sum())
    costs = float(daily["costs"].sum())
    final_nav = float(daily["equity"].iloc[-1])
    total_pnl = final_nav - cfg.initial_cash
    loss_run = max_loss_run = 0
    for trade in trades:
        loss_run = loss_run + 1 if trade["net_pnl"] < 0 else 0
        max_loss_run = max(max_loss_run, loss_run)
    metrics = {"initial_cash": cfg.initial_cash, "final_equity": final_nav, "total_return": final_nav / cfg.initial_cash - 1,
               "annualized_return": (final_nav / cfg.initial_cash) ** (252 / len(daily)) - 1 if final_nav > 0 else None,
               "annualized_volatility": std * math.sqrt(252),
               "sharpe": float(returns.mean() / std * math.sqrt(252)) if std > 1e-14 else None,
               "max_drawdown": float(daily["drawdown"].min()), "longest_drawdown_sessions": longest_drawdown,
               "worst_day": float(returns.min()), "worst_week": float(weekly.min()), "worst_month": float(monthly.min()),
               "daily_var_95": float(returns.quantile(0.05)),
               "daily_expected_shortfall_95": float(returns[returns <= returns.quantile(0.05)].mean()),
               "costs": costs, "fees": costs, "trade_count": len(trades), "fill_count": len(fills),
               "entry_count": len(all_positions), "win_rate": sum(trade["net_pnl"] > 0 for trade in trades) / len(trades) if trades else None,
               "max_consecutive_losing_trades": max_loss_run, "open_positions": len(ending_positions),
               "turnover_notional": turnover_notional, "two_sided_turnover_initial_capital": turnover_notional / cfg.initial_cash,
               "annualized_two_sided_turnover_average_nav": turnover_notional / float(daily["equity"].mean()) * 252 / len(daily),
               "break_even_cost_bps_fixed_path": (total_pnl + costs) / turnover_notional * 10_000 if turnover_notional else None,
               "average_exposure": float(daily["exposure"].mean()), "session_count": len(daily),
               "stale_position_days": int(daily["stale_positions"].sum()), "overdue_position_days": int(daily["overdue_positions"].sum()),
               "total_distribution_entitlements": sum(pos["distributions"] for pos in all_positions),
               "ending_distribution_receivables": sum(item["amount"] for item in receivables),
               "ending_settled_cash": settled_cash, "ending_unsettled_cash": sum(item["amount"] for item in unsettled)}
    metrics["cagr"] = metrics["annualized_return"]
    metrics["ending_equity"] = metrics["final_equity"]
    if metrics["stale_position_days"]:
        limitations.add("Stale holding marks occurred; missing recovery/delisting values can overstate equity and make results incomplete.")
    return {"status": "completed", "validation_status": "daily_price_proxy_only", "profitability_validated": False,
            "config": asdict(cfg), "start": sessions[0], "end": sessions[-1], "metrics": metrics, "equity": equity,
            "fills": fills, "trades": trades, "rejections": rejections, "corporate_actions": actions,
            "ending_positions": ending_positions, "ending_receivables": receivables, "ending_unsettled": unsettled,
            "annual": annual, "limitations": sorted(limitations)}
