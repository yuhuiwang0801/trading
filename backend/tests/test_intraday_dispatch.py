"""Read-only intraday context uses sealed peers and fails closed per holding."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading.intraday_market import build_context, peer_quote_symbols
from trading.reversal_dispatch import quote_frame
from trading import reversal_runtime


NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
SYMBOLS = ['OWN', 'P1', 'P2', 'P3', 'P4', 'P5']


def packet():
    master = {symbol: {'security_id': f'id-{symbol}', 'issuer_id': f'issuer-{symbol}', 'industry': '35'}
              for symbol in SYMBOLS}
    return {'strategy_id': 'reversal_v2', 'signal_session': '2026-10-29', 'candidates': [],
            'intraday_reference_version': 1,
            'security_master': master,
            'intraday_reference': {symbol: {**master[symbol], 'session': '2026-10-29',
                'raw_close': '100', 'tr_close': '97', 'eligible': True,
                'peer_symbols': [peer for peer in SYMBOLS if peer != symbol]} for symbol in SYMBOLS}}


def positions():
    return [{'symbol': 'OWN', 'strategy_id': 'reversal_v2', 'quantity': '2',
             'entry_session': '2026-10-28', 'issuer_id': 'issuer-OWN', 'industry': '35'}]


def quotes():
    return {symbol: {'symbol': symbol, 'source': 'alpaca', 'feed': 'sip', 'quality': 'valid',
                     'bid': '100.99', 'ask': '101.01', 'timestamp': NOW.isoformat(), 'tradable': True}
            for symbol in SYMBOLS}


def context(p=None, q=None, holdings=None, actions=None, checked=True, now=NOW):
    return build_context(p or packet(), holdings if holdings is not None else positions(),
                         q if q is not None else quotes(), now,
                         corporate_actions=[] if actions is None else actions,
                         corporate_actions_checked=checked)['OWN']


def test_fixed_peers_equal_weight_simple_returns_and_bid_evidence():
    q = quotes()
    q['P1'].update(bid='104.99', ask='105.01')
    result = context(q=q)
    assert result['status'] == 'ready'
    assert result['price_basis_valid'] and result['relative_eligible']
    assert Decimal(result['peer_return']) == Decimal('.018')
    assert result['stock_bid'] == '100.99'
    assert result['peer_count'] == 5 and 'OWN' not in result['peer_symbols']
    assert result['reference_session'] == '2026-10-29'
    assert result['session'] == '2026-10-30'
    assert datetime.fromisoformat(result['oldest_quote_at']) == NOW
    assert 'delayed' in result['corporate_action_limitations']


@pytest.mark.parametrize('mutation', ['missing', 'stale', 'future', 'wide', 'nan', 'untradable', 'timezone_missing'])
def test_bad_peer_disables_relative_without_disabling_own_price_risk(mutation):
    q = quotes()
    if mutation == 'missing': del q['P1']
    elif mutation == 'stale': q['P1']['timestamp'] = (NOW - timedelta(seconds=31)).isoformat()
    elif mutation == 'future': q['P1']['timestamp'] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == 'wide': q['P1']['ask'] = '110'
    elif mutation == 'nan': q['P1']['ask'] = 'NaN'
    elif mutation == 'untradable': q['P1']['tradable'] = False
    elif mutation == 'timezone_missing': q['P1']['timestamp'] = NOW.replace(tzinfo=None).isoformat()
    result = context(q=q)
    assert result['status'] == 'blocked' and not result['relative_eligible']
    assert result['price_basis_valid']
    assert result['peer_return'] is None


@pytest.mark.parametrize('age,eligible', [(10, True), (11, False)])
def test_cross_symbol_quote_time_alignment(age, eligible):
    q = quotes()
    q['P1']['timestamp'] = (NOW - timedelta(seconds=age)).isoformat()
    result = context(q=q)
    assert result['relative_eligible'] is eligible
    assert result['price_basis_valid']
    if eligible: assert datetime.fromisoformat(result['oldest_quote_at']) == NOW - timedelta(seconds=age)


def test_exact_30_second_quote_age_passes_and_older_fails():
    q = quotes()
    for item in q.values(): item['timestamp'] = (NOW - timedelta(seconds=30)).isoformat()
    assert context(q=q)['relative_eligible']
    q['OWN']['timestamp'] = (NOW - timedelta(seconds=30, microseconds=1)).isoformat()
    assert not context(q=q)['price_basis_valid']


@pytest.mark.parametrize('mutation', ['self', 'duplicate_symbol', 'duplicate_id', 'wrong_industry', 'ineligible', 'wrong_date', 'too_few'])
def test_peer_membership_never_backfills_or_silently_shrinks(mutation):
    p = packet()
    if mutation == 'self': p['intraday_reference']['OWN']['peer_symbols'][0] = 'OWN'
    elif mutation == 'duplicate_symbol': p['intraday_reference']['OWN']['peer_symbols'][-1] = 'P1'
    elif mutation == 'duplicate_id':
        p['security_master']['P2']['security_id'] = 'id-P1'
        p['intraday_reference']['P2']['security_id'] = 'id-P1'
    elif mutation == 'wrong_industry': p['security_master']['P2']['industry'] = '99'
    elif mutation == 'ineligible': p['intraday_reference']['P2']['eligible'] = False
    elif mutation == 'wrong_date': p['intraday_reference']['P2']['session'] = '2026-10-28'
    elif mutation == 'too_few': p['intraday_reference']['OWN']['peer_symbols'].pop()
    result = context(p=p)
    assert not result['relative_eligible'] and result['price_basis_valid']


def test_missing_or_old_reference_only_disables_relative():
    p = packet()
    p.pop('intraday_reference')
    result = context(p=p)
    assert result['reason'] == 'intraday_reference_unavailable'
    assert result['price_basis_valid'] and not result['relative_eligible']
    p = packet()
    p['signal_session'] = '2026-10-28'
    assert context(p=p)['reason'] == 'intraday_reference_stale'


@pytest.mark.parametrize('kind', ['cash_dividends', 'forward_splits', 'reverse_splits', 'spin_offs', 'mergers'])
def test_any_own_action_since_entry_disables_raw_triggers(kind):
    result = context(actions=[{'action_type': kind, 'source_symbol': 'OWN', 'ex_date': '2026-10-28'}])
    assert result['reason'] == 'holding_corporate_action_requires_normalization'
    assert not result['price_basis_valid'] and not result['relative_eligible']


def test_own_action_before_entry_does_not_change_entry_cost_basis():
    result = context(actions=[{'action_type': 'forward_splits', 'symbol': 'OWN', 'ex_date': '2026-10-27'}])
    assert result['price_basis_valid'] and result['relative_eligible']


def test_peer_action_today_blocks_relative_but_prior_action_is_already_in_reference():
    action = {'action_type': 'cash_dividends', 'symbol': 'P1', 'ex_date': '2026-10-30'}
    result = context(actions=[action])
    assert result['price_basis_valid'] and not result['relative_eligible']
    assert result['reason'] == 'peer_corporate_action_requires_normalization'
    action['ex_date'] = '2026-10-29'
    assert context(actions=[action])['relative_eligible']


def test_action_read_failure_unknown_date_and_malformed_event_fail_closed():
    assert not context(checked=False)['price_basis_valid']
    assert not context(actions=[{'symbol': 'OWN', 'action_type': 'splits'}])['price_basis_valid']
    assert not context(actions=[None])['price_basis_valid']
    assert not context(actions=[{'action_type': 'cash_dividends', 'ex_date': '2026-10-30'}])['price_basis_valid']


@pytest.mark.parametrize('event', [{}, {'action_type': 'forward_splits', 'symbol': None, 'ex_date': '2026-10-30'},
                                  {'symbol': 7, 'ex_date': '2026-10-30'},
                                  {'unknown_symbol_field': 'OWN', 'ex_date': '2026-10-30'}])
def test_unattributable_actions_cannot_be_assumed_unrelated(event):
    result = context(actions=[event])
    assert not result['price_basis_valid'] and not result['relative_eligible']


def test_unrelated_action_description_never_matches_a_holding_symbol():
    result = context(actions=[{'action_type': 'forward_splits', 'symbol': 'OTHER',
                               'description': 'OWN', 'ex_date': '2026-10-30'}])
    assert result['price_basis_valid'] and result['relative_eligible']


def test_outside_session_and_invalid_entry_are_not_price_basis_valid():
    assert not context(now=NOW.replace(hour=12))['price_basis_valid']
    holdings = positions()
    holdings[0]['entry_session'] = '2026-11-02'
    assert not context(holdings=holdings)['price_basis_valid']


def test_manual_holdings_and_closed_positions_never_get_context_or_peer_queries():
    holdings = [{'symbol': 'OWN', 'quantity': '2'}, {**positions()[0], 'quantity': '0'}]
    assert peer_quote_symbols(packet(), holdings) == set()
    assert build_context(packet(), holdings, quotes(), NOW, corporate_actions=[], corporate_actions_checked=True) == {}


class Provider:
    feed = 'sip'

    def __init__(self, fail_actions=False):
        self.calls = []
        self.fail_actions = fail_actions

    def assets(self):
        self.calls.append(('assets',))
        return [{'symbol': symbol, 'tradable': True, 'status': 'active'} for symbol in SYMBOLS + ['CANDIDATE', 'MANUAL']]

    def corporate_actions(self, symbols, start, end):
        self.calls.append(('actions', symbols, start, end))
        if self.fail_actions: raise RuntimeError('secret-like provider message never echoed')
        return []

    def quotes(self, symbols):
        self.calls.append(('quotes', symbols))
        return {symbol: {'bp': '100.99', 'ap': '101.01', 't': NOW.isoformat(), 'bs': 10, 'as': 10}
                for symbol in symbols}


def test_dispatch_fetches_peers_only_for_owned_v2_and_batches_with_candidates():
    provider, p = Provider(), packet()
    p['candidates'] = [{'symbol': 'CANDIDATE'}]
    frame = quote_frame(provider, p, {'symbols': ['CANDIDATE']},
                        positions() + [{'symbol': 'MANUAL', 'quantity': '10'}], now=NOW)
    assert len([c for c in provider.calls if c[0] == 'quotes']) == 1
    action = next(c for c in provider.calls if c[0] == 'actions')
    assert action == ('actions', sorted(SYMBOLS), '2025-10-28', '2027-10-30')
    assert provider.calls.index(action) < next(i for i, c in enumerate(provider.calls) if c[0] == 'quotes')
    assert set(frame['quotes']) == set(SYMBOLS + ['CANDIDATE', 'MANUAL'])
    assert set(frame['intraday_context']) == {'OWN'}
    assert frame['intraday_context']['OWN']['relative_eligible']
    assert frame['intraday_context']['OWN']['corporate_action_query'] == {
        'start': '2025-10-28', 'end': '2027-10-30', 'date_basis': 'provider_process_date',
        'coverage_guaranteed': False}


@pytest.mark.parametrize('missing_method', [False, True])
def test_missing_or_failing_action_endpoint_preserves_quotes_and_daily_path(missing_method):
    provider = Provider(fail_actions=True)
    if missing_method: provider.corporate_actions = None
    frame = quote_frame(provider, packet(), {'symbols': SYMBOLS}, positions(), now=NOW)
    assert frame['quality'] == 'valid' and 'OWN' in frame['quotes']
    assert frame['intraday_context']['OWN']['reason'] == 'corporate_actions_unavailable'
    assert not frame['intraday_context']['OWN']['price_basis_valid']
    assert 'secret-like' not in str(frame)


def test_no_held_v2_means_no_action_or_peer_fetch():
    provider, p = Provider(), packet()
    p['candidates'] = [{'symbol': 'OWN'}]
    result = quote_frame(provider, p, {'symbols': ['OWN']}, now=NOW)
    assert result['intraday_context'] == {}
    assert not any(c[0] == 'actions' for c in provider.calls)
    assert next(c[1] for c in provider.calls if c[0] == 'quotes') == ['OWN']


def test_runtime_reference_is_frozen_from_effective_eligible_membership(monkeypatch):
    from test_reversal_runtime import frames
    panel, fundamentals, master, cfg = frames()
    first = reversal_runtime.packet_from_frames(panel, fundamentals, master, cfg, '2024-02-20T23:00:00Z')
    ref = first['intraday_reference']['S0']
    assert ref['peer_symbols'] == ['S1', 'S2', 'S3', 'S4', 'S5']
    assert ref['session'] == '2024-02-20'
    assert ref['raw_close'] == first['observations']['S0'][-1]['raw_close']
    assert ref['security_id'] == 'S0' and ref['eligible'] is True
    original = reversal_runtime.prepare_signals
    def with_ineligible_peer(*args, **kwargs):
        prepared, scores, coverage = original(*args, **kwargs)
        prepared.loc[prepared.symbol.eq('S5') & prepared.session.eq('2024-02-20'), 'eligible'] = False
        return prepared, scores, coverage
    monkeypatch.setattr(reversal_runtime, 'prepare_signals', with_ineligible_peer)
    second = reversal_runtime.packet_from_frames(panel, fundamentals, master, cfg, '2024-02-20T23:00:00Z')
    assert second['intraday_reference']['S0']['peer_symbols'] == ['S1', 'S2', 'S3', 'S4']
    assert second['intraday_reference']['S5']['eligible'] is False


def test_context_is_pure_and_does_not_change_reference_or_accumulate_returns():
    p, holdings, q = packet(), positions(), quotes()
    before = deepcopy((p, holdings, q))
    first = context(p, q, holdings)
    second = context(p, q, holdings)
    assert first == second
    assert (p, holdings, q) == before


@pytest.mark.parametrize('version_change', ['old_schema', 'missing_reference', 'wrong_reference_version'])
def test_sealed_packet_upgrade_rebuilds_once_without_overwriting_old_packet(tmp_path, monkeypatch, version_change):
    from test_reversal_runtime import mock_fresh_builder, packet as daily_packet
    from trading import research_worker, config
    calls, _ = mock_fresh_builder(tmp_path, monkeypatch)
    legacy = daily_packet()
    if version_change == 'old_schema':
        legacy['schema'] = 'reversal_v2_runtime_packet_v1'
        legacy.pop('intraday_reference')
        legacy.pop('intraday_reference_version')
    elif version_change == 'missing_reference': legacy.pop('intraday_reference')
    else: legacy['intraday_reference_version'] = 999
    root = tmp_path / 'reversal' / 'runtime_v2'
    sealed = reversal_runtime._publish(root, legacy)
    old_file = root / 'packets' / (sealed['packet_id'] + '.json')
    before = old_file.read_bytes()
    loaded = reversal_runtime.load_runtime_packet('2024-02-21T15:00:00Z', root)
    assert loaded['status'] == 'blocked' and not loaded['execution_eligible']
    assert 'intraday_reference_upgrade_required' in loaded['blocked_reasons']
    assert loaded['observations'] == legacy['observations']

    class Store:
        def __init__(self): self.data = {}
        def get(self, key, default=None): return self.data.get(key, default)
        def put(self, key, value): self.data[key] = value
        def jobs(self, kind): return []
        def enqueue(self, kind, payload): return {'id': 'one-upgrade-job', 'kind': kind, 'status': 'queued'}
    store = Store()
    monkeypatch.setattr(config, 'SOURCE', 'alpaca')
    monkeypatch.setattr(config, 'DATA', tmp_path)
    now = datetime(2024, 2, 21, 15, tzinfo=timezone.utc)
    assert research_worker.schedule_reversal_refresh(store, now)['id'] == 'one-upgrade-job'
    fresh = reversal_runtime.build_runtime_packet(now, root)
    assert fresh['schema'] == reversal_runtime.SCHEMA
    assert fresh['intraday_reference_version'] == 1
    assert fresh['packet_id'] != sealed['packet_id']
    assert fresh['config_hash'] == legacy['config_hash']
    assert fresh['status'] == 'expired'  # Rebuilding never reopens today's buy window.
    assert old_file.read_bytes() == before
    assert calls == ['provider', 'closed']
    assert research_worker.schedule_reversal_refresh(store, now + timedelta(hours=1)) is None
    assert reversal_runtime.build_runtime_packet(now + timedelta(hours=1), root)['packet_id'] == fresh['packet_id']
    assert calls == ['provider', 'closed']
