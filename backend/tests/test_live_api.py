"""HTTP commissioning uses the real isolated service and a synthetic broker only."""
import json
from fastapi.testclient import TestClient
import pytest
from trading.app import create_app
from trading.live_execution import LiveExecutionService
from trading.robinhood_adapter import RobinhoodTradingAdapter
from trading.live_runtime import create_live_service, LIVE_CONTROL_VERSION, live_control_identity
from test_broker_connection_api import FakeOAuth
from test_robinhood_adapter import Client, ACCOUNT


@pytest.fixture(autouse=True)
def ready_v2_packet(monkeypatch):
    # API activation tests isolate the data-readiness gate from local artifacts.
    from trading import reversal_runtime
    monkeypatch.setattr(reversal_runtime, 'load_runtime_packet', lambda **kw: {
        'status': 'ready', 'candidates': [], 'blocked_reasons': []})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('ROBINHOOD_LIVE_ENABLED', 'false')
    remote = Client()
    oauth = FakeOAuth()
    oauth.authorized = True
    live = LiveExecutionService(tmp_path/'live.sqlite3', RobinhoodTradingAdapter(remote), write_enabled=False)
    app = create_app('sqlite:///'+str(tmp_path/'main.db'), 'sqlite:///'+str(tmp_path/'demo.db'),
                     'alpaca', oauth=oauth, live_service=live)
    return app, live, remote, oauth


def test_configuration_and_retired_preview_never_submit_or_mutate_paper_ledger(setup):
    app, live, remote, _ = setup
    before = app.state.engine.snapshot()
    with TestClient(app) as client:
        initial = client.get('/api/live').json()
        assert initial['enabled'] is False and initial['armed'] is False
        assert initial['configuration_revision'] == 0
        assert client.post('/api/live/verify', json={}).status_code == 200
        scope = {'symbols':['AAPL'], 'max_order_amount':'110', 'max_daily_added':'110', 'max_positions':3}
        configured = client.post('/api/live/config', json=scope)
        assert configured.status_code == 200, configured.text
        assert configured.json()['config']['symbols'] == ['AAPL']
        request = {'symbol':'AAPL','side':'BUY','quantity':'1','limit_price':'100.00'}
        result = client.post('/api/live/preview', json=request)
        assert result.status_code == 410, result.text
        status = client.get('/api/live').json()
        assert status['last_preview'] is None
        assert status['account_masked'] == '••••0001'
        assert status['configuration_revision'] == 1
        assert not status['readiness']['ready']
        assert ACCOUNT not in json.dumps(status)
        blocked = client.post('/api/live/arm', json={'confirmation':'ENABLE LIVE AUTO TRADING','configuration_revision':1})
        assert blocked.status_code == 409
        assert not client.get('/api/live').json()['armed']
        assert not client.get('/api/health').json()['live_enabled']
    after = app.state.engine.snapshot()
    for key in ('account','settings','positions','orders','fills','intents'):
        assert before[key] == after[key]
    assert all(name.startswith('get_') or name == 'review_equity_order' for name, _ in remote.calls)


@pytest.mark.parametrize('body', [
    {'symbol': 'AAPL', 'side': 'BUY', 'quantity': '1', 'limit_price': '100'},
    {'symbol': 'AAPL', 'side': 'SELL', 'type': 'market', 'dollar_amount': '10'},
    {'symbol': 'AAPL', 'side': 'BUY', 'type': 'market', 'quantity': '0.5'},
    {'account_number': 'PRIVATE', 'side': 'SELL_SHORT'},
    {},
])
def test_retired_preview_rejects_every_order_without_broker_access(setup, body):
    app, live, remote, oauth = setup
    with TestClient(app) as client:
        before = live.snapshot()
        assert client.post('/api/live/preview', json=body).status_code == 410
        assert client.post('/api/live/preview', json=body, headers={'Origin':'https://attacker.example'}).status_code == 403
        assert client.post('/api/live/preview', content=json.dumps(body), headers={'Content-Type':'text/plain'}).status_code == 410
        assert '/api/live/preview' not in client.get('/openapi.json').json()['paths']
        oauth.authorized = False
        assert client.post('/api/live/preview', json=body).status_code == 410
        assert client.post('/api/live/verify', json={}).status_code == 409
        assert remote.calls == []
        after = live.snapshot()
        for key in ('configuration', 'control', 'positions', 'intents', 'orders', 'fills', 'preview'):
            assert after[key] == before[key]


def test_disarm_needs_no_authorization_and_sends_no_broker_actions(setup):
    app, live, remote, oauth = setup
    oauth.authorized = False
    with TestClient(app) as client:
        result = client.post('/api/live/disarm', json={})
        assert result.status_code == 200
        assert result.json()['armed'] is False
        assert not remote.calls
        assert client.patch('/api/settings', json={'mode':'live'}).status_code == 400


def test_live_state_refresh_remains_separate_from_paper_state(setup):
    app, live, remote, _ = setup
    with TestClient(app) as client:
        checked = client.post('/api/live/verify', json={})
        assert checked.status_code == 200
        assert checked.json()['account']['buying_power'] == '20.0000'
        assert not checked.json()['positions']  # Existing broker holding is not adopted.
        state = client.get('/api/state').json()
        assert state['account']['id'] == 'paper-usd'
        assert state['live_execution']['account_masked'] == '••••0001'
        assert not state['positions']


@pytest.mark.parametrize('legacy_flag', [None, 'false', 'true'])
def test_activation_uses_ui_confirmation_without_env_and_restart_revokes_it(tmp_path, monkeypatch, legacy_flag):
    from trading.runtime import utcnow
    if legacy_flag is None:
        monkeypatch.delenv('ROBINHOOD_LIVE_ENABLED', raising=False)
    else:
        monkeypatch.setenv('ROBINHOOD_LIVE_ENABLED', legacy_flag)
    remote = Client()  # Synthetic broker; no HTTP transport exists in this test.
    remote.data['get_equity_orders']['orders'] = [row for row in remote.data['get_equity_orders']['orders']
                                                 if row['state'] == 'filled']
    oauth = FakeOAuth()
    oauth.authorized = True
    database = 'sqlite:///'+str(tmp_path/'main.db')
    live = create_live_service(database, client=remote, oauth=oauth)
    app = create_app(database, 'sqlite:///'+str(tmp_path/'demo.db'),
                     'alpaca', oauth=oauth, live_service=live)
    with TestClient(app) as client:
        initial = client.get('/api/live').json()
        assert initial['activation_available'] is True and initial['activation_mode'] == 'ui_confirmation'
        assert initial['armed'] is initial['enabled'] is False
        client.post('/api/live/prepare', json={'symbols':['AAPL'], 'max_order_amount':'110',
            'max_daily_added':'110', 'max_positions':3}).raise_for_status()
        body = {'confirmation':'ENABLE LIVE AUTO TRADING','configuration_revision':1}
        assert client.post('/api/live/arm', json=body).status_code == 409
        app.state.store.put('worker', {'status':'running','heartbeat':utcnow().isoformat(),'live_write_enabled':False})
        assert client.post('/api/live/arm', json=body).status_code == 409
        app.state.store.put('worker', {'status':'running','heartbeat':utcnow().isoformat(),
            'live_write_enabled':False, 'source':'alpaca','live_control_version':LIVE_CONTROL_VERSION,
            'live_control_id':live_control_identity(live)})
        assert client.get('/api/live').json()['readiness']['ready']
        assert client.post('/api/live/arm', json={**body,'confirmation':'yes'}).status_code == 400
        assert client.post('/api/live/arm', json={**body,'configuration_revision':2}).status_code == 400
        result = client.post('/api/live/arm', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['armed'] is True
        assert result.json()['enabled'] is True
        assert client.get('/api/health').json()['live_enabled'] is True
        assert client.get('/api/state').json()['settings']['mode'] == 'paper'
        stopped = client.post('/api/live/disarm', json={})
        assert stopped.json()['enabled'] is stopped.json()['armed'] is False
        assert not live._may_write()
        assert client.post('/api/live/arm', json=body).status_code == 200
    # A fresh API lifespan, even without a worker restart, revokes real activation.
    with TestClient(app) as client:
        assert client.get('/api/live').json()['armed'] is False
        assert not client.get('/api/health').json()['live_enabled']
    assert all(name.startswith('get_') or name == 'review_equity_order' for name, _ in remote.calls)


@pytest.mark.parametrize('worker_patch, blocker', [
    ({'live_control_version':None}, 'worker_restart_required'),
    ({'live_control_version':True}, 'worker_restart_required'),
    ({'live_control_id':'different-ledger'}, 'worker_ledger_mismatch'),
    ({'source':'demo'}, 'worker_source_mismatch'),
    ({'status':'offline'}, 'worker_offline'),
    ({'heartbeat':'invalid'}, 'worker_offline'),
    ({'heartbeat':'2999-01-01T00:00:00+00:00'}, 'worker_offline'),
    ({'heartbeat':'2000-01-01T00:00:00+00:00'}, 'worker_offline'),
    ({'heartbeat':'2026-10-27T14:00:00'}, 'worker_offline'),
])
def test_ui_cannot_arm_with_wrong_or_stale_worker(tmp_path, worker_patch, blocker):
    from trading.runtime import utcnow
    remote, oauth = Client(), FakeOAuth()
    oauth.authorized = True
    database = 'sqlite:///'+str(tmp_path/'main.db')
    live = create_live_service(database, client=remote, oauth=oauth)
    app = create_app(database, 'sqlite:///'+str(tmp_path/'demo.db'),'alpaca',oauth=oauth,live_service=live)
    worker = {'status':'running','heartbeat':utcnow().isoformat(),'source':'alpaca',
              'live_control_version':LIVE_CONTROL_VERSION,'live_control_id':live_control_identity(live), **worker_patch}
    with TestClient(app) as client:
        app.state.store.put('worker', worker)
        status = client.get('/api/live').json()
        assert blocker in status['readiness']['blockers'] and not status['readiness']['ready']
        assert client.post('/api/live/arm', json={'confirmation':'ENABLE LIVE AUTO TRADING',
            'configuration_revision':1}).status_code == 409
        assert not client.get('/api/live').json()['armed']
    assert remote.calls == []


def test_activation_route_retains_origin_auth_scope_guards_without_manual_preview(tmp_path):
    from trading.runtime import utcnow
    remote, oauth = Client(), FakeOAuth()
    oauth.authorized = True
    remote.data['get_equity_orders']['orders'] = [row for row in remote.data['get_equity_orders']['orders']
                                                 if row['state'] == 'filled']
    database = 'sqlite:///'+str(tmp_path/'main.db')
    live = create_live_service(database, client=remote, oauth=oauth)
    app = create_app(database, 'sqlite:///'+str(tmp_path/'demo.db'),'alpaca',oauth=oauth,live_service=live)
    body = {'confirmation':'ENABLE LIVE AUTO TRADING','configuration_revision':1}
    with TestClient(app) as client:
        app.state.store.put('worker', {'status':'running','heartbeat':utcnow().isoformat(),'source':'alpaca',
            'live_control_version':LIVE_CONTROL_VERSION,'live_control_id':live_control_identity(live)})
        assert client.post('/api/live/arm',json=body,headers={'Origin':'https://attacker.example'}).status_code == 403
        assert client.post('/api/live/arm',json=body).status_code == 409  # No confirmed V2 scope.
        client.post('/api/live/verify',json={}).raise_for_status()
        client.post('/api/live/config',json={'symbols':['AAPL'],'max_order_amount':'110',
            'max_daily_added':'110','max_positions':3}).raise_for_status()
        assert client.post('/api/live/arm',json=body).status_code == 200  # No manual preview required.
        assert client.post('/api/live/disarm',json={}).status_code == 200
        oauth.authorized = False
        assert client.post('/api/live/arm',json=body).status_code == 409
        assert client.get('/api/live').json()['enabled'] is False
    assert all(name.startswith('get_') for name,_ in remote.calls)


def test_prepare_combines_read_only_refresh_and_idempotent_scope_save(setup):
    app, live, remote, _ = setup
    before = app.state.engine.snapshot()
    scope = {'symbols':['AAPL'], 'max_order_amount':'110', 'max_daily_added':'110', 'max_positions':3}
    with TestClient(app) as client:
        result = client.post('/api/live/prepare', json=scope)
        assert result.status_code == 200, result.text
        state = result.json()
        assert state['configuration_revision'] == 1 and state['config']['symbols'] == ['AAPL']
        assert state['account_masked'] == '••••0001' and state['checked_at']
        assert state['armed'] is state['enabled'] is False
        assert state['last_preview'] is None
        assert state['intents'] == state['orders'] == state['fills'] == state['positions'] == []
        assert state['activation_mode'] == 'ui_confirmation'
        repeat = client.post('/api/live/prepare', json={**scope, 'max_order_amount':'110.00'})
        assert repeat.status_code == 200
        assert repeat.json()['configuration_revision'] == 1
        assert repeat.json()['config'] == state['config']
    assert remote.calls and all(name.startswith('get_') for name, _ in remote.calls)
    assert not live._may_write()
    after = app.state.engine.snapshot()
    for key in ('account','settings','positions','orders','fills','intents'):
        assert before[key] == after[key]


@pytest.mark.parametrize('patch,expected', [
    ({'symbols':[]}, 422), ({'symbols':['bad']}, 400),
    ({'max_order_amount':'NaN'}, 400), ({'max_daily_added':'0'}, 400),
    ({'max_positions':True}, 422), ({'account_number':'PRIVATE'}, 422),
    ({'confirmation':'ENABLE LIVE AUTO TRADING'}, 422),
])
def test_prepare_invalid_body_never_reads_or_changes_saved_scope(setup, patch, expected):
    app, live, remote, _ = setup
    scope = {'symbols':['AAPL'], 'max_order_amount':'110', 'max_daily_added':'110', 'max_positions':3}
    with TestClient(app) as client:
        client.post('/api/live/prepare', json=scope).raise_for_status()
        before = live.snapshot()
        calls = list(remote.calls)
        result = client.post('/api/live/prepare', json={**scope, **patch})
        assert result.status_code == expected, result.text
        after = live.snapshot()
        assert after['configuration'] == before['configuration']
        assert after['control'] == before['control']
        assert remote.calls == calls


def test_prepare_requires_authorization_alpaca_and_allowed_origin(setup, tmp_path):
    app, live, remote, oauth = setup
    scope = {'symbols':['AAPL'], 'max_order_amount':'110', 'max_daily_added':'110', 'max_positions':3}
    with TestClient(app) as client:
        assert client.post('/api/live/prepare', json=scope,
                           headers={'Origin':'https://attacker.example'}).status_code == 403
        oauth.authorized = False
        assert client.post('/api/live/prepare', json=scope).status_code == 409
        assert remote.calls == []
        assert live.snapshot()['configuration']['revision'] == 0
    oauth.authorized = True
    demo = create_app('sqlite:///'+str(tmp_path/'other.db'), 'sqlite:///'+str(tmp_path/'other-demo.db'),
                     'demo', oauth=oauth, live_service=live)
    with TestClient(demo) as client:
        assert client.post('/api/live/prepare', json=scope).status_code == 409
        assert remote.calls == []
        assert live.snapshot()['configuration']['revision'] == 0


def test_prepare_cannot_replace_or_disarm_an_armed_scope(tmp_path):
    from trading.runtime import utcnow
    remote, oauth = Client(), FakeOAuth()
    oauth.authorized = True
    remote.data['get_equity_orders']['orders'] = [row for row in remote.data['get_equity_orders']['orders']
                                                 if row['state'] == 'filled']
    database = 'sqlite:///'+str(tmp_path/'main.db')
    live = create_live_service(database, client=remote, oauth=oauth)
    app = create_app(database, 'sqlite:///'+str(tmp_path/'demo.db'),'alpaca',oauth=oauth,live_service=live)
    scope = {'symbols':['AAPL'], 'max_order_amount':'110', 'max_daily_added':'110', 'max_positions':3}
    with TestClient(app) as client:
        app.state.store.put('worker', {'status':'running','heartbeat':utcnow().isoformat(),'source':'alpaca',
            'live_control_version':LIVE_CONTROL_VERSION,'live_control_id':live_control_identity(live)})
        client.post('/api/live/prepare', json=scope).raise_for_status()
        client.post('/api/live/arm', json={'confirmation':'ENABLE LIVE AUTO TRADING',
                                         'configuration_revision':1}).raise_for_status()
        before, calls = live.snapshot(), list(remote.calls)
        result = client.post('/api/live/prepare', json={**scope, 'max_order_amount':'100'})
        assert result.status_code == 400
        after = live.snapshot()
        assert after['configuration'] == before['configuration']
        assert after['control'] == before['control'] and after['control']['armed']
        assert remote.calls == calls
    assert all(name.startswith('get_') for name, _ in remote.calls)
