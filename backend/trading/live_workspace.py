"""Read-only, allowlisted view of the isolated Robinhood execution snapshot.

No paper ledger, network client or storage handle is accepted here. Broker
positions remain authoritative; local ownership only annotates their quantities.
Missing marks and profit figures stay unknown instead of becoming zero.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re


_TOKEN = re.compile(r'[A-Za-z0-9_.:\-]{1,128}\Z')
_SYMBOL = re.compile(r'[A-Z][A-Z0-9.\-]{0,14}\Z')
_CODE = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,79}\Z')
_MASK = re.compile(r'(?:[•*]{4,}|[Xx]{4,})[A-Za-z0-9]{4}\Z')
_STATUSES = frozenset({'PREPARED', 'PREVIEWED', 'SUBMITTING', 'UNKNOWN', 'AWAITING_APPROVAL',
    'SUBMITTED', 'ACCEPTED', 'CONFIRMED', 'QUEUED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'FILLED', 'CANCELLED', 'REJECTED',
    'FAILED',
    'PREVIEW_BLOCKED', 'ABANDONED', 'EXPIRED', 'CANCEL_REJECTED'})
_TERMINAL = frozenset({'FILLED', 'CANCELLED', 'REJECTED', 'FAILED', 'PREVIEW_BLOCKED', 'ABANDONED', 'EXPIRED'})
_NUMBERS = frozenset({'quantity', 'filled_quantity', 'limit_price', 'price', 'fees', 'realized_pnl',
    'reserved_cash', 'reserved_quantity', 'atr', 'ma20', 'signal_price', 'bid', 'ask', 'last',
    'spread_pct', 'timestamp_age_seconds', 'bid_at_age_seconds', 'ask_at_age_seconds',
    'bar_time_age_seconds', 'bid_feed_pct', 'ask_feed_pct', 'limit_robinhood_pct', 'signal_ask_pct',
    'signal_limit_pct', 'quote_max_age_seconds', 'bar_max_age_seconds', 'max_spread_pct',
    'max_price_deviation_pct', 'limit_notional', 'max_order_amount', 'max_daily_added'})
_TIMES = frozenset({'checked_at', 'timestamp', 'created_at', 'updated_at', 'expires_at',
    'signal_bar_time', 'bar_time', 'bid_at', 'ask_at'})
_IDS = frozenset({'id', 'ref_id', 'intent_id', 'order_id', 'broker_order_id', 'approval_id', 'fill_id'})
_CODES = frozenset({'status', 'state', 'side', 'reason', 'unsent_reason', 'quality', 'event', 'type'})
_FLAGS = frozenset({'never_submitted', 'scope_matched', 'passed', 'has_traded', 'required', 'complete', 'armed'})
_COUNTS = frozenset({'configuration_revision', 'orders_sent', 'cancel_requests', 'max_positions',
    'positions', 'orders', 'fills', 'count'})
_CONTAINERS = frozenset({'order', 'evidence', 'alpaca', 'robinhood', 'signal', 'deviations',
    'thresholds', 'quote_check', 'readiness', 'configuration'})


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _match(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _amount(value, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if len(str(value)) > 128:
        return None
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or abs(number.as_tuple().exponent) > 30
                or len(number.as_tuple().digits) > 64 or abs(number.adjusted()) > 30
                or nonnegative and number < 0):
            return None
        return format(number, 'f')
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _time(value):
    try:
        if isinstance(value, str) and len(value) <= 64:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _stamp(value):
    parsed = _time(value)
    return parsed.isoformat() if parsed else None


def _public_detail(value, depth=0):
    """Keep typed operational evidence; never propagate arbitrary payload text."""
    if depth > 6 or not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        cleaned = None
        if key == 'symbol':
            cleaned = _match(item, _SYMBOL)
        elif key in _IDS:
            cleaned = _match(item, _TOKEN)
        elif key in _NUMBERS:
            cleaned = _amount(item)
        elif key in _TIMES:
            cleaned = _stamp(item)
        elif key in _CODES:
            cleaned = _match(item, _CODE)
        elif key in _FLAGS and isinstance(item, bool):
            cleaned = item
        elif key in _COUNTS and type(item) is int and item >= 0:
            cleaned = item
        elif key in {'reasons', 'blockers', 'symbols'} and isinstance(item, list):
            pattern = _SYMBOL if key == 'symbols' else _CODE
            cleaned = [entry for entry in item if _match(entry, pattern)]
        elif key in _CONTAINERS:
            cleaned = _public_detail(item, depth + 1)
        if cleaned is not None:
            result[key] = cleaned
    return result


def _intent(row):
    result = _public_detail(row)
    result['id'] = _match(row.get('id'), _TOKEN)
    status = _match(row.get('status'), _CODE)
    result['status'] = status if status in _STATUSES else 'UNKNOWN'
    result['broker_order_id'] = _match(row.get('broker_order_id'), _TOKEN)
    result['approval_id'] = _match(row.get('approval_id'), _TOKEN)
    return result


def _positions(snapshot, known):
    if not known:
        return [], []
    broker = _rows(snapshot.get('broker_positions'))
    owned = _rows(snapshot.get('positions'))
    broker_counts = Counter(row.get('symbol') for row in broker if isinstance(row.get('symbol'), str))
    owned_counts = Counter(row.get('symbol') for row in owned if isinstance(row.get('symbol'), str))
    owned_by_symbol = {row.get('symbol'): row for row in owned if isinstance(row.get('symbol'), str)}
    result, problems, seen = [], [], set()
    for row in broker:
        symbol = _match(row.get('symbol'), _SYMBOL)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        quantity = _amount(row.get('quantity'))
        if quantity is not None and Decimal(quantity) == 0:
            continue
        system = owned_by_symbol.get(symbol, {})
        local = _amount(system.get('quantity'), nonnegative=True) if system else '0'
        mismatch = (quantity is None or local is None or Decimal(quantity) < 0
            or Decimal(local) > Decimal(quantity) or broker_counts[symbol] != 1 or owned_counts[symbol] > 1)
        if mismatch:
            management, unmanaged = 'mismatch', None
            problems.append({'symbol': symbol, 'reason': 'POSITION_QUANTITY_MISMATCH'})
        elif Decimal(local) == 0:
            management, unmanaged = 'unmanaged', quantity
        elif Decimal(local) == Decimal(quantity):
            management, unmanaged = 'managed', '0'
        else:
            management, unmanaged = 'partial', str(Decimal(quantity) - Decimal(local))
        managed = management in {'managed', 'partial'}
        result.append({'id': symbol, 'symbol': symbol, 'quantity': quantity,
            'available_quantity': _amount(row.get('available_quantity'), nonnegative=True),
            'sellable_quantity': _amount(row.get('available_quantity'), nonnegative=True),
            'average_price': _amount(row.get('average_price'), nonnegative=True),
            'avg_price': _amount(row.get('average_price'), nonnegative=True),
            'managed_quantity': local, 'unmanaged_quantity': unmanaged,
            'management': management, 'managed': managed, 'read_only': not managed, 'can_adopt': False,
            'managed_avg_price': _amount(system.get('avg_price', system.get('average_price')), nonnegative=True) if managed else None,
            'protection': _amount(system.get('protection')) if managed else None,
            'protective_line': _amount(system.get('protection')) if managed else None,
            'protection_quantity': local if managed else None,
            'ma20': _amount(system.get('ma20'), nonnegative=True) if managed else None,
            'entry_atr': _amount(system.get('entry_atr'), nonnegative=True) if managed else None,
            'highest_close': _amount(system.get('highest_close'), nonnegative=True) if managed else None,
            'opened_at': _stamp(system.get('opened_at')) if managed else None,
            'last_bar_at': _stamp(system.get('last_bar_at')) if managed else None,
            'managed_reference_price': _amount(system.get('last_price'), nonnegative=True) if managed else None,
            'mark_price': None, 'last_price': None, 'market_value': None, 'unrealized_pnl': None,
            'source': 'robinhood', 'checked_at': _stamp(snapshot.get('checked_at'))})
    for symbol, row in owned_by_symbol.items():
        local = _amount(row.get('quantity'), nonnegative=True)
        if _match(symbol, _SYMBOL) and symbol not in seen and local is not None and Decimal(local) > 0:
            problems.append({'symbol': symbol, 'reason': 'MANAGED_POSITION_NOT_IN_BROKER_SNAPSHOT'})
    return sorted(result, key=lambda row: row['symbol']), problems


def _orders(snapshot, intents):
    by_id = {row['id']: row for row in intents if row.get('id')}
    by_ref = {row[key]: row for row in intents for key in ('ref_id', 'approval_id') if row.get(key)}
    by_broker = {row['broker_order_id']: row for row in intents if row.get('broker_order_id')}
    results, linked, seen = [], set(), set()
    for row in sorted(_rows(snapshot.get('orders')), key=lambda item: _stamp(item.get('updated_at')) or '', reverse=True):
        ident = _match(row.get('id'), _TOKEN)
        if not ident or ident in seen or row.get('owner', 'system') != 'system':
            continue
        seen.add(ident)
        intent = (by_id.get(_match(row.get('intent_id'), _TOKEN)) or by_broker.get(ident)
                  or by_ref.get(_match(row.get('ref_id'), _TOKEN)) or {})
        if intent.get('id'):
            linked.add(intent['id'])
        status = _match(row.get('state', row.get('status')), _CODE)
        results.append({**_public_detail(row), 'id': ident, 'order_id': ident, 'broker_order_id': ident,
            'intent_id': intent.get('id') or _match(row.get('intent_id'), _TOKEN),
            'ref_id': _match(row.get('ref_id'), _TOKEN) or intent.get('ref_id'),
            'approval_id': intent.get('approval_id'), 'intent_status': intent.get('status'),
            'status': status if status in _STATUSES else 'UNKNOWN',
            'quantity': _amount(row.get('quantity'), nonnegative=True),
            'filled_quantity': _amount(row.get('filled_quantity'), nonnegative=True),
            'limit_price': _amount(row.get('limit_price'), nonnegative=True),
            'created_at': _stamp(row.get('created_at')) or intent.get('created_at'),
            'updated_at': _stamp(row.get('updated_at')), 'expires_at': intent.get('expires_at'),
            'reserved_cash': intent.get('reserved_cash'), 'reserved_quantity': intent.get('reserved_quantity'),
            'source_kind': 'broker_order', 'owner': 'system', 'can_cancel': False})
    for row in intents:
        if not row.get('id') or row['id'] in linked or row.get('broker_order_id') in seen:
            continue
        results.append({**row, 'intent_id': row['id'], 'order_id': row.get('broker_order_id'),
            'source_kind': 'intent', 'owner': 'system', 'can_cancel': False})
    for row in results:
        row['terminal'] = row['status'] in _TERMINAL
    return sorted(results, key=lambda row: row.get('created_at') or '', reverse=True)


def build_live_workspace(snapshot, *, authorized=True, now=None):
    """Project a service snapshot without refreshing or mutating any account."""
    snapshot = _dict(snapshot)
    checked = _time(snapshot.get('checked_at'))
    current = _time(now) if now is not None else datetime.now(timezone.utc)
    age = (current - checked).total_seconds() if current and checked else None
    raw_account = _dict(snapshot.get('account'))
    known = authorized is True and bool(raw_account) and checked is not None
    status = ('unauthorized' if authorized is not True else 'unavailable' if not known else
              'fresh' if age is not None and 0 <= age <= 60 else 'stale')
    account = {key: _amount(raw_account.get(key)) if known else None for key in (
        'total_value', 'cash', 'buying_power', 'unleveraged_buying_power',
        'execution_buying_power', 'local_reserved_cash')}
    account.update(equity=account['total_value'], reserved_cash=account['local_reserved_cash'],
        masked_number=_match(raw_account.get('masked_number'), _MASK) if known else None,
        currency='USD' if known and raw_account.get('currency') == 'USD' else None,
        checked_at=checked.isoformat() if checked else None, age_seconds=age, status=status,
        unrealized_pnl=None, realized_pnl=None, exposure=None, exposure_pct=None, market_value=None)
    intents = [_intent(row) for row in _rows(snapshot.get('intents'))]
    intents = [row for row in intents if row.get('id')]
    # One local instruction per stable ID; retain its most recent snapshot.
    intents = sorted({row['id']: row for row in reversed(intents)}.values(),
                     key=lambda row: row.get('created_at') or '', reverse=True)
    positions, problems = _positions(snapshot, known)
    orders = _orders(snapshot, intents)
    fills, seen_fills = [], set()
    for row in _rows(snapshot.get('fills')):
        ident = _match(row.get('id'), _TOKEN)
        if not ident or ident in seen_fills:
            continue
        seen_fills.add(ident)
        fills.append({**_public_detail(row), 'id': ident, 'status': 'FILLED', 'owner': 'system'})
    audit = []
    for row in _rows(snapshot.get('audit')):
        sequence = row.get('sequence')
        event = _match(row.get('event'), _CODE)
        if type(sequence) is not int or sequence < 0 or not event:
            continue
        detail = _public_detail(row.get('detail'))
        audit.append({'id': f'live-audit:{sequence}', 'sequence': sequence, 'event': event,
            'timestamp': _stamp(row.get('timestamp')), 'detail': detail,
            'symbol': detail.get('symbol') or _dict(detail.get('order')).get('symbol'),
            'category': 'system', 'level': 'warning' if any(word in event for word in ('BLOCK', 'UNKNOWN', 'FAIL')) else 'info'})
    scan = _public_detail(snapshot.get('last_scan'))
    runs = [{**scan, 'source': 'robinhood', 'completed_at': scan.get('checked_at')}] if scan else []
    return {'source': 'robinhood', 'ledger': 'isolated_live', 'read_only': True,
        'account_status': status, 'checked_at': account['checked_at'], 'account': account,
        'positions': positions, 'position_reconciliation': problems,
        'orders': orders, 'intents': intents, 'fills': fills, 'audit': audit, 'runs': runs,
        'signals': [], 'equity_curve': [], 'scope': 'broker_account_and_system_execution',
        'order_scope': 'system_only', 'fill_scope': 'system_only', 'external_positions_adoptable': False}
