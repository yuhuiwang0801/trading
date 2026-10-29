"""Runtime packets preserve causal dates, immutable inputs and V2 entry rules."""
import copy
import json
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from trading import reversal_runtime as runtime
from trading.reversal_refinement import prepare_refinement
from trading.reversal_signals import SignalConfig, prepare_signals


def frames():
    config = json.loads(runtime.CONFIG_PATH.read_text())
    config.update(min_history_sessions=3, liquidity_sessions=3)
    days = xcals.get_calendar('XNYS').sessions_in_range('2024-01-03', '2024-02-20').strftime('%Y-%m-%d').tolist()
    master, prices, fundamentals = [], [], []
    for stock in range(6):
        sid = f'S{stock}'
        master.append({'security_id': sid, 'symbol': sid, 'issuer_id': f'I{stock}', 'industry': '35'})
        for index, day in enumerate(days):
            price = 100 * np.exp((-.025 if stock == 0 else .001) * index)
            prices.append({'session': day, 'symbol': sid, 'security_id': sid, 'issuer_id': f'I{stock}',
                'industry': '35', 'open': price, 'close': price, 'volume': 1000000,
                'tr_close': price, 'adj_open': price, 'split_close': price, 'split_open': price,
                'source': 'alpaca', 'feed': 'sip'})
            fundamentals.append({'session': day, 'security_id': sid, 'symbol': sid, 'issuer_id': f'I{stock}',
                'fundamental_status': 'known', 'filing_history_complete': True, 'events_known': True,
                'revenue_yoy': .1, 'net_income': 100, 'net_income_yoy': .1,
                'earnings_age_sessions': np.nan, 'risk_event_age_sessions': np.nan})
    return pd.DataFrame(prices), pd.DataFrame(fundamentals), pd.DataFrame(master), config


def packet():
    return runtime.packet_from_frames(*frames(), now='2024-02-20T23:00:00Z',
                                      config_hash=runtime._file_hash(runtime.CONFIG_PATH),
                                      metadata_hash=runtime._metadata_hash())


def test_runtime_entries_exactly_match_frozen_research_transform():
    prices, fundamentals, master, config = frames()
    prepared, scores, _ = prepare_signals(prices, SignalConfig(min_history_sessions=3, liquidity_sessions=3))
    scored, exits = prepare_refinement(prepared, scores, fundamentals, config['entry_filters'])
    expected = scored[scored.session.eq('2024-02-20') & scored.candidate_v2]
    actual = runtime.packet_from_frames(prices, fundamentals, master, config, '2024-02-20T23:00:00Z')
    assert actual['signal_session'] == '2024-02-20'
    assert actual['execution_session'] == '2024-02-21'
    assert [r['security_id'] for r in actual['candidates']] == expected.security_id.tolist() == ['S0']
    assert actual['candidates'][0]['entry_gap'] == pytest.approx(expected.entry_gap.iloc[0])
    assert actual['candidates'][0]['raw_return'] == pytest.approx(expected.raw_return.iloc[0])
    assert actual['candidates'][0]['peer_return'] == pytest.approx(expected.peer_return.iloc[0])
    observation = actual['observations']['S0'][-1]
    reference = exits[exits.security_id.eq('S0') & exits.session.eq('2024-02-20')].iloc[0]
    assert observation['residual_log_return'] == pytest.approx(reference.residual_log_return)
    assert observation['peer_return'] == pytest.approx(np.exp(.001) - 1)
    assert observation['open'] > 0 and observation['adjusted_open'] > 0
    assert observation['close_time'] == '2024-02-20T21:00:00Z'
    assert observation['fundamental_block'] is False
    assert len(actual['security_master']) == 6
    json.dumps(actual, allow_nan=False)


def test_unknown_latest_fundamentals_remove_entry_without_replacing_rank():
    prices, fundamentals, master, config = frames()
    fundamentals.loc[fundamentals.security_id.eq('S0'), 'fundamental_status'] = 'unknown'
    result = runtime.packet_from_frames(prices, fundamentals, master, config, '2024-02-20T23:00:00Z')
    assert result['candidates'] == []
    assert result['securities']['S0']['risk_class'] == 'unknown'


def test_missing_peer_residual_keeps_observed_prices_available():
    result = packet()
    rows = result['observations']['S0']
    missing = next(r for r in rows if r['residual_log_return'] is None)
    assert missing['quality'] == 'ready'
    assert missing['residual_quality'] == 'missing'
    assert missing['close'] > 0


@pytest.mark.parametrize('mutation', ['stale_price', 'future_price', 'missing_security', 'mixed_feed', 'stale_sec', 'future_sec', 'missing_sec'])
def test_incomplete_or_mixed_inputs_fail_closed(mutation):
    prices, fundamentals, master, config = frames()
    if mutation == 'stale_price':
        prices = prices[prices.session.ne('2024-02-20')]
    elif mutation == 'future_price':
        prices.loc[0, 'session'] = '2024-02-21'
    elif mutation == 'missing_security':
        prices = prices[~(prices.session.eq('2024-02-20') & prices.security_id.eq('S0'))]
    elif mutation == 'mixed_feed':
        prices.loc[0, 'feed'] = 'iex'
    elif mutation == 'stale_sec':
        fundamentals = fundamentals[fundamentals.session.ne('2024-02-20')]
    elif mutation == 'future_sec':
        fundamentals.loc[0, 'session'] = '2024-02-21'
    elif mutation == 'missing_sec':
        fundamentals = fundamentals[~(fundamentals.session.eq('2024-02-20') & fundamentals.security_id.eq('S0'))]
    with pytest.raises(ValueError):
        runtime.packet_from_frames(prices, fundamentals, master, config, '2024-02-20T23:00:00Z')


def test_freeze_cutoff_holidays_and_dst():
    assert runtime.expected_signal_session('2024-02-20T22:59:59Z') == '2024-02-16'
    assert runtime.expected_signal_session('2024-02-20T23:00:00Z') == '2024-02-20'
    dates = runtime.expected_sessions('2024-03-08T23:00:00Z')
    assert dates['execution_session'] == '2024-03-11'
    assert dates['execution_starts_at'] == '2024-03-11T09:35:00-04:00'


def test_packet_is_single_session_and_exact_window_only(tmp_path):
    sealed = runtime._publish(tmp_path, packet())
    packet_path = tmp_path / 'packets' / (sealed['packet_id'] + '.json')
    original = packet_path.read_bytes()
    assert not runtime.load_runtime_packet('2024-02-21T14:34:59Z', tmp_path)['execution_eligible']
    assert runtime.load_runtime_packet('2024-02-21T14:35:00Z', tmp_path)['execution_eligible']
    assert runtime.load_runtime_packet('2024-02-21T14:39:59Z', tmp_path)['execution_eligible']
    assert runtime.load_runtime_packet('2024-02-21T14:40:00Z', tmp_path)['status'] == 'expired'
    next_day = runtime.load_runtime_packet('2024-02-21T23:00:00Z', tmp_path)
    assert not next_day['execution_eligible']
    assert 'stale_signal_session' in next_day['blocked_reasons']
    assert packet_path.read_bytes() == original


def test_tampering_cannot_enable_old_packet(tmp_path):
    sealed = runtime._publish(tmp_path, packet())
    path = tmp_path / 'packets' / (sealed['packet_id'] + '.json')
    altered = copy.deepcopy(sealed)
    altered['execution_session'] = '2024-02-22'
    path.write_text(json.dumps(altered))
    result = runtime.load_runtime_packet('2024-02-21T14:35:00Z', tmp_path)
    assert result['status'] == 'blocked'
    assert result['candidates'] == []


def test_initial_packet_display_enrichment_uses_same_sealed_path(tmp_path):
    initial = packet()
    candidate = initial['candidates'][0]
    raw_return = candidate.pop('raw_return')
    peer_return = candidate.pop('peer_return')
    sealed = runtime._publish(tmp_path, initial)
    path = tmp_path / 'packets' / (sealed['packet_id'] + '.json')
    original = path.read_bytes()
    loaded = runtime.load_runtime_packet('2024-02-21T14:35:00Z', tmp_path)
    assert loaded['candidates'][0]['raw_return'] == pytest.approx(raw_return)
    assert loaded['candidates'][0]['peer_return'] == pytest.approx(peer_return)
    assert path.read_bytes() == original


def test_configuration_change_blocks_execution(tmp_path, monkeypatch):
    runtime._publish(tmp_path, packet())
    config_file = tmp_path / 'config.json'
    config_file.write_text('{}')
    monkeypatch.setattr(runtime, 'CONFIG_PATH', config_file)
    assert 'strategy_configuration_changed' in runtime.load_runtime_packet('2024-02-21T14:35:00Z', tmp_path)['blocked_reasons']


@pytest.mark.parametrize('now', ['2024-02-21T14:36:00Z', '2024-02-21T15:00:00Z'])
def test_existing_sealed_packet_does_not_refresh_or_replace(tmp_path, monkeypatch, now):
    sealed = runtime._publish(tmp_path, packet())
    monkeypatch.setattr(runtime, 'provider_from_environment', lambda: pytest.fail('Unexpected network refresh'))
    original = (tmp_path / 'packets' / (sealed['packet_id'] + '.json')).read_bytes()
    result = runtime.build_runtime_packet(now, tmp_path)
    assert result['packet_id'] == sealed['packet_id']
    assert result['created_at'] == sealed['created_at']
    assert (tmp_path / 'packets' / (sealed['packet_id'] + '.json')).read_bytes() == original


def mock_fresh_builder(tmp_path, monkeypatch):
    prices, fundamentals, master, config = frames()
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps(config))
    monkeypatch.setattr(runtime, 'CONFIG_PATH', config_file)
    monkeypatch.setattr(runtime, '_master', lambda: master)
    calls = []
    class Provider:
        def __init__(self): calls.append('provider')
        def close(self): calls.append('closed')
    monkeypatch.setattr(runtime, 'provider_from_environment', Provider)
    monkeypatch.setattr(runtime, '_fetch_bars', lambda *args: prices)
    monkeypatch.setattr(runtime, '_fetch_fundamentals', lambda *args: fundamentals)
    return calls, fundamentals


@pytest.mark.parametrize('previous_failure', [False, True])
def test_after_window_build_recovers_exit_observations_without_new_entry(tmp_path, monkeypatch, previous_failure):
    calls, _ = mock_fresh_builder(tmp_path, monkeypatch)
    if previous_failure:
        failed = {'schema': runtime.SCHEMA, 'strategy_id': 'reversal_v2', 'status': 'blocked',
                  'quality': 'blocked', 'blocked_reasons': ['sec_refresh_failed:test'],
                  **runtime.expected_sessions('2024-02-21T14:00:00Z'), 'created_at': '2024-02-21T14:00:00Z',
                  'config_hash': runtime._file_hash(runtime.CONFIG_PATH), 'metadata_hash': runtime._metadata_hash(),
                  'candidates': [], 'observations': {}}
        runtime._publish(tmp_path, failed)
    result = runtime.build_runtime_packet('2024-02-21T15:00:00Z', tmp_path)
    assert result['status'] == result['quality'] == 'expired'
    assert result['blocked_reasons'] == ['execution_window_expired']
    assert not result['execution_eligible']
    assert result['built_after_entry_deadline']
    assert result['observations']['S0'][-1]['session'] == '2024-02-20'
    assert result['candidates'][0]['symbol'] == 'S0'
    assert calls == ['provider', 'closed']
    repeated = runtime.build_runtime_packet('2024-02-21T18:00:00Z', tmp_path)
    assert repeated['packet_id'] == result['packet_id']
    assert calls == ['provider', 'closed']  # No repeated data refresh after success.
    earlier = runtime.load_runtime_packet('2024-02-21T14:36:00Z', tmp_path)
    assert 'future_packet_timestamp' in earlier['blocked_reasons']
    assert not earlier['execution_eligible']


@pytest.mark.parametrize('started,completed,expected_status', [
    ('2024-02-21T14:39:00Z', '2024-02-21T14:41:00Z', 'expired'),
    ('2024-02-21T22:59:00Z', '2024-02-21T23:00:00Z', 'blocked'),
])
def test_build_crossing_entry_deadline_keeps_exits_but_freeze_boundary_rejects(tmp_path, monkeypatch, started, completed, expected_status):
    _, fundamentals = mock_fresh_builder(tmp_path, monkeypatch)
    original_as_utc = runtime.as_utc
    clock = {'now': started}
    monkeypatch.setattr(runtime, 'as_utc', lambda value=None: original_as_utc(clock['now'] if value is None else value))
    def fetch_fundamentals(*args):
        clock['now'] = completed
        return fundamentals
    monkeypatch.setattr(runtime, '_fetch_fundamentals', fetch_fundamentals)
    result = runtime.build_runtime_packet(data_dir=tmp_path)
    assert result['status'] == expected_status
    assert not result['execution_eligible']
    if expected_status == 'blocked':
        assert result['blocked_reasons'] == ['build_crossed_signal_session_boundary']
        assert result['observations'] == {}
    else:
        assert result['observations']['S0']


def test_concurrent_builder_lock(tmp_path):
    with runtime._build_lock(tmp_path):
        with pytest.raises(ValueError, match='already_running'):
            with runtime._build_lock(tmp_path):
                pass


def test_refresh_failure_publishes_blocked_packet_with_identity(tmp_path, monkeypatch):
    class Provider:
        def close(self):
            pass
    monkeypatch.setattr(runtime, 'provider_from_environment', Provider)
    def fail(*args):
        raise ValueError('incomplete_latest_sip_session:S0')
    monkeypatch.setattr(runtime, '_fetch_bars', fail)
    result = runtime.build_runtime_packet('2024-02-20T23:00:00Z', tmp_path)
    assert result['status'] == 'blocked'
    assert len(result['packet_id']) == 64
    loaded = runtime.load_runtime_packet('2024-02-21T14:35:00Z', tmp_path)
    assert loaded['status'] == 'blocked'
    assert not loaded['execution_eligible']
    assert loaded['candidates'] == []
