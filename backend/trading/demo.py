"""Deterministic synthetic fixtures. These prices are NOT observations or returns."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pandas as pd
from .calendar import calendar_for_year, previous_session, session_bounds, iso
from .market import daily_indicators

SYMBOLS = [('NVDA', 'NVIDIA', '100'), ('AAPL', 'Apple', '200'), ('MSFT', 'Microsoft', '420'),
           ('AMZN', 'Amazon', '190'), ('META', 'Meta Platforms', '560'), ('GOOGL', 'Alphabet', '170'),
           ('AVGO', 'Broadcom', '180'), ('JPM', 'JPMorgan Chase', '210')]

def candidates_for(day):
    cal = calendar_for_year(int(day[:4]))
    prev = pd.Timestamp(previous_session(day))
    sessions = cal.sessions[cal.sessions <= prev][-130:]
    candidates = []
    for rank, (symbol, name, base_text) in enumerate(SYMBOLS, 1):
        base = Decimal(base_text)
        rows = []
        for index, session in enumerate(sessions):
            close = base * (Decimal('0.65') + Decimal(index) * Decimal('0.35') / 129)
            opened = session_bounds(session.date().isoformat())[0]
            rows.append({'t': iso(opened), 'o': str(close - base*Decimal('.002')),
                         'h': str(close + base*Decimal('.01')), 'l': str(close-base*Decimal('.01')),
                         'c': str(close), 'v': '3000000'})
        metric = daily_indicators(rows, day, 'sip')
        if metric is None: raise RuntimeError('Synthetic warm-up failed')
        candidates.append({**metric, 'symbol': symbol, 'name': name, 'security_id': f'DEMO:{symbol}',
            'rank': rank, 'tradable': True, 'source': 'demo', 'feed': 'synthetic', 'volume_scope': 'synthetic',
            'reasons': ['SYNTHETIC FIXTURE', '130 complete warm-up sessions', 'close > MA20 > MA60', '20-day dollar volume > $20M']})
    return candidates

def scenario(day='2026-10-19'):
    candidates = candidates_for(day)
    start = session_bounds(day)[0] + timedelta(minutes=30)
    offsets = [0, 300, 330, 600, 900, 1200, 1230, 1500]
    # Entry confirmed at t+5m; fill only at subsequent t+5m30s quote. Peak then exit.
    multipliers = ['1.005', '1.014', '1.014', '1.045', '1.075', '1.004', '1.005', '1.005']
    frames, history = [], {item['symbol']: [] for item in candidates}
    for frame_index, seconds in enumerate(offsets):
        now = start + timedelta(seconds=seconds)
        quotes = {}
        for index, item in enumerate(candidates):
            base = Decimal(item['close'])
            active = index < 2
            price = base * Decimal(multipliers[frame_index] if active else '1.003')
            spread = base * Decimal('0.0001')
            # Two bars first; 30s matching observations do not invent extra 5m bars.
            if frame_index == 0:
                prior = now - timedelta(minutes=5)
                history[item['symbol']].append({'timestamp': iso(prior), 'close': str(base), 'volume': '50000', 'complete': True})
            if seconds % 300 == 0:
                history[item['symbol']].append({'timestamp': iso(now), 'open': str(price),
                    'high': str(price+spread), 'low': str(price-spread), 'close': str(price),
                    'volume': '50000', 'source': 'demo', 'quality': 'valid', 'granularity': '5Min',
                    'received_at': iso(now), 'adjustment': 'raw', 'complete': True})
            quotes[item['symbol']] = {'bid': str(price-spread), 'ask': str(price+spread), 'last': str(price),
                'timestamp': iso(now), 'received_at': iso(now), 'source': 'demo', 'quality': 'valid',
                'volume': '50000', 'bid_size': '1000', 'ask_size': '1000', 'tradable': True, 'granularity': 'quote'}
        frames.append({'timestamp': iso(now), 'source': 'demo', 'quality': 'valid', 'market_open': True,
            'session_date': day, 'market_filter': True, 'candidates': copy.deepcopy(candidates),
            'quotes': quotes, 'bars': copy.deepcopy(history), 'data_version': 'synthetic-v1',
            'indicators': {c['symbol']: copy.deepcopy(c) for c in candidates},
            'metadata': {'synthetic': True, 'profitability': 'NOT_VALIDATED'}})
    return frames

def dataset():
    dates = ['2026-10-19', '2026-10-20', '2026-10-21']
    return {'kind': 'synthetic', 'source': 'demo', 'version': 'synthetic-v1', 'granularity': '5Min',
            'snapshots': [frame for day in dates for frame in scenario(day)],
            'splits': {'train': [dates[0], dates[0]], 'validation': [dates[1], dates[1]], 'test': [dates[2], dates[2]]},
            'historical_universe_verified': False, 'actions_verified': False,
            'description': 'Deterministic synthetic software validation. Not investment performance.'}

def run_demo(engine, store=None):
    existing = engine.snapshot()
    def completed(state):
        return (sum(o['side']=='BUY' and o['status']=='FILLED' for o in state['orders']) >= 2
                and sum(o['side']=='SELL' and o['status']=='FILLED' for o in state['orders']) >= 2
                and not state['positions'])
    if completed(existing):
        # Stable replay identity is intentionally idempotent; no hidden account reset.
        return {'status': 'already_completed', 'message': 'Synthetic scenario already exists; inspect orders and audit. No duplicate orders were submitted.',
                'orders': len(existing['orders']), 'fills': len(existing.get('fills', []))}
    points = store.get('demo_equity', []) if store else []
    results = []
    previous_times = [r['started_at'] for r in existing.get('runs', []) if r['status']=='SUCCESS']
    watermark = max((datetime.fromisoformat(t.replace('Z','+00:00')) for t in previous_times), default=None)
    engine.recover()
    for frame in scenario():
        now = datetime.fromisoformat(frame['timestamp'].replace('Z', '+00:00'))
        if watermark and now <= watermark:
            continue
        results.append(engine.scan(frame, now=now))
        state = engine.snapshot()
        points.append({'timestamp': frame['timestamp'], 'equity': state['account']['equity'],
                       'cash': state['account']['cash'], 'benchmark': '10000'})
        if store:
            store.put('demo_equity', points)
            store.put('demo_last_frame', frame)
    if store:
        store.put('demo_equity', points)
        store.put('demo_last_frame', scenario()[-1])
    state = engine.snapshot()
    return {'status': 'completed' if completed(state) else 'incomplete',
            'message': 'Inspect risk decisions and order states; replay never resets or duplicates an existing intent.' if not completed(state) else 'Verified buy and exit fills recorded.',
            'source': 'synthetic', 'profitability': 'NOT_VALIDATED',
            'runs': len(results), 'signals': len(state['signals']), 'orders': len(state['orders']),
            'fills': len(state['fills']), 'positions': len(state['positions']), 'account': state['account'],
            'equity_curve': points}
