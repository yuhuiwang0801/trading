import logging
import httpx
import pytest
from fastapi.testclient import TestClient
from trading.app import create_app, OAuthAccessLogFilter
from trading.mcp_broker import MCPClient, RobinhoodMCPBroker, TokenProvider


class FakeOAuth:
    def __init__(self):
        self.authorized = False
        self.completions = []
    def status(self):
        return {'authorized':self.authorized, 'configured':self.authorized,
                'expires_at':None, 'refresh_available':False, 'authorization_pending':False,
                'blocker':None if self.authorized else 'authorization_required'}
    def start(self, redirect_uri):
        assert redirect_uri == 'http://127.0.0.1:8010/broker/oauth/callback'
        return {'state':'test-state', 'authorization_url':'https://robinhood.com/oauth?state=test-state'}
    def complete(self, code, state, issuer):
        self.completions.append((code, state, issuer))
        self.authorized = True
        return self.status()
    def disconnect(self): self.authorized = False
    def cancel_pending(self, state): return True


@pytest.fixture
def connection_app(tmp_path):
    oauth = FakeOAuth()
    def no_broker_writes(request): pytest.fail('Connection management must not call broker trading APIs')
    broker = RobinhoodMCPBroker(client=MCPClient(http_client=httpx.Client(transport=httpx.MockTransport(no_broker_writes))))
    app = create_app('sqlite:///'+str(tmp_path/'real.db'), 'sqlite:///'+str(tmp_path/'paper.db'),
                     'demo', oauth=oauth, broker=broker)
    return app, oauth, broker


def test_oauth_callback_requires_same_browser_and_sanitizes_redirect(connection_app):
    app, oauth, broker = connection_app
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        baseline = client.get('/api/state').json()
        failed = client.get('/broker/oauth/callback?code=private-code&state=test-state', follow_redirects=False)
        assert not oauth.completions
        assert failed.headers['location'] == '/?broker=authorization_failed#settings'
        start = client.post('/api/broker/connect', json={})
        assert set(start.json()) == {'authorization_url'}
        assert 'HttpOnly' in start.headers['set-cookie'] and 'SameSite=lax' in start.headers['set-cookie']
        assert 'Path=/broker/oauth/callback' in start.headers['set-cookie']
        assert client.get('/api/broker/connection').json()['readiness']['stage'] == 'authorization_required'
        callback = client.get('/broker/oauth/callback?code=private-code&state=test-state&iss=expected', follow_redirects=False)
        assert callback.headers['location'] == '/?broker=connected#settings'
        assert callback.headers['cache-control'] == 'no-store'
        assert callback.headers['referrer-policy'] == 'no-referrer'
        assert len(oauth.completions) == 1
        assert 'private-code' not in callback.text
        assert not broker.client.initialized
        state = client.get('/api/broker/connection').json()
        assert state['readiness']['stage'] == 'discovery_required'
        assert state['readiness']['live_enabled'] is False
        assert client.patch('/api/settings', json={'mode':'live'}).status_code == 400
        assert client.get('/api/state').json()['settings'] == baseline['settings']
        assert client.get('/api/state').json()['orders'] == baseline['orders']


def test_callback_wrong_state_and_provider_error_do_not_exchange(connection_app):
    app, oauth, _ = connection_app
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        client.post('/api/broker/connect', json={})
        result = client.get('/broker/oauth/callback?code=secret&state=wrong', follow_redirects=False)
        assert 'authorization_failed' in result.headers['location'] and not oauth.completions
        client.post('/api/broker/connect', json={})
        client.get('/broker/oauth/callback?state=test-state&error=access_denied&error_description=private', follow_redirects=False)
        assert not oauth.completions
        assert client.get('/broker/oauth/callback?state=%E5%BC%82%E5%B8%B8&code=secret', follow_redirects=False).status_code == 303


def test_connect_origin_json_body_and_local_disconnect(connection_app):
    app, oauth, broker = connection_app
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        assert client.post('/api/broker/connect', json={}, headers={'Origin':'https://untrusted.example'}).status_code == 403
        assert client.post('/api/broker/connect').status_code == 422
        assert client.post('/api/broker/connect', content='{}', headers={'Content-Type':'text/plain'}).status_code == 422
        oauth.authorized = True
        broker.client.initialized = True
        broker.client.tools = {'some_tool':{}}
        result = client.post('/api/broker/disconnect', json={}).json()
        assert not result['connection']['authorized']
        assert not result['capabilities']['connected']
        assert not broker.client.tools


def test_oauth_access_logs_redact_code_state_and_error():
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1, '%s %s %s %s %s',
                              ('client', 'GET', '/broker/oauth/callback?code=secret&state=private', '1.1', 303), None)
    assert OAuthAccessLogFilter().filter(record)
    assert 'secret' not in record.getMessage() and 'private' not in record.getMessage()


def test_platform_disconnect_cannot_fall_back_to_legacy_credentials(monkeypatch):
    monkeypatch.setenv('ROBINHOOD_ACCESS_TOKEN', 'legacy-secret')
    monkeypatch.setenv('ROBINHOOD_TOKEN_FILE', '/must-not-read')
    oauth = FakeOAuth()
    provider = TokenProvider(oauth, allow_legacy=False)
    oauth.disconnect()
    assert provider.get() == ''


def test_account_verification_is_read_only_and_requires_authorization(connection_app, monkeypatch):
    from datetime import datetime, timezone
    app, oauth, broker = connection_app
    calls = []
    report = {'status':'partial', 'checked_at':datetime.now(timezone.utc).isoformat(),
              'account_verified':True, 'connection_verified':True, 'account':{'masked_number':'••••1234'},
              'blockers':['live_execution_not_commissioned'], 'checks':[],
              'execution':{'mode':'paper','live_enabled':False,'orders_sent':0}}
    def verify(client):
        assert client is broker.client
        calls.append('read-only')
        broker.client.initialized = True
        return report
    monkeypatch.setattr('trading.app.verify_robinhood', verify)
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        before = app.state.engine.snapshot()
        assert client.post('/api/broker/verify', json={}).status_code == 409
        assert not calls
        oauth.authorized = True
        assert client.post('/api/broker/verify', json={}).json() == report
        status = client.get('/api/broker/connection').json()
        assert status['verification']['account']['masked_number'] == '••••1234'
        assert status['verification']['stale'] is False
        assert status['readiness']['stage'] == 'account_verified'
        assert status['readiness']['live_enabled'] is False
        after = app.state.engine.snapshot()
        for field in ['account','settings','orders','fills','positions','intents']:
            assert after[field] == before[field]
        client.post('/api/broker/disconnect', json={})
        assert client.get('/api/broker/connection').json()['verification'] is None
        assert app.state.store.get('robinhood_verification') is None


def test_verification_snapshot_is_dated_and_invalidated_on_new_grant(connection_app):
    app, oauth, _ = connection_app
    oauth.authorized = True
    app.state.store.put('robinhood_verification', {'account_verified':True,
        'checked_at':'2000-01-01T00:00:00+00:00', 'blockers':[]})
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        status = client.get('/api/broker/connection').json()
        assert status['verification']['stale'] is True
        assert 'account_snapshot_stale' in status['readiness']['blockers']
        client.post('/api/broker/connect', json={})
        client.get('/broker/oauth/callback?state=test-state&code=code&iss=expected', follow_redirects=False)
        assert client.get('/api/broker/connection').json()['verification'] is None


def test_remote_auth_failure_hides_cached_account_and_verify_failure_clears_it(connection_app, monkeypatch):
    from datetime import datetime, timezone
    app, oauth, broker = connection_app
    oauth.authorized = True
    report = {'account_verified':True, 'checked_at':datetime.now(timezone.utc).isoformat(), 'blockers':[]}
    app.state.store.put('robinhood_verification', report)
    broker.client.last_error = 'authorization_required'
    with TestClient(app, base_url='http://127.0.0.1:8010') as client:
        status = client.get('/api/broker/connection').json()
        assert not status['connection']['authorized'] and status['verification'] is None
        assert status['readiness']['stage'] == 'authorization_required'
        broker.client.last_error = None
        def fail(_): raise ValueError('private account payload')
        monkeypatch.setattr('trading.app.verify_robinhood', fail)
        response = client.post('/api/broker/verify', json={})
        assert response.status_code == 503 and 'private' not in response.text
        assert app.state.store.get('robinhood_verification') is None

