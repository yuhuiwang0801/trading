from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading import live_reversal
from trading.live_execution import LiveExecutionService
from test_live_execution import FakeAdapter, PARAMS, SCOPE, arm, frame

NOW = datetime(2026, 10, 26, 13, 36, tzinfo=timezone.utc)


def packet(symbols=('XYZ',), signal='2026-10-23', execute='2026-10-26'):
    return {'strategy_id': 'reversal_v2', 'quality': 'ready', 'config_hash': live_reversal.config_hash(),
            'signal_session': signal, 'execution_session': execute,
            'candidates': [{'symbol': s, 'rank': n, 'entry_gap': '.03', 'score': '.03',
                            'issuer_id': s, 'industry': '35', 'signal_price': '10.02'} for n, s in enumerate(symbols)],
            'security_master': {s: {'issuer_id': s, 'industry': '35'} for s in symbols},
            'observations': {}}


def vframe(now=NOW, symbols=('XYZ',), daily=None, **kwargs):
    return frame(now, symbols=symbols, strategy_id='reversal_v2', reversal_packet=daily or packet(symbols), **kwargs)


@pytest.fixture
def live(tmp_path):
    adapter = FakeAdapter()
    adapter.now = NOW
    service = LiveExecutionService(tmp_path / 'live.sqlite3', adapter, write_enabled=True)
    service.reconcile(NOW)
    service.configure({**SCOPE, 'strategy_id': 'reversal_v2', 'max_positions': 20}, PARAMS)
    return service, adapter


def bought(live):
    service, adapter = live
    arm(service, NOW)
    result = service.scan(vframe(), NOW)
    assert result['orders_sent'] == 1
    return service, adapter


def observation(day, close='10.40', residual='.025', **patch):
    return {'session': day, 'quality': 'valid', 'open': '10', 'adjusted_open': '10',
            'close': close, 'total_return_index': close, 'residual_log_return': residual,
            'fundamental_known': True, 'fundamental_exit': False, **patch}


def test_select_strategy_is_local_disarmed_revisioned_and_preserves_scope(live):
    service, adapter = live
    before = service.snapshot()
    adapter.fail_read = True
    changed = service.select_strategy('trend_breakout')
    assert changed['configuration']['revision'] == before['configuration']['revision'] + 1
    assert changed['configuration']['symbols'] == before['configuration']['symbols']
    assert not changed['control']['armed']
    assert not adapter.submissions
    unchanged = service.select_strategy('trend_breakout')
    assert unchanged['configuration']['revision'] == changed['configuration']['revision']


def test_strategy_mismatch_and_stale_packets_cannot_trade(live):
    service, adapter = live
    arm(service, NOW)
    assert service.scan(frame(NOW), NOW)['status'] == 'STRATEGY_MISMATCH'
    for p in [packet(signal='2026-10-22'), {**packet(), 'config_hash': 'changed'}, {**packet(), 'quality': 'blocked'}]:
        assert service.scan(vframe(daily=p), NOW)['orders_sent'] == 0
    assert adapter.submissions == []


@pytest.mark.parametrize('minutes', [-2, 4])
def test_entry_requires_five_minute_window(live, minutes):
    service, adapter = live
    moment = NOW + timedelta(minutes=minutes)
    adapter.now = moment
    service.reconcile(moment)
    arm(service, moment)
    assert service.scan(vframe(moment), moment)['orders_sent'] == 0
    assert adapter.submissions == []


def test_v2_uses_five_percent_cash_allocation_and_daily_dedup(live):
    service, adapter = bought(live)
    order = adapter.submissions[0]
    # The unrelated trend ATR/risk/max_positions parameters never shrink V2.
    assert Decimal(order['quantity']) == 4
    assert Decimal(order['quantity']) * Decimal(order['limit_price']) <= 50
    service.scan(vframe(), NOW)
    assert len(adapter.submissions) == 1
    intent = service.snapshot()['intents'][0]
    assert intent['strategy_id'] == 'reversal_v2'
    assert intent['expires_at'].startswith('2026-10-26T13:40:00')
    with pytest.raises(ValueError): service.select_strategy('trend_breakout')


def test_four_daily_entries_and_pending_issuer_reservations(live):
    service, adapter = live
    symbols = ('AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'FFF')
    service.configure({**SCOPE, 'symbols': list(symbols), 'max_positions': 20, 'max_daily_added': '1000',
                       'strategy_id': 'reversal_v2'}, PARAMS)
    arm(service, NOW)
    assert service.scan(vframe(symbols=symbols), NOW)['orders_sent'] == 4
    assert len(adapter.submissions) == 4
    assert service.scan(vframe(symbols=symbols), NOW)['orders_sent'] == 0


def test_same_issuer_pending_order_occupies_five_percent(live):
    service, adapter = live
    symbols = ('AAA', 'BBB')
    service.configure({**SCOPE, 'symbols': list(symbols), 'max_positions': 20, 'strategy_id': 'reversal_v2'}, PARAMS)
    p = packet(symbols)
    for c in p['candidates']: c['issuer_id'] = 'ONE'
    for m in p['security_master'].values(): m['issuer_id'] = 'ONE'
    arm(service, NOW)
    assert service.scan(vframe(symbols=symbols, daily=p), NOW)['orders_sent'] == 1
    assert len(adapter.submissions) == 1


def test_verified_partial_fills_preserve_anchor_and_restart(live):
    service, adapter = bought(live)
    ident = adapter.data['orders'][0]['id']
    adapter.fill(ident, 2, price='10')
    service.reconcile(NOW)
    first = service.snapshot()['positions'][0]
    adapter.fill(ident, 2, price='10.02')
    service.reconcile(NOW)
    second = service.snapshot()['positions'][0]
    assert second['entry_gap'] == first['entry_gap'] == '.03'
    assert second['opened_at'] == first['opened_at']
    assert Decimal(second['avg_price']) == Decimal('10.01')
    assert 'entry_atr' not in second
    service.recover(NOW)
    restored = service.snapshot()['positions'][0]
    assert restored['entry_gap'] == first['entry_gap']
    assert restored['max_exit_session'] == first['max_exit_session']
    assert not service.snapshot()['control']['armed']


def test_rebound_exit_from_daily_path_uses_actual_fill_and_is_idempotent(live):
    service, adapter = bought(live)
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    service.reconcile(NOW)
    tomorrow = NOW + timedelta(days=1)
    adapter.now = tomorrow
    adapter.quote_price = '10.40'
    p = packet(signal='2026-10-26', execute='2026-10-27')
    p['observations']['XYZ'] = [observation('2026-10-26')]
    result = service.scan(vframe(tomorrow, daily=p, price='10.40'), tomorrow)
    assert result['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'
    pos = service.snapshot()['positions'][0]
    assert pos['exit_reason'] == 'REBOUND_TARGET'
    assert pos['entry_gap'] == '.03'
    assert Decimal(pos['exit_signal_total_return']) == Decimal('.04')
    service.scan(vframe(tomorrow, daily=p, price='10.40'), tomorrow)
    assert len(adapter.submissions) == 2
    assert Decimal(service.snapshot()['positions'][0]['cumulative_relative_log_return']) == Decimal('.025')


def test_latched_exit_retries_cancelled_order_after_entry_window(live):
    service, adapter = bought(live)
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    service.reconcile(NOW)
    tomorrow = NOW + timedelta(days=1, minutes=30)
    adapter.now = tomorrow
    p = packet(signal='2026-10-26', execute='2026-10-27')
    p['observations']['XYZ'] = [observation('2026-10-26', close='10.02')]
    service.scan(vframe(tomorrow, daily=p), tomorrow)
    assert adapter.submissions[-1]['side'] == 'SELL'
    adapter.data['orders'][-1]['state'] = 'CANCELLED'
    service.scan(vframe(tomorrow, daily={**p, 'quality': 'blocked'}), tomorrow)
    assert len(adapter.submissions) == 3
    assert adapter.submissions[-1]['side'] == 'SELL'


def test_max_hold_exit_survives_missing_daily_data(live):
    service, adapter = bought(live)
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    service.reconcile(NOW)
    due = service.snapshot()['positions'][0]['max_exit_session']
    late = datetime.fromisoformat(due + 'T15:00:00+00:00')
    adapter.now = late
    empty = {**packet(), 'quality': 'blocked', 'observations': {}}
    result = service.scan(vframe(late, daily=empty), late)
    assert result['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'
    assert service.snapshot()['positions'][0]['exit_reason'] == 'MAX_HOLD'


def test_missing_residual_disables_false_rebound_but_not_fundamental_exit():
    pos = {'signal_session': '2026-10-23', 'entry_session': '2026-10-26', 'entry_gap': '.03',
           'avg_price': '10', 'max_exit_session': '2026-11-09', 'exit_latched': False}
    incomplete = live_reversal.update_position(pos, [observation('2026-10-26', quality='missing_peer_residual', residual_log_return=None)], '2026-10-27')
    result = live_reversal.update_position(incomplete, [observation('2026-10-27', residual='.5')], '2026-10-28')
    assert not result['exit_latched']
    result = live_reversal.update_position(result, [observation('2026-10-28', fundamental_exit=True)], '2026-10-29')
    assert result['exit_reason'] == 'FUNDAMENTAL_RISK'


def test_unmanaged_position_is_never_adopted_or_sold(live):
    service, adapter = live
    # Baseline reconstruction for an account that already owned XYZ.
    adapter.data['positions'] = [{'symbol': 'XYZ', 'quantity': '2', 'available_quantity': '2', 'average_price': '10'}]
    with service._locked() as db:
        service._put(db, 'baseline', None)
        db.commit()
    service.reconcile(NOW)
    arm(service, NOW)
    service.scan(vframe(), NOW)
    assert adapter.submissions == []
    assert service.snapshot()['positions'] == []


def test_preview_delay_cannot_submit_after_entry_window(live, monkeypatch):
    service, adapter = live
    moment = NOW.replace(minute=39, second=59)
    adapter.now = moment
    service.reconcile(moment)
    arm(service, moment)
    clock = [0.0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    def delayed_preview(_):
        clock[0] += 2
    adapter.preview_hook = delayed_preview
    assert service.scan(vframe(moment), moment)['orders_sent'] == 0
    assert adapter.submissions == []
    assert service.snapshot()['intents'][0]['unsent_reason'] == 'PRE_SUBMIT_GATE_BLOCKED'


def test_switching_strategy_does_not_adopt_existing_owned_position(live):
    service, adapter = bought(live)
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    service.reconcile(NOW)
    service.disarm()
    with pytest.raises(ValueError, match='positions'):
        service.select_strategy('trend_breakout')
    with pytest.raises(ValueError, match='positions'):
        service.configure({**SCOPE, 'strategy_id': 'trend_breakout'})
    assert service.snapshot()['positions'][0]['strategy_id'] == 'reversal_v2'


def test_absolute_stop_uses_verified_fill_price_and_rebased_series():
    pos = {'signal_session': '2026-10-23', 'entry_session': '2026-10-26', 'entry_gap': '.03',
           'avg_price': '10.20', 'max_exit_session': '2026-11-09', 'exit_latched': False}
    obs = observation('2026-10-26', close='9.68', residual='0', open='10', adjusted_open='5', total_return_index='4.84')
    result = live_reversal.update_position(pos, [obs], '2026-10-27')
    assert result['exit_reason'] == 'ABSOLUTE_STOP'
    # Actual cost 10.20 matters: using market open 10 would miss this stop.
    assert Decimal(result['exit_signal_total_return']) < Decimal('-.05')


def test_future_daily_close_cannot_trigger_exit_early():
    pos = {'signal_session': '2026-10-23', 'entry_session': '2026-10-26', 'entry_gap': '.03',
           'avg_price': '10', 'max_exit_session': '2026-11-09', 'exit_latched': False}
    result = live_reversal.update_position(pos, [observation('2026-10-26')], '2026-10-26')
    assert not result['exit_latched']
    assert result['cumulative_relative_log_return'] == '0'


def test_delayed_packet_keeps_checkpoint_and_can_replay_later():
    pos = {'signal_session': '2026-10-23', 'entry_session': '2026-10-26', 'entry_gap': '.03',
           'avg_price': '10', 'max_exit_session': '2026-11-09', 'exit_latched': False}
    delayed = live_reversal.update_position(pos, [], '2026-10-27')
    assert delayed['relative_path_complete']
    assert delayed.get('last_daily_session') is None
    refreshed = live_reversal.update_position(delayed, [observation('2026-10-26')], '2026-10-27')
    assert refreshed['exit_reason'] == 'REBOUND_TARGET'


def test_missing_peer_residual_still_allows_known_fundamental_exit():
    pos = {'signal_session': '2026-10-23', 'entry_session': '2026-10-26', 'entry_gap': '.03',
           'avg_price': '10', 'max_exit_session': '2026-11-09', 'exit_latched': False}
    obs = observation('2026-10-26', quality='missing_peer_residual', residual_log_return=None, fundamental_exit=True)
    result = live_reversal.update_position(pos, [obs], '2026-10-27')
    assert result['exit_reason'] == 'FUNDAMENTAL_RISK'
    assert not result['relative_path_complete']
