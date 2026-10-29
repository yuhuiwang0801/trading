"""Deterministic, sequential 5-minute research replay with conservative fills.

Synthetic results validate software only. Historical efficacy needs documented
point-in-time universe, corporate actions and benchmark coverage. Final test
parameter hashes can be sealed on disk; this is experiment discipline, not a
claim that externally relabeled datasets cannot be reused.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from statistics import mean, stdev

from .calendar import NEW_YORK, as_utc, iso, market_state, previous_session, session_bounds
from .risk import size_entry, validate_quote
from .strategy import DEFAULT_PARAMS, D, closed_bars, entry_signal, exit_signal, limit_price_for


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def _serial(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serial(item) for item in value]
    return value


class ReplayBacktest:
    def __init__(self, *, lock_path: str | None = None):
        self.lock_path = lock_path
        self._test_seals = {}

    def __call__(self, dataset: dict, params: dict | None = None, split: str | None = None) -> dict:
        return self.run(dataset, params, split)

    def _seal_test(self, key: str, parameter_hash: str) -> bool:
        if not self.lock_path:
            existing = self._test_seals.setdefault(key, parameter_hash)
            return existing == parameter_hash
        # POSIX app target: lock an independent file so atomic replace is safe.
        import fcntl
        path = Path(self.lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            seals = json.loads(path.read_text()) if path.exists() else {}
            existing = seals.setdefault(key, parameter_hash)
            if existing != parameter_hash:
                return False
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_text(json.dumps(seals, sort_keys=True))
            os.replace(temporary, path)
            return True

    def run(self, dataset: dict, params: dict | None = None, split: str | None = None) -> dict:
        p = {**DEFAULT_PARAMS, **(params or {})}
        synthetic = dataset.get('kind') == 'synthetic' or dataset.get('source') == 'demo'
        result = {'status': 'blocked', 'validation_status': '待验证',
                  'software_validation': 'not_run', 'profitability_validated': False,
                  'source': dataset.get('source', 'unspecified'), 'kind': dataset.get('kind', 'unspecified'),
                  'data_version': dataset.get('version', _hash(dataset)[:20]),
                  'data_hash': _hash(dataset), 'parameters': p, 'parameter_hash': _hash(p),
                  'strategy_version': 'trend-breakout-v1', 'split': split,
                  'created_at': iso(as_utc()), 'metrics': None, 'equity': [], 'drawdown': [],
                  'trades': [], 'fills': [], 'intents': [], 'decisions': [], 'corporate_action_events': [], 'limitations': []}
        if dataset.get('granularity') not in {'5Min', '5m'}:
            result['reason'] = 'INTRADAY_DATA_REQUIRED: daily bars cannot precisely validate this 5-minute strategy'
            return result
        snapshots = copy.deepcopy(dataset.get('snapshots') or [])
        if len(snapshots) < 2:
            result['reason'] = 'INSUFFICIENT_REPLAY_SNAPSHOTS'
            return result
        try:
            initial_cash = D(p.get('initial_cash', 10000))
            if not initial_cash.is_finite():
                raise ValueError('INITIAL_CASH_MUST_BE_FINITE')
            for field in ('participation_rate', 'fee_bps', 'slippage_bps', 'limit_offset_bps', 'max_price_deviation_pct'):
                if not D(p[field]).is_finite() or D(p[field]) < 0:
                    raise ValueError('INVALID_NONNEGATIVE_PARAMETER')
            if D(p['participation_rate']) > 1 or int(p['order_ttl_seconds']) <= 0:
                raise ValueError('INVALID_LIQUIDITY_OR_TTL_PARAMETER')
            times = [as_utc(frame['timestamp']) for frame in snapshots]
            if any(left >= right for left, right in zip(times, times[1:])):
                raise ValueError('SNAPSHOTS_MUST_BE_STRICTLY_CHRONOLOGICAL')
            intervals = dataset.get('splits') or {}
            if intervals:
                boundaries = []
                for name in ('train', 'validation', 'test'):
                    if name not in intervals or len(intervals[name]) != 2:
                        raise ValueError('TRAIN_VALIDATION_TEST_INTERVALS_REQUIRED')
                    start, end = intervals[name]
                    if start > end:
                        raise ValueError('INVALID_SPLIT_INTERVAL')
                    boundaries.append((start, end))
                if boundaries[0][1] >= boundaries[1][0] or boundaries[1][1] >= boundaries[2][0]:
                    raise ValueError('SPLITS_MUST_BE_CHRONOLOGICAL_AND_NONOVERLAPPING')
                result['split_manifest'] = copy.deepcopy(intervals)
                result['split_manifest_hash'] = _hash(intervals)
            if split:
                if split not in intervals:
                    raise ValueError('REQUESTED_SPLIT_NOT_DEFINED')
                start, end = intervals[split]
                snapshots = [frame for frame in snapshots if start <= frame['session_date'] <= end]
                if len(snapshots) < 2:
                    raise ValueError('INSUFFICIENT_SNAPSHOTS_IN_SPLIT')
            for frame in snapshots:
                timestamp = as_utc(frame['timestamp'])
                session = frame['session_date']
                if not synthetic and 'quality' not in frame:
                    raise ValueError('HISTORICAL_QUALITY_STATUS_REQUIRED')
                if market_state(timestamp)['session_date'] != session:
                    raise ValueError('SESSION_DATE_DOES_NOT_MATCH_TIMESTAMP')
                for candidate in frame.get('candidates', []):
                    for field in ('high20', 'atr14', 'ma20'):
                        if not D(candidate[field]).is_finite() or D(candidate[field]) <= 0:
                            raise ValueError('INVALID_CANDIDATE_VALUE')
                    known = candidate.get('known_at')
                    bounds = session_bounds(session)
                    cutoff = min(timestamp, bounds[0]) if bounds else timestamp
                    if known and as_utc(known) > cutoff:
                        raise ValueError('LOOKAHEAD_DAILY_INDICATORS')
                    if not known and not synthetic:
                        raise ValueError('HISTORICAL_CANDIDATE_KNOWN_AT_REQUIRED')
                    if candidate.get('asof_session', previous_session(session)) >= session:
                        raise ValueError('CURRENT_DAY_IN_DAILY_THRESHOLD')
                    if candidate.get('asof_session', previous_session(session)) != previous_session(session):
                        raise ValueError('STALE_DAILY_INDICATORS')
                for quote in frame.get('quotes', {}).values():
                    if as_utc(quote['timestamp']) > timestamp:
                        raise ValueError('FUTURE_QUOTE_IN_SNAPSHOT')
                    for field in ('bid', 'ask', 'last', 'volume'):
                        if not D(quote.get(field, 0)).is_finite():
                            raise ValueError('NONFINITE_QUOTE')
                    if quote.get('volume_timestamp') and as_utc(quote['volume_timestamp']) > timestamp:
                        raise ValueError('FUTURE_LIQUIDITY_OBSERVATION')
                for symbol_bars in frame.get('bars', {}).values():
                    for bar in symbol_bars:
                        bar_end = as_utc(bar['timestamp'])
                        # Future bars are ignored by the strategy. Observable bars
                        # must be complete regular-session five-minute intervals.
                        if bar_end <= timestamp:
                            bar_day = bar_end.astimezone(NEW_YORK).date().isoformat()
                            regular = session_bounds(bar_day)
                            if not regular or not regular[0] + timedelta(minutes=5) <= bar_end <= regular[1]:
                                raise ValueError('EXTENDED_SESSION_BAR_NOT_ALLOWED')
                            if (bar_end-regular[0]).total_seconds() % 300:
                                raise ValueError('MISALIGNED_FIVE_MINUTE_BAR')
                        if not D(bar['close']).is_finite() or D(bar['close']) <= 0:
                            raise ValueError('INVALID_BAR_VALUE')
            actions = copy.deepcopy(dataset.get('corporate_actions', []))
            action_schedule = []
            if actions and dataset.get('corporate_action_price_basis') != 'as_of_snapshot':
                raise ValueError('CORPORATE_ACTION_PRICE_BASIS_REQUIRED: indicators and prices must use each snapshot basis')
            action_ids = set()
            for action in actions:
                if not isinstance(action.get('symbol'), str) or not action['symbol']:
                    raise ValueError('CORPORATE_ACTION_SYMBOL_REQUIRED')
                identifier = action['id']
                if identifier in action_ids:
                    raise ValueError('DUPLICATE_CORPORATE_ACTION_ID')
                action_ids.add(identifier)
                kind = action['type']
                if kind == 'cash_dividend':
                    ex_at, pay_at = as_utc(action['ex_at']), as_utc(action['pay_at'])
                    if pay_at < ex_at or not D(action['cash_per_share']).is_finite() or D(action['cash_per_share']) < 0:
                        raise ValueError('INVALID_DIVIDEND_TERMS')
                    action_schedule.extend([(ex_at, 'dividend_entitlement', action), (pay_at, 'dividend_payment', action)])
                elif kind in {'split', 'symbol_change', 'cash_delisting'}:
                    if kind == 'split' and (not D(action['ratio']).is_finite() or D(action['ratio']) <= 0):
                        raise ValueError('INVALID_SPLIT_RATIO')
                    if kind == 'cash_delisting' and (not D(action['cash_per_share']).is_finite() or D(action['cash_per_share']) < 0):
                        raise ValueError('DELISTING_CASH_TERMS_REQUIRED')
                    if kind == 'symbol_change' and not action.get('new_symbol'):
                        raise ValueError('NEW_SYMBOL_REQUIRED')
                    action_schedule.append((as_utc(action['effective_at']), kind, action))
                else:
                    raise ValueError('UNSUPPORTED_CORPORATE_ACTION: explicit normalization is required')
            action_schedule.sort(key=lambda item: (item[0], 0 if item[1] == 'dividend_entitlement' else 1, item[2]['id']))
            if not synthetic and not intervals:
                result['limitations'].append('No immutable train/validation/final-test split supplied.')
            if split == 'test' and not synthetic:
                key = _hash([dataset.get('research_id', 'default'), result['data_version'], intervals['test']])
                if not self._seal_test(key, result['parameter_hash']):
                    raise ValueError('FINAL_TEST_SEALED: changed parameters cannot reuse this final test')
                result['final_test_sealed'] = True
            if initial_cash <= 0:
                raise ValueError('INITIAL_CASH_MUST_BE_POSITIVE')
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            result['reason'] = str(exc)
            return result

        cash, positions, pending, seen_entries, last_prices = initial_cash, {}, [], set(), {}
        fees = D(0)
        turnover = D(0)
        day_added = D(0)
        day_start_equity = initial_cash
        last_day = None
        peak = initial_cash
        history_by_day = {}
        round_trips = []
        rate = D(p['participation_rate'])
        fee_rate = D(p['fee_bps']) / 10000
        slippage = D(p['slippage_bps']) / 10000
        quote_liquidity_used = {}
        action_cursor, receivables, inactive_assets = 0, {}, set()
        settled_cash, settlements, entry_observed_at = initial_cash, [], {}

        def equity():
            return cash + sum(receivables.values()) + sum(pos['quantity'] * last_prices.get(symbol, pos['avg_price']) for symbol, pos in positions.items())

        for frame in snapshots:
            now = as_utc(frame['timestamp'])
            day = frame['session_date']
            state = market_state(now)
            quotes = frame.get('quotes', {})
            bars = frame.get('bars', {})
            for settlement in list(settlements):
                if settlement['session_date'] <= day:
                    settled_cash += settlement['amount']
                    settlements.remove(settlement)
            while action_cursor < len(action_schedule) and action_schedule[action_cursor][0] <= now:
                effective, kind, action = action_schedule[action_cursor]
                action_cursor += 1
                symbol = action['symbol']
                pos = positions.get(symbol)
                event = {'action_id': action['id'], 'type': kind, 'symbol': symbol,
                         'effective_at': iso(effective), 'applied_at': iso(now)}
                if kind == 'dividend_entitlement':
                    amount = (pos['quantity'] if pos else D(0)) * D(action['cash_per_share'])
                    receivables[action['id']] = amount
                    event['receivable'] = amount
                elif kind == 'dividend_payment':
                    amount = receivables.pop(action['id'], D(0))
                    cash += amount
                    settled_cash += amount
                    event['cash_credit'] = amount
                else:
                    # Replay's simulated venue confirms cancellation immediately.
                    # Production cancellation acknowledgement remains a broker task.
                    for order in list(pending):
                        if order['symbol'] == symbol:
                            order['status'] = 'canceled_corporate_action'
                            pending.remove(order)
                    if kind == 'split' and pos:
                        ratio = D(action['ratio'])
                        pos['quantity'] *= ratio
                        for field in ('avg_price', 'entry_atr', 'protection', 'highest_close', 'ma20'):
                            pos[field] /= ratio
                        if symbol in last_prices:
                            last_prices[symbol] /= ratio
                        pos.pop('pending_exit_signal', None)
                        pos.pop('pending_exit_observed_at', None)
                        event['ratio'], event['quantity_after'] = ratio, pos['quantity']
                    elif kind == 'symbol_change':
                        renamed = action['new_symbol']
                        inactive_assets.add(symbol)
                        if pos:
                            positions[renamed] = positions.pop(symbol)
                            if symbol in last_prices:
                                last_prices[renamed] = last_prices.pop(symbol)
                        event['new_symbol'] = renamed
                    elif kind == 'cash_delisting':
                        inactive_assets.add(symbol)
                        if pos:
                            proceeds = pos['quantity'] * D(action['cash_per_share'])
                            cash += proceeds
                            settled_cash += proceeds
                            round_trips.append({'symbol': symbol, 'entry_at': pos['opened_at'], 'exit_at': iso(effective),
                                'quantity': pos['quantity'], 'entry_price': pos['avg_price'], 'exit_price': D(action['cash_per_share']),
                                'net_pnl': proceeds-pos['entry_cost'], 'holding_minutes': (effective-as_utc(pos['opened_at'])).total_seconds()/60,
                                'reason': 'Verified cash delisting proceeds', 'corporate_action_id': action['id']})
                            event['cash_credit'] = proceeds
                            del positions[symbol]
                result['corporate_action_events'].append(event)
            # Mark-to-market uses only quotes observable by this time. Synthetic
            # bars still cannot inject future prices into equity.
            for symbol, quote in quotes.items():
                if not validate_quote(quote, now, p):
                    last_prices[symbol] = D(quote['last'])
            if day != last_day:
                day_start_equity, day_added, last_day = equity(), D(0), day
            usable = frame.get('quality', 'valid') == 'valid' and state['is_open'] and frame.get('market_open', True)
            # A simulated venue TTL expires independently of quote availability.
            for intent in list(pending):
                if now >= as_utc(intent['expires_at']):
                    intent['status'] = 'expired'
                    pending.remove(intent)
            if usable:
                # Old intents only. A signal cannot fill at its confirmation timestamp.
                for intent in list(pending):
                    quote = quotes.get(intent['symbol'])
                    if not quote or as_utc(quote['timestamp']) <= as_utc(intent['submitted_at']) or validate_quote(quote, now, p):
                        continue
                    if now <= as_utc(intent['submitted_at']):
                        continue
                    symbol, side = intent['symbol'], intent['side']
                    execution = D(quote['ask']) * (1 + slippage) if side == 'buy' else D(quote['bid']) * (1 - slippage)
                    if (side == 'buy' and execution > intent['limit_price']) or (side == 'sell' and execution < intent['limit_price']):
                        continue
                    # Participation uses a single observable snapshot volume, not
                    # bar-touch fill assumptions. Repeated quote IDs reuse budget.
                    liq_key = (symbol, quote.get('volume_timestamp') or quote['timestamp'])
                    liquidity = max(D(0), (D(quote.get('volume', 0)) * rate).to_integral_value(rounding=ROUND_DOWN) - quote_liquidity_used.get(liq_key, D(0)))
                    quantity = min(intent['remaining'], liquidity)
                    if side == 'buy':
                        quantity = min(quantity, (settled_cash / (execution * (1 + fee_rate))).to_integral_value(rounding=ROUND_DOWN))
                    else:
                        quantity = min(quantity, positions.get(symbol, {}).get('quantity', D(0)))
                    if quantity <= 0:
                        continue
                    quote_liquidity_used[liq_key] = quote_liquidity_used.get(liq_key, D(0)) + quantity
                    gross, fee = quantity * execution, quantity * execution * fee_rate
                    fees += fee
                    turnover += gross
                    fill = {'fill_id': f'fill-{len(result["fills"])+1:05d}', 'intent_id': intent['intent_id'],
                            'timestamp': iso(now), 'market_timestamp': quote['timestamp'], 'symbol': symbol,
                            'side': side, 'quantity': quantity, 'price': execution, 'fee': fee,
                            'signal_timestamp': intent['signal_timestamp']}
                    result['fills'].append(fill)
                    if side == 'buy':
                        cash -= gross + fee
                        settled_cash -= gross + fee
                        day_added += gross + fee
                        old = positions.get(symbol)
                        if old:
                            previous_qty = old['quantity']
                            old['quantity'] += quantity
                            old['avg_price'] = (old['avg_price'] * previous_qty + gross) / old['quantity']
                            old['entry_cost'] += gross + fee
                            old['entry_fees'] += fee
                        else:
                            positions[symbol] = {'quantity': quantity, 'avg_price': execution,
                                'entry_atr': intent['atr'], 'protection': execution - D(p['initial_stop_atr']) * intent['atr'],
                                'highest_close': execution, 'opened_at': iso(now), 'params': p,
                                'entry_cost': gross + fee, 'entry_fees': fee, 'ma20': intent['ma20']}
                    else:
                        cash += gross - fee
                        settlements.append({'session_date': as_utc(state['next_open']).astimezone(NEW_YORK).date().isoformat(), 'amount': gross-fee})
                        pos = positions[symbol]
                        fraction = quantity / pos['quantity']
                        cost = pos['entry_cost'] * fraction
                        pnl = gross - fee - cost
                        round_trips.append({'symbol': symbol, 'entry_at': pos['opened_at'], 'exit_at': iso(now),
                            'quantity': quantity, 'entry_price': pos['avg_price'], 'exit_price': execution,
                            'net_pnl': pnl, 'holding_minutes': (now - as_utc(pos['opened_at'])).total_seconds()/60,
                            'reason': intent['reason']})
                        pos['quantity'] -= quantity
                        pos['entry_cost'] -= cost
                        if pos['quantity'] == 0:
                            del positions[symbol]
                    intent['remaining'] -= quantity
                    intent['status'] = 'filled' if intent['remaining'] == 0 else 'partially_filled'
                    if intent['remaining'] == 0:
                        pending.remove(intent)

                candidate_map = {item['symbol']: item for item in frame.get('candidates', [])}
                # Holdings removed from pool keep the last known daily exit metric.
                indicators = frame.get('holding_indicators', frame.get('indicators', {}))
                for symbol, pos in list(positions.items()):
                    metric = indicators.get(symbol) or candidate_map.get(symbol)
                    if metric:
                        pos['ma20'] = D(metric['ma20'])
                    if any(item['symbol'] == symbol and item['side'] == 'sell' for item in pending):
                        continue
                    signal = exit_signal(pos, bars.get(symbol, []), pos['ma20'], now)
                    pos['protection'], pos['highest_close'] = signal['protection'], signal['highest_close']
                    completed = closed_bars(bars.get(symbol, []), now)
                    if completed:
                        pos['last_bar_at'] = completed[-1]['timestamp']
                    if signal['triggered']:
                        pos['pending_exit_signal'] = signal
                        pos['pending_exit_observed_at'] = iso(now)
                    signal = pos.get('pending_exit_signal', signal)
                    if pos.get('pending_exit_observed_at') and now >= as_utc(pos['pending_exit_observed_at']) + timedelta(seconds=int(p['order_ttl_seconds'])):
                        pos.pop('pending_exit_signal', None)
                        pos.pop('pending_exit_observed_at', None)
                        continue
                    if signal['triggered'] and not validate_quote(quotes.get(symbol), now, p):
                        # A partially filled entry is canceled before any exit, so
                        # a pending buy cannot add shares after the exit starts.
                        for buy in list(pending):
                            if buy['symbol'] == symbol and buy['side'] == 'buy':
                                buy['status'] = 'canceled'
                                pending.remove(buy)
                        intent = self._intent(result, symbol, 'sell', pos['quantity'], signal,
                                              quotes[symbol], now, p, pos['ma20'])
                        pending.append(intent)
                if frame.get('market_filter'):
                    candidates = sorted(frame.get('candidates', []), key=lambda item: (int(item.get('rank', 999)), item['symbol']))
                    for candidate in candidates:
                        symbol = candidate['symbol']
                        if symbol in inactive_assets or symbol in positions or (day, symbol) in seen_entries or any(item['symbol'] == symbol for item in pending):
                            continue
                        signal = entry_signal(candidate, bars.get(symbol, []), now)
                        if not signal:
                            continue
                        observed = entry_observed_at.setdefault((day, symbol), now)
                        if now >= observed + timedelta(seconds=int(p['order_ttl_seconds'])):
                            continue
                        quote_error = validate_quote(quotes.get(symbol), now, p)
                        if quote_error or candidate.get('tradable') is False:
                            result['decisions'].append({'timestamp': iso(now), 'symbol': symbol, 'reason': quote_error or 'NOT_TRADABLE'})
                            continue
                        if (now - as_utc(signal['bar_time'])).total_seconds() > int(p['bar_max_age_seconds']):
                            continue
                        reserved = sum(item['remaining'] * item['limit_price'] * (1 + fee_rate) for item in pending if item['side'] == 'buy')
                        exposure = sum(pos['quantity'] * last_prices.get(sym, pos['avg_price']) for sym, pos in positions.items())
                        limit_price = limit_price_for('BUY', quotes[symbol], p)
                        if abs(limit_price-signal['price'])/signal['price'] > D(p['max_price_deviation_pct']):
                            result['decisions'].append({'timestamp': iso(now), 'symbol': symbol, 'reason': 'MAX_PRICE_DEVIATION'})
                            continue
                        sizing = size_entry(equity(), settled_cash, exposure, reserved,
                            len(positions) + sum(item['side'] == 'buy' for item in pending), limit_price,
                            signal['atr'], p, day_added, equity() - day_start_equity)
                        result['decisions'].append({'timestamp': iso(now), 'symbol': symbol, 'reason': sizing['reason']})
                        if sizing['allowed']:
                            intent = self._intent(result, symbol, 'buy', sizing['quantity'], signal,
                                                  quotes[symbol], now, p, D(candidate['ma20']))
                            pending.append(intent)
                            seen_entries.add((day, symbol))
            value = equity()
            peak = max(peak, value)
            draw = value / peak - 1
            result['equity'].append({'timestamp': iso(now), 'equity': value, 'cash': cash,
                                     'settled_cash': settled_cash, 'unsettled_cash': cash-settled_cash,
                                     'positions': len(positions), 'pending_orders': len(pending)})
            result['drawdown'].append({'timestamp': iso(now), 'drawdown': draw})
            history_by_day[day] = value

        result['trades'] = round_trips
        profits = [D(trade['net_pnl']) for trade in round_trips]
        winners = [value for value in profits if value > 0]
        losers = [value for value in profits if value < 0]
        daily_values = [initial_cash] + list(history_by_day.values())
        daily_returns = [float(right/left-1) for left,right in zip(daily_values,daily_values[1:])]
        sharpe = None
        if len(daily_returns) >= 20 and stdev(daily_returns) > 0:
            sharpe = mean(daily_returns)/stdev(daily_returns)*math.sqrt(252)
        result['metrics'] = {
            'initial_equity': initial_cash, 'final_equity': equity(),
            'net_return': equity()/initial_cash-1,
            'max_drawdown': min([item['drawdown'] for item in result['drawdown']] or [D(0)]),
            'sharpe_daily': sharpe, 'sharpe_reason': None if sharpe is not None else 'At least 20 sessions with variable daily returns required',
            'trade_count': len(round_trips), 'fill_count': len(result['fills']),
            'win_rate': D(len(winners))/len(profits) if profits else None,
            'profit_factor': sum(winners)/abs(sum(losers)) if losers else None,
            'payoff_ratio': (sum(winners)/len(winners))/(abs(sum(losers))/len(losers)) if winners and losers else None,
            'turnover': turnover/initial_cash, 'fees': fees,
            'average_holding_minutes': mean(trade['holding_minutes'] for trade in round_trips) if round_trips else None,
            'open_positions': len(positions), 'pending_orders': len(pending),
            'spy_return': None, 'excess_return_vs_spy': None,
        }
        # Benchmark must have point-in-time observations for selected interval.
        benchmark = dataset.get('benchmark', [])
        benchmark = [item for item in benchmark if snapshots[0]['timestamp'] <= item['timestamp'] <= snapshots[-1]['timestamp']]
        if len(benchmark) >= 2:
            benchmark.sort(key=lambda item: item['timestamp'])
            spy_return = D(benchmark[-1]['close']) / D(benchmark[0]['close']) - 1
            result['metrics']['spy_return'] = spy_return
            result['metrics']['excess_return_vs_spy'] = result['metrics']['net_return'] - spy_return
        else:
            result['limitations'].append('SPY comparison unavailable: no aligned benchmark observations supplied.')
        result['limitations'].extend([
            'Quote replay uses observed quote price plus configured slippage, capped by limit and volume participation; it does not model queue priority.',
            'Open positions are marked to observable prices at replay end; no artificial terminal liquidation.',
            'Trade statistics count completed exit lots; partial exits can create several lots per position.',
            'Local exits depend on the worker; no broker-native protection is implied.',
        ])
        if synthetic:
            result['validation_status'] = 'software_replay_only'
            result['efficacy_status'] = 'not_applicable_synthetic'
            result['limitations'].append('DEMONSTRATION DATA: replay statistics are software test outputs and provide no evidence of investment returns.')
        else:
            required = {'historical_universe_verified': 'Point-in-time universe and delistings not verified.',
                        'actions_verified': 'Split/dividend/symbol-change handling not verified.',
                        'availability_timestamps_verified': 'Original publication/availability timestamps not verified.'}
            for field, reason in required.items():
                if not dataset.get(field):
                    result['limitations'].append(reason)
            result['validation_status'] = 'research_unvalidated'
            result['efficacy_status'] = 'blocked_missing_data' if any(not dataset.get(field) for field in required) or not intervals else 'pending_independent_evaluation'
            # Even verified metadata cannot prove profitable. A research report
            # needs independent review and adequate out-of-sample evaluation.
        result['status'], result['software_validation'] = 'completed', 'passed_sequential_replay'
        result['reason'] = None
        result['open_positions'] = positions
        result['dividend_receivables'] = receivables
        result['unsettled_proceeds'] = settlements
        result['unfilled_intents'] = pending
        result['matching_rules'] = {'after_signal_only': True, 'spread': 'observed_bid_ask',
            'slippage_bps': p['slippage_bps'], 'fee_bps': p['fee_bps'], 'participation_rate': p['participation_rate'],
            'fills_from_bar_touch': False, 'time_in_force': 'configured TTL; no automatic repricing'}
        if actions:
            result['limitations'].append('Corporate-action replay requires normalized, verified terms and snapshot-consistent price bases; exit-lot PnL excludes dividend income, which is included in total equity.')
        return _serial(result)

    @staticmethod
    def _intent(result, symbol, side, quantity, signal, quote, now, params, ma20):
        limit_price = limit_price_for(side.upper(), quote, params)
        intent = {'intent_id': f'intent-{len(result["intents"])+1:05d}', 'symbol': symbol,
                  'side': side, 'quantity': quantity, 'remaining': quantity, 'limit_price': limit_price,
                  'submitted_at': iso(now), 'signal_timestamp': signal['bar_time'],
                  'expires_at': iso(now + timedelta(seconds=int(params['order_ttl_seconds']))),
                  'status': 'accepted', 'atr': signal.get('atr', D(0)), 'ma20': ma20, 'reason': signal['reason']}
        result['intents'].append(intent)
        return intent
