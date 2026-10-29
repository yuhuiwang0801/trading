import json
from decimal import Decimal
from fastapi.testclient import TestClient
import pytest
from trading.app import create_app
from trading.demo import run_demo
from trading.runtime import RuntimeStore

@pytest.fixture
def app(tmp_path):
    return create_app('sqlite:///'+str(tmp_path/'real.db'), 'sqlite:///'+str(tmp_path/'demo.db'), source='demo')

def test_full_business_flow_and_api_trace(app):
    with TestClient(app) as c:
        empty=c.get('/api/state').json()
        assert empty['provider']['source']=='demo'
        assert empty['account']['cash']=='10000'
        result=run_demo(app.state.engine,app.state.store)
        assert result['orders']==4 and result['fills']==4 and result['positions']==0
        state=c.get('/api/state').json()
        assert len(state['equity_curve'])==8
        assert Decimal(state['account']['cash'])!=10000
        assert all(o['status']=='FILLED' for o in state['orders'])
        for fill in state['fills']:
            order=next(o for o in state['orders'] if o['id']==fill['order_id'])
            intent=next(i for i in state['intents'] if i['id']==order['intent_id'])
            assert any(s['id']==intent['signal_id'] for s in state['signals'])
            assert any(a['event']=='FILL_APPLIED' and a['order_id']==order['id'] for a in state['audit'])
        assert run_demo(app.state.engine)['status']=='already_completed'
        lines=c.get('/api/audit/export').text.splitlines()
        assert len(lines)==len(state['audit'])
        assert all(isinstance(json.loads(line),dict) for line in lines)
        assert c.patch('/api/settings',json={'mode':'live'}).status_code==400
        assert c.post('/api/control',json={'action':'pause_entries'}).json()['settings']['paused_entries']
        assert c.post('/api/control',json={'action':'resume_entries'}).json()['settings']['paused_entries'] is False

def test_retired_research_and_demo_cannot_enqueue_but_history_remains_readable(app):
    historical = app.state.store.enqueue('backtest', {'dataset': 'demo', 'split': 'test'})
    app.state.store.claim(['backtest'])
    app.state.store.finish(historical['id'], {'status': 'completed', 'historical': True})
    before = app.state.engine.snapshot()
    with TestClient(app) as c:
        prior_jobs = app.state.store.jobs()
        for path in ('/api/backtests', '/api/demo/run', '/api/scan'):
            assert c.post(path, json={'dataset': 'demo', 'split': 'test'}).status_code == 410
        assert app.state.store.jobs() == prior_jobs
        assert c.get('/api/jobs/' + historical['id']).json()['status'] == 'completed'
        assert c.get('/api/backtests').json()[0]['id'] == historical['id']
        after = app.state.engine.snapshot()
        for key in ('orders', 'positions', 'fills', 'account'):
            assert after[key] == before[key]

def test_local_browser_origin_guard(app):
    with TestClient(app) as c:
        assert c.post('/api/control',json={'action':'emergency'},headers={'Origin':'https://attacker.invalid'}).status_code==403
        assert c.get('/api/health',headers={'host':'attacker.invalid'}).status_code==400

def test_market_verification_is_read_only_durable_job(app):
    with TestClient(app) as c:
        before=app.state.engine.snapshot()
        assert c.get('/api/market/verification').json() is None
        job=c.post('/api/market/verify')
        assert job.status_code==202 and job.json()['kind']=='market_verify'
        assert c.post('/api/market/verify').json()['id']==job.json()['id']
        report={'status':'credentials_required','execution':{'orders_sent':0,'live_enabled':False}}
        app.state.store.put('market_verification',report)
        assert c.get('/api/market/verification').json()==report
        assert c.get('/api/state').json()['market_verification']==report
        assert app.state.engine.snapshot()['orders']==before['orders']
        assert app.state.engine.snapshot()['settings']==before['settings']

def test_nonlocal_deploy_auth_and_http_only_session(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ACCESS_TOKEN','test-only-token')
    app=create_app('sqlite:///'+str(tmp_path/'state.db'),'sqlite:///'+str(tmp_path/'demo.db'),'demo')
    with TestClient(app) as c:
        assert c.get('/api/state').status_code==401
        assert c.post('/api/session',json={'token':'wrong'}).status_code==401
        assert c.post('/api/session',json={'token':'test-only-token'}).status_code==200
        assert c.get('/api/state').status_code==200
        assert 'httponly' in c.post('/api/session',json={'token':'test-only-token'}).headers['set-cookie'].lower()

def test_demo_and_real_data_accounts_are_separate(tmp_path):
    real='sqlite:///'+str(tmp_path/'real.db'); demo='sqlite:///'+str(tmp_path/'demo.db')
    a=create_app(real,demo,'demo'); run_demo(a.state.engine)
    b=create_app(real,demo,'alpaca')
    assert b.state.engine.snapshot()['orders']==[]
    assert b.state.engine.snapshot()['account']['cash']=='10000'
    with TestClient(b) as c:
        assert c.post('/api/demo/run').status_code==410


def test_interrupted_demo_does_not_falsely_report_completed(app):
    from trading.demo import scenario
    from datetime import datetime
    frames=scenario()
    for frame in frames[:2]:
        app.state.engine.scan(frame,datetime.fromisoformat(frame['timestamp'].replace('Z','+00:00')))
    assert len(app.state.engine.snapshot()['orders'])==2
    assert not app.state.engine.snapshot()['fills']
    result=run_demo(app.state.engine,app.state.store)
    assert result['status']=='completed'
    assert len(app.state.engine.snapshot()['fills'])==4
