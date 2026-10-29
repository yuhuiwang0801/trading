"""Bridge sealed daily V2 signals to fresh executable quotes; no order writes."""
from __future__ import annotations

from decimal import Decimal
from .calendar import as_utc, iso, market_state
from .market import MarketDataError


def quote_frame(provider, packet, configuration, positions=(), intents=(), now=None):
    fixed = as_utc(now) if now is not None else None
    clock = fixed or as_utc()
    calendar = market_state(clock)
    allowed = set(configuration.get('symbols', []))
    candidates = [dict(c) for c in packet.get('candidates', []) if c.get('symbol') in allowed]
    active = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED', 'PREVIEW_BLOCKED', 'ABANDONED'}
    symbols = sorted({c['symbol'] for c in candidates} |
                     {p['symbol'] for p in positions} |
                     {i['symbol'] for i in intents if i.get('status') not in active})
    frame = {'strategy_id': 'reversal_v2', 'source': 'alpaca', 'quality': 'valid',
             'timestamp': iso(clock), 'session_date': calendar['session_date'], 'market_open': calendar['is_open'],
             'calendar': calendar, 'candidates': candidates, 'quotes': {}, 'bars': {}, 'market_filter': True,
             'reversal_packet': packet, 'signal_packet': packet,
             'metadata': {'daily_signal_feed': 'sip', 'execution_quote_feed': provider.feed},
             'reason': None}
    if not calendar['is_open'] or not symbols:
        return frame
    try:
        assets = {row['symbol']: row for row in provider.assets() if row.get('symbol')}
        raw = provider.quotes(symbols)
        clock = fixed or as_utc()
        for symbol in symbols:
            row = raw.get(symbol)
            if not row:
                continue
            try:
                bid, ask = Decimal(str(row['bp'])), Decimal(str(row['ap']))
                if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                    continue
                stamp = as_utc(row['t'])
                asset = assets.get(symbol, {})
                frame['quotes'][symbol] = {
                    'symbol': symbol, 'bid': str(bid), 'ask': str(ask),
                    # Legacy validation requires a positive `last`. Explicitly
                    # label the midpoint; it is never a fabricated last trade.
                    'last': str((bid + ask) / 2), 'last_source': 'current_quote_midpoint',
                    'timestamp': iso(stamp), 'received_at': iso(clock), 'source': 'alpaca', 'feed': provider.feed,
                    'quality': 'valid' if 0 <= (clock - stamp).total_seconds() <= 30 else 'stale',
                    'bid_size': str(row.get('bs', 0)), 'ask_size': str(row.get('as', 0)),
                    'volume': '0', 'volume_source': 'not_used_by_v2_sizing',
                    'tradable': asset.get('tradable') is True and asset.get('status') == 'active'}
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        frame['missing_quote_symbols'] = sorted(set(symbols) - set(frame['quotes']))
        # A missing candidate quote cannot suppress a different holding's exit.
        # Every instruction independently passes quote/preview/dual-source gates.
        for c in frame['candidates']:
            c['tradable'] = assets.get(c['symbol'], {}).get('tradable') is True
    except (MarketDataError, ValueError, TypeError, KeyError):
        frame['quality'], frame['reason'] = 'unavailable', 'execution_quotes_unavailable'
    completed = fixed or as_utc()
    state = market_state(completed)
    frame.update(timestamp=iso(completed), received_at=iso(completed), calendar=state, market_open=state['is_open'])
    if state['session_date'] != frame['session_date']:
        frame.update(quality='unavailable', reason='session_changed_during_quotes')
    return frame
