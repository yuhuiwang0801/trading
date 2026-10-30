"""Chronological quote scenarios, not a historical return backtest."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from trading import intraday_reversal as intraday

NOW = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)


def position(**patch):
    return {'symbol': 'XYZ', 'strategy_id': 'reversal_v2', 'quantity': '2', 'avg_price': '100',
            'opened_at': '2026-10-26T13:36:00Z', 'entry_session': '2026-10-26',
            'signal_session': '2026-10-23', 'last_daily_session': '2026-10-23',
            'entry_gap': '.1', 'relative_path_complete': True,
            'cumulative_relative_log_return': '0', 'exit_latched': False, **patch}


def frame(now=NOW, **patch):
    return {'intraday_context': {'XYZ': {'session': '2026-10-26', 'reference_session': '2026-10-23',
            'as_of': now.isoformat(), 'oldest_quote_at': now.isoformat(),
            'price_basis_valid': True, 'corporate_actions_checked': True,
            'relative_eligible': True, 'stock_previous_close': '100', 'peer_return': '0',
            'peer_count': 5, **patch}}}


def step(pos, bid, now=NOW, context=None, packet=None):
    return intraday.update_position(pos, packet or {'signal_session': '2026-10-23'},
                                    context or frame(now), now, confirmed_bid=bid)


def test_rebound_triggers_before_close_and_remains_latched_after_retracement():
    original = position()
    result = step(original, '109')
    assert result['exit_reason'] == 'INTRADAY_REBOUND_TARGET'
    assert result['exit_due_session'] == '2026-10-26'
    assert result['exit_signal_total_return'] is None
    assert not original['exit_latched']
    later = step(result, '101', NOW + timedelta(seconds=30))
    assert later['exit_reason'] == result['exit_reason']
    assert later['intraday_evidence'] == result['intraday_evidence']


def test_snapshots_do_not_double_count_daily_relative_path():
    original = position(cumulative_relative_log_return='.01')
    once = step(original, '101')
    twice = step(once, '101', NOW + timedelta(seconds=30))
    assert once['cumulative_relative_log_return'] == twice['cumulative_relative_log_return'] == '.01'
    assert once['intraday_evidence']['relative_log_return'] == twice['intraday_evidence']['relative_log_return']
    assert twice['last_daily_session'] == original['last_daily_session']
    assert not twice['exit_latched']


def test_market_wide_rally_is_not_industry_relative_repair():
    pos = step(position(), '110', context=frame(peer_return='.1'))
    assert not pos['exit_latched']
    assert abs(float(pos['intraday_evidence']['relative_log_return'])) < 1e-12


def test_broker_read_delay_can_expire_peer_quotes_without_disabling_price_stop():
    # Frame created 10 seconds ago with a 29-second-old peer quote. The own
    # quote can still pass its independent check after that broker read delay.
    ctx = frame(as_of=(NOW - timedelta(seconds=10)).isoformat(),
                oldest_quote_at=(NOW - timedelta(seconds=39)).isoformat())
    high = step(position(), '110', context=ctx)
    assert not high['exit_latched']
    assert high['intraday_evidence']['relative_blocker'] == 'relative_quotes_stale_at_decision'
    low = step(position(), '94', context=ctx)
    assert low['exit_reason'] == 'INTRADAY_ABSOLUTE_STOP'


def test_relative_risk_can_exit_before_absolute_loss_threshold():
    pos = step(position(entry_gap='.02'), '99', context=frame(peer_return='.02'))
    assert pos['exit_reason'] == 'INTRADAY_RELATIVE_STOP'


@pytest.mark.parametrize('patch', [{'relative_eligible': False, 'reason': 'peer_quotes_missing'},
                                 {'relative_eligible': False, 'reference_session': '2026-10-22'}])
def test_five_percent_price_stop_survives_missing_relative_data(patch):
    result = step(position(), '95', context=frame(**patch))
    assert result['exit_reason'] == 'INTRADAY_ABSOLUTE_STOP'
    assert result['intraday_evidence']['relative_log_return'] is None


def test_trailing_activates_at_two_percent_and_survives_serialized_state():
    import json
    initial = step(position(), '101.99', context=frame(relative_eligible=False))
    assert not initial['intraday_trailing_active']
    peak = step(initial, '102', NOW + timedelta(seconds=30),
                frame(NOW + timedelta(seconds=30), relative_eligible=False))
    assert peak['intraday_trailing_active']
    recovered = json.loads(json.dumps(peak))
    before = deepcopy(recovered)
    result = step(recovered, '100.47', NOW + timedelta(seconds=60),
                  frame(NOW + timedelta(seconds=60), relative_eligible=False))
    assert result['exit_reason'] == 'INTRADAY_TRAILING_STOP'
    assert result['intraday_peak_bid'] == '102'
    assert recovered == before


@pytest.mark.parametrize('patch', [
    {'price_basis_valid': False, 'reason': 'corporate_action_detected'},
    {'corporate_actions_checked': False},
    {'as_of': '2026-10-26T15:59:29Z'},
    {'as_of': '2026-10-26T16:00:01Z'},
    {'session': '2026-10-23'},
])
def test_bad_price_basis_or_stale_frame_cannot_create_peak_or_exit(patch):
    pos = position()
    result = step(pos, '90', context=frame(**patch))
    assert result['intraday_status'] == 'blocked'
    assert not result['exit_latched']
    assert 'intraday_peak_bid' not in result


@pytest.mark.parametrize('patch', [{'relative_path_complete': False}, {'last_daily_session': '2026-10-22'}])
def test_missing_daily_path_never_becomes_zero_return(patch):
    result = step(position(**patch), '110')
    assert not result['exit_latched']
    assert result['intraday_evidence']['relative_log_return'] is None
    assert result['intraday_evidence']['relative_blocker'] == 'daily_relative_path_incomplete'


@pytest.mark.parametrize('bid', ['NaN', 'Infinity', '0', '-1', True])
def test_invalid_confirmed_quote_is_rejected(bid):
    result = step(position(), bid)
    assert result['intraday_status'] == 'blocked'
    assert not result['exit_latched']


def test_preopen_and_future_positions_do_not_trigger():
    before = NOW.replace(hour=13, minute=29)
    assert not step(position(), '90', before, frame(before))['exit_latched']
    assert not step(position(opened_at='2026-10-26T16:01:00Z'), '90')['exit_latched']


def test_existing_daily_exit_is_not_replaced_by_intraday_reason():
    result = step(position(exit_latched=True, exit_reason='FUNDAMENTAL_RISK'), '90')
    assert result['exit_reason'] == 'FUNDAMENTAL_RISK'


def test_disabled_policy_keeps_state_unchanged_except_status(monkeypatch):
    cfg = intraday.rules()
    monkeypatch.setattr(intraday, 'rules', lambda: {**cfg, 'enabled': False})
    pos = position()
    assert step(pos, '90') == {**pos, 'intraday_status': 'disabled'}
