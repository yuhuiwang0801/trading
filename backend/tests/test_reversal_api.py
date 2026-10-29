import json

from fastapi.testclient import TestClient

from trading import config
from trading.app import create_app


def research_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.delenv('APP_ACCESS_TOKEN', raising=False)
    return create_app(f'sqlite:///{tmp_path / "state.db"}',
                      f'sqlite:///{tmp_path / "demo.db"}', source='demo')


def test_missing_research_is_explicit_and_does_not_start_a_run(tmp_path, monkeypatch):
    app = research_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        before = app.state.engine.snapshot()
        result = client.get('/api/research/reversal/latest')
        assert result.status_code == 200
        assert result.json() == {'status': 'not_run', 'runs': [], 'report_available': False}
        assert client.get('/api/research/reversal/report').status_code == 404
        assert client.post('/api/research/reversal/latest').status_code == 405
        assert app.state.store.jobs() == []
        after = app.state.engine.snapshot()
        for field in ('settings', 'orders', 'fills', 'positions', 'account'):
            assert after[field] == before[field]


def test_report_returns_compact_summary_and_fixed_html_without_mutation(tmp_path, monkeypatch):
    app = research_app(tmp_path, monkeypatch)
    directory = tmp_path / 'reversal' / 'latest'
    directory.mkdir(parents=True)
    summary = {'status': 'exploratory', 'title': 'Reversal research',
               'period': {'start': '2024-01-02', 'end': '2026-10-26'},
               'data': {'source': 'Alpaca', 'feed': 'sip', 'symbol_count': 60},
               'runs': [{'experiment': 'E01', 'cost_bps': 10,
                         'metrics': {'total_return': -.02, 'max_drawdown': -.12,
                                     'cagr': -.007, 'sharpe': -.1, 'ending_equity': 98000,
                                     'internal_diagnostic': 'excluded'},
                         'trades': [{'large': 'omitted'}]}],
               'benchmark': {'total_return': .1}, 'limitations': ['Exploratory sample']}
    (directory / 'report.json').write_text(json.dumps({'summary': summary, 'development': {'large': 'omitted'}}))
    html = '<!doctype html><title>Research only</title><p>Historical result</p>'
    (directory / 'report.html').write_text(html)
    (tmp_path / 'outside.html').write_text('not the report')
    with TestClient(app) as client:
        before = app.state.engine.snapshot()
        response = client.get('/api/research/reversal/latest')
        data = response.json()
        assert data['status'] == 'exploratory' and data['report_available'] is True
        assert data['runs'][0]['metrics']['total_return'] == -.02
        assert 'internal_diagnostic' not in data['runs'][0]['metrics']
        assert 'trades' not in data['runs'][0] and 'development' not in data
        assert response.headers['cache-control'] == 'no-store'
        report = client.get('/api/research/reversal/report?path=../../outside.html')
        assert report.text == html and report.headers['content-type'].startswith('text/html')
        assert app.state.store.jobs() == []
        after = app.state.engine.snapshot()
        for field in ('settings', 'orders', 'fills', 'positions', 'account'):
            assert after[field] == before[field]


def test_malformed_report_has_no_fabricated_results(tmp_path, monkeypatch):
    app = research_app(tmp_path, monkeypatch)
    directory = tmp_path / 'reversal' / 'latest'
    directory.mkdir(parents=True)
    with TestClient(app) as client:
        for contents in ('{partial', '[]', '{"summary": null}'):
            (directory / 'report.json').write_text(contents)
            response = client.get('/api/research/reversal/latest')
            assert response.status_code == 503
            assert response.json()['status'] == 'unavailable'
            assert response.json()['runs'] == []


def test_research_endpoints_use_existing_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setenv('APP_ACCESS_TOKEN', 'research-test-only')
    app = create_app(f'sqlite:///{tmp_path / "state.db"}',
                     f'sqlite:///{tmp_path / "demo.db"}', source='demo')
    with TestClient(app) as client:
        for path in ('/api/research/reversal/latest', '/api/research/reversal/report'):
            assert client.get(path).status_code == 401
        client.post('/api/session', json={'token': 'research-test-only'})
        assert client.get('/api/research/reversal/latest').status_code == 200
