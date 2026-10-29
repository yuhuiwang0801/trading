"""Real process integration, without an HTTP server or any browser session."""
import os
import subprocess
import sys
import time
from trading.engine import Engine
from trading.runtime import RuntimeStore


def until(predicate, timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.1)
    raise AssertionError('Worker condition timed out')

def test_worker_retires_legacy_jobs_and_recovers_without_browser(tmp_path):
    primary='sqlite:///'+str(tmp_path/'runtime.db')
    demo='sqlite:///'+str(tmp_path/'demo.db')
    env={**os.environ,'DATABASE_URL':primary,'DEMO_DATABASE_URL':demo,'TRADING_DATA_DIR':str(tmp_path),'MARKET_SOURCE':'demo'}
    store=RuntimeStore(primary); engine=Engine(demo)
    job=store.enqueue('demo',{'source':'demo'})
    log=open(tmp_path/'worker.log','w')
    worker=subprocess.Popen([sys.executable,'-m','trading.worker'],env=env,stdout=log,stderr=log)
    try:
        result=until(lambda: next((j for j in store.jobs() if j['id']==job['id'] and j['status']=='failed'),None))
        assert result['result']['error']=='STRATEGY_RETIRED'
        first=store.get('worker')['heartbeat']
        until(lambda: store.get('worker')['heartbeat']!=first)
        assert engine.snapshot()['fills']==[]
    finally:
        worker.terminate();worker.wait(timeout=10)
    # A new process preserves retirement history and never starts the old replay.
    nextjob=store.enqueue('demo',{'source':'demo'})
    worker=subprocess.Popen([sys.executable,'-m','trading.worker'],env=env,stdout=log,stderr=log)
    try:
        result=until(lambda: next((j for j in store.jobs() if j['id']==nextjob['id'] and j['status']=='failed'),None))
        assert result['result']['status']=='retired'
        assert next(j for j in store.jobs() if j['id']==job['id'])['result']['error']=='STRATEGY_RETIRED'
        assert engine.snapshot()['fills']==[]
        wrong=store.enqueue('scan',{'source':'alpaca'})
        until(lambda: next((j for j in store.jobs() if j['id']==wrong['id'] and j['status']=='failed'),None))
        assert engine.snapshot()['fills']==[]
    finally:
        worker.terminate();worker.wait(timeout=10);log.close()


def test_research_retires_legacy_backtests_without_browser(tmp_path):
    primary='sqlite:///'+str(tmp_path/'runtime.db')
    env={**os.environ,'DATABASE_URL':primary,'TRADING_DATA_DIR':str(tmp_path),'MARKET_SOURCE':'demo'}
    store=RuntimeStore(primary)
    job=store.enqueue('backtest',{'split':'test'})
    with open(tmp_path/'research.log','w') as log:
        process=subprocess.Popen([sys.executable,'-m','trading.research_worker'],env=env,stdout=log,stderr=log)
        try:
            result=until(lambda: next((j for j in store.jobs() if j['id']==job['id'] and j['status']=='failed'),None))
            assert result['result']['error']=='STRATEGY_RETIRED'
            assert result['result']['status']=='retired'
            assert 'metrics' not in result['result']
            assert not (tmp_path/'research-seals.json').exists()
        finally:
            process.terminate();process.wait(timeout=10)


def test_market_verification_runs_without_browser_or_credentials(tmp_path):
    primary='sqlite:///'+str(tmp_path/'runtime.db')
    env={**os.environ,'DATABASE_URL':primary,'TRADING_DATA_DIR':str(tmp_path),'MARKET_SOURCE':'alpaca',
         'ALPACA_API_KEY':'','ALPACA_SECRET_KEY':'','ALPACA_DATA_FEED':'iex'}
    store=RuntimeStore(primary)
    ledger=Engine(primary)
    job=store.enqueue('market_verify',{'source':'alpaca'})
    with open(tmp_path/'research.log','w') as log:
        process=subprocess.Popen([sys.executable,'-m','trading.research_worker'],env=env,stdout=log,stderr=log)
        try:
            result=until(lambda: next((j for j in store.jobs() if j['id']==job['id'] and j['status']=='completed'),None))
            assert result['result']['status']=='credentials_required'
            assert store.get('market_verification')['execution']['orders_sent']==0
            assert (tmp_path/'alpaca-verification.json').is_file()
            assert ledger.snapshot()['orders']==[]
            assert ledger.snapshot()['settings']['mode']=='paper'
        finally:
            process.terminate();process.wait(timeout=10)
