"""Strategy-only setup over HTTP and worker policy dispatch; all brokers are fake."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from trading.app import create_app
from trading.live_execution import LiveExecutionService
from trading.live_policy import managed_configuration
from trading.runtime import RuntimeStore
from trading.strategy import DEFAULT_PARAMS
from trading.worker import scan_cycle
from trading.strategy_registry import entry_feasibility
from test_broker_connection_api import FakeOAuth
from test_live_execution import FakeAdapter


@pytest.fixture
def managed_app(tmp_path, monkeypatch):
    from trading import reversal_runtime
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {
        'status': 'ready', 'candidates': [], 'blocked_reasons': [], 'universe_count': 190})
    adapter = FakeAdapter()
    adapter.now = datetime.now(timezone.utc)
    live = LiveExecutionService(tmp_path / 'live.sqlite3', adapter, write_enabled=True)
    oauth = FakeOAuth()
    oauth.authorized = True
    app = create_app('sqlite:///' + str(tmp_path / 'main.db'), 'sqlite:///' + str(tmp_path / 'demo.db'),
                     'alpaca', oauth=oauth, live_service=live)
    return app, live, adapter, oauth


def test_prepare_v2_needs_only_strategy_and_never_arms_or_writes(managed_app):
    app, live, adapter, _ = managed_app
    with TestClient(app) as client:
        client.post('/api/strategies/select', json={'strategy_id': 'reversal_v2'}).raise_for_status()
        result = client.post('/api/live/prepare-strategy', json={'strategy_id': 'reversal_v2'})
        assert result.status_code == 200, result.text
        state = result.json()
        cfg = state['configuration']
        assert cfg['scope_mode'] == 'strategy_managed'
        assert cfg['strategy_id'] == 'reversal_v2' and len(cfg['symbols']) == 190
        assert cfg['max_positions'] == 20
        assert cfg['max_order_amount'] == '50.00' and cfg['max_daily_added'] == '200.00'
        assert state['armed'] is False
        assert not adapter.submissions and not adapter.cancellations and not adapter.previews
        again = client.post('/api/live/prepare-strategy', json={'strategy_id': 'reversal_v2'}).json()
        assert again['configuration_revision'] == state['configuration_revision']
        policies = client.get('/api/strategies').json()['strategies']
        v2 = next(p for p in policies if p['id'] == 'reversal_v2')['execution_policy']
        assert v2['universe_count'] == 190 and v2['max_entries_per_day'] == 4
        assert 'execution_params' not in v2 and 'security_master' not in v2
        assert live.snapshot()['positions'] == []


@pytest.mark.parametrize('extra', [{'symbols': ['AAPL']}, {'max_order_amount': '1000000'},
                                  {'account_number': 'forged'}, {'policy_hash': 'forged'}])
def test_strategy_prepare_rejects_scope_or_account_overrides(managed_app, extra):
    app, live, adapter, _ = managed_app
    with TestClient(app) as client:
        call = Mock(wraps=live.prepare_strategy)
        live.prepare_strategy = call
        response = client.post('/api/live/prepare-strategy', json={'strategy_id': 'reversal_v2', **extra})
        assert response.status_code == 422
        call.assert_not_called()
        assert not adapter.submissions


def test_strategy_prepare_rejects_retired_strategy_and_missing_authorization(managed_app):
    app, live, _, oauth = managed_app
    with TestClient(app) as client:
        call = Mock(wraps=live.prepare_strategy)
        live.prepare_strategy = call
        assert client.post('/api/live/prepare-strategy', json={'strategy_id': 'trend_breakout'}).status_code == 422
        call.assert_not_called()
        oauth.authorized = False
        assert client.post('/api/live/prepare-strategy', json={'strategy_id': 'reversal_v2'}).status_code == 409
        call.assert_not_called()


def test_retired_worker_scope_cannot_scan_or_transfer_existing_positions(tmp_path, monkeypatch):
    from trading import reversal_runtime
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {
        'status': 'ready', 'candidates': [], 'blocked_reasons': []})
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'runtime.db'))
    store.put('active_strategy', {'id': 'trend_breakout'})
    scope = managed_configuration('trend_breakout', FakeAdapter().data['account'])
    engine = SimpleNamespace(snapshot=lambda: {'settings': {'params': DEFAULT_PARAMS}},
                             scan=Mock(side_effect=AssertionError('retired trend scan ran')))
    market = SimpleNamespace(provider=SimpleNamespace(feed='iex'),
                             fetch_scan=Mock(side_effect=AssertionError('retired trend quotes fetched')))
    positions = [{'symbol': 'AAPL', 'quantity': '1', 'strategy_id': 'trend_breakout'}]
    intents = [{'symbol': 'AAPL', 'status': 'SUBMITTED', 'strategy_id': 'trend_breakout'}]
    live = SimpleNamespace(snapshot=lambda: {'control': {'armed': True}, 'configuration': scope,
                                            'positions': positions, 'intents': intents},
                           scan=Mock(side_effect=AssertionError('retired strategy may not submit')),
                           reconcile=Mock())
    result = scan_cycle(engine, market, market, live, store)
    assert result['strategy_id'] == 'reversal_v2'
    assert result['live']['status'] == 'STRATEGY_RETIRED'
    assert result['live']['orders_sent'] == 0 and result['live']['read_only_sync']
    live.reconcile.assert_called_once()
    assert scope['strategy_id'] == positions[0]['strategy_id'] == intents[0]['strategy_id'] == 'trend_breakout'
    assert store.get('active_strategy')['id'] == 'trend_breakout'  # No silent authorization migration.


def test_whole_share_feasibility_is_reference_only_and_includes_execution_costs():
    cfg = managed_configuration('reversal_v2', {'total_value': '100', 'cash': '100',
             'buying_power': '100', 'unleveraged_buying_power': '100'})
    account = {'execution_buying_power': '100'}
    data = {'status': 'ready', 'data_session': '2026-10-28', 'candidates': [{'raw_close': '5'}]}
    blocked = entry_feasibility(data, cfg, account)
    assert blocked['status'] == 'no_affordable_candidates'  # $5 close plus offset and fee exceeds $5.
    assert blocked['estimated_affordable_count'] == 0 and blocked['execution_quote_required']
    data['candidates'].append({'raw_close': '4.90'})
    ready = entry_feasibility(data, cfg, account)
    assert ready['status'] == 'some_affordable_candidates' and ready['estimated_affordable_count'] == 1
    assert entry_feasibility({**data, 'status': 'blocked'}, cfg, account)['status'] == 'data_unavailable'
    assert entry_feasibility({**data, 'candidates': []}, cfg, account)['status'] == 'no_candidates'


def test_retired_managed_position_is_not_adopted_by_v2(managed_app):
    app, live, adapter, _ = managed_app
    with TestClient(app) as client:
        app.state.store.put('active_strategy', {'id': 'trend_breakout'})
        with live._locked() as db:
            live._save(db, 'live_positions', 'AAPL', {'symbol': 'AAPL', 'quantity': '1',
                       'strategy_id': 'trend_breakout'})
            db.commit()
        before = live.snapshot()
        assert client.get('/api/strategies').json()['active_strategy']['id'] == 'reversal_v2'
        assert client.post('/api/strategies/select', json={'strategy_id': 'reversal_v2'}).status_code == 400
        assert client.post('/api/live/prepare-strategy', json={'strategy_id': 'reversal_v2'}).status_code == 400
        after = live.snapshot()
        assert after['configuration'] == before['configuration']
        assert after['positions'] == before['positions']
        assert not after['control']['armed']
        assert not adapter.submissions and not adapter.cancellations and not adapter.previews
