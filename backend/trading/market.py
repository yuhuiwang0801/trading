"""Read-only Alpaca market data adapter; contains no order execution API.

Raw daily and intraday prices share one adjustment basis. Securities with price
adjusting actions in the lookback are excluded pending an explicit normalization
workflow; missing action permission also prevents automated instructions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from .calendar import NEW_YORK, as_utc, iso, market_state, previous_session, session_bounds, calendar_for_year


class MarketDataError(RuntimeError):
    """Sanitized provider failure: never contains credentials or response bodies."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 error_code: str | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.endpoint = endpoint


TRADING_URLS = {'https://paper-api.alpaca.markets', 'https://api.alpaca.markets'}
DATA_URLS = {'https://data.alpaca.markets'}
READ_ONLY_ENDPOINTS = {'/v2/account', '/v2/assets', '/v2/stocks/bars',
                       '/v2/stocks/quotes/latest', '/v1/corporate-actions'}


class AlpacaMarketData:
    def __init__(self, key: str = '', secret: str = '', feed: str = 'iex', *,
                 trading_url: str = 'https://paper-api.alpaca.markets',
                 data_url: str = 'https://data.alpaca.markets',
                 client: httpx.Client | None = None, rpm: int = 180,
                 retries: int = 2, batch_size: int = 100, sleep=time.sleep):
        if feed not in {'iex', 'sip'}:
            raise ValueError('Only explicit iex or sip regular-session feeds are supported')
        self.key, self.secret, self.feed = key, secret, feed
        self.trading_url, self.data_url = trading_url.rstrip('/'), data_url.rstrip('/')
        if self.trading_url not in TRADING_URLS or self.data_url not in DATA_URLS:
            raise ValueError('Alpaca URLs must use the official HTTPS trading and data hosts')
        self.client = client or httpx.Client(timeout=httpx.Timeout(12, connect=5))
        self.rpm, self.retries, self.batch_size = max(1, rpm), max(0, min(retries, 3)), max(1, batch_size)
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self.last_success: str | None = None
        self.last_data_success: str | None = None
        self.last_error: str | None = None
        self.endpoint_status: dict[str, dict] = {}
        self.request_count = 0
        self.cache_hits = 0
        self.action_status = 'not_verified'

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def _get(self, host: str, path: str, params: dict, ttl: int = 0):
        # Validate every call, not just configuration: API credentials must never
        # follow a redirect or reach an arbitrary host/path supplied by a caller.
        if host not in TRADING_URLS | DATA_URLS or path not in READ_ONLY_ENDPOINTS:
            raise MarketDataError('Alpaca request destination is not permitted', error_code='invalid_destination')
        if not self.configured:
            raise MarketDataError('Alpaca credentials are not configured', error_code='credentials_missing', endpoint=path)
        cache_key = json.dumps([host, path, params], sort_keys=True)
        item = self._cache.get(cache_key)
        if item and item[0] > time.monotonic():
            self.cache_hits += 1
            return copy.deepcopy(item[1])
        headers = {'APCA-API-KEY-ID': self.key, 'APCA-API-SECRET-KEY': self.secret}
        for attempt in range(self.retries + 1):
            with self._lock:
                delay = self._last_request + 60 / self.rpm - time.monotonic()
                if delay > 0:
                    self.sleep(delay)
                self._last_request = time.monotonic()
            try:
                self.request_count += 1
                response = self.client.get(host + path, params=params, headers=headers, follow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.retries:
                        # Retry-After is bounded. Long limits fail this scan and the
                        # worker will try its next scheduled cycle.
                        try:
                            delay = float(response.headers.get('Retry-After', 2 ** attempt))
                        except ValueError:
                            delay = 2 ** attempt
                        self.sleep(min(10, max(0, delay)))
                        continue
                if response.status_code != 200:
                    # Only the documented numeric code is retained. Error bodies
                    # and arbitrary server text can echo submitted credentials.
                    code = None
                    try:
                        error_body = response.json()
                        raw_code = error_body.get('code') if isinstance(error_body, dict) else None
                        if (isinstance(raw_code, (int, str)) and str(raw_code).isascii()
                                and str(raw_code).isdigit() and len(str(raw_code)) <= 16
                                and str(raw_code) not in {self.key, self.secret}):
                            code = str(raw_code)
                    except (ValueError, TypeError):
                        pass
                    raise MarketDataError(f'Alpaca {path}: HTTP {response.status_code}',
                                          http_status=response.status_code, error_code=code, endpoint=path)
                payload = response.json()
                self.last_success, self.last_error = iso(as_utc()), None
                if host == self.data_url:
                    self.last_data_success = self.last_success
                self.endpoint_status[path] = {'status': 'success', 'http_status': 200,
                    'error_code': None, 'checked_at': self.last_success, 'last_success': self.last_success}
                if ttl:
                    self._cache[cache_key] = (time.monotonic() + ttl, copy.deepcopy(payload))
                    if len(self._cache) > 512:
                        self._cache.pop(next(iter(self._cache)))
                return payload
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < self.retries:
                    self.sleep(min(4, 2 ** attempt))
                    continue
                self.last_error = f'Alpaca {path}: timeout or connection failure'
                error = MarketDataError(self.last_error, error_code='connection_failed', endpoint=path)
                self._record_error(error)
                raise error from None
            except (ValueError, TypeError):
                self.last_error = f'Alpaca {path}: invalid response'
                error = MarketDataError(self.last_error, error_code='invalid_response', endpoint=path)
                self._record_error(error)
                raise error from None
            except MarketDataError as exc:
                self._record_error(exc)
                raise
        raise MarketDataError(f'Alpaca {path}: retry limit reached', error_code='retry_limit', endpoint=path)

    def _record_error(self, error: MarketDataError):
        self.last_error = str(error)
        if error.endpoint:
            previous = self.endpoint_status.get(error.endpoint, {})
            self.endpoint_status[error.endpoint] = {'status': 'failed', 'http_status': error.http_status,
                'error_code': error.error_code, 'checked_at': iso(as_utc()), 'last_success': previous.get('last_success')}

    def assets(self) -> list[dict]:
        result = self._get(self.trading_url, '/v2/assets', {'status': 'active', 'asset_class': 'us_equity'}, ttl=3600)
        if not isinstance(result, list):
            raise MarketDataError('Alpaca assets: expected an array')
        return result

    def _batches(self, symbols):
        unique = list(dict.fromkeys(symbols))
        for index in range(0, len(unique), self.batch_size):
            yield unique[index:index + self.batch_size]

    def bars(self, symbols: list[str], timeframe: str, start: str, end: str, *, asof: str, ttl: int = 0) -> dict:
        result = {symbol: [] for symbol in symbols}
        for batch in self._batches(symbols):
            params = {'symbols': ','.join(batch), 'timeframe': timeframe, 'start': start,
                      'end': end, 'adjustment': 'raw', 'asof': asof, 'feed': self.feed,
                      'sort': 'asc', 'limit': 10000}
            seen_tokens = set()
            for _ in range(1000):
                data = self._get(self.data_url, '/v2/stocks/bars', params, ttl=ttl)
                if not isinstance(data, dict) or not isinstance(data.get('bars') or {}, dict):
                    raise MarketDataError('Alpaca bars: unrecognized schema', error_code='invalid_response', endpoint='/v2/stocks/bars')
                for symbol, rows in (data.get('bars') or {}).items():
                    if symbol in result:
                        if not isinstance(rows, list):
                            raise MarketDataError('Alpaca bars: unrecognized schema', error_code='invalid_response', endpoint='/v2/stocks/bars')
                        result[symbol].extend(rows)
                token = data.get('next_page_token')
                if not token:
                    break
                if token in seen_tokens:
                    raise MarketDataError('Alpaca bars: pagination cycle')
                seen_tokens.add(token)
                params = {**params, 'page_token': token}
            else:
                raise MarketDataError('Alpaca bars: pagination safety bound reached')
        return result

    def quotes(self, symbols: list[str]) -> dict:
        result = {}
        for batch in self._batches(symbols):
            data = self._get(self.data_url, '/v2/stocks/quotes/latest',
                             {'symbols': ','.join(batch), 'feed': self.feed})
            if not isinstance(data, dict) or not isinstance(data.get('quotes') or {}, dict):
                raise MarketDataError('Alpaca quotes: unrecognized schema', error_code='invalid_response', endpoint='/v2/stocks/quotes/latest')
            result.update(data.get('quotes') or {})
        return result

    def corporate_actions(self, symbols: list[str], start: str, end: str) -> list[dict]:
        actions = []
        try:
            for batch in self._batches(symbols):
                params = {'symbols': ','.join(batch), 'start': start, 'end': end,
                          'limit': 1000, 'sort': 'asc', 'data_quality': 'all'}
                seen = set()
                for _ in range(100):
                    data = self._get(self.data_url, '/v1/corporate-actions', params, ttl=900)
                    if not isinstance(data, dict):
                        raise MarketDataError('Alpaca corporate actions: unrecognized schema', error_code='invalid_response', endpoint='/v1/corporate-actions')
                    grouped = data.get('corporate_actions')
                    if not isinstance(grouped, dict):
                        raise MarketDataError('Alpaca corporate actions: unrecognized schema')
                    for kind, rows in grouped.items():
                        actions.extend({**row, 'action_type': kind} for row in rows)
                    token = data.get('next_page_token')
                    if not token:
                        break
                    if token in seen:
                        raise MarketDataError('Alpaca corporate actions: pagination cycle')
                    seen.add(token)
                    params = {**params, 'page_token': token}
                else:
                    raise MarketDataError('Alpaca corporate actions: pagination bound reached')
            self.action_status = 'available_delayed_completeness_not_guaranteed'
            return actions
        except MarketDataError:
            self.action_status = 'unavailable'
            raise

    def close(self):
        self.client.close()


def _number(value) -> Decimal:
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('Non-finite data')
    return value


def _common_equity(asset: dict) -> bool:
    # The API exposes us_equity, not a complete security subtype taxonomy.
    # Conservative name-based admission; its limitations are exposed in status.
    name = asset.get('name', '').lower()
    excluded = ('etf', 'etn', 'preferred', 'warrant', 'depositary', 'depositary', ' units', 'fund', 'trust')
    return (bool(asset.get('tradable')) and asset.get('status') == 'active'
            and asset.get('class') == 'us_equity'
            and asset.get('exchange') in {'NYSE', 'NASDAQ', 'AMEX', 'ARCA', 'NYSEARCA', 'BATS'}
            and not any(word in name for word in excluded)
            and ('common' in name or 'ordinary' in name))


def daily_indicators(rows: list[dict], asof_session: str, feed: str = 'iex', min_listing_sessions: int = 120) -> dict | None:
    """Only prior complete sessions; provider daily t is session start, not end."""
    prior = previous_session(asof_session)
    normalized = {}
    for row in rows:
        day = as_utc(row['t']).astimezone(NEW_YORK).date().isoformat()
        if day < asof_session and session_bounds(day):
            normalized[day] = row
    days = sorted(normalized)
    required = max(61, int(min_listing_sessions))
    if len(days) < required or days[-1] != prior:
        return None
    cal = calendar_for_year(int(prior[:4]))
    end_index = cal.sessions.get_loc(prior)
    expected = [session.date().isoformat() for session in cal.sessions[max(0, end_index-required+1):end_index+1]]
    if any(day not in normalized for day in expected):
        return None
    history = [normalized[day] for day in days]
    closes = [_number(row['c']) for row in history]
    if any(value <= 0 for value in closes):
        return None
    true_ranges = []
    for index in range(1, len(history)):
        high, low = _number(history[index]['h']), _number(history[index]['l'])
        if low <= 0 or high < low or not low <= closes[index] <= high:
            return None
        true_ranges.append(max(high-low, abs(high-closes[index-1]), abs(low-closes[index-1])))
    values = {
        'close': closes[-1], 'ma20': sum(closes[-20:]) / 20,
        'ma60': sum(closes[-60:]) / 60,
        'high20': max(_number(row['h']) for row in history[-20:]),
        'atr14': sum(true_ranges[-14:]) / 14,
        'return60': closes[-1] / closes[-61] - 1,
        'avg_dollar_volume': sum(_number(row['c']) * _number(row['v']) for row in history[-20:]) / 20,
    }
    return {**{key: str(value) for key, value in values.items()}, 'history_sessions': len(days),
            'required_history_sessions': required,
            'asof_session': prior, 'known_at': iso(session_bounds(prior)[1]),
            'adjustment': 'raw', 'feed': feed,
            'volume_scope': 'single_venue_IEX' if feed == 'iex' else 'consolidated_US'}


class MarketService:
    def __init__(self, provider: AlpacaMarketData | None = None, symbols: list[str] | None = None,
                 *, stale_seconds: int = 30, min_dollar_volume: int = 20_000_000,
                 candidate_limit: int = 30, minimum_price: int = 5, min_listing_sessions: int = 120):
        self.provider = provider or AlpacaMarketData()
        self.symbols = symbols  # None explicitly means all eligible assets.
        self.stale_seconds = stale_seconds
        self.min_dollar_volume, self.candidate_limit = min_dollar_volume, candidate_limit
        self.minimum_price = minimum_price
        self.min_listing_sessions = min_listing_sessions
        self.last_scan: dict | None = None

    @classmethod
    def from_env(cls):
        raw_symbols = os.getenv('MARKET_SYMBOLS', '')
        symbols = [value.strip().upper() for value in raw_symbols.split(',') if value.strip()] or None
        return cls(AlpacaMarketData(
            os.getenv('ALPACA_API_KEY', os.getenv('APCA_API_KEY_ID', '')),
            os.getenv('ALPACA_SECRET_KEY', os.getenv('ALPACA_API_SECRET', os.getenv('APCA_API_SECRET_KEY', ''))),
            os.getenv('ALPACA_DATA_FEED', 'iex'),
            trading_url=os.getenv('ALPACA_TRADING_URL', 'https://paper-api.alpaca.markets'),
            data_url=os.getenv('ALPACA_DATA_URL', 'https://data.alpaca.markets'),
            rpm=int(os.getenv('ALPACA_REQUESTS_PER_MINUTE', '180'))), symbols)

    def status(self) -> dict:
        provider = self.provider
        scan = self.last_scan
        connection = 'unverified' if provider.configured else 'missing_credentials'
        if provider.configured and scan:
            if scan['quality'] == 'valid':
                connection = 'verified' if scan['market_open'] else 'market_closed'
            else:
                connection = 'failed' if scan['quality'] == 'unavailable' else 'degraded'
        return {'provider': 'alpaca', 'configured': provider.configured,
                'connection_status': connection,
                'request_connection_status': 'failed' if provider.last_error else ('reachable' if provider.last_success else 'unverified'),
                'feed': provider.feed, 'last_success': provider.last_success, 'last_error': provider.last_error,
                'last_data_success': provider.last_data_success,
                'last_scan_at': scan['timestamp'] if scan else None,
                'scan_quality': scan['quality'] if scan else None,
                'endpoint_status': copy.deepcopy(provider.endpoint_status),
                'request_count': provider.request_count, 'cache_hits': provider.cache_hits,
                'latency': 'observed_from_timestamps; entitlement_unverified_until_success',
                'volume_scope': 'single_venue_IEX' if provider.feed == 'iex' else 'consolidated_US',
                'scope': 'configured_symbols' if self.symbols else 'eligible_active_asset_master',
                'symbols': self.symbols,
                'capabilities': {'assets': True, 'stable_id': 'Alpaca asset UUID', 'daily': True,
                    'five_minute': True, 'latest_bid_ask': True, 'corporate_actions': provider.action_status,
                    'corporate_action_timeliness_guaranteed': False, 'historical_point_in_time_universe': False,
                    'common_stock_classification': 'conservative_name_filter_requires_reference_verification',
                    'real_time_halt_feed': False, 'tradability': 'asset_master_not_a_halt_guarantee'},
                'limitations': [
                    'IEX volume is single-venue volume; the dollar-volume screen is feed-specific and does not reproduce a consolidated-market screen.' if provider.feed == 'iex' else 'SIP entitlement must be verified with configured credentials.',
                    'No guaranteed real-time halt status. Stale/invalid quotes block execution.',
                    'Raw-price lookbacks with price-adjusting corporate actions are excluded pending normalization.',
                    'Asset master is current; not suitable as a historical point-in-time backtest universe.']}

    def readiness(self) -> dict:
        status = self.status()
        scan = self.last_scan
        ready = bool(status['configured'] and scan and scan['quality'] == 'valid' and scan['market_open'])
        reason = ('credentials_required' if not status['configured'] else 'not_checked' if not scan
                  else scan.get('reason') or ('market_closed' if not scan['market_open'] else None))
        return {**status, 'ready': ready, 'reason': reason}

    def fetch_scan(self, now: datetime | str | None = None, symbols: list[str] | None = None) -> dict:
        fixed_now = as_utc(now) if now is not None else None
        now = fixed_now or as_utc()
        state = market_state(now)
        result = {'source': 'alpaca', 'quality': 'unavailable', 'market_open': state['is_open'],
                  'session_date': state['session_date'], 'timestamp': iso(now), 'started_at': iso(now),
                  'received_at': None,
                  'market_filter': False, 'candidates': [], 'quotes': {}, 'bars': {}, 'calendar': state,
                  'metadata': self.status(), 'exclusions': [], 'corporate_actions': []}
        if not self.provider.configured:
            result['reason'] = 'credentials_required'
            self.last_scan = result
            return result
        try:
            assets = self.provider.assets()
            asset_map = {asset['symbol']: asset for asset in assets if asset.get('symbol') and asset.get('id')}
            configured = set(self.symbols) if self.symbols else None
            eligible = [symbol for symbol, asset in asset_map.items()
                        if _common_equity(asset) and (configured is None or symbol in configured)]
            # Explicit extra symbols are holdings: fetch them even after they leave the pool.
            monitored = sorted(set(eligible) | set(symbols or []) | {'SPY'})
            start = (now.astimezone(NEW_YORK).date() - timedelta(days=400)).isoformat()
            day = state['session_date']
            daily = self.provider.bars(monitored, '1Day', start, day, asof=day, ttl=3600)
            # A point-in-time actions API is not assumed. Any returned price
            # altering event in this raw lookback conservatively excludes entry.
            actions = self.provider.corporate_actions(monitored, start, day)
            result['received_at'] = iso(fixed_now or as_utc())
            result['corporate_actions'] = actions
            action_symbols = set()
            for action in actions:
                if action['action_type'] not in {'cash_dividends', 'cash_dividend'}:
                    # Some mergers have several symbol fields; traverse all strings.
                    action_symbols.update(value for value in action.values() if isinstance(value, str) and value in monitored)
            metrics = {}
            for symbol in monitored:
                metric = daily_indicators(daily.get(symbol, []), day, self.provider.feed, self.min_listing_sessions)
                if not metric:
                    result['exclusions'].append({'symbol': symbol, 'reason': 'missing_required_sessions_or_prior_close', 'required_sessions': max(61, self.min_listing_sessions)})
                    continue
                if symbol in action_symbols:
                    result['exclusions'].append({'symbol': symbol, 'reason': 'raw_corporate_action_requires_normalization'})
                    continue
                metrics[symbol] = metric
            spy = metrics.get('SPY')
            result['market_filter'] = bool(spy and _number(spy['close']) > _number(spy['ma60']))
            for symbol in eligible:
                metric = metrics.get(symbol)
                if not metric:
                    continue
                if (_number(metric['close']) >= self.minimum_price and _number(metric['avg_dollar_volume']) >= self.min_dollar_volume
                        and _number(metric['close']) > _number(metric['ma20']) > _number(metric['ma60'])):
                    result['candidates'].append({'symbol': symbol, 'security_id': asset_map[symbol]['id'],
                        'name': asset_map[symbol].get('name', symbol), 'tradable': True, **metric,
                        'reasons': [f'{max(61, self.min_listing_sessions)}+ completed sessions', f'price >= ${self.minimum_price}', 'feed dollar volume >= configured minimum', 'close > MA20 > MA60']})
            result['candidates'].sort(key=lambda item: (_number(item['return60']), item['symbol']), reverse=True)
            result['candidates'] = result['candidates'][:self.candidate_limit]
            for rank, item in enumerate(result['candidates'], 1):
                item['rank'] = rank
            current = sorted({item['symbol'] for item in result['candidates']} | set(symbols or []))
            result['indicators'] = metrics
            result['holding_indicators'] = metrics
            result['universe_count'] = len(eligible)
            result['coverage_count'] = len(metrics)
            result['quality'] = 'valid'
            result['reason'] = None
            # A live scan can spend seconds fetching history. Request intraday
            # data against the current clock, while explicit replay times stay fixed.
            now = fixed_now or as_utc()
            if current and state['open'] and now >= as_utc(state['open']):
                # Regular-session strategies never include postmarket bars. The
                # API end is inclusive; completed-bar filtering below is still
                # needed for the bar whose start equals the session close.
                bars_end = min(now, as_utc(state['close']))
                raw_bars = self.provider.bars(current, '5Min', state['open'], iso(bars_end), asof=day, ttl=5)
                bars_received_at = iso(fixed_now or as_utc())
                raw_quotes = self.provider.quotes(current)
                now = fixed_now or as_utc()
                result['received_at'] = iso(now)
                state = market_state(now)
                invalid = []
                for symbol in current:
                    normalized = []
                    for row in raw_bars.get(symbol, []):
                        bar_start = as_utc(row['t'])
                        end = bar_start + timedelta(minutes=5)
                        # A bar that completed while the request was in flight
                        # may still contain a partial snapshot. Admit only bars
                        # complete before this request's cutoff.
                        if bar_start < as_utc(result['calendar']['open']) or end > bars_end:
                            continue
                        values = {target: str(_number(row[source])) for target, source in
                                  [('open','o'),('high','h'),('low','l'),('close','c'),('volume','v')]}
                        if (_number(values['low']) <= 0 or _number(values['high']) < _number(values['low'])
                                or not _number(values['low']) <= _number(values['close']) <= _number(values['high'])
                                or _number(values['volume']) < 0):
                            raise MarketDataError('Invalid OHLC data')
                        normalized.append({**values, 'timestamp': iso(end), 'start': iso(bar_start),
                            'source': 'alpaca', 'feed': self.provider.feed, 'received_at': bars_received_at,
                            'granularity': '5Min', 'adjustment': 'raw', 'quality': 'valid'})
                    normalized.sort(key=lambda item: item['timestamp'])
                    result['bars'][symbol] = normalized
                    quote = raw_quotes.get(symbol)
                    if not quote:
                        invalid.append(symbol)
                        continue
                    quote_time = as_utc(quote['t'])
                    bid, ask = _number(quote['bp']), _number(quote['ap'])
                    age = (now - quote_time).total_seconds()
                    valid = bid > 0 and ask >= bid and 0 <= age <= self.stale_seconds
                    result['quotes'][symbol] = {'bid': str(bid), 'ask': str(ask),
                        # Last is a separately labeled last CLOSED bar, not a fabricated trade.
                        'last': normalized[-1]['close'] if normalized else None,
                        'last_source': 'last_closed_5m_bar', 'timestamp': iso(quote_time),
                        'volume': normalized[-1]['volume'] if normalized else '0',
                        'volume_timestamp': normalized[-1]['timestamp'] if normalized else None,
                        'volume_source': 'last_closed_5m_bar',
                        'bid_size': str(quote.get('bs', 0)), 'ask_size': str(quote.get('as', 0)),
                        'source': 'alpaca', 'feed': self.provider.feed, 'received_at': result['received_at'],
                        'granularity': 'quote', 'quality': 'valid' if valid else 'stale_or_invalid',
                        'age_seconds': age, 'tradable': bool(asset_map.get(symbol, {}).get('tradable')),
                        'volume_scope': 'single_venue_IEX' if self.provider.feed == 'iex' else 'consolidated_US'}
                    latest_bar_stale = not normalized or (now - as_utc(normalized[-1]['timestamp'])).total_seconds() > 360
                    if state['is_open'] and (not valid or latest_bar_stale):
                        invalid.append(symbol)
                if invalid:
                    # Fail the scan closed instead of allowing incomplete portfolio risk.
                    result['quality'], result['reason'] = 'stale', 'stale_or_missing_quotes_or_closed_bars'
                    result['affected_symbols'] = sorted(set(invalid))
            if not spy:
                result['quality'], result['reason'] = 'incomplete', 'market_filter_history_unavailable'
            missing_holdings = sorted(set(symbols or []) - set(metrics))
            if missing_holdings:
                result['quality'], result['reason'] = 'incomplete', 'held_security_indicators_unavailable'
                result['affected_symbols'] = sorted(set(result.get('affected_symbols', [])) | set(missing_holdings))
            result['data_version'] = hashlib.sha256(json.dumps({'daily': daily, 'actions': actions}, sort_keys=True).encode()).hexdigest()[:20]
        except (MarketDataError, ValueError, KeyError, TypeError) as exc:
            result['quality'] = 'unavailable'
            result['reason'] = str(exc) if isinstance(exc, MarketDataError) else 'provider_schema_or_value_error'
            result['market_filter'] = False
        completed = fixed_now or as_utc()
        completed_state = market_state(completed)
        result['timestamp'] = iso(completed)
        result['calendar'] = completed_state
        result['market_open'] = completed_state['is_open']
        if completed_state['session_date'] != result['session_date']:
            result['market_open'] = False
            result['quality'], result['reason'] = 'incomplete', 'session_changed_during_scan'
        self.last_scan = result
        result['metadata'] = self.status()
        return result
