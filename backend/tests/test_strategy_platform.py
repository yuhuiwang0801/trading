"""Platform V2 dispatch and commissioning, with no network or broker writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from trading import config, reversal_runtime
from trading.reversal_dispatch import quote_frame
from trading.runtime import RuntimeStore
from trading.strategy_registry import daily_data_ready
from trading.worker import scan_cycle
from trading.research_worker import schedule_reversal_refresh
from test_live_api import setup

NOW = datetime(2026, 10, 27, 13, 36, tzinfo=timezone.utc)
PACKET = {'status': 'ready', 'quality': 'ready', 'strategy_id': 'reversal_v2',
          'data_session': '2026-10-26', 'signal_session': '2026-10-26',
          'execute_session': '2026-10-27', 'created_at': '2026-10-26T22:00:00Z',
          'blocked_reasons': [], 'candidates': [{'symbol': 'XYZ', 'score': .04, 'entry_gap': .04,
              'raw_close': 10, 'raw_return': -.03, 'peer_return': .01}]}


@pytest.fixture(autouse=True)
def isolated_packet(monkeypatch):
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: deepcopy(PACKET))


def test_selection_persists_without_broker_access_or_scope_expansion(setup):
    app, live, remote, oauth = setup
    with TestClient(app) as client:
        client.post('/api/live/verify', json={}).raise_for_status()
        client.post('/api/live/config', json={'symbols': ['AAPL'], 'max_order_amount': '110',
                    'max_daily_added': '110', 'max_positions': 3}).raise_for_status()
        before = live.snapshot()['configuration']
        remote.calls.clear()
        oauth.authorized = False  # Strategy selection does not require broker access.
        result = client.post('/api/strategies/select', json={'strategy_id': 'reversal_v2'})
        assert result.status_code == 200, result.text
        assert result.json()['active_strategy']['id'] == 'reversal_v2'
        assert result.json()['live_armed'] is False
        assert result.json()['authorized_symbols'] == ['AAPL']
        after = live.snapshot()['configuration']
        for key in ('symbols', 'max_order_amount', 'max_daily_added', 'max_positions'):
            assert after[key] == before[key]
        assert after['revision'] == before['revision']  # Scope was already configured for the default V2.
        assert after['strategy_id'] == 'reversal_v2'
        assert app.state.store.get('active_strategy')['id'] == 'reversal_v2'
        assert client.get('/api/state').json()['candidates'][0]['symbol'] == 'XYZ'
        assert remote.calls == []
        first_job = result.json()['refresh_job']['id']
        assert client.post('/api/strategies/refresh', json={}).json()['id'] == first_job
        assert not client.get('/api/live').json()['readiness']['ready_to_arm']
        assert client.post('/api/strategies/select', json={'strategy_id': 'made_up'}).status_code == 422


def test_v2_prepare_carries_selected_strategy_and_failure_does_not_select(setup):
    app, live, remote, _ = setup
    with TestClient(app) as client:
        client.post('/api/strategies/select', json={'strategy_id': 'reversal_v2'}).raise_for_status()
        result = client.post('/api/live/prepare', json={'symbols': ['AAPL'], 'max_order_amount': '110',
                       'max_daily_added': '110', 'max_positions': 3})
        assert result.status_code == 200, result.text
        assert result.json()['configuration']['strategy_id'] == 'reversal_v2'
        assert not result.json()['armed']
        with live._locked() as db:
            control = live._get(db, 'control')
            live._put(db, 'control', {**control, 'armed': True})
            db.commit()
        refused = client.post('/api/strategies/select', json={'strategy_id': 'trend_breakout'})
        assert refused.status_code == 422
        assert client.get('/api/strategies').json()['active_strategy']['id'] == 'reversal_v2'
        assert all(name.startswith('get_') or name == 'review_equity_order' for name, _ in remote.calls)


def test_expired_window_is_valid_data_but_not_a_new_entry_permission():
    assert daily_data_ready({**PACKET, 'status': 'expired', 'blocked_reasons': ['execution_window_expired']})
    assert not daily_data_ready({**PACKET, 'status': 'expired', 'blocked_reasons': ['stale_signal_session']})
    assert not daily_data_ready({**PACKET, 'status': 'blocked'})


@pytest.mark.parametrize('status,positions,expected', [
    ('expired', [], 200), ('blocked', [], 409), ('blocked', [{'symbol': 'OWNED'}], 200),
])
def test_activation_data_gate_allows_existing_exit_recovery_only(setup, monkeypatch, status, positions, expected):
    from trading.live_runtime import LIVE_CONTROL_VERSION, live_control_identity
    from trading.runtime import utcnow
    app, live, remote, _ = setup
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {**PACKET,
        'status': status, 'blocked_reasons': ['execution_window_expired'] if status == 'expired' else ['missing_data']})
    with TestClient(app) as client:
        app.state.store.put('active_strategy', {'id': 'reversal_v2'})
        app.state.store.put('worker', {'status': 'running', 'heartbeat': utcnow().isoformat(),
            'source': 'alpaca', 'live_control_version': LIVE_CONTROL_VERSION,
            'live_control_id': live_control_identity(live)})
        # The test isolates HTTP readiness gating; the service's actual arm and
        # execution guards have separate fake-broker coverage.
        monkeypatch.setattr(live, 'snapshot', lambda: {'configuration': {'strategy_id': 'reversal_v2', 'revision': 1},
             'control': {'armed': False}, 'positions': positions, 'intents': [],
             'readiness': {'ready_to_arm': True, 'server_write_enabled': True}, 'orders': []})
        monkeypatch.setattr(live, 'readiness', lambda: {'server_write_enabled': True})
        arm_spy = Mock()
        monkeypatch.setattr(live, 'arm', arm_spy)
        result = client.post('/api/live/arm', json={'confirmation': 'ENABLE LIVE AUTO TRADING', 'configuration_revision': 1})
        assert result.status_code == expected, result.text
        assert arm_spy.call_count == int(expected == 200)
        assert client.get('/api/live').json()['readiness']['ready'] is (expected == 200)
        assert remote.calls == []


class QuoteProvider:
    feed = 'iex'

    def __init__(self): self.requested = []

    def assets(self):
        return [{'symbol': s, 'tradable': True, 'status': 'active'} for s in ('XYZ', 'HOLD', 'PENDING')]

    def quotes(self, symbols):
        self.requested = symbols
        return {s: {'bp': 10, 'ap': 10.02, 'bs': 100, 'as': 100, 't': NOW.isoformat()} for s in symbols}


def test_quote_dispatch_preserves_full_peer_packet_and_monitors_exit_symbols():
    provider = QuoteProvider()
    packet = {**PACKET, 'candidates': [*PACKET['candidates'], {'symbol': 'OUTSIDE', 'score': .06}]}
    result = quote_frame(provider, packet, {'symbols': ['XYZ']}, [{'symbol': 'HOLD'}],
                         [{'symbol': 'PENDING', 'status': 'SUBMITTED'}], NOW)
    assert provider.requested == ['HOLD', 'PENDING', 'XYZ']
    assert [c['symbol'] for c in result['candidates']] == ['XYZ']
    assert len(result['reversal_packet']['candidates']) == 2
    assert result['quotes']['HOLD']['quality'] == 'valid'
    assert result['quotes']['HOLD']['last_source'] == 'current_quote_midpoint'
    assert result['metadata'] == {'daily_signal_feed': 'sip', 'execution_quote_feed': 'iex'}
    assert result['bars'] == {}


def test_closed_market_dispatch_does_not_request_quotes():
    provider = QuoteProvider()
    provider.assets = Mock(side_effect=AssertionError('network called'))
    result = quote_frame(provider, PACKET, {'symbols': ['XYZ']}, now=NOW - timedelta(hours=7))
    assert not result['market_open'] and not result['quotes']


def test_v2_worker_never_runs_legacy_trend_scan_or_fetches_quotes_while_stopped(tmp_path):
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'state.db'))
    store.put('active_strategy', {'id': 'reversal_v2'})
    engine = SimpleNamespace(snapshot=lambda: {'settings': {'params': {}}},
                             scan=Mock(side_effect=AssertionError('trend scan called')))
    market = SimpleNamespace(provider=SimpleNamespace(feed='iex'),
                             fetch_scan=Mock(side_effect=AssertionError('trend data requested')))
    live = SimpleNamespace(snapshot=lambda: {'control': {'armed': False}, 'positions': [], 'intents': []},
                           scan=Mock(side_effect=AssertionError('stopped live scan called')))
    result = scan_cycle(engine, market, market, live, store)
    assert result['status'] == 'SUCCESS' and result['live']['orders_sent'] == 0
    assert store.get('last_market_scan')['strategy_id'] == 'reversal_v2'
    assert store.get('paper_equity') is None


def test_v2_worker_passes_blocked_packet_to_exits_and_marks_manual_holdings(tmp_path, monkeypatch):
    from trading import reversal_dispatch
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'state.db'))
    store.put('active_strategy', {'id': 'reversal_v2'})
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {**PACKET, 'status': 'blocked'})
    bridge = Mock(return_value={'strategy_id': 'reversal_v2'})
    monkeypatch.setattr(reversal_dispatch, 'quote_frame', bridge)
    live = SimpleNamespace(snapshot=lambda: {'control': {'armed': True}, 'configuration': {'symbols': ['XYZ'], 'strategy_id': 'reversal_v2'},
         'positions': [{'symbol': 'OWNED'}], 'broker_positions': [{'symbol': 'MANUAL'}], 'intents': []},
         scan=Mock(return_value={'orders_sent': 0}))
    engine = SimpleNamespace(snapshot=lambda: {'settings': {'params': {}}})
    market = SimpleNamespace(provider=SimpleNamespace(feed='iex'))
    result = scan_cycle(engine, market, market, live, store)
    assert result['status'] == 'DEGRADED'
    assert bridge.call_args.args[3] == [{'symbol': 'OWNED'}, {'symbol': 'MANUAL'}]
    live.scan.assert_called_once()


def test_daily_scheduler_deduplicates_retries_and_does_not_rebuild_expired_data(tmp_path, monkeypatch):
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'state.db'))
    store.put('active_strategy', {'id': 'reversal_v2'})
    monkeypatch.setattr(config, 'SOURCE', 'alpaca')
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {'status': 'blocked'})
    first = schedule_reversal_refresh(store, NOW)
    assert first['kind'] == 'reversal_refresh'
    assert schedule_reversal_refresh(store, NOW + timedelta(minutes=1)) is None
    assert schedule_reversal_refresh(store, NOW + timedelta(minutes=31))['id'] == first['id']
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw:
        {**PACKET, 'status': 'expired', 'blocked_reasons': ['execution_window_expired']})
    assert schedule_reversal_refresh(store, NOW + timedelta(hours=2)) is None



@pytest.mark.parametrize('saved', [None, {'id': 'trend_breakout'}, {'id': 'reversal_v2'}])
def test_public_catalog_retires_trend_without_changing_live_authority(setup, saved):
    app, live, remote, _ = setup
    with TestClient(app) as client:
        if saved is not None:
            app.state.store.put('active_strategy', saved)
        before = live.snapshot()
        result = client.get('/api/strategies').json()
        assert [item['id'] for item in result['strategies']] == ['reversal_v2']
        assert result['active_strategy']['id'] == 'reversal_v2'
        for endpoint in ('/api/strategies/select', '/api/live/prepare-strategy'):
            assert client.post(endpoint, json={'strategy_id': 'trend_breakout'}).status_code == 422
        after = live.snapshot()
        for key in ('configuration', 'control', 'positions', 'intents', 'orders', 'fills'):
            assert after[key] == before[key]
        assert remote.calls == []


def test_unknown_saved_strategy_still_fails_closed(tmp_path):
    from trading.strategy_registry import active_strategy
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'state.db'))
    store.put('active_strategy', {'id': 'unknown_strategy'})
    with pytest.raises(ValueError, match='no fallback execution'):
        active_strategy(store)
