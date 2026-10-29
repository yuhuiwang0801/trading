"""Retired queued work stays visible but cannot run legacy trading/research."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading import config, demo, worker, research_worker
from trading.backtest import ReplayBacktest
from trading.runtime import RuntimeStore


@pytest.fixture
def queue(tmp_path, monkeypatch):
    store = RuntimeStore('sqlite:///' + str(tmp_path / 'runtime.db'))
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setattr(config, 'SOURCE', 'demo')
    monkeypatch.setattr(worker, 'RuntimeStore', lambda *_: store)
    monkeypatch.setattr(research_worker, 'RuntimeStore', lambda *_: store)
    monkeypatch.setattr(worker.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(demo, 'run_demo', Mock(side_effect=AssertionError('legacy demo ran')))
    monkeypatch.setattr(ReplayBacktest, 'run', Mock(side_effect=AssertionError('legacy replay ran')))
    return store


@pytest.mark.parametrize('kind', ['demo', 'scan'])
def test_market_worker_retires_queued_demo_without_execution(queue, tmp_path, monkeypatch, kind):
    queued = queue.enqueue(kind, {'source': 'demo'})
    engine = SimpleNamespace(recover=Mock(return_value={'ok': True}),
                             snapshot=lambda: {'settings': {'params': {}}, 'orders': []},
                             scan=Mock(side_effect=AssertionError('legacy engine scan ran')))
    live = SimpleNamespace(path=tmp_path / 'live.db', _may_write=lambda: False,
                           recover=Mock(return_value={'readiness': {}}),
                           scan=Mock(side_effect=AssertionError('live scan ran')))
    market = SimpleNamespace(provider=SimpleNamespace(feed='iex'))
    monkeypatch.setattr(worker, 'Engine', lambda *_: engine)
    monkeypatch.setattr(worker.MarketService, 'from_env', lambda: market)
    monkeypatch.setattr(worker, 'create_live_service', lambda *_: live)
    worker.main(once=True)
    job = next(job for job in queue.jobs() if job['id'] == queued['id'])
    assert job['status'] == 'failed'
    assert job['result']['error'] == 'STRATEGY_RETIRED'
    assert job['result']['status'] == 'retired'
    engine.scan.assert_not_called()
    live.scan.assert_not_called()
    assert queue.get('demo_equity') is None


def test_research_worker_retires_queued_backtest_without_replay(queue, monkeypatch):
    queued = queue.enqueue('backtest', {'dataset': 'demo', 'split': 'test'})
    monkeypatch.setattr(research_worker, 'schedule_reversal_refresh', lambda *_: None)
    verification = Mock(side_effect=AssertionError('unrelated verification ran'))
    monkeypatch.setattr(research_worker, 'run_verification', verification)
    research_worker.main(once=True)
    job = next(job for job in queue.jobs() if job['id'] == queued['id'])
    assert job['status'] == 'failed' and job['result']['error'] == 'STRATEGY_RETIRED'
    verification.assert_not_called()
    ReplayBacktest.run.assert_not_called()
