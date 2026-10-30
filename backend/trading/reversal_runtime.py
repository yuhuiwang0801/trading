"""Daily, read-only production signal packets for the fixed reversal V2 rules.

Expensive SIP/SEC refresh runs outside the quote worker. Frozen research inputs
are only metadata: no historical backtest candidate is promoted to a live signal.
Every successful daily packet is immutable and valid for one execution window.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

import exchange_calendars as xcals
import httpx
import numpy as np
import pandas as pd

from .calendar import NEW_YORK, as_utc, calendar_for_year, iso, session_bounds
from .reversal_data import provider_from_environment, valid_daily_bar
from .reversal_fundamentals import (fetch_json, normalized_facts, normalized_filings,
                                   issuer_daily_features)
from .reversal_refinement import prepare_refinement
from .reversal_signals import SignalConfig, prepare_signals
from .intraday_market import REFERENCE_VERSION

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / 'config/reversal_research_v2.json'
SNAPSHOT = ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2'
IDENTITY_PATH = ROOT / 'data/reversal/v2_identity_review.json'
STRATEGY_ID = 'reversal_v2'
SCHEMA = 'reversal_v2_runtime_packet_v2'
HISTORY_DAYS = 750
OBSERVATION_SESSIONS = 32


def _root(data_dir=None):
    return Path(data_dir) if data_dir is not None else Path(os.getenv('TRADING_DATA_DIR', str(ROOT / 'data'))) / 'reversal/runtime_v2'


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    return value


def _encoded(value):
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{uuid.uuid4().hex}.tmp')
    temp.write_bytes(_encoded(value) + b'\n')
    temp.replace(path)


def _read(path):
    return json.loads(Path(path).read_text())


def expected_sessions(now=None):
    """18:00 NY is the daily freeze; holidays/early-close days use XNYS."""
    stamp = as_utc(now)
    local = stamp.astimezone(NEW_YORK)
    cal = calendar_for_year(local.year)
    day = pd.Timestamp(local.date())
    if cal.is_session(day) and local.hour >= 18:
        signal = day
    else:
        signal = cal.date_to_session(day - pd.Timedelta(days=1), direction='previous')
    execution = cal.next_session(signal)
    execute_local = datetime.combine(execution.date(), datetime.min.time(), NEW_YORK)
    return {
        'data_session': signal.date().isoformat(), 'signal_session': signal.date().isoformat(),
        'execute_session': execution.date().isoformat(), 'execution_session': execution.date().isoformat(),
        'execution_starts_at': iso(execute_local.replace(hour=9, minute=35)),
        'expires_at': iso(execute_local.replace(hour=9, minute=40)),
    }


def expected_signal_session(now=None):
    """Session eligible for the 18:00 New York daily freeze."""
    return expected_sessions(now)['signal_session']


def load_runtime_packet(now=None, data_dir=None):
    """Load/verify without networking; execution_eligible never re-dates inputs."""
    stamp = as_utc(now)
    root = _root(data_dir)
    dates = expected_sessions(stamp)
    try:
        pointer = _read(root / 'latest.json')
        if len(pointer['packet_id']) != 64 or any(c not in '0123456789abcdef' for c in pointer['packet_id']):
            raise ValueError('invalid_packet_id')
        path = root / 'packets' / (pointer['packet_id'] + '.json')
        packet = _read(path)
        if _hash({k: v for k, v in packet.items() if k != 'packet_id'}) != pointer['packet_id']:
            raise ValueError('packet_hash_mismatch')
        if packet.get('packet_id') != pointer['packet_id']:
            raise ValueError('packet_identity_mismatch')
    except (OSError, ValueError, KeyError, TypeError):
        return {'schema': SCHEMA, 'strategy_id': STRATEGY_ID, 'status': 'blocked', 'quality': 'blocked',
                'blocked_reasons': ['missing_or_invalid_signal_packet'], 'execution_eligible': False,
                'candidates': [], 'observations': {}, 'security_master': {},
                'expected_signal_session': dates['signal_session'], **dates}
    packet['execution_eligible'] = False
    packet['expected_signal_session'] = dates['signal_session']
    reasons = list(packet.get('blocked_reasons', []))
    # A new data schema needs a fresh build from the same authoritative daily
    # inputs, not guessed eligibility reconstructed from an old candidate list.
    # Retain old observations for already-owned positions' daily exit recovery.
    if packet.get('status') in {'ready', 'expired'} and (
            packet.get('schema') != SCHEMA or packet.get('intraday_reference_version') != REFERENCE_VERSION
            or not isinstance(packet.get('intraday_reference'), dict)
            or not packet.get('intraday_reference')):
        reasons.append('intraday_reference_upgrade_required')
    try:
        if packet.get('config_hash') != _file_hash(CONFIG_PATH):
            reasons.append('strategy_configuration_changed')
        if packet.get('metadata_hash') != _metadata_hash():
            reasons.append('universe_metadata_changed')
    except OSError:
        reasons.append('strategy_metadata_unavailable')
    if packet.get('signal_session') != dates['signal_session']:
        reasons.append('stale_signal_session')
    if packet.get('data_session') != packet.get('signal_session'):
        reasons.append('data_session_mismatch')
    if packet.get('execution_session') != dates['execution_session']:
        reasons.append('execution_session_mismatch')
    if as_utc(packet['created_at']) > stamp:
        reasons.append('future_packet_timestamp')
    if reasons:
        packet.update(status='blocked', quality='blocked', blocked_reasons=sorted(set(reasons)))
    elif stamp >= as_utc(packet['expires_at']):
        packet.update(status='expired', quality='expired', blocked_reasons=['execution_window_expired'])
    elif packet.get('status') == 'ready':
        packet['execution_eligible'] = as_utc(packet['execution_starts_at']) <= stamp < as_utc(packet['expires_at'])
    # Presentation compatibility for packets sealed by the first runtime build.
    # Derive only display returns from that SAME immutable packet, never prices
    # from a later snapshot; identity, rank, entry gap and score stay untouched.
    for candidate in packet.get('candidates', []):
        if 'raw_return' in candidate and 'peer_return' in candidate:
            continue
        rows = sorted((r for r in packet.get('observations', {}).get(candidate.get('symbol'), [])
                       if r['session'] <= packet['signal_session']), key=lambda r: r['session'])[-6:]
        try:
            if len(rows) == 6 and all(r.get('peer_return') is not None for r in rows[-5:]):
                candidate['raw_return'] = rows[-1]['total_return_index'] / rows[0]['total_return_index'] - 1
                candidate['peer_return'] = math.prod(1 + r['peer_return'] for r in rows[-5:]) - 1
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            pass
    return packet


def _metadata_hash():
    return _hash({'security_master': _file_hash(SNAPSHOT / 'security_master.csv'),
                  'identity_review': _file_hash(IDENTITY_PATH)})


def runtime_packet_status(now=None, data_dir=None):
    packet = load_runtime_packet(now, data_dir)
    return {k: v for k, v in packet.items() if k not in {'observations', 'securities', 'security_master', 'screening'}}


def _master():
    master = pd.read_csv(SNAPSHOT / 'security_master.csv', dtype=str).fillna('')
    exclusions = set(_read(IDENTITY_PATH)['excluded_security_ids'])
    master = master[~master.security_id.isin(exclusions)].copy()
    if len(master) != 190 or master.security_id.duplicated().any() or master.symbol.duplicated().any():
        raise ValueError('fixed_v2_universe_invalid')
    if master[['issuer_id', 'industry']].eq('').any().any():
        raise ValueError('fixed_v2_metadata_missing')
    return master


def _save_raw(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        json.dump(_clean(value), stream, separators=(',', ':'), allow_nan=False)


def _fetch_bars(provider, master, attempt, signal, progress):
    symbols = sorted(master.symbol.tolist())
    start = (pd.Timestamp(signal) - pd.Timedelta(days=HISTORY_DAYS)).date().isoformat()
    end = signal + 'T23:59:59Z'
    result = {}
    # Assets are checked against the frozen security UUID to detect ticker reuse.
    assets = provider.assets()
    _save_raw(attempt / 'raw/assets.json.gz', {'retrieved_at': iso(as_utc()), 'response': assets})
    asset_map = {a.get('symbol'): a for a in assets}
    for item in master.itertuples():
        asset = asset_map.get(item.symbol, {})
        if asset.get('id') != item.security_id or asset.get('tradable') is not True:
            raise ValueError('security_identity_or_tradability_changed:' + item.symbol)
    for adjustment in ('raw', 'split', 'all'):
        result[adjustment] = {symbol: {} for symbol in symbols}
        for batch_index, batch in enumerate(provider._batches(symbols)):
            params = {'symbols': ','.join(batch), 'timeframe': '1Day', 'start': start, 'end': end,
                      'adjustment': adjustment, 'asof': signal, 'feed': 'sip', 'sort': 'asc', 'limit': 10000}
            tokens = set()
            for page in range(1000):
                payload = provider._get(provider.data_url, '/v2/stocks/bars', params)
                _save_raw(attempt / f'raw/bars_{adjustment}_{batch_index}_{page}.json.gz',
                          {'params': params, 'retrieved_at': iso(as_utc()), 'response': payload})
                for symbol, rows in (payload.get('bars') or {}).items():
                    if symbol not in result[adjustment]:
                        raise ValueError('unexpected_provider_symbol')
                    for bar in rows:
                        day = str(bar['t'])[:10]
                        if day > signal or day < start or day in result[adjustment][symbol]:
                            raise ValueError('invalid_bar_date_or_duplicate')
                        result[adjustment][symbol][day] = bar
                token = payload.get('next_page_token')
                if not token:
                    break
                if token in tokens:
                    raise ValueError('bar_pagination_cycle')
                tokens.add(token)
                params = {**params, 'page_token': token}
            else:
                raise ValueError('bar_pagination_limit')
            progress(stage='sip_bars', adjustment=adjustment, batch=batch_index + 1)
    output = []
    for item in master.itertuples():
        symbol = item.symbol
        for day, raw in result['raw'][symbol].items():
            split, total = result['split'][symbol].get(day), result['all'][symbol].get(day)
            if split is None or total is None:
                continue
            row = {'session': day, 'symbol': symbol, 'security_id': item.security_id,
                   'issuer_id': item.issuer_id, 'industry': item.industry, 'instrument_type': 'common_stock',
                   'open': raw['o'], 'high': raw['h'], 'low': raw['l'], 'close': raw['c'], 'volume': raw['v'],
                   'tr_close': total['c'], 'adj_open': total['o'], 'split_close': split['c'], 'split_open': split['o'],
                   'source': 'alpaca', 'feed': 'sip'}
            if valid_daily_bar(row):
                output.append(row)
    panel = pd.DataFrame(output)
    if panel.empty:
        raise ValueError('empty_sip_history')
    latest = panel[panel.session.eq(signal)]
    if set(latest.security_id) != set(master.security_id):
        missing = sorted(set(master.symbol) - set(latest.symbol))
        raise ValueError('incomplete_latest_sip_session:' + ','.join(missing))
    panel.to_csv(attempt / 'daily_bars.csv.gz', index=False)
    return panel


def _fetch_fundamentals(master, attempt, signal, progress):
    # Long calendar preserves historical filing availability and prior-year pairs.
    cal = xcals.get_calendar('XNYS', start='1999-12-20', end=str(pd.Timestamp(signal) + pd.Timedelta(days=7))[:10])
    sessions = cal.sessions_in_range('2000-01-01', signal).strftime('%Y-%m-%d').tolist()
    history_start = (pd.Timestamp(signal) - pd.Timedelta(days=550)).date().isoformat()
    provenance, features, coverage = {}, [], []
    with httpx.Client(headers={'User-Agent': os.getenv('SEC_USER_AGENT', 'NorthstarTradingResearch/2.0 (personal research)')},
                      timeout=httpx.Timeout(60, connect=15)) as client:
        groups = list(master.groupby('issuer_id'))
        for number, (cik, group) in enumerate(groups, 1):
            cik = cik.zfill(10)
            try:
                recent = fetch_json(client, f'https://data.sec.gov/submissions/CIK{cik}.json',
                                    attempt / 'raw/submissions' / f'CIK{cik}.json.gz', provenance)
                if str(recent.get('cik', '')).zfill(10) != cik:
                    raise ValueError('SEC issuer identity mismatch')
                payloads = [recent]
                for history in recent.get('filings', {}).get('files', []):
                    if history.get('filingTo', '') < history_start or history.get('filingFrom', '') > signal:
                        continue
                    name = history['name']
                    if not name.startswith('CIK' + cik) or '/' in name or '..' in name:
                        raise ValueError('unexpected_SEC_archive_name')
                    payloads.append(fetch_json(client, f'https://data.sec.gov/submissions/{name}',
                                              attempt / 'raw/submissions' / (name + '.gz'), provenance))
                facts = fetch_json(client, f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',
                                   attempt / 'raw/companyfacts' / f'CIK{cik}.json.gz', provenance)
                if str(facts.get('cik', '')).zfill(10) != cik:
                    raise ValueError('SEC fact issuer identity mismatch')
                normalized = normalized_facts(facts, sessions)
                filings = normalized_filings(payloads, sessions)
                rows = issuer_daily_features(normalized, filings, sessions, history_complete=True)
                rows = rows[-OBSERVATION_SESSIONS:]
                for security in group.itertuples():
                    features.extend({'security_id': security.security_id, 'symbol': security.symbol,
                                     'issuer_id': cik, **row} for row in rows)
                coverage.append({'issuer_id': cik, 'status': 'fresh', 'observed_at': iso(as_utc())})
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # A fresh partial universe is not a substitute for missing issuer
                # risk observations: block this entire daily packet and retry.
                coverage.append({'issuer_id': cik, 'status': 'refresh_failed'})
                _atomic_json(attempt / 'sec_coverage.json', coverage)
                raise ValueError('sec_refresh_failed:' + cik) from None
            if number % 10 == 0 or number == len(groups):
                progress(stage='sec_fundamentals', completed=number, total=len(groups))
    _atomic_json(attempt / 'sec_provenance.json', provenance)
    _atomic_json(attempt / 'sec_coverage.json', coverage)
    frame = pd.DataFrame(features)
    # Financially unknown issuers remain explicit rows, never healthy by default.
    for name in ('revenue_yoy', 'net_income', 'net_income_yoy'):
        if name not in frame:
            frame[name] = np.nan
    frame.to_csv(attempt / 'fundamentals.csv.gz', index=False)
    return frame


def packet_from_frames(panel, fundamentals, master, config, now, *, config_hash=None, metadata_hash=None):
    """Pure deterministic strategy transformation, also used by integration tests."""
    stamp = as_utc(now)
    dates = expected_sessions(stamp)
    signal = dates['signal_session']
    if panel.empty or panel.session.max() != signal or (panel.session > signal).any():
        raise ValueError('prices_not_current_completed_session')
    if not panel.source.eq('alpaca').all() or not panel.feed.eq('sip').all():
        raise ValueError('mixed_or_incorrect_market_feed')
    if set(panel.loc[panel.session.eq(signal), 'security_id']) != set(master.security_id):
        raise ValueError('missing_latest_security_bars')
    if fundamentals.empty or fundamentals.session.max() != signal or (fundamentals.session > signal).any():
        raise ValueError('fundamentals_not_current_session')
    if set(fundamentals.loc[fundamentals.session.eq(signal), 'security_id']) != set(master.security_id):
        raise ValueError('missing_latest_fundamental_rows')
    signal_config = SignalConfig(**{field.name: config[field.name] for field in fields(SignalConfig) if field.name in config})
    prepared, scores, coverage = prepare_signals(panel, signal_config)
    scores, exit_features = prepare_refinement(prepared, scores, fundamentals, config['entry_filters'], config['min_other_peers'])
    latest = scores[scores.session.eq(signal)].sort_values(['industry_score', 'security_id'], ascending=[False, True])
    price_index = panel.set_index(['session', 'security_id'])
    candidates = []
    for row in latest[latest.candidate_v2].to_dict('records'):
        price = price_index.loc[(signal, row['security_id'])]
        candidates.append({'symbol': row['symbol'], 'security_id': row['security_id'], 'issuer_id': row['issuer_id'],
                           'industry': row['industry'], 'rank': int(row['rank_e01']), 'score': row['industry_score'],
                           'entry_gap': row['entry_gap'], 'signal_price': price['close'], 'raw_close': price['close'],
                           'raw_return': row['raw_return'], 'peer_return': row['peer_return'],
                           'tr_close': price['tr_close'], 'signal_session': signal})
    feature_index = exit_features.set_index(['session', 'security_id'])
    known = scores.set_index(['session', 'security_id'])
    observations, securities = {}, {}
    last_sessions = sorted(panel.session.unique())[-OBSERVATION_SESSIONS:]
    for security in master.itertuples():
        rows = panel[panel.security_id.eq(security.security_id)].sort_values('session').copy()
        rows['own_return'] = rows.tr_close.pct_change(fill_method=None)
        split_basis = rows.split_close / rows.close
        rows['split_ratio'] = split_basis / split_basis.shift(1)
        observations[security.symbol] = []
        for row in rows[rows.session.isin(last_sessions)].to_dict('records'):
            key = (row['session'], security.security_id)
            feature = feature_index.loc[key] if key in feature_index.index else {}
            residual = feature.get('residual_log_return', np.nan)
            own = row['own_return']
            peer = np.expm1(np.log1p(own) - residual) if pd.notna(own) and pd.notna(residual) else None
            observations[security.symbol].append({'session': row['session'], 'open': row['open'], 'close': row['close'],
                'raw_close': row['close'], 'tr_close': row['tr_close'],
                'total_return_index': row['tr_close'], 'adjusted_open': row['adj_open'],
                'split_close': row['split_close'], 'split_open': row['split_open'],
                'split_ratio': row['split_ratio'], 'cash_dividend': None,
                'corporate_action_quality': 'adjusted_price_proxy_not_entitlement_ledger',
                'close_time': iso(session_bounds(row['session'])[1]),
                'residual_log_return': residual, 'peer_return': peer,
                'fundamental_exit': bool(feature.get('fundamental_block', False)),
                'fundamental_block': bool(feature.get('fundamental_block', False)),
                'fundamental_known': bool(feature.get('fundamental_known', False)),
                # Missing peers disable relative-path triggers, not an observed
                # price stop or an independently known SEC risk event.
                'quality': 'ready', 'residual_quality': 'ready' if pd.notna(residual) else 'missing'})
        current = price_index.loc[(signal, security.security_id)]
        score = known.loc[(signal, security.security_id)] if (signal, security.security_id) in known.index else {}
        risk = feature_index.loc[(signal, security.security_id)]
        securities[security.security_id] = {'symbol': security.symbol, 'issuer_id': security.issuer_id,
            'industry': security.industry, 'raw_close': current['close'], 'tr_close': current['tr_close'],
            'fundamental_known': bool(risk.get('fundamental_known', False)),
            'fundamental_block': bool(risk.get('fundamental_block', False)),
            'risk_class': score.get('risk_class', 'unknown'), 'residual_history': observations[security.symbol]}
    security_master = {r.symbol: {'security_id': r.security_id, 'issuer_id': r.issuer_id, 'industry': r.industry}
                       for r in master.itertuples()}
    # Seal the daily membership and raw quote baselines separately from the
    # unchanged daily strategy. Live snapshots must not select a different peer
    # benchmark merely because one constituent's quote is missing or stale.
    latest_prepared = prepared[prepared.session.eq(signal)].copy()
    eligible_rows = latest_prepared[
        latest_prepared.eligible.eq(True) & latest_prepared.close.gt(0)
        & latest_prepared.volume.gt(0) & latest_prepared.tr_close.gt(0)
        & np.isfinite(latest_prepared.close) & np.isfinite(latest_prepared.tr_close)]
    if 'interpolated' in eligible_rows:
        eligible_rows = eligible_rows[~eligible_rows.interpolated.fillna(False).astype(bool)]
    eligible_ids = set(eligible_rows.security_id)
    intraday_reference = {}
    for security in master.itertuples():
        price = price_index.loc[(signal, security.security_id)]
        peers = eligible_rows[(eligible_rows.industry == security.industry)
                              & eligible_rows.security_id.ne(security.security_id)]
        intraday_reference[security.symbol] = {
            'session': signal, 'security_id': security.security_id, 'issuer_id': security.issuer_id,
            'industry': security.industry, 'raw_close': price['close'], 'tr_close': price['tr_close'],
            'eligible': security.security_id in eligible_ids,
            'peer_symbols': sorted(peers.symbol.tolist()), 'minimum_peers': config['min_other_peers']}
    return _clean({'schema': SCHEMA, 'strategy_id': STRATEGY_ID, 'strategy_version': config['strategy_version'],
        'status': 'ready', 'quality': 'ready', 'blocked_reasons': [], 'created_at': iso(stamp), 'as_of': iso(stamp),
        **dates, 'config_hash': config_hash or _hash(config), 'metadata_hash': metadata_hash,
        'universe_hash': _hash(security_master), 'source': 'alpaca_sip_daily+SEC', 'feed': 'sip',
        'market_data_refreshed': True, 'sec_refreshed': True, 'universe_count': len(master),
        'coverage': coverage[coverage.session.eq(signal)].to_dict('records')[0],
        'candidates': candidates, 'observations': observations, 'securities': securities, 'security_master': security_master,
        'intraday_reference': intraday_reference, 'intraday_reference_version': REFERENCE_VERSION,
        'screening': latest[['symbol', 'entry_rejection_reason', 'risk_class', 'candidate_v2']].to_dict('records'),
        'rules': {'entry_filters': config['entry_filters'], 'dynamic_exit': config['dynamic_exit'],
                  'max_positions': config['max_positions'], 'max_entries_per_day': config['max_entries_per_day'],
                  'max_security_weight': config['max_security_weight'], 'max_issuer_weight': config['max_issuer_weight'],
                  'max_industry_weight': config['max_industry_weight'], 'max_daily_buy_weight': config['max_daily_buy_weight']},
        'limitations': ['Fixed 190-name survivor universe and frozen SIC proxy.',
            'SEC disclosed-risk veto cannot identify all fundamental news at event time.',
            'Daily close triggers; execution quotes and actual fill prices must be checked separately.']})


@contextmanager
def _build_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.build.lock').open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('reversal_daily_builder_already_running') from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _publish(root, packet):
    packet = _clean(packet)
    packet_id = _hash(packet)
    packet['packet_id'] = packet_id
    path = root / 'packets' / (packet_id + '.json')
    if not path.exists():
        _atomic_json(path, packet)
    _atomic_json(root / 'latest.json', {'packet_id': packet_id})
    return packet


def build_runtime_packet(now=None, data_dir=None):
    """Fresh SIP + SEC GET requests; no account mutation or order endpoint.

    Call from a dedicated daily worker/queued research job, never the quote loop.
    A completed session packet is reused as immutable; failed attempts may retry.
    After the entry window, fresh data may still recover holding-exit checks.
    The loader keeps that packet expired and ineligible for any new entry.
    """
    started = as_utc(now)
    root = _root(data_dir)
    with _build_lock(root):
        existing = load_runtime_packet(started, root)
        if (existing.get('status') == 'ready' or
                (existing.get('status') == 'expired' and
                 existing.get('blocked_reasons') == ['execution_window_expired'])):
            return existing
        dates = expected_sessions(started)
        attempt = root / 'attempts' / (started.strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8])
        attempt.mkdir(parents=True)
        def progress(**fields):
            _atomic_json(root / 'build_status.json', {'status': 'building', 'started_at': iso(started),
                         'updated_at': iso(as_utc()), 'signal_session': dates['signal_session'], **fields})
        config_hash, metadata_hash = _file_hash(CONFIG_PATH), _metadata_hash()
        config = _read(CONFIG_PATH)
        try:
            master = _master()
            progress(stage='starting', universe_count=len(master))
            provider = provider_from_environment()
            try:
                panel = _fetch_bars(provider, master, attempt, dates['signal_session'], progress)
            finally:
                provider.close()
            fundamentals = _fetch_fundamentals(master, attempt, dates['signal_session'], progress)
            progress(stage='calculating_signals')
            completed = as_utc() if now is None else started
            if expected_sessions(completed) != dates:
                raise ValueError('build_crossed_signal_session_boundary')
            if _file_hash(CONFIG_PATH) != config_hash or _metadata_hash() != metadata_hash:
                raise ValueError('configuration_changed_during_build')
            packet = packet_from_frames(panel, fundamentals, master, config, completed,
                                        config_hash=config_hash, metadata_hash=metadata_hash)
            packet['build_started_at'] = iso(started)
            packet['built_after_entry_deadline'] = completed >= as_utc(dates['expires_at'])
            packet['input_hashes'] = {str(p.relative_to(attempt)): _file_hash(p) for p in attempt.rglob('*') if p.is_file()}
            packet['input_directory'] = str(attempt)
            packet = _publish(root, packet)
            loaded = load_runtime_packet(completed, root)
            _atomic_json(root / 'build_status.json', {'status': loaded['status'], 'updated_at': iso(as_utc()),
                         'signal_session': dates['signal_session'], 'packet_id': packet['packet_id'],
                         'candidate_count': len(packet['candidates'])})
            return loaded
        except Exception as exc:
            # Only locally generated codes are retained. Provider payloads and
            # arbitrary transport messages are deliberately excluded.
            reason = str(exc) if type(exc) is ValueError and str(exc).isascii() and len(str(exc)) < 200 else type(exc).__name__
            packet = {'schema': SCHEMA, 'strategy_id': STRATEGY_ID, 'strategy_version': config['strategy_version'],
                'status': 'blocked', 'quality': 'blocked', 'blocked_reasons': [reason], **dates,
                'created_at': iso(started), 'as_of': iso(started), 'config_hash': config_hash,
                'metadata_hash': metadata_hash, 'source': 'alpaca_sip_daily+SEC', 'feed': 'sip',
                'candidates': [], 'observations': {}, 'security_master': {}, 'execution_eligible': False}
            packet = _publish(root, packet)
            _atomic_json(root / 'build_status.json', {'status': 'failed', 'updated_at': iso(as_utc()),
                         'signal_session': dates['signal_session'], 'reason': reason})
            return packet
