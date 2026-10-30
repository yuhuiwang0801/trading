"""Pure intraday quote context; no credentials, network calls or order writes.

Daily references are immutable. The intraday peer set never shrinks to whichever
quotes happen to arrive. Corporate-action coverage is conservative and delayed;
an empty response is not a guarantee that every action has been announced.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from .calendar import NEW_YORK, as_utc, iso, market_state, previous_session


ACTION_LIMITATION = 'Provider corporate-action data may be delayed or incomplete; no entitlement normalization is inferred.'
REFERENCE_VERSION = 1
MAX_AGE_SECONDS = 30
MAX_QUOTE_SKEW_SECONDS = 10
MAX_SPREAD = Decimal('0.005')


def _positive(value):
    if isinstance(value, bool):
        raise ValueError('Boolean price')
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Invalid price')
    return result


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid session date')
    return value


def managed_positions(positions):
    """Broker mirrors lack ownership and must never acquire strategy context."""
    result = {}
    for position in positions:
        try:
            if position.get('strategy_id') == 'reversal_v2' and _positive(position.get('quantity')):
                symbol = position['symbol']
                if isinstance(symbol, str) and symbol:
                    result[symbol] = position
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    return result


def peer_quote_symbols(packet, positions):
    reference = packet.get('intraday_reference', {})
    master = packet.get('security_master', {})
    result = set()
    for symbol in managed_positions(positions):
        row = reference.get(symbol, {}) if isinstance(reference, dict) else {}
        peers = row.get('peer_symbols', []) if isinstance(row, dict) else []
        if isinstance(peers, list):
            result.update(peer for peer in peers if isinstance(peer, str) and peer in master and peer != symbol)
    return result


def action_query_start(positions, session):
    days = []
    for position in managed_positions(positions).values():
        try:
            entry = _day(position.get('entry_session'))
            if entry <= session:
                days.append(entry)
        except (ValueError, TypeError):
            continue
    return min(days, default=session)


def action_query_window(positions, session):
    """Broaden process-date lookup around the relevant ex/effective-date range.

Alpaca sorts this endpoint by process_date, which may differ from ex_date.
The one-year margins also include known future payment/processing records;
event relevance is still filtered by the actual ex/effective date below.
This bounded lookup and the provider's delays do not prove complete coverage.
    """
    first = date.fromisoformat(action_query_start(positions, session))
    last = date.fromisoformat(session)
    return {'start': (first - timedelta(days=365)).isoformat(),
            'end': (last + timedelta(days=365)).isoformat(), 'date_basis': 'provider_process_date',
            'coverage_guaranteed': False}


def _quote(row, symbol, now, session):
    if not isinstance(row, dict) or row.get('symbol') != symbol or row.get('source') != 'alpaca':
        raise ValueError('missing_or_invalid_quote')
    if row.get('quality') != 'valid' or row.get('tradable') is not True:
        raise ValueError('unusable_quote')
    bid, ask = _positive(row.get('bid')), _positive(row.get('ask'))
    if ask < bid or (ask - bid) / ((ask + bid) / 2) > MAX_SPREAD:
        raise ValueError('invalid_or_wide_quote')
    raw_time = row.get('timestamp')
    parsed = datetime.fromisoformat(raw_time.replace('Z', '+00:00')) if isinstance(raw_time, str) else raw_time
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('invalid_quote_time')
    timestamp = as_utc(parsed)
    if (not 0 <= (now - timestamp).total_seconds() <= MAX_AGE_SECONDS
            or timestamp.astimezone(NEW_YORK).date().isoformat() != session):
        raise ValueError('stale_or_future_quote')
    return bid, ask, timestamp


def _action_symbols(event):
    # Only provider identity fields carry security attribution. Descriptions,
    # event IDs and dates must not make an unrelated action match a ticker.
    fields = {'symbol', 'source_symbol', 'target_symbol', 'old_symbol', 'new_symbol',
              'initiating_symbol', 'acquiring_symbol', 'acquired_symbol'}
    result = set()
    for field in fields:
        value = event.get(field)
        if value is None or value == '':
            continue
        if not isinstance(value, str) or not value.strip():
            return None
        result.add(value)
    if event.get('symbols') is not None:
        values = event['symbols']
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        result.update(values)
    return result or None


def _action_affects(actions, symbol, start, end):
    for event in actions:
        if not isinstance(event, dict):
            return True
        symbols = _action_symbols(event)
        if not symbols:
            # Incomplete early records without attributable symbols may affect
            # any requested holding; never infer that they are unrelated.
            return True
        if symbol not in symbols:
            continue
        # A symbol-matched event without a verifiable date is not safe to ignore.
        try:
            effective = _day(event.get('ex_date') or event.get('effective_date'))
        except (ValueError, TypeError):
            return True
        if start <= effective <= end:
            return True
    return False


def build_context(packet, positions, quotes, now, *, corporate_actions=None, corporate_actions_checked=False,
                  action_window=None):
    """Build independent own-price and relative-return readiness per holding.

``price_basis_valid`` allows an absolute price stop/trail without peer data.
``relative_eligible`` additionally requires every frozen peer and a reference
from the immediately preceding session. Both require a successful action read;
any relevant action is blocked rather than assigned a guessed adjustment.
The caller must still verify Robinhood before persisting a trigger/high-water
mark or submitting an order. This context alone is not broker confirmation.
    """
    clock = as_utc(now)
    market = market_state(clock)
    session = market['session_date']
    references = packet.get('intraday_reference', {})
    master = packet.get('security_master', {})
    actions_ok = corporate_actions_checked is True and isinstance(corporate_actions, list)
    output = {}
    for symbol, position in managed_positions(positions).items():
        context = {'status': 'blocked', 'reason': None, 'session': session,
                   'reference_session': None, 'as_of': iso(clock), 'quote_max_age_seconds': MAX_AGE_SECONDS,
                   'max_quote_skew_seconds': MAX_QUOTE_SKEW_SECONDS,
                   'price_basis_valid': False, 'relative_eligible': False,
                   'corporate_actions_checked': actions_ok, 'corporate_action_limitations': ACTION_LIMITATION,
                   'corporate_action_query': dict(action_window or {}), 'oldest_quote_at': None,
                   'stock_previous_close': None, 'peer_return': None, 'peer_count': 0, 'peer_symbols': []}
        output[symbol] = context
        try:
            if not market['is_open']:
                raise ValueError('outside_regular_session')
            entry = _day(position.get('entry_session'))
            if entry > session:
                raise ValueError('invalid_entry_session')
            bid, ask, stock_time = _quote(quotes.get(symbol), symbol, clock, session)
            context.update(stock_bid=str(bid), stock_ask=str(ask), stock_quote_at=iso(stock_time))
            if not actions_ok:
                raise ValueError('corporate_actions_unavailable')
            if _action_affects(corporate_actions, symbol, entry, session):
                raise ValueError('holding_corporate_action_requires_normalization')
            context['price_basis_valid'] = True

            ref = references.get(symbol) if isinstance(references, dict) else None
            if not isinstance(ref, dict) or packet.get('intraday_reference_version') != REFERENCE_VERSION:
                raise ValueError('intraday_reference_unavailable')
            context['reference_session'] = ref.get('session')
            prior = previous_session(session)
            if (ref.get('session') != prior or packet.get('signal_session') != prior
                    or packet.get('strategy_id') != 'reversal_v2'):
                raise ValueError('intraday_reference_stale')
            own = master.get(symbol, {})
            if (not own.get('security_id') or ref.get('security_id') != own['security_id']
                    or not own.get('industry') or ref.get('industry') != own['industry']
                    or position.get('industry') != own['industry']
                    or position.get('issuer_id') != own.get('issuer_id')):
                raise ValueError('holding_reference_identity_mismatch')
            baseline = _positive(ref.get('raw_close'))
            context['stock_previous_close'] = str(baseline)
            peers = ref.get('peer_symbols')
            if (not isinstance(peers, list) or any(not isinstance(peer, str) for peer in peers)
                    or len(peers) < 5 or len(set(peers)) != len(peers) or symbol in peers):
                raise ValueError('invalid_frozen_peer_set')
            context['peer_symbols'] = list(peers)
            ids, returns, timestamps = {own['security_id']}, [], [stock_time]
            peer_prices = {}
            for peer in peers:
                pref, identity = references.get(peer, {}), master.get(peer, {})
                if (not identity.get('security_id') or identity['security_id'] in ids
                        or identity.get('industry') != own['industry']
                        or pref.get('security_id') != identity['security_id']
                        or pref.get('industry') != own['industry'] or pref.get('eligible') is not True
                        or pref.get('session') != prior):
                    raise ValueError('peer_reference_identity_mismatch')
                ids.add(identity['security_id'])
                if _action_affects(corporate_actions, peer, session, session):
                    raise ValueError('peer_corporate_action_requires_normalization')
                pbid, pask, timestamp = _quote(quotes.get(peer), peer, clock, session)
                close = _positive(pref.get('raw_close'))
                midpoint = (pbid + pask) / 2
                returns.append(midpoint / close - 1)
                timestamps.append(timestamp)
                peer_prices[peer] = {'previous_close': str(close), 'midpoint': str(midpoint), 'quoted_at': iso(timestamp)}
            if (max(timestamps) - min(timestamps)).total_seconds() > MAX_QUOTE_SKEW_SECONDS:
                raise ValueError('intraday_quote_times_not_aligned')
            context.update(status='ready', reason=None, relative_eligible=True,
                           oldest_quote_at=iso(min(timestamps)),
                           peer_return=str(sum(returns, Decimal(0)) / len(returns)), peer_count=len(peers),
                           peer_prices=peer_prices, price_source='stock_bid_and_peer_quote_midpoints')
        except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError) as exc:
            known = str(exc)
            # Exceptions from malformed numeric/date values may carry raw data.
            allowed = {'outside_regular_session', 'invalid_entry_session', 'missing_or_invalid_quote',
                       'unusable_quote', 'invalid_or_wide_quote', 'invalid_quote_time', 'stale_or_future_quote',
                       'corporate_actions_unavailable', 'holding_corporate_action_requires_normalization',
                       'intraday_reference_unavailable', 'intraday_reference_stale', 'holding_reference_identity_mismatch',
                       'invalid_frozen_peer_set', 'peer_reference_identity_mismatch',
                       'peer_corporate_action_requires_normalization', 'intraday_quote_times_not_aligned'}
            context['reason'] = known if known in allowed else 'invalid_intraday_input'
    return output
