"""Financial as-of and filing-event boundary checks use controlled fixtures."""
import numpy as np
import pandas as pd

from trading.reversal_fundamentals import (
    financial_state, issuer_daily_features, next_available_session,
    normalized_facts, normalized_filings,
)


def sessions():
    return pd.bdate_range('2023-01-02', '2025-08-29').strftime('%Y-%m-%d').tolist()


def financial_payload(revenue=110, income=12, filed='2024-02-01', revision=False):
    def items(prior, current):
        rows = [
            {'start': '2022-01-01', 'end': '2022-12-31', 'val': prior,
             'filed': '2023-02-01', 'form': '10-K', 'accn': 'prior'},
            {'start': '2023-01-01', 'end': '2023-12-31', 'val': current,
             'filed': filed, 'form': '10-K', 'accn': 'current'},
        ]
        if revision:
            rows.append({'start': '2023-01-01', 'end': '2023-12-31', 'val': current * .5,
                         'filed': '2024-06-03', 'form': '10-K/A', 'accn': 'revision'})
        return rows
    return {'facts': {'us-gaap': {
        'Revenues': {'units': {'USD': items(100, revenue)}},
        'NetIncomeLoss': {'units': {'USD': items(10, income)}},
    }}}


def test_filed_date_is_not_same_day_available_and_weekend_delays():
    dates = ['2024-02-01', '2024-02-02', '2024-02-05']
    assert next_available_session('2024-02-01', dates) == '2024-02-02'
    assert next_available_session('2024-02-02', dates) == '2024-02-05'
    assert next_available_session('2024-02-03', dates) == '2024-02-05'
    assert next_available_session('2024-02-05', dates) is None


def test_future_revision_cannot_change_earlier_feature_or_same_filing_day():
    dates = sessions()
    old = normalized_facts(financial_payload(), dates)
    revised = normalized_facts(financial_payload(revision=True), dates)
    a = {r['session']: r for r in issuer_daily_features(old, [], dates)}
    b = {r['session']: r for r in issuer_daily_features(revised, [], dates)}
    assert not a['2024-02-01']['financial_known']
    assert a['2024-02-02']['financial_known']
    assert abs(a['2024-02-02']['revenue_yoy'] - .1) < 1e-10
    assert a['2024-06-03'] == b['2024-06-03']
    assert b['2024-06-04']['revenue'] == 55
    assert b['2024-06-04']['revenue_accession'] == 'revision'
    assert b['2024-06-04']['financial_filed_max'] == '2024-06-03'


def test_currency_or_missing_revenue_is_unknown_not_zero_decline():
    payload = financial_payload()
    payload['facts']['us-gaap']['Revenues']['units']['EUR'] = payload['facts']['us-gaap']['Revenues']['units'].pop('USD')
    dates = sessions()
    rows = issuer_daily_features(normalized_facts(payload, dates), [], dates)
    assert not any(r['financial_known'] for r in rows)
    assert all(r['fundamental_status'] == 'unknown_no_comparable_revenue_income' for r in rows)


def test_financials_stale_by_period_end_not_latest_republication():
    payload = financial_payload(filed='2025-05-01')
    dates = sessions()
    result = {r['session']: r for r in issuer_daily_features(normalized_facts(payload, dates), [], dates)}
    assert result['2025-05-02']['fundamental_status'] == 'unknown_stale_financials'
    assert not result['2025-05-02']['financial_known']


def test_revenue_and_income_must_have_identical_period_windows():
    payload = financial_payload()
    payload['facts']['us-gaap']['NetIncomeLoss']['units']['USD'][1]['start'] = '2023-01-02'
    facts = normalized_facts(payload, sessions())
    state = {(r['metric'], r['concept'], r['start'], r['end']): r for r in facts}
    assert financial_state(state)['fundamental_status'] != 'known'


def test_material_events_remain_known_even_if_filing_history_incomplete():
    dates = sessions()
    payload = {'accessionNumber': ['earn', 'risk'], 'filingDate': ['2024-02-01', '2024-02-02'],
               'form': ['8-K', '8-K'], 'items': ['2.02,9.01', '1.05,4.02'],
               'acceptanceDateTime': ['2024-02-01T22:00:00Z', '2024-02-02T23:00:00Z']}
    filings = normalized_filings([payload], dates)
    result = {r['session']: r for r in issuer_daily_features([], filings, dates, history_complete=False)}
    assert result['2024-02-01']['earnings_age_sessions'] is None
    assert result['2024-02-02']['earnings_age_sessions'] == 0
    assert result['2024-02-02']['risk_event_age_sessions'] is None
    assert result['2024-02-05']['risk_event_age_sessions'] == 0
    assert result['2024-02-06']['risk_event_age_sessions'] == 1
    assert result['2024-02-05']['latest_risk_items'] == '1.05|4.02'
    assert result['2024-02-05']['events_known'] is False
    assert result['2024-02-05']['financial_known'] is False


def test_financial_filing_and_amendment_also_start_earnings_cooldown():
    dates = sessions()
    payload = {'accessionNumber': ['x'], 'filingDate': ['2024-02-01'],
               'form': ['10-Q/A'], 'items': ['']}
    records = normalized_filings([payload, payload], dates)
    assert len(records) == 1
    assert records[0]['is_earnings'] is True
    assert records[0]['risk_items'] == ''


def test_negative_prior_income_uses_absolute_denominator_and_loss_is_visible():
    payload = financial_payload(income=-5)
    payload['facts']['us-gaap']['NetIncomeLoss']['units']['USD'][0]['val'] = -10
    facts = normalized_facts(payload, sessions())
    state = {(r['metric'], r['concept'], r['start'], r['end']): r for r in facts}
    result = financial_state(state)
    assert result['net_income'] == -5
    assert result['net_income_yoy'] == .5
    assert result['financial_period_kind'] == 'annual'


def test_frame_labels_never_override_filing_time_or_period_matching():
    payload = financial_payload()
    for concept in payload['facts']['us-gaap'].values():
        for item in concept['units']['USD']:
            item['frame'] = 'CY1900'
    facts = normalized_facts(payload, sessions())
    assert len(facts) == 4
    assert max(f['available_session'] for f in facts) == '2024-02-02'


def test_later_quarter_is_preferred_to_older_annual_when_jointly_comparable():
    payload = financial_payload()
    for concept, values in [('Revenues', (30, 33)), ('NetIncomeLoss', (3, 4))]:
        for year, value in zip((2023, 2024), values):
            payload['facts']['us-gaap'][concept]['units']['USD'].append({
                'start': f'{year}-01-01', 'end': f'{year}-03-31', 'val': value,
                'filed': '2024-05-01', 'form': '10-Q', 'accn': 'quarter'})
    facts = normalized_facts(payload, sessions())
    state = {(r['metric'], r['concept'], r['start'], r['end']): r for r in facts}
    result = financial_state(state)
    assert result['financial_period_kind'] == 'quarter'
    assert result['financial_period_end'] == '2024-03-31'
    assert np.isclose(result['revenue_yoy'], .1)


def test_conflicting_values_with_same_accession_and_period_are_unknown():
    payload = financial_payload()
    entries = payload['facts']['us-gaap']['Revenues']['units']['USD']
    entries.append({**entries[-1], 'val': 99})
    facts = normalized_facts(payload, sessions())
    assert any(f['ambiguous_value'] for f in facts)
    rows = issuer_daily_features(facts, [], sessions())
    assert not any(r['financial_known'] for r in rows)


def test_newer_report_without_comparable_facts_cannot_keep_old_healthy_status():
    dates = sessions()
    filings = normalized_filings([{'accessionNumber': ['new-quarter'],
                                  'filingDate': ['2024-05-01'], 'form': ['10-Q'],
                                  'reportDate': ['2024-03-31'], 'items': ['']}], dates)
    result = {r['session']: r for r in issuer_daily_features(
        normalized_facts(financial_payload(), dates), filings, dates)}
    assert result['2024-05-01']['financial_known']
    assert result['2024-05-02']['fundamental_status'] == 'unknown_latest_report_not_comparable'
    assert result['2024-05-02']['events_known']


def test_uncategorized_8k_is_event_unknown_for_20_sessions():
    dates = sessions()
    filings = normalized_filings([{'accessionNumber': ['unknown'],
                                  'filingDate': ['2024-05-01'], 'form': ['8-K'],
                                  'items': ['']}], dates)
    result = issuer_daily_features([], filings, dates)
    first = dates.index('2024-05-02')
    assert result[first - 1]['events_known']
    assert all(not r['events_known'] for r in result[first:first + 20])
    assert result[first + 20]['events_known']


def test_zero_base_profit_is_explicitly_unknown_and_never_healthy():
    payload = financial_payload()
    payload['facts']['us-gaap']['NetIncomeLoss']['units']['USD'][0]['val'] = 0
    result = {r['session']: r for r in issuer_daily_features(
        normalized_facts(payload, sessions()), [], sessions())}
    row = result['2024-02-02']
    assert row['fundamental_status'] == 'unknown_zero_prior_net_income'
    assert row['financial_known'] is False
    assert row['net_income_yoy'] is None
