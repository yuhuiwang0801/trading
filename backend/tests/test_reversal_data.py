"""Snapshot-contract checks use synthetic fixtures, never reserved returns."""
import json
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from trading.market import AlpacaMarketData, MarketDataError
from trading.reversal_data import (ASOF, END, download_bars, load_exploration_panel,
                                   valid_daily_bar)


@pytest.fixture
def good_bar():
    return dict(open=10, high=12, low=9, close=11, volume=100,
                tr_close=5.5, adj_open=5, split_close=11, split_open=10)


@pytest.mark.parametrize('field,value', [('open', 20), ('low', 12), ('close', float('nan')),
                                         ('volume', 0), ('volume', float('inf')),
                                         ('tr_close', -1), ('adj_open', None), ('split_open', 'bad')])
def test_invalid_price_or_placeholder_is_rejected(good_bar, field, value):
    assert valid_daily_bar(good_bar)
    good_bar[field] = value
    assert not valid_daily_bar(good_bar)


def test_pages_use_only_read_get_with_identical_adjustment_contract(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        q = parse_qs(request.url.query.decode())
        second = 'page_token' in q
        symbol = 'MSFT' if second else 'AAPL'
        return httpx.Response(200, json={'bars': {symbol: [{'t': '2016-01-04T05:00:00Z', 'c': 10}]},
                                       'next_page_token': None if second else 'next-page'})

    provider = AlpacaMarketData('test-key', 'test-secret', feed='sip', rpm=100000,
                               sleep=lambda _: None, client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        for adjustment in ['raw', 'split', 'all']:
            result = download_bars(provider, tmp_path, ['AAPL', 'MSFT'], '2016-01-01', END, adjustment, adjustment)
            assert len(result['AAPL']) == len(result['MSFT']) == 1
            # Cache resumption must reuse the same pages and not issue requests.
            count = len(requests)
            assert download_bars(provider, tmp_path, ['AAPL', 'MSFT'], '2016-01-01', END, adjustment, adjustment) == result
            assert len(requests) == count
        with pytest.raises(MarketDataError):
            provider._get(provider.trading_url, '/v2/orders', {})
    finally:
        provider.close()
    assert len(requests) == 6
    for request in requests:
        q = parse_qs(request.url.query.decode())
        assert request.method == 'GET'
        assert request.url.host == 'data.alpaca.markets'
        assert request.url.path == '/v2/stocks/bars'
        assert q['symbols'] == ['AAPL,MSFT']
        assert q['timeframe'] == ['1Day'] and q['feed'] == ['sip']
        assert q['start'] == ['2016-01-01'] and q['end'] == [END]
        assert q['asof'] == [ASOF]
    assert [parse_qs(r.url.query.decode())['adjustment'][0] for r in requests] == ['raw', 'raw', 'split', 'split', 'all', 'all']


def test_loader_quarantines_identity_and_cannot_open_holdout(tmp_path):
    path = tmp_path / 'daily_bars.csv.gz'
    pd.DataFrame([{'session': '2023-12-29', 'symbol': symbol, 'security_id': sid,
                   'issuer_id': '0000000001', 'industry': '02'}
                  for symbol, sid in [('SAFE', 'safe-id'), ('BAD', 'bad-id')]]).to_csv(path, index=False)
    (tmp_path / 'identity_quarantine.json').write_text(json.dumps({'excluded_symbols': ['BAD'], 'excluded_security_ids': ['bad-id']}))
    panel = load_exploration_panel(tmp_path)
    assert panel.symbol.tolist() == ['SAFE']
    assert panel.industry.tolist() == ['02']
    assert panel.issuer_id.tolist() == ['0000000001']
    assert len(load_exploration_panel(tmp_path, include_quarantined=True)) == 2
    panel.loc[:, 'session'] = '2024-01-02'
    panel.to_csv(path, index=False)
    with pytest.raises(ValueError, match='Reserved holdout'):
        load_exploration_panel(tmp_path)
