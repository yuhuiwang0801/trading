"""Read-only, reproducible market-data snapshot for exploratory reversal research.

This intentionally does NOT claim a historical point-in-time security master.
The current asset master and current SEC SIC mapping cause survivorship and
classification look-ahead bias. No execution or account endpoint is imported.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from statistics import median

import httpx
import pandas as pd

from .market import AlpacaMarketData

ASOF = '2026-10-27'
START = '2016-01-01'
END = '2026-10-26T23:59:59Z'
HOLDOUT_START = '2024-01-01'
SOURCES = {
    'bars': 'https://docs.alpaca.markets/us/reference/stockbars',
    'feed': 'https://docs.alpaca.markets/us/docs/market-data-faq',
    'sec': 'https://www.sec.gov/search-filings/edgar-application-programming-interfaces',
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if path.suffix == '.gz':
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(text)
    else:
        path.write_text(text + '\n')


def read_json(path):
    if Path(path).suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            return json.load(handle)
    return json.loads(Path(path).read_text())


def provider_from_environment():
    """Only existing environment credentials; never exposes their contents."""
    return AlpacaMarketData(
        os.getenv('ALPACA_API_KEY', os.getenv('APCA_API_KEY_ID', '')),
        os.getenv('ALPACA_SECRET_KEY', os.getenv('ALPACA_API_SECRET', os.getenv('APCA_API_SECRET_KEY', ''))),
        feed='sip', trading_url=os.getenv('ALPACA_TRADING_URL', 'https://paper-api.alpaca.markets'),
        rpm=160, retries=3, batch_size=100,
        client=httpx.Client(timeout=httpx.Timeout(40, connect=10)))


def common_stock_proxy(asset):
    """Conservative name heuristic, not a verified historical security type."""
    name = asset.get('name', '')
    excluded = re.compile(r'\b(etf|etn|fund|trust|adr|ads|depositary|deposit|warrant|right|units?|preferred|preference|debenture|notes?|bond)\b', re.I)
    # Alpaca's NYSE names often omit "Common Stock" (JPM, JNJ, XOM, etc.).
    # Requiring that exact label would introduce a large exchange/sector skew.
    included = re.compile(r'\bcommon (stock|shares)|\bordinary shares|\bclass [abc]\b|'
                          r'\b(inc|incorporated|corp|corporation|company|co|bank|bancorp|'
                          r'group|holdings|industries|communications|plc|limited|ltd)\b', re.I)
    return (asset.get('exchange') in {'NYSE', 'NASDAQ', 'AMEX'}
            and asset.get('tradable') is True and not excluded.search(name)
            and bool(included.search(name))
            and bool(re.fullmatch(r'[A-Z]{1,5}(\.[AB])?', asset.get('symbol', ''))))


def valid_daily_bar(row):
    """Fail closed on prices, zero-volume placeholders and malformed candles."""
    try:
        numeric = [float(row[key]) for key in ['open', 'high', 'low', 'close', 'volume',
                                               'tr_close', 'adj_open', 'split_close', 'split_open']]
        if not all(math.isfinite(value) and value > 0 for value in numeric):
            return False
        opened, high, low, close = numeric[:4]
        return low <= min(opened, close) <= max(opened, close) <= high
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def download_bars(provider, root, symbols, start, end, adjustment, label):
    """Persist every original page and request, resume without replacing pages."""
    merged = {symbol: [] for symbol in symbols}
    raw_dir = Path(root) / 'raw' / label
    raw_dir.mkdir(parents=True, exist_ok=True)
    for batch_index, batch in enumerate(provider._batches(symbols)):
        params = {'symbols': ','.join(batch), 'timeframe': '1Day', 'start': start,
                  'end': end, 'adjustment': adjustment, 'asof': ASOF,
                  'feed': 'sip', 'sort': 'asc', 'limit': 10000}
        seen = set()
        for page_index in range(1000):
            path = raw_dir / f'batch_{batch_index:03d}_page_{page_index:03d}.json.gz'
            if path.exists():
                saved = read_json(path)
                if saved['params'] != params:
                    raise ValueError('Cached snapshot parameters differ; use a new output folder')
                data = saved['response']
            else:
                data = provider._get(provider.data_url, '/v2/stocks/bars', params)
                write_json(path, {'endpoint': '/v2/stocks/bars', 'params': params,
                                  'ingested_at': utc_now(), 'response': data})
            for symbol, rows in (data.get('bars') or {}).items():
                if symbol in merged:
                    merged[symbol].extend(rows)
            token = data.get('next_page_token')
            if not token:
                break
            if token in seen:
                raise ValueError('Pagination cycle')
            seen.add(token)
            params = {**params, 'page_token': token}
        else:
            raise ValueError('Pagination safety bound')
        print(json.dumps({'stage': label, 'batch': batch_index + 1,
                          'batches': math.ceil(len(symbols) / provider.batch_size),
                          'rows': sum(len(v) for v in merged.values())}), flush=True)
    return merged


def fetch_sec_json(client, url, path):
    if Path(path).exists():
        return read_json(path)
    # SEC permits no more than 10 requests/s. This sequential limit is <= 5/s.
    time.sleep(.22)
    response = client.get(url, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f'SEC HTTP {response.status_code}')
    data = response.json()
    write_json(path, data)
    return data


def build_snapshot(root, sample_size=200):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'manifest.json').exists():
        raise ValueError('Completed snapshot is immutable; choose a new output folder')
    provider = provider_from_environment()
    try:
        assets_path = root / 'raw' / 'current_assets.json.gz'
        if assets_path.exists():
            assets = read_json(assets_path)['response']
        else:
            assets = provider.assets()
            write_json(assets_path, {'endpoint': '/v2/assets', 'params': {'status': 'active', 'asset_class': 'us_equity'}, 'ingested_at': utc_now(), 'response': assets})
        candidates = sorted([a for a in assets if common_stock_proxy(a)], key=lambda a: a['symbol'])
        candidate_map = {a['symbol']: a for a in candidates}
        selection = download_bars(provider, root, list(candidate_map), '2016-10-01', '2016-12-30T23:59:59Z', 'raw', 'selection_2016')
        ranked = []
        for symbol, bars in selection.items():
            rows = sorted(bars, key=lambda b: b['t'])[-60:]
            if len(rows) == 60 and rows[-1]['c'] >= 5:
                ranked.append((median([b['c'] * b['v'] for b in rows]), candidate_map[symbol]['id'], symbol))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        selected = ranked[:sample_size]
        master = []
        sec_errors = []
        with httpx.Client(headers={'User-Agent': 'NorthstarTradingResearch/1.0 (personal academic research)'}, timeout=30) as client:
            tickers = fetch_sec_json(client, 'https://www.sec.gov/files/company_tickers_exchange.json', root / 'raw' / 'sec_tickers.json.gz')
            ticker_rows = [dict(zip(tickers['fields'], row)) for row in tickers['data']]
            by_ticker = {row['ticker'].replace('-', '.'): row for row in ticker_rows}
            for rank, (dollar_volume, _, symbol) in enumerate(selected, 1):
                asset = candidate_map[symbol]
                ticker = by_ticker.get(symbol, {})
                cik = str(ticker.get('cik', '')).zfill(10) if ticker.get('cik') else ''
                sec = {}
                if cik:
                    try:
                        sec = fetch_sec_json(client, f'https://data.sec.gov/submissions/CIK{cik}.json', root / 'raw' / 'sec' / f'CIK{cik}.json.gz')
                    except (httpx.HTTPError, ValueError):
                        sec_errors.append(symbol)
                sic = str(sec.get('sic', ''))
                master.append({'symbol': symbol, 'security_id': asset['id'], 'issuer_id': cik,
                               'name': asset['name'], 'exchange': asset['exchange'],
                               'industry': sic[:2] if len(sic) == 4 else '', 'sic': sic,
                               'industry_description': sec.get('sicDescription', ''),
                               'selection_rank': rank, 'selection_median_dollar_volume': dollar_volume,
                               'security_type_quality': 'current_name_heuristic',
                               'industry_quality': 'current_SEC_SIC_backfilled_NOT_point_in_time',
                               'exclusion_reason': ('missing_SEC_issuer_id' if not cik else
                                                    'missing_SEC_SIC' if len(sic) != 4 else ''),
                               'master_asof': ASOF})
                if rank % 25 == 0:
                    print(json.dumps({'stage': 'SEC', 'complete': rank, 'total': len(selected)}), flush=True)
        pd.DataFrame(master).to_csv(root / 'security_master.csv', index=False)
        print(json.dumps({'stage': 'selected', 'symbols': len(master), 'sic_coverage': sum(bool(r['industry']) for r in master)}), flush=True)
        symbols = [row['symbol'] for row in master if not row['exclusion_reason']] + ['SPY']
        bars = {adjustment: download_bars(provider, root, symbols, START, END, adjustment, f'history_{adjustment}')
                for adjustment in ['raw', 'split', 'all']}
        actions_path = root / 'raw' / 'corporate_actions.json.gz'
        if actions_path.exists():
            actions = read_json(actions_path)
        else:
            actions = provider.corporate_actions(symbols, START, END[:10])
            write_json(actions_path, {'params': {'symbols': symbols, 'start': START, 'end': END[:10], 'data_quality': 'all'},
                                      'ingested_at': utc_now(), 'response': actions})
        actions_payload = actions['response'] if isinstance(actions, dict) else actions
        write_json(root / 'corporate_actions.json.gz', actions_payload)
    finally:
        provider.close()

    spy_asset = next(a for a in assets if a['symbol'] == 'SPY')
    lookup = {row['symbol']: row for row in master}
    lookup['SPY'] = {'security_id': spy_asset['id'], 'issuer_id': 'benchmark:SPY', 'industry': 'MARKET'}
    output = []
    issues = []
    for symbol in symbols:
        mapped = {adj: {row['t']: row for row in data[symbol]} for adj, data in bars.items()}
        if any(len(mapped[adj]) != len(bars[adj][symbol]) for adj in bars):
            raise ValueError('Duplicate bars in provider response')
        for stamp, raw in mapped['raw'].items():
            split, total = mapped['split'].get(stamp), mapped['all'].get(stamp)
            if split is None or total is None:
                issues.append({'symbol': symbol, 'session': stamp[:10], 'reason': 'missing_adjustment_pair'})
                continue
            row = {'session': stamp[:10], 'symbol': symbol, 'security_id': lookup[symbol]['security_id'],
                   'issuer_id': lookup[symbol]['issuer_id'], 'industry': lookup[symbol]['industry'],
                   'open': raw['o'], 'high': raw['h'], 'low': raw['l'], 'close': raw['c'], 'volume': raw['v'],
                   'tr_close': total['c'], 'adj_open': total['o'],
                   'split_close': split['c'], 'split_open': split['o'],
                   'source': 'alpaca', 'feed': 'sip', 'availability_quality': 'reconstructed',
                   'dataset_version': root.name}
            if not valid_daily_bar(row):
                issues.append({'symbol': symbol, 'session': stamp[:10], 'reason': 'invalid_ohlcv'})
                continue
            output.append(row)
    panel = pd.DataFrame(output).sort_values(['session', 'symbol'])
    panel[panel.session < HOLDOUT_START].to_csv(root / 'daily_bars.csv.gz', index=False, compression='gzip')
    panel[panel.session >= HOLDOUT_START].to_csv(root / 'daily_bars_holdout_LOCKED.csv.gz', index=False, compression='gzip')
    coverage = panel.groupby('symbol').agg(first=('session', 'min'), last=('session', 'max'), rows=('session', 'size')).reset_index()
    coverage.to_csv(root / 'coverage.csv', index=False)
    pd.DataFrame(issues, columns=['symbol', 'session', 'reason']).to_csv(root / 'data_issues.csv', index=False)
    manifest = {'dataset_version': root.name, 'created_at': utc_now(), 'status': 'exploratory_proxy',
                'orders_sent': 0, 'research_only': True, 'source': 'alpaca', 'feed': 'sip',
                'start': START, 'end': END, 'warmup_year': 2016, 'evaluation_end': '2023-12-31',
                'holdout_start': HOLDOUT_START, 'holdout_evaluated': False,
                'asset_count': len(assets), 'candidate_count': len(candidates), 'sample_count': len(symbols) - 1,
                'selected_before_SEC_validation': len(master),
                'excluded_master': [{'symbol': r['symbol'], 'reason': r['exclusion_reason']} for r in master if r['exclusion_reason']],
                'total_rows_including_benchmark': len(panel), 'issues': len(issues), 'sec_errors': sec_errors,
                'sample_rule': 'Current active tradable common-stock name heuristic; fixed top sample_size ranked by last 60 sessions median raw dollar volume through 2016-12-30, last raw close >= $5; no return optimization.',
                'industry_counts': pd.DataFrame(master).groupby('industry').size().to_dict(),
                'adjustments': {'raw': 'unadjusted prices and shares', 'split': 'forward and reverse splits',
                                'all': 'split + cash dividend + spin-off adjusted price proxy; not an exact shareholder total-return ledger'},
                'known_limitations': ['Current surviving asset universe; delisted constituents unavailable',
                                      'Current SEC SIC backfilled; not historical industry membership',
                                      'Name-based common-stock classification may over-exclude or misclassify securities',
                                      'Fixed pre-2017 sample differs from historical monthly top-500 universe',
                                      'Provider symbol-asof mapping unverified as permanent historical identifier',
                                      'Historical receipt timestamps unavailable; availability reconstructed',
                                      'Corporate-action coverage and delayed corrections not guaranteed'],
                'official_documentation': SOURCES,
                'files': {str(path.relative_to(root)): {'sha256': sha256(path), 'bytes': path.stat().st_size}
                          for path in sorted(root.rglob('*')) if path.is_file()}}
    write_json(root / 'manifest.json', manifest)
    return manifest


def load_exploration_panel(root, *, include_quarantined=False):
    """Load development data, applying identity acceptance before any positions.

    This filters a research input universe; it must never be applied to an open
    position ledger. ``include_quarantined`` is for inspecting rejected data.
    """
    panel = pd.read_csv(Path(root) / 'daily_bars.csv.gz', dtype={'security_id': str, 'issuer_id': str, 'industry': str}, keep_default_na=False)
    if (panel.session >= HOLDOUT_START).any():
        raise ValueError('Reserved holdout data in exploration partition')
    quarantine = Path(root) / 'identity_quarantine.json'
    if not include_quarantined and quarantine.exists():
        data = read_json(quarantine)
        panel = panel[~panel.symbol.isin(data.get('excluded_symbols', []))
                      & ~panel.security_id.isin(data.get('excluded_security_ids', []))].copy()
    return panel
