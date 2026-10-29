"""Commissioning verifies real endpoint contracts without sending broker orders."""
from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from trading import alpaca_verify
from trading.calendar import as_utc, calendar_for_year, iso
from trading.market import AlpacaMarketData, MarketService
from trading.runtime import RuntimeStore


OPEN_TIME = '2026-10-26T14:10:20Z'
CLOSED_TIME = '2026-10-26T12:00:00Z'
API_KEY = 'verify-test-api-key'
SECRET = 'verify-test-secret'


def daily_rows():
    sessions = calendar_for_year(2026).sessions_in_range('2026-04-01', '2026-10-23')
    return [{'t': f'{day.date()}T04:00:00Z', 'o': 90 + i / 10,
             'h': 92 + i / 10, 'l': 89 + i / 10, 'c': 91 + i / 10, 'v': 1_000_000}
            for i, day in enumerate(sessions)]


@pytest.fixture
def provider_factory():
    providers = []

    def make(*, now=OPEN_TIME, quote_age=5, overrides=None, **kwargs):
        calls = []

        def handler(request):
            calls.append(request)
            path, query = request.url.path, request.url.params
            if path in (overrides or {}):
                override = overrides[path]
                return override(request) if callable(override) else override
            if path == '/v2/account':
                return httpx.Response(200, json={'status': 'ACTIVE', 'id': 'private-account-id',
                                               'cash': '99999.99', 'email': 'private@example.test'})
            if path == '/v2/assets':
                return httpx.Response(200, json=[
                    {'id': f'uuid-{symbol}', 'symbol': symbol, 'status': 'active',
                     'tradable': True, 'class': 'us_equity', 'exchange': 'NASDAQ'}
                    for symbol in ('AAPL', 'SPY')])
            if path == '/v2/stocks/bars':
                symbols = query['symbols'].split(',')
                if query['timeframe'] == '1Day':
                    rows = daily_rows()
                else:
                    step = timedelta(minutes=5 if query['timeframe'] == '5Min' else 1)
                    stamp = as_utc(query['start'])
                    end = as_utc(query['end']) + timedelta(microseconds=1)
                    rows = []
                    while stamp + step <= end:
                        rows.append({'t': iso(stamp), 'o': 110, 'h': 111, 'l': 109,
                                     'c': 110, 'v': 1000})
                        stamp += step
                return httpx.Response(200, json={'bars': {symbol: rows for symbol in symbols}})
            if path == '/v2/stocks/quotes/latest':
                return httpx.Response(200, json={'quotes': {
                    symbol: {'t': iso(as_utc(now) - timedelta(seconds=quote_age)),
                             'bp': 109.99, 'ap': 110.01, 'bs': 10, 'as': 10}
                    for symbol in query['symbols'].split(',')}})
            if path == '/v1/corporate-actions':
                return httpx.Response(200, json={'corporate_actions': {}})
            raise AssertionError(f'Unexpected endpoint: {path}')

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = AlpacaMarketData(kwargs.pop('key', API_KEY), kwargs.pop('secret', SECRET),
                                   client=client, retries=0, sleep=lambda _: None, **kwargs)
        providers.append(provider)
        return provider, calls

    yield make
    for provider in providers:
        provider.close()


def check(report, name):
    return next(item for item in report['checks'] if item['name'] == name)


@pytest.mark.parametrize('key,secret', [('', ''), (API_KEY, ''), ('', SECRET)])
def test_missing_credentials_make_no_network_requests(provider_factory, key, secret):
    provider, calls = provider_factory(key=key, secret=secret)
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'credentials_required'
    assert check(report, 'credentials')['status'] == 'failed'
    assert not calls and provider.request_count == 0
    assert not report['readiness']['can_generate_orders']
    assert not report['connectivity_verified']


def test_qualified_open_session_verifies_data_with_get_requests_only(provider_factory):
    provider, calls = provider_factory()
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'passed'
    assert report['connectivity_verified']
    assert report['history_verified']
    assert report['selected_feed_verified']
    assert report['readiness']['can_generate_orders']
    assert report['symbols'] == ['AAPL', 'SPY']
    assert check(report, 'quote_freshness')['status'] == 'passed'
    assert check(report, 'current_five_minute_bars')['status'] == 'passed'
    assert all(count >= 120 for count in check(report, 'daily_bars')['counts'].values())
    assert {request.method for request in calls} == {'GET'}
    assert not any('orders' in request.url.path for request in calls)
    assert {request.url.host for request in calls} == {'paper-api.alpaca.markets', 'data.alpaca.markets'}
    assert all(request.headers['APCA-API-KEY-ID'] == API_KEY for request in calls)
    assert report['execution'] == {'mode': 'paper', 'broker': 'local_paper',
                                   'live_enabled': False, 'orders_sent': 0}
    serialized = json.dumps(report)
    for private in (API_KEY, SECRET, 'private-account-id', '99999.99', 'private@example.test'):
        assert private not in serialized


@pytest.mark.parametrize('error_code', ['40110000', SECRET])
def test_failed_authentication_redacts_response_and_stops_followup_requests(provider_factory, error_code):
    provider, calls = provider_factory(overrides={'/v2/account': httpx.Response(401, json={
        'code': error_code, 'message': f'{API_KEY} {SECRET} sensitive-provider-body'})})
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'failed'
    assert check(report, 'authentication')['http_status'] == 401
    assert [request.url.path for request in calls] == ['/v2/account']
    assert not report['connectivity_verified']
    assert not report['readiness']['can_generate_orders']
    serialized = json.dumps(report)
    assert all(private not in serialized for private in (API_KEY, SECRET, 'sensitive-provider-body'))
    assert check(report, 'authentication')['error_code'] == ('40110000' if error_code.isdigit() else None)


@pytest.mark.parametrize('feed', ['iex', 'sip'])
def test_feed_permission_failure_never_falls_back_to_a_different_feed(provider_factory, feed):
    denial = lambda _: httpx.Response(403, json={'code': 40310000, 'message': SECRET})
    provider, calls = provider_factory(feed=feed, overrides={
        '/v2/stocks/bars': denial, '/v2/stocks/quotes/latest': denial})
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'failed'
    assert report['connectivity_verified']
    assert not report['selected_feed_verified']
    assert not report['history_verified']
    assert not report['readiness']['can_generate_orders']
    assert check(report, 'latest_quotes')['http_status'] == 403
    market_requests = [request for request in calls if request.url.path.startswith('/v2/stocks/')]
    assert len(market_requests) == 4
    assert {request.url.params['feed'] for request in market_requests} == {feed}
    assert SECRET not in json.dumps(report)


def test_closed_session_verifies_history_but_never_claims_intraday_readiness(provider_factory):
    provider, calls = provider_factory(now=CLOSED_TIME, quote_age=100_000)
    report = alpaca_verify.verify(provider, now=CLOSED_TIME)
    assert report['status'] == 'partial'
    assert report['connectivity_verified'] and report['history_verified']
    assert report['selected_feed_verified']
    assert not report['session']['is_open']
    assert not report['readiness']['can_generate_orders']
    assert check(report, 'quote_freshness')['status'] == 'unverified'
    assert not any(item['status'] == 'failed' for item in report['checks'])
    assert not report['blockers']
    intraday_calls = [request for request in calls if request.url.path == '/v2/stocks/bars'
                      and request.url.params['timeframe'] != '1Day']
    assert len(intraday_calls) == 2
    assert all(request.url.params['start'].startswith('2026-10-23') for request in intraday_calls)


@pytest.mark.parametrize('quote_age', [31, 600, -1])
def test_stale_or_future_open_session_quotes_do_not_enable_orders(provider_factory, quote_age):
    provider, _ = provider_factory(quote_age=quote_age)
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'partial'
    assert report['connectivity_verified'] and report['history_verified']
    assert report['selected_feed_verified']
    assert check(report, 'quote_freshness')['status'] == 'unverified'
    assert not report['readiness']['can_generate_orders']


def test_corporate_action_permission_failure_blocks_otherwise_fresh_data(provider_factory):
    provider, _ = provider_factory(overrides={'/v1/corporate-actions': httpx.Response(
        403, json={'code': 40310000, 'message': f'{SECRET} entitlement unavailable'})})
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'failed'
    assert check(report, 'corporate_actions')['http_status'] == 403
    assert check(report, 'quote_freshness')['status'] == 'passed'
    assert report['history_verified'] and report['selected_feed_verified']
    assert not report['readiness']['can_generate_orders']
    assert report['blockers']
    assert SECRET not in json.dumps(report)


def test_live_trading_host_is_rejected_without_any_network_request(provider_factory):
    provider, calls = provider_factory(trading_url='https://api.alpaca.markets')
    report = alpaca_verify.verify(provider, now=OPEN_TIME)
    assert report['status'] == 'failed'
    assert check(report, 'execution_safety')['status'] == 'failed'
    assert not report['readiness']['can_generate_orders']
    assert not calls and provider.request_count == 0


def test_run_verification_persists_only_sanitized_report_and_closes_client(provider_factory, monkeypatch, tmp_path):
    provider, calls = provider_factory(overrides={'/v2/account': httpx.Response(401, json={
        'code': 40110000, 'message': f'{API_KEY} {SECRET} private-account-id'})})
    monkeypatch.setattr(MarketService, 'from_env', lambda: SimpleNamespace(provider=provider))
    store = RuntimeStore(f'sqlite:///{tmp_path / "verification.db"}')
    output = tmp_path / 'verification.json'
    report = alpaca_verify.run_verification(store, output_path=output)
    assert report['status'] == 'failed'
    assert store.get('market_verification') == report
    assert json.loads(output.read_text()) == report
    assert not output.with_suffix('.tmp').exists()
    assert provider.client.is_closed
    assert len(calls) == 1
    for private in (API_KEY, SECRET, 'private-account-id'):
        assert private not in output.read_text()
    store.engine.dispose()


def test_invalid_configuration_is_persisted_as_sanitized_failure(monkeypatch, tmp_path):
    def invalid_configuration():
        raise ValueError(f'{SECRET} invalid endpoint')

    monkeypatch.setattr(MarketService, 'from_env', invalid_configuration)
    output = tmp_path / 'invalid.json'
    report = alpaca_verify.run_verification(output_path=output)
    assert report['status'] == 'failed'
    assert check(report, 'configuration')['status'] == 'failed'
    assert not report['readiness']['can_generate_orders']
    assert SECRET not in output.read_text()
