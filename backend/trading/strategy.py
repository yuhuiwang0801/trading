"""Pure, reusable strategy. All bars are COMPLETED five-minute bar ends in UTC."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from .db import utc, stamp

DEFAULT_PARAMS = {
    'scan_interval_seconds': 30, 'max_position_pct': '0.10', 'max_exposure_pct': '0.60',
    'max_positions': 6, 'risk_per_trade_pct': '0.005', 'initial_stop_atr': '2',
    'trailing_stop_atr': '3', 'max_order_amount': '1000', 'max_daily_added': '3000',
    'daily_loss_pause_pct': '0.03', 'max_spread_pct': '0.005', 'quote_max_age_seconds': 90,
    'bar_max_age_seconds': 600, 'max_price_deviation_pct': '0.005',
    'limit_offset_bps': '5', 'order_ttl_seconds': 300, 'fee_bps': '1',
    'slippage_bps': '2', 'participation_rate': '0.01', 'max_consecutive_failures': 3,
    'minimum_price': '5', 'min_avg_dollar_volume': '20000000', 'min_listing_sessions': 120,
    'max_candidates': 30, 'regular_session_only': True,
}

def D(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid decimal value') from exc
    if not result.is_finite():
        raise ValueError('Decimal value must be finite')
    return result

def closed_bars(bars, now):
    completed=sorted([b for b in bars if utc(b['timestamp']) <= utc(now) and b.get('complete', True)], key=lambda b: utc(b['timestamp']))
    # Never bridge an invalid completed bar to manufacture a crossing. Retain only
    # the contiguous admitted suffix; an invalid latest bar blocks new decisions.
    invalid=[i for i,b in enumerate(completed) if b.get('quality','valid')!='valid']
    return completed[invalid[-1]+1:] if invalid else completed

def entry_signal(candidate, bars, now):
    bars = closed_bars(bars, now)
    if len(bars) < 2:
        return None
    previous, latest = bars[-2:]
    threshold = D(candidate['high20'])
    if D(previous['close']) <= threshold < D(latest['close']):
        return {'reason': '5m close crossed above prior 20-session high', 'price': D(latest['close']),
                'threshold': threshold, 'bar_time': stamp(latest['timestamp']), 'atr': D(candidate['atr14']),
                'previous_close': D(previous['close'])}
    return None

def exit_signal(position, bars, ma20, now):
    params = position.get('params', DEFAULT_PARAMS)
    atr = D(position['entry_atr'])
    initial = D(position['avg_price']) - D(params.get('initial_stop_atr', '2')) * atr
    protection = max(initial, D(position.get('protection', initial)))
    highest = D(position.get('highest_close', position['avg_price']))
    eligible = [b for b in closed_bars(bars, now) if utc(b['timestamp']) > utc(position.get('last_bar_at') or position['opened_at'])]
    if not eligible:
        return {'triggered': False, 'reason': 'No completed post-entry bar', 'price': D(position.get('last_price', position['avg_price'])), 'bar_time': None, 'highest_close': highest, 'protection': protection}
    # Process every available closed bar so a short-lived exit is not hidden by a later recovery.
    trigger = None
    for bar in eligible:
        price = D(bar['close'])
        highest = max(highest, price)
        protection = max(protection, highest - D(params.get('trailing_stop_atr', '3')) * atr)
        if trigger is None and (price < protection or price < D(ma20)):
            trigger = {'triggered': True, 'reason': 'Closed 5m bar below protection' if price < protection else 'Closed 5m bar below prior-session MA20', 'price': price, 'bar_time': stamp(bar['timestamp'])}
    return {**(trigger or {'triggered': False, 'reason': 'Monitoring local exit rules', 'price': D(eligible[-1]['close']), 'bar_time': stamp(eligible[-1]['timestamp'])}), 'highest_close': highest, 'protection': protection}


def limit_price_for(side, quote, params=None):
    """Shared whole-cent quote-based limit intent policy; stops are not fill prices."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    if side == 'BUY':
        return (D(quote['ask']) * (1 + D(p['limit_offset_bps']) / 10000)).quantize(D('.01'), rounding=ROUND_CEILING)
    if side == 'SELL':
        return (D(quote['bid']) * (1 - D(p['limit_offset_bps']) / 10000)).quantize(D('.01'), rounding=ROUND_FLOOR)
    raise ValueError('Side must be BUY or SELL')
