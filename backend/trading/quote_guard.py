"""Pure, fail-closed comparison of an Alpaca signal and a Robinhood book.

This check never reprices an order.  A passed limit order may rest unfilled.
``signal.bar_time`` is the completed bar's *end*, as in ``entry_signal``.
All evidence is allowlisted and JSON-safe; broker error text is never returned.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re

from .risk import validate_quote
from .strategy import D, DEFAULT_PARAMS


_SYMBOL = re.compile(r'[A-Z][A-Z0-9.\-]{0,9}\Z')
_QUOTE_ERRORS = {
    'MISSING_QUOTE', 'INVALID_QUOTE_QUALITY', 'STALE_QUOTE',
    'INVALID_QUOTE', 'SPREAD_TOO_WIDE', 'SECURITY_NOT_TRADABLE',
}


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An aware time is required')
    return value.astimezone(timezone.utc)


def _number(value, *, positive=True):
    if isinstance(value, bool):
        raise ValueError('A number is required')
    number = D(value)
    if number < 0 or (positive and number == 0):
        raise ValueError('A positive number is required')
    return number


def _stamp(value):
    return value.isoformat(timespec='microseconds')


def cross_quote_check(order, alpaca_quote, robinhood_quote, now, params, *, signal=None):
    """Return ``PASSED`` or ``BLOCKED`` and redacted, reproducible evidence.

    Price deviations use the Alpaca side as denominator; the order limit uses
    the Robinhood executable side (ask for BUY, bid for SELL).  BUY signal
    deviations use the completed bar close.  Bounds are inclusive.  Each
    Robinhood side independently observes the stricter of 30 seconds and the
    configured quote age.  SELL deliberately ignores old entry/trigger prices.
    """
    result = {
        'status': 'BLOCKED', 'reasons': [], 'checked_at': None,
        'order': {}, 'alpaca': {}, 'robinhood': {}, 'signal': {},
        'deviations': {}, 'thresholds': {},
    }
    reasons = result['reasons']

    def block(code):
        if code not in reasons:
            reasons.append(code)

    def number(value, code, evidence, field):
        try:
            parsed = _number(value)
            evidence[field] = str(parsed)
            return parsed
        except (ValueError, TypeError, ArithmeticError):
            block(code)
            return None

    def timed(value, prefix, evidence, field, max_age):
        try:
            parsed = _time(value)
            age = D((checked - parsed).total_seconds())
            evidence[field] = _stamp(parsed)
            evidence[field + '_age_seconds'] = str(age)
            if age < 0:
                block(prefix + '_FUTURE')
            elif age > max_age:
                block(prefix + '_STALE')
            return parsed
        except (ValueError, TypeError, ArithmeticError, OverflowError):
            block(prefix + '_INVALID')
            return None

    def spread(bid, ask, prefix, evidence):
        if bid is None or ask is None:
            return
        if ask < bid:
            block(prefix + '_CROSSED_BOOK')
            return
        ratio = (ask - bid) / ((ask + bid) / 2)
        evidence['spread_pct'] = str(ratio)
        if ratio > max_spread:
            block(prefix + '_SPREAD_TOO_WIDE')

    def deviation(value, reference, key, code):
        if value is None or reference is None:
            return
        ratio = abs(value - reference) / reference
        result['deviations'][key] = str(ratio)
        if ratio > max_deviation:
            block(code)

    try:
        try:
            checked = _time(now)
            result['checked_at'] = _stamp(checked)
        except (ValueError, TypeError, OverflowError):
            block('CHECK_TIME_INVALID')
            return result
        try:
            p = {**DEFAULT_PARAMS, **(params or {})}
            max_age = min(Decimal(30), _number(p['quote_max_age_seconds']))
            bar_age = _number(p['bar_max_age_seconds'])
            max_spread = _number(p['max_spread_pct'], positive=False)
            max_deviation = _number(p['max_price_deviation_pct'], positive=False)
            if (D(p['quote_max_age_seconds']) != D(p['quote_max_age_seconds']).to_integral_value()
                    or bar_age != bar_age.to_integral_value()
                    or max_spread > 1 or max_deviation > 1):
                raise ValueError('Invalid strategy thresholds')
            result['thresholds'] = {
                'quote_max_age_seconds': str(max_age), 'bar_max_age_seconds': str(bar_age),
                'max_spread_pct': str(max_spread), 'max_price_deviation_pct': str(max_deviation),
            }
        except (ValueError, TypeError, ArithmeticError, KeyError):
            block('PARAMETERS_INVALID')
            return result

        order = order if isinstance(order, dict) else {}
        safe_order = result['order']
        symbol = order.get('symbol')
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            block('ORDER_SYMBOL_INVALID')
            symbol = None
        else:
            safe_order['symbol'] = symbol
        side = order.get('side')
        if side not in ('BUY', 'SELL'):
            block('ORDER_SIDE_INVALID')
            side = None
        else:
            safe_order['side'] = side
        limit = number(order.get('limit_price'), 'ORDER_LIMIT_INVALID', safe_order, 'limit_price')
        quantity = number(order.get('quantity'), 'ORDER_QUANTITY_INVALID', safe_order, 'quantity')
        if limit is not None and quantity is not None:
            safe_order['limit_notional'] = str(limit * quantity)

        aq = alpaca_quote if isinstance(alpaca_quote, dict) else {}
        ae = result['alpaca']
        if not aq:
            block('ALPACA_MISSING_QUOTE')
        if 'symbol' in aq and (symbol is None or aq['symbol'] != symbol):
            block('ALPACA_SYMBOL_MISMATCH')
        if aq.get('quality') != 'valid':
            block('ALPACA_INVALID_QUOTE_QUALITY')
        else:
            ae['quality'] = 'valid'
        abid = number(aq.get('bid'), 'ALPACA_BID_INVALID', ae, 'bid')
        aask = number(aq.get('ask'), 'ALPACA_ASK_INVALID', ae, 'ask')
        last = number(aq.get('last'), 'ALPACA_LAST_INVALID', ae, 'last')
        atime = timed(aq.get('timestamp'), 'ALPACA_TIMESTAMP', ae, 'timestamp', max_age)
        spread(abid, aask, 'ALPACA', ae)
        if atime is not None and all(v is not None for v in (abid, aask, last)):
            # Retain shared quote checks, including explicit trading/halt flags.
            error = validate_quote(aq, checked, {**p, 'quote_max_age_seconds': max_age})
            if error:
                block('ALPACA_' + (error if error in _QUOTE_ERRORS else 'INVALID_QUOTE'))

        rq = robinhood_quote if isinstance(robinhood_quote, dict) else {}
        revidence = result['robinhood']
        if not rq:
            block('ROBINHOOD_MISSING_QUOTE')
        if symbol is None or rq.get('symbol') != symbol:
            block('ROBINHOOD_SYMBOL_MISMATCH')
        if rq.get('state') != 'active':
            block('ROBINHOOD_NOT_ACTIVE')
        else:
            revidence['state'] = 'active'
        if rq.get('has_traded') is not True:
            block('ROBINHOOD_NOT_TRADED')
        else:
            revidence['has_traded'] = True
        rbid = number(rq.get('bid_price'), 'ROBINHOOD_BID_INVALID', revidence, 'bid')
        rask = number(rq.get('ask_price'), 'ROBINHOOD_ASK_INVALID', revidence, 'ask')
        timed(rq.get('venue_bid_time'), 'ROBINHOOD_BID_TIMESTAMP', revidence, 'bid_at', max_age)
        timed(rq.get('venue_ask_time'), 'ROBINHOOD_ASK_TIMESTAMP', revidence, 'ask_at', max_age)
        spread(rbid, rask, 'ROBINHOOD', revidence)
        deviation(rbid, abid, 'bid_feed_pct', 'BID_FEED_DEVIATION')
        deviation(rask, aask, 'ask_feed_pct', 'ASK_FEED_DEVIATION')
        if side is not None:
            reference = rask if side == 'BUY' else rbid
            deviation(limit, reference, 'limit_robinhood_pct', 'LIMIT_PRICE_DEVIATION')

        if side == 'BUY':
            se = result['signal']
            signal = signal if isinstance(signal, dict) else {}
            if not signal:
                block('SIGNAL_MISSING')
            if signal.get('complete', True) is not True:
                block('SIGNAL_INCOMPLETE')
            if signal.get('quality', 'valid') != 'valid':
                block('SIGNAL_INVALID_QUALITY')
            if 'symbol' in signal and signal['symbol'] != symbol:
                block('SIGNAL_SYMBOL_MISMATCH')
            sprice = number(signal.get('price'), 'SIGNAL_PRICE_INVALID', se, 'price')
            timed(signal.get('bar_time'), 'SIGNAL_TIMESTAMP', se, 'bar_time', bar_age)
            deviation(rask, sprice, 'signal_ask_pct', 'SIGNAL_ASK_DEVIATION')
            deviation(limit, sprice, 'signal_limit_pct', 'SIGNAL_LIMIT_DEVIATION')
        else:
            result['signal']['required'] = False
        result['status'] = 'BLOCKED' if reasons else 'PASSED'
    except Exception:
        # The guard is a veto boundary.  Malformed upstream objects and decimal
        # arithmetic failures must never let a caller proceed or echo raw text.
        block('QUOTE_CHECK_FAILED')
        result['status'] = 'BLOCKED'
    return result
