from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading.calendar import as_utc, market_state, previous_session, session_bounds, calendar_for_year, iso
from trading.market import AlpacaMarketData, MarketDataError, MarketService, daily_indicators


def daily_rows():
    cal = calendar_for_year(2026)
    days = cal.sessions_in_range('2026-04-01', '2026-10-23')
    return [{'t': f'{day.date()}T04:00:00Z', 'o': 90+i/10, 'h': 92+i/10,
             'l': 89+i/10, 'c': 91+i/10, 'v': 1_000_000} for i, day in enumerate(days)]


def test_calendar_dst_holiday_and_half_day():
    assert market_state('2026-03-06T14:30:00Z')['is_open']
    assert market_state('2026-03-09T13:30:00Z')['is_open']
    assert market_state('2026-03-09T13:29:59Z')['phase'] == 'pre_market'
    assert not market_state('2026-07-03T15:00:00Z')['is_open']
    assert market_state('2026-07-03T15:00:00Z')['next_open'] == '2026-07-06T13:30:00Z'
    state = market_state('2026-11-27T17:59:59Z')
    assert state['early_close'] and state['close'] == '2026-11-27T18:00:00Z'
    assert not market_state('2026-11-27T18:00:00Z')['is_open']
    assert previous_session('2026-10-26') == '2026-10-23'


def test_calendar_rejects_naive_time():
    with pytest.raises(ValueError, match='timezone'):
        market_state(datetime(2026, 10, 26, 14))


def test_daily_indicators_exclude_current_day_and_require_recent_complete_history():
    rows = daily_rows()
    original = daily_indicators(rows, '2026-10-26')
    mutated = rows + [{'t': '2026-10-26T04:00:00Z', 'c': 999999, 'h': 999999, 'l': 1, 'v': 99999999}]
    assert original == daily_indicators(mutated, '2026-10-26')
    assert original['known_at'] == '2026-10-23T20:00:00Z'
    assert original['volume_scope'] == 'single_venue_IEX'
    assert daily_indicators(rows[:-1], '2026-10-26') is None
    assert daily_indicators(rows[-119:], '2026-10-26') is None


def test_no_credentials_never_fabricates_market_data():
    service = MarketService()
    scan = service.fetch_scan('2026-10-26T14:00:00Z')
    assert scan['quality'] == 'unavailable'
    assert scan['reason'] == 'credentials_required'
    assert scan['candidates'] == [] and not scan['market_filter']
    assert not service.readiness()['ready']


def test_adapter_batches_paginates_and_preserves_raw_feed():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['APCA-API-KEY-ID'] == 'test-key'
        assert request.url.params['adjustment'] == 'raw'
        assert request.url.params['feed'] == 'iex'
        symbols = request.url.params['symbols'].split(',')
        if 'page_token' not in request.url.params:
            return httpx.Response(200, json={'bars': {symbols[0]: [{'c': 1}]}, 'next_page_token': 'next'})
        return httpx.Response(200, json={'bars': {symbols[-1]: [{'c': 2}]}, 'next_page_token': None})
    adapter = AlpacaMarketData('test-key', 'test-secret', client=httpx.Client(transport=httpx.MockTransport(handler)), batch_size=2, sleep=lambda _: None)
    data = adapter.bars(['A', 'B', 'C'], '1Day', '2026-01-01', '2026-10-26', asof='2026-10-26', ttl=10)
    assert len(calls) == 4 and data['A'] and data['B'] and data['C']
    adapter.bars(['A', 'B', 'C'], '1Day', '2026-01-01', '2026-10-26', asof='2026-10-26', ttl=10)
    assert len(calls) == 4


def test_adapter_retries_finite_redacts_errors():
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503, text='sensitive body test-secret')
    adapter = AlpacaMarketData('key', 'test-secret', client=httpx.Client(transport=httpx.MockTransport(handler)), retries=2, sleep=lambda _: None)
    with pytest.raises(MarketDataError) as error:
        adapter.assets()
    assert count == 3 and 'test-secret' not in str(error.value)


def test_latest_bars_are_closed_and_stale_quotes_fail_scan_closed():
    rows = daily_rows()
    now = datetime(2026, 10, 26, 14, 10, 30, tzinfo=timezone.utc)
    def handler(request):
        path, q = request.url.path, request.url.params
        if path == '/v2/assets':
            return httpx.Response(200, json=[{'id': 'stable-uuid', 'symbol': 'ABC', 'name': 'ABC Common Stock', 'class': 'us_equity', 'exchange': 'NYSE', 'status': 'active', 'tradable': True}])
        if path == '/v1/corporate-actions':
            return httpx.Response(200, json={'corporate_actions': {}, 'next_page_token': None})
        if path == '/v2/stocks/bars':
            if q['timeframe'] == '1Day':
                return httpx.Response(200, json={'bars': {'ABC': rows, 'SPY': rows}})
            bars = [{'t': stamp, 'o': 110, 'h': 111, 'l': 109, 'c': 110, 'v': 1000}
                    for stamp in ['2026-10-26T14:00:00Z','2026-10-26T14:05:00Z','2026-10-26T14:10:00Z']]
            return httpx.Response(200, json={'bars': {'ABC': bars}})
        if path == '/v2/stocks/quotes/latest':
            return httpx.Response(200, json={'quotes': {'ABC': {'t':'2026-10-26T14:00:00Z','bp':109.99,'ap':110.01,'bs':10,'as':10}}})
        raise AssertionError(path)
    provider = AlpacaMarketData('key', 'secret', client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    scan = MarketService(provider).fetch_scan(now)
    assert scan['quality'] == 'stale'
    assert scan['bars']['ABC'][-1]['timestamp'] == '2026-10-26T14:10:00Z'
    assert len(scan['bars']['ABC']) == 2
    assert scan['candidates'][0]['security_id'] == 'stable-uuid'
    assert scan['quotes']['ABC']['volume_scope'] == 'single_venue_IEX'


def test_corporate_action_permission_failure_blocks_scan():
    def handler(request):
        if request.url.path == '/v2/assets':
            return httpx.Response(200, json=[])
        if request.url.path == '/v2/stocks/bars':
            return httpx.Response(200, json={'bars': {'SPY': daily_rows()}})
        return httpx.Response(403, json={'message': 'forbidden'})
    provider = AlpacaMarketData('key', 'secret', client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    scan = MarketService(provider).fetch_scan('2026-10-26T14:00:00Z')
    assert scan['quality'] == 'unavailable'
    assert provider.action_status == 'unavailable'


def test_missing_recent_daily_session_is_not_silently_skipped():
    rows = daily_rows()
    del rows[-10]
    assert len(rows) >= 120
    assert daily_indicators(rows, '2026-10-26') is None


def test_configured_listing_age_preserves_indicator_warmup_floor():
    rows = daily_rows()
    assert daily_indicators(rows[-70:], '2026-10-26') is None
    assert daily_indicators(rows[-70:], '2026-10-26', min_listing_sessions=70)['required_history_sessions'] == 70
    assert daily_indicators(rows[-61:], '2026-10-26', min_listing_sessions=1)['required_history_sessions'] == 61
    assert daily_indicators(rows[-60:], '2026-10-26', min_listing_sessions=1) is None


@pytest.mark.parametrize('http_status', [401, 403])
def test_authentication_and_entitlement_failures_are_not_retried_or_downgraded(http_status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(http_status, json={'code': 40010001, 'message': 'test-secret sensitive response'})
    provider = AlpacaMarketData('key', 'test-secret', feed='sip',
        client=httpx.Client(transport=httpx.MockTransport(handler)), retries=3, sleep=lambda _: None)
    with pytest.raises(MarketDataError) as failure:
        provider.quotes(['SPY'])
    assert len(calls) == 1
    assert calls[0].url.params['feed'] == 'sip'
    assert failure.value.http_status == http_status
    assert failure.value.error_code == '40010001'
    assert failure.value.endpoint == '/v2/stocks/quotes/latest'
    assert 'test-secret' not in str(failure.value)
    assert 'sensitive response' not in str(provider.endpoint_status)
    assert provider.endpoint_status['/v2/stocks/quotes/latest']['status'] == 'failed'


def test_asset_access_does_not_verify_failed_market_data_access():
    def handler(request):
        if request.url.path == '/v2/assets':
            return httpx.Response(200, json=[])
        return httpx.Response(403, json={'code': 40310000, 'message': 'restricted'})
    provider = AlpacaMarketData('key', 'secret',
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    service = MarketService(provider)
    provider.assets()
    assert provider.last_success and not provider.last_data_success
    assert service.status()['connection_status'] == 'unverified'
    scan = service.fetch_scan('2026-10-26T14:00:00Z')
    assert scan['quality'] == 'unavailable'
    assert service.status()['connection_status'] == 'failed'
    assert scan['metadata']['connection_status'] == 'failed'
    assert service.status()['request_connection_status'] == 'failed'
    assert not service.readiness()['ready']


@pytest.mark.parametrize('kwargs', [
    {'data_url': 'http://data.alpaca.markets'},
    {'data_url': 'https://data.alpaca.markets.attacker.invalid'},
    {'data_url': 'https://attacker.invalid'},
    {'trading_url': 'https://paper-api.alpaca.markets@attacker.invalid'},
    {'trading_url': 'https://paper-api.alpaca.markets/path'},
])
def test_credentials_cannot_be_sent_to_untrusted_configuration(kwargs):
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request)))
    with pytest.raises(ValueError, match='official HTTPS'):
        AlpacaMarketData('key', 'secret', client=client, **kwargs)
    assert not requests


def test_redirects_and_arbitrary_get_destinations_never_receive_credentials():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'https://attacker.invalid'})
    provider = AlpacaMarketData('key', 'secret',
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), sleep=lambda _: None)
    with pytest.raises(MarketDataError) as failure:
        provider.assets()
    assert failure.value.http_status == 302
    assert len(calls) == 1 and calls[0].url.host == 'paper-api.alpaca.markets'
    with pytest.raises(MarketDataError, match='not permitted'):
        provider._get('https://attacker.invalid', '/v2/assets', {})
    with pytest.raises(MarketDataError, match='not permitted'):
        provider._get(provider.data_url, '/sensitive-secret-path', {})
    assert len(calls) == 1
    assert 'sensitive-secret-path' not in provider.last_error


@pytest.mark.parametrize('now,expected_intraday_calls', [
    ('2026-10-26T12:00:00Z', 0), ('2026-10-26T21:00:00Z', 1),
])
def test_closed_session_scan_is_connected_but_not_execution_ready(now, expected_intraday_calls):
    intraday_calls = []
    def handler(request):
        path, query = request.url.path, request.url.params
        if path == '/v2/assets':
            return httpx.Response(200, json=[{'id':'stable-uuid', 'symbol':'ABC', 'name':'ABC Common Stock',
                'class':'us_equity', 'exchange':'NYSE', 'status':'active', 'tradable':True}])
        if path == '/v1/corporate-actions':
            return httpx.Response(200, json={'corporate_actions': {}})
        if path == '/v2/stocks/bars':
            if query['timeframe'] == '1Day':
                return httpx.Response(200, json={'bars': {'ABC': daily_rows(), 'SPY': daily_rows()}})
            intraday_calls.append(request)
            assert query['start'] == '2026-10-26T13:30:00Z'
            assert query['end'] == '2026-10-26T20:00:00Z'
            return httpx.Response(200, json={'bars': {'ABC': [{'t':'2026-10-26T19:55:00Z',
                'o':110, 'h':111, 'l':109, 'c':110, 'v':1000}]}})
        if path == '/v2/stocks/quotes/latest':
            return httpx.Response(200, json={'quotes': {'ABC': {'t':'2026-10-26T20:00:00Z', 'bp':109.99, 'ap':110.01}}})
        raise AssertionError(path)
    provider = AlpacaMarketData('key', 'secret',
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    service = MarketService(provider)
    scan = service.fetch_scan(now)
    assert scan['quality'] == 'valid'
    assert len(intraday_calls) == expected_intraday_calls
    assert service.status()['connection_status'] == 'market_closed'
    assert scan['metadata']['connection_status'] == 'market_closed'
    assert not service.readiness()['ready']
    assert service.readiness()['reason'] == 'market_closed'


@pytest.mark.parametrize('started,bars_received,quote_time,quotes_received,bar_starts,expected_end,is_open', [
    ('2026-10-26T14:10:20Z', '2026-10-26T14:10:21Z', '2026-10-26T14:10:22Z',
     '2026-10-26T14:10:24Z', ['14:05', '14:10'], '14:10', True),
    # A partial bar fetched before the five-minute boundary must not become
    # admissible just because the quote response arrives after that boundary.
    ('2026-10-26T14:09:59Z', '2026-10-26T14:10:01Z', '2026-10-26T14:10:02Z',
     '2026-10-26T14:10:03Z', ['14:00', '14:05'], '14:05', True),
    # Receiving the quote after the closing bell closes execution immediately.
    ('2026-10-26T19:59:59Z', '2026-10-26T20:00:01Z', '2026-10-26T20:00:01Z',
     '2026-10-26T20:00:02Z', ['19:50', '19:55'], '19:55', False),
])
def test_live_scan_uses_receipt_time_without_admitting_partial_bars(
        monkeypatch, started, bars_received, quote_time, quotes_received, bar_starts, expected_end, is_open):
    clock = [as_utc(started)]
    monkeypatch.setattr('trading.market.as_utc', lambda value=None: as_utc(value) if value is not None else clock[0])

    def handler(request):
        path, query = request.url.path, request.url.params
        if path == '/v2/assets':
            return httpx.Response(200, json=[{'id': 'stable-uuid', 'symbol': 'ABC', 'name': 'ABC Common Stock',
                'class': 'us_equity', 'exchange': 'NYSE', 'status': 'active', 'tradable': True}])
        if path == '/v1/corporate-actions':
            return httpx.Response(200, json={'corporate_actions': {}})
        if path == '/v2/stocks/bars':
            if query['timeframe'] == '1Day':
                return httpx.Response(200, json={'bars': {'ABC': daily_rows(), 'SPY': daily_rows()}})
            assert query['end'] == started
            clock[0] = as_utc(bars_received)
            return httpx.Response(200, json={'bars': {'ABC': [
                {'t': f'2026-10-26T{stamp}:00Z', 'o': 110, 'h': 111, 'l': 109, 'c': 110, 'v': 1000}
                for stamp in bar_starts]}})
        if path == '/v2/stocks/quotes/latest':
            clock[0] = as_utc(quotes_received)
            return httpx.Response(200, json={'quotes': {'ABC': {'t': quote_time, 'bp': 109.99, 'ap': 110.01}}})
        raise AssertionError(path)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AlpacaMarketData('key', 'secret', client=client, sleep=lambda _: None)
        service = MarketService(provider)
        scan = service.fetch_scan()
    assert scan['quality'] == 'valid'
    assert scan['started_at'] == started
    assert scan['timestamp'] == scan['received_at'] == quotes_received
    assert scan['market_open'] is is_open
    assert scan['calendar']['is_open'] is is_open
    assert service.readiness()['ready'] is is_open
    assert scan['quotes']['ABC']['quality'] == 'valid'
    assert scan['quotes']['ABC']['age_seconds'] == (as_utc(quotes_received) - as_utc(quote_time)).total_seconds()
    assert scan['quotes']['ABC']['received_at'] == quotes_received
    assert len(scan['bars']['ABC']) == 1
    assert scan['bars']['ABC'][0]['timestamp'] == f'2026-10-26T{expected_end}:00Z'
    assert scan['bars']['ABC'][0]['received_at'] == bars_received
