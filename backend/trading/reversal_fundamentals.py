"""SEC filing-time financial/event observations for exploratory reversal research.

This module never uses SEC frames (which select latest-filed values). Companyfacts
observations retain accession and filing date. A value becomes usable only at
the close of the first trading session STRICTLY AFTER its filed date. This
deliberate extra delay avoids assuming reliable historical intraday timestamps.
It does not make the current security master point-in-time or capture all news.
"""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_right
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time

import httpx
import pandas as pd

SEC_API = 'https://www.sec.gov/search-filings/edgar-application-programming-interfaces'
FORM_8K = 'https://www.sec.gov/files/form8-k.pdf'
REVENUE_TAGS = ('RevenueFromContractWithCustomerExcludingAssessedTax',
                'RevenueFromContractWithCustomerIncludingAssessedTax',
                'Revenues', 'SalesRevenueNet')
PROFIT_TAGS = ('NetIncomeLoss', 'ProfitLoss')
# Risk flags are conservative event-category screens, not sentiment labels.
RISK_ITEMS = {'1.03', '1.05', '2.04', '2.05', '2.06', '3.01', '4.01', '4.02'}
FINANCIAL_FORMS = {'10-K', '10-K/A', '10-Q', '10-Q/A'}


def _read(path):
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with gzip.open(temp, 'wt', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))
    temp.replace(path)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch_json(client, url, path, provenance):
    """Resumable read-only SEC fetch, sequentially at most 4.5 requests/sec."""
    path = Path(path)
    if not path.exists():
        for attempt in range(4):
            time.sleep(.23 if attempt == 0 else 2 ** attempt)
            try:
                response = client.get(url, follow_redirects=False)
                response.raise_for_status()
                payload = response.json()
                _write(path, payload)
                break
            except (httpx.HTTPError, ValueError):
                if attempt == 3:
                    raise
    payload = _read(path)
    provenance[str(path)] = {
        'url': url, 'sha256': file_hash(path),
        'downloaded_or_cache_observed_at': datetime.now(timezone.utc).isoformat(),
        'method': 'GET',
    }
    return payload


def download_sec_fundamentals(snapshot_root, output_root, start='2016-01-01',
                              end='2026-10-26', user_agent=None):
    """Acquire company facts and complete relevant filings with raw provenance."""
    snapshot_root, output_root = Path(snapshot_root), Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    master = pd.read_csv(snapshot_root / 'security_master.csv', dtype=str).fillna('')
    provenance, coverage = {}, []
    with httpx.Client(headers={'User-Agent': user_agent or
                              'NorthstarTradingResearch/2.0 (personal research)'},
                      timeout=httpx.Timeout(60, connect=15)) as client:
        for number, (cik, group) in enumerate(master.groupby('issuer_id'), 1):
            if not cik:
                continue
            cik = cik.zfill(10)
            row = {'issuer_id': cik, 'symbols': ','.join(group.symbol),
                   'companyfacts_ok': False, 'submissions_ok': False,
                   'history_complete': False, 'error': ''}
            try:
                # Reuse the immutable market snapshot's contemporaneous SEC master.
                cached = snapshot_root / 'raw' / 'sec' / f'CIK{cik}.json.gz'
                recent_path = output_root / 'raw' / 'submissions' / f'CIK{cik}.json.gz'
                if cached.exists() and not recent_path.exists():
                    recent_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(cached, recent_path)
                recent = fetch_json(client, f'https://data.sec.gov/submissions/CIK{cik}.json',
                                    recent_path, provenance)
                row['submissions_ok'] = True
                for history in recent.get('filings', {}).get('files', []):
                    if history.get('filingTo', '') < start or history.get('filingFrom', '') > end:
                        continue
                    name = history['name']
                    fetch_json(client, f'https://data.sec.gov/submissions/{name}',
                               output_root / 'raw' / 'submissions' / (name + '.gz'), provenance)
                row['history_complete'] = True
                facts = fetch_json(client, f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',
                                   output_root / 'raw' / 'companyfacts' / f'CIK{cik}.json.gz', provenance)
                row['companyfacts_ok'] = True
                tags = facts.get('facts', {}).get('us-gaap', {})
                row['revenue_tags'] = '|'.join(t for t in REVENUE_TAGS if t in tags)
                row['profit_tags'] = '|'.join(t for t in PROFIT_TAGS if t in tags)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                row['error'] = f'{type(exc).__name__}: {str(exc)[:150]}'
            coverage.append(row)
            pd.DataFrame(coverage).to_csv(output_root / 'download_coverage.csv', index=False)
            (output_root / 'raw_provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
            if number % 10 == 0 or number == master.issuer_id.nunique():
                print(json.dumps({'stage': 'SEC_fundamentals', 'issuers': number,
                                  'total': master.issuer_id.nunique(),
                                  'complete': sum(c['companyfacts_ok'] and c['history_complete'] for c in coverage)}), flush=True)
    return coverage


def next_available_session(filed, sessions):
    """Strictly later session, never same-day information availability."""
    index = bisect_right(sessions, str(filed)[:10])
    return sessions[index] if index < len(sessions) else None


def filing_rows(payload, relevant_only=False):
    columns = payload.get('filings', {}).get('recent', payload)
    accessions = columns.get('accessionNumber', [])
    forms = columns.get('form', [''] * len(accessions))
    relevant_forms = FINANCIAL_FORMS | {'8-K', '8-K/A'}
    for index, accession in enumerate(accessions):
        if relevant_only and forms[index] not in relevant_forms:
            continue
        yield {key: value[index] for key, value in columns.items()
               if isinstance(value, list) and len(value) == len(accessions)} | {'accessionNumber': accession}


def normalized_filings(payloads, sessions):
    records = {}
    for payload in payloads:
        for filing in filing_rows(payload, relevant_only=True):
            filed = filing.get('filingDate', '')
            if not filed:
                continue
            available = next_available_session(filed, sessions)
            if available is None:
                continue
            items = set(re.findall(r'\d\.\d{2}', filing.get('items', '')))
            form = filing.get('form', '')
            records[filing['accessionNumber']] = {
                'accession': filing['accessionNumber'], 'filed': filed,
                'available_session': available, 'form': form,
                'report_date': filing.get('reportDate', ''),
                'items': '|'.join(sorted(items)),
                'is_earnings': form in FINANCIAL_FORMS or (form.startswith('8-K') and '2.02' in items),
                'risk_items': '|'.join(sorted(items & RISK_ITEMS)) if form.startswith('8-K') else '',
                'unknown_event_items': form.startswith('8-K') and not items,
                'acceptanceDateTime_raw': filing.get('acceptanceDateTime', ''),
            }
    return sorted(records.values(), key=lambda r: (r['available_session'], r['accession']))


def normalized_facts(payload, sessions):
    """Retain all as-filed versions; custom taxonomy/foreign currency unknown."""
    records = []
    tags = payload.get('facts', {}).get('us-gaap', {})
    for metric, names in [('revenue', REVENUE_TAGS), ('net_income', PROFIT_TAGS)]:
        for priority, tag in enumerate(names):
            for item in tags.get(tag, {}).get('units', {}).get('USD', []):
                if item.get('form') not in FINANCIAL_FORMS or not item.get('start') or not item.get('filed'):
                    continue
                try:
                    duration = (date.fromisoformat(item['end']) - date.fromisoformat(item['start'])).days + 1
                    value = float(item['val'])
                    if not math.isfinite(value):
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
                kind = 'quarter' if 70 <= duration <= 110 else 'annual' if 330 <= duration <= 400 else None
                if kind is None or item['filed'] < item['end']:
                    continue
                available = next_available_session(item['filed'], sessions)
                if available is None:
                    continue
                records.append({'metric': metric, 'concept': tag, 'priority': priority,
                                'start': item['start'], 'end': item['end'], 'value': value,
                                'filed': item['filed'], 'available_session': available,
                                'accession': item.get('accn', ''), 'kind': kind,
                                'duration_days': duration})
    # If one filing contains conflicting entity-level values for exactly the
    # same concept/period, preserve an unknown marker instead of picking order.
    groups = defaultdict(list)
    for row in records:
        groups[(row['metric'], row['concept'], row['start'], row['end'],
                row['filed'], row['accession'])].append(row)
    unique = []
    for rows in groups.values():
        row = dict(rows[0])
        row['ambiguous_value'] = len({r['value'] for r in rows}) > 1
        if row['ambiguous_value']:
            row['value'] = None
        unique.append(row)
    return sorted(unique, key=lambda r: (r['available_session'], r['filed'], r['accession']))


def comparable_pair(state, metric, kind):
    candidates = []
    records = [r for r in state.values() if r['metric'] == metric and r['kind'] == kind
               and not r.get('ambiguous_value')]
    by_concept = defaultdict(list)
    for row in records:
        by_concept[row['concept']].append(row)
    for concept, rows in by_concept.items():
        rows.sort(key=lambda r: r['end'], reverse=True)
        for current in rows:
            previous = [r for r in rows
                        if 350 <= (date.fromisoformat(current['end']) - date.fromisoformat(r['end'])).days <= 380
                        and abs(current['duration_days'] - r['duration_days']) <= 10]
            if previous:
                prior = max(previous, key=lambda r: (r['filed'], r['accession']))
                candidates.append((current, prior))
                break
    return sorted(candidates, key=lambda p: (p[0]['end'], -p[0]['priority']), reverse=True)


def financial_state(state):
    """Require jointly comparable revenue and income, not unrelated latest facts."""
    pairs = []
    for kind in ('quarter', 'annual'):
        revenues = comparable_pair(state, 'revenue', kind)
        profits = comparable_pair(state, 'net_income', kind)
        for revenue, old_revenue in revenues:
            for profit, old_profit in profits:
                if (revenue['start'], revenue['end'], old_revenue['start'], old_revenue['end']) != (
                    profit['start'], profit['end'], old_profit['start'], old_profit['end']):
                    continue
                if old_revenue['value'] <= 0 or revenue['value'] <= 0:
                    continue
                pairs.append((revenue, old_revenue, profit, old_profit))
    if not pairs:
        return {'fundamental_status': 'unknown_no_comparable_revenue_income'}
    # Prefer most recent period; quarter beats annual only at equal period end.
    revenue, old_revenue, profit, old_profit = max(
        pairs, key=lambda p: (p[0]['end'], p[0]['kind'] == 'quarter', -p[0]['priority'], -p[2]['priority']))
    if any(r.get('ambiguous_value') and r['end'] >= revenue['end'] for r in state.values()):
        return {'fundamental_status': 'unknown_conflicting_reported_values'}
    denominator = abs(old_profit['value'])
    return {
        'fundamental_status': 'known' if denominator else 'unknown_zero_prior_net_income',
        'financial_period_kind': revenue['kind'],
        'financial_period_start': revenue['start'], 'financial_period_end': revenue['end'],
        'financial_prior_period_end': old_revenue['end'],
        'financial_available_session': max(r['available_session'] for r in (revenue, old_revenue, profit, old_profit)),
        'revenue': revenue['value'], 'prior_revenue': old_revenue['value'],
        'revenue_yoy': revenue['value'] / old_revenue['value'] - 1,
        'net_income': profit['value'], 'prior_net_income': old_profit['value'],
        'net_income_yoy': ((profit['value'] - old_profit['value']) / denominator) if denominator else None,
        'revenue_concept': revenue['concept'], 'net_income_concept': profit['concept'],
        'revenue_accession': revenue['accession'], 'net_income_accession': profit['accession'],
        'prior_revenue_accession': old_revenue['accession'], 'prior_net_income_accession': old_profit['accession'],
        'financial_filed_max': max(r['filed'] for r in (revenue, old_revenue, profit, old_profit)),
    }


def issuer_daily_features(facts, filings, sessions, history_complete=True):
    by_session, events = defaultdict(list), defaultdict(list)
    for fact in facts:
        by_session[fact['available_session']].append(fact)
    for event in filings:
        events[event['available_session']].append(event)
    state, financial = {}, {'fundamental_status': 'unknown_no_comparable_revenue_income'}
    last_earnings, last_risk, last_unknown_event, last_risk_items = None, None, None, ''
    latest_report_end = ''
    rows = []
    for index, session in enumerate(sessions):
        for fact in by_session[session]:
            key = fact['metric'], fact['concept'], fact['start'], fact['end']
            state[key] = fact
        if by_session[session]:
            financial = financial_state(state)
        for event in events[session]:
            if event['is_earnings']:
                last_earnings = index
            if event['risk_items']:
                last_risk, last_risk_items = index, event['risk_items']
            if event.get('unknown_event_items'):
                last_unknown_event = index
            if event['form'] in FINANCIAL_FORMS:
                latest_report_end = max(latest_report_end, event.get('report_date', ''))
        row = {'session': session, **financial,
               'filing_history_complete': bool(history_complete),
               'earnings_age_sessions': index - last_earnings if last_earnings is not None else None,
               'risk_event_age_sessions': index - last_risk if last_risk is not None else None,
               'unknown_event_age_sessions': index - last_unknown_event if last_unknown_event is not None else None,
               'latest_risk_items': last_risk_items}
        row['latest_filed_report_period_end'] = latest_report_end
        if financial['fundamental_status'] == 'known':
            age = (date.fromisoformat(session) - date.fromisoformat(financial['financial_period_end'])).days
            row['financial_age_days'] = age
            limit = 200 if financial['financial_period_kind'] == 'quarter' else 460
            if age > limit:
                row['fundamental_status'] = 'unknown_stale_financials'
            if latest_report_end and latest_report_end > financial['financial_period_end']:
                row['fundamental_status'] = 'unknown_latest_report_not_comparable'
        if not history_complete:
            row['fundamental_status'] = 'unknown_incomplete_filing_history'
        row['financial_known'] = row['fundamental_status'] == 'known'
        row['events_known'] = bool(history_complete) and (last_unknown_event is None or index - last_unknown_event >= 20)
        rows.append(row)
    return rows


def build_fundamental_features(snapshot_root, output_root, sessions):
    """Build daily feature table keyed by session/security_id from cached SEC data."""
    snapshot_root, output_root = Path(snapshot_root), Path(output_root)
    sessions = sorted(set(str(s)[:10] for s in sessions))
    master = pd.read_csv(snapshot_root / 'security_master.csv', dtype=str).fillna('')
    downloads = pd.read_csv(output_root / 'download_coverage.csv', dtype={'issuer_id': str}).fillna('')
    complete = {r.issuer_id: bool(r.history_complete) for r in downloads.itertuples()}
    output, observations, all_filings, coverage = [], [], [], []
    for cik, group in master.groupby('issuer_id'):
        cik = cik.zfill(10)
        fact_path = output_root / 'raw' / 'companyfacts' / f'CIK{cik}.json.gz'
        facts = normalized_facts(_read(fact_path), sessions) if fact_path.exists() else []
        submission_paths = sorted((output_root / 'raw' / 'submissions').glob(f'CIK{cik}*.json.gz'))
        filings = normalized_filings([_read(p) for p in submission_paths], sessions)
        rows = issuer_daily_features(facts, filings, sessions, complete.get(cik, False))
        observations.extend({'issuer_id': cik, **r} for r in facts)
        all_filings.extend({'issuer_id': cik, **r} for r in filings)
        for security in group.itertuples():
            output.extend({'security_id': security.security_id, 'symbol': security.symbol,
                           'issuer_id': cik, **r} for r in rows)
            coverage.append({'symbol': security.symbol, 'issuer_id': cik, 'sessions': len(rows),
                             'known_sessions': sum(r['fundamental_status'] == 'known' for r in rows),
                             'known_fraction': sum(r['fundamental_status'] == 'known' for r in rows) / len(rows),
                             'facts': len(facts), 'filings': len(filings),
                             'history_complete': complete.get(cik, False)})
    frame = pd.DataFrame(output)
    frame.to_csv(output_root / 'daily_features.csv.gz', index=False)
    pd.DataFrame(observations).to_csv(output_root / 'fact_observations.csv.gz', index=False)
    pd.DataFrame(all_filings).to_csv(output_root / 'filing_events.csv.gz', index=False)
    pd.DataFrame(coverage).to_csv(output_root / 'feature_coverage.csv', index=False)
    manifest = {
        'schema': 'reversal_SEC_fundamentals_v2', 'created_at': datetime.now(timezone.utc).isoformat(),
        'source_api': SEC_API, 'event_item_source': FORM_8K,
        'security_master_sha256': file_hash(snapshot_root / 'security_master.csv'),
        'code_sha256': file_hash(__file__), 'sessions': [sessions[0], sessions[-1]],
        'security_count': len(master), 'issuer_count': master.issuer_id.nunique(),
        'feature_rows': len(frame), 'known_fraction': float((frame.fundamental_status == 'known').mean()),
        'availability_policy': 'Usable at close of first trading session strictly after SEC filed date. Never same day.',
        'selection_policy': 'As-filed USD US-GAAP 10-K/Q versions only; latest jointly comparable quarter or annual revenue and net income; same concepts and period windows YoY.',
        'risk_items': sorted(RISK_ITEMS),
        'stale_after_period_end_days': {'quarter': 200, 'annual': 460},
        'uncategorized_8k_unknown_sessions': 20,
        'limits': [
            'Current security-to-CIK mapping and SIC remain survivor/look-ahead proxies.',
            'Historical SEC companyfacts is a present-day extraction of historical as-filed facts, not archived daily vendor vintages.',
            'No custom taxonomy, foreign currency, IFRS, analyst estimate, news sentiment or pre-announced earnings calendar.',
            '8-K may be filed several days after the underlying public event; this source cannot block the first news-driven decline in real time.',
            'Risk item categories indicate possible material events, not proven negative economic impact.',
            'Missing or stale comparable financial facts remain unknown; never interpreted as healthy.',
        ],
        'files': {p.name: file_hash(p) for p in output_root.iterdir()
                  if p.is_file() and p.name != 'manifest.json' and p.suffix != '.log'},
    }
    (output_root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
