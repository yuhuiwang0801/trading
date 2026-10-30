"""Intraday execution through the isolated ledger and synthetic broker only."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from trading.live_execution import LiveExecutionService
from test_live_execution import FakeAdapter, PARAMS, SCOPE, arm
from test_live_reversal import NOW, packet, vframe
from test_live_policy import managed_packet


@pytest.fixture
def owned(tmp_path):
    adapter = FakeAdapter()
    adapter.now = NOW
    live = LiveExecutionService(tmp_path / 'live.sqlite3', adapter, write_enabled=True)
    live.reconcile(NOW)
    live.configure({**SCOPE, 'strategy_id': 'reversal_v2', 'max_positions': 20}, PARAMS)
    arm(live, NOW)
    assert live.scan(vframe(), NOW)['orders_sent'] == 1
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    live.reconcile(NOW)
    adapter.quote_reads.clear()
    return live, adapter


def intraday_frame(moment, price, *, symbol='XYZ', relative=False, daily=None, **context_patch):
    daily = deepcopy(daily or packet((symbol,)))
    context = {'price_basis_valid': True, 'corporate_actions_checked': True,
               'relative_eligible': relative, 'session': str(moment.date()),
               'reference_session': daily['signal_session'], 'as_of': moment.isoformat(),
               'oldest_quote_at': moment.isoformat(),
               'stock_previous_close': '10', 'peer_return': '0', 'peer_count': 5,
               'reason': None if relative else 'peer_quotes_incomplete', **context_patch}
    return vframe(moment, symbols=(symbol,), daily=daily, price=price,
                  intraday_context={symbol: context})


def scan(owned, moment, price, **kwargs):
    live, adapter = owned
    adapter.now, adapter.quote_price = moment, price
    return live.scan(intraday_frame(moment, price, **kwargs), moment)


@pytest.mark.parametrize('quality', ['ready', 'expired'])
def test_midday_rebound_dual_confirmation_and_repeat_do_not_duplicate(owned, quality):
    live, adapter = owned
    moment = NOW + timedelta(hours=3)
    daily = packet()
    daily['quality'] = quality
    result = scan(owned, moment, '10.50', relative=True, daily=daily)
    assert result['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'
    assert adapter.submissions[-1]['quantity'] == '4'
    # One quote confirms the trigger; another after preview confirms submission.
    assert adapter.quote_reads == ['XYZ', 'XYZ']
    state = live.snapshot()
    pos = state['positions'][0]
    assert pos['exit_reason'] == 'INTRADAY_REBOUND_TARGET'
    assert pos['cumulative_relative_log_return'] == '0'
    assert pos['intraday_quote_check']['status'] == 'PASSED'
    sell = next(i for i in state['intents'] if i['side'] == 'SELL')
    assert sell['evidence']['trigger_quote_check']['status'] == 'PASSED'
    assert sell['evidence']['intraday_evidence']['confirmed_bid'] == '10.49'
    scan(owned, moment, '10.50', relative=True)
    assert len(adapter.submissions) == 2
    assert len([x for x in live.snapshot()['audit'] if x['event'] == 'INTRADAY_EXIT_TRIGGERED']) == 1


def test_unconfirmed_alpaca_spike_never_advances_peak_or_latches(owned):
    live, adapter = owned
    moment = NOW + timedelta(hours=2)
    adapter.now, adapter.quote_price = moment, '10.02'
    result = live.scan(intraday_frame(moment, '10.50', relative=True), moment)
    assert result['orders_sent'] == 0
    pos = live.snapshot()['positions'][0]
    assert not pos['exit_latched'] and 'intraday_peak_bid' not in pos
    assert pos['intraday_reason'] == 'intraday_quote_unconfirmed'
    assert pos['intraday_quote_check']['status'] == 'BLOCKED'
    scan(owned, moment + timedelta(seconds=30), '10.02', relative=True)
    pos = live.snapshot()['positions'][0]
    assert not pos['exit_latched'] and pos['intraday_peak_bid'] == '10.01'
    assert len(adapter.submissions) == 1


def test_missing_peers_do_not_disable_confirmed_absolute_stop(owned):
    live, adapter = owned
    result = scan(owned, NOW + timedelta(hours=1), '9.50')
    assert result['orders_sent'] == 1
    pos = live.snapshot()['positions'][0]
    assert pos['exit_reason'] == 'INTRADAY_ABSOLUTE_STOP'
    assert pos['intraday_evidence']['relative_log_return'] is None
    assert adapter.submissions[-1]['side'] == 'SELL'


def test_confirmed_peak_then_pullback_triggers_protection_without_daily_update(owned):
    live, adapter = owned
    moment = NOW + timedelta(hours=1)
    assert scan(owned, moment, '10.30')['orders_sent'] == 0
    assert live.snapshot()['positions'][0]['intraday_trailing_active']
    assert scan(owned, moment + timedelta(minutes=1), '10.12')['orders_sent'] == 1
    assert live.snapshot()['positions'][0]['exit_reason'] == 'INTRADAY_TRAILING_STOP'
    assert adapter.submissions[-1]['side'] == 'SELL'


@pytest.mark.parametrize('kind', ['stale_broker', 'future_broker', 'unavailable_broker', 'stale_alpaca'])
def test_invalid_or_unavailable_quote_cannot_latch(owned, kind):
    live, adapter = owned
    moment = NOW + timedelta(hours=1)
    data = intraday_frame(moment, '9.30')
    adapter.now, adapter.quote_price = moment, '9.30'
    if kind == 'stale_broker':
        adapter.quote_patch['venue_bid_time'] = (moment - timedelta(seconds=31)).isoformat()
    elif kind == 'future_broker':
        adapter.quote_patch['venue_bid_time'] = (moment + timedelta(seconds=10)).isoformat()
    elif kind == 'unavailable_broker':
        def failed(_):
            raise RuntimeError('must not be copied to audit')
        adapter.quote_hook = failed
    else:
        data['quotes']['XYZ']['timestamp'] = (moment - timedelta(minutes=3)).isoformat()
    assert live.scan(data, moment)['orders_sent'] == 0
    assert not live.snapshot()['positions'][0]['exit_latched']
    assert len(adapter.submissions) == 1


@pytest.mark.parametrize('patch', [
    {'price_basis_valid': False, 'reason': 'corporate_action_detected'},
    {'corporate_actions_checked': False},
    {'as_of': '2026-10-26T13:00:00Z'},
    {'session': '2026-10-23'},
])
def test_untrusted_price_context_does_not_start_a_new_exit(owned, patch):
    live, adapter = owned
    assert scan(owned, NOW + timedelta(hours=1), '9.30', **patch)['orders_sent'] == 0
    pos = live.snapshot()['positions'][0]
    assert not pos['exit_latched'] and 'intraday_peak_bid' not in pos
    assert len(adapter.submissions) == 1


def test_stop_during_trigger_quote_read_cannot_latch_or_trade(owned):
    live, adapter = owned
    adapter.quote_hook = lambda _: live._stop_now()
    assert scan(owned, NOW + timedelta(hours=1), '9.30')['orders_sent'] == 0
    assert not live.snapshot()['positions'][0]['exit_latched']
    assert len(adapter.submissions) == 1


def test_peer_quote_expiring_during_broker_read_cannot_trigger_relative_exit(owned, monkeypatch):
    live, adapter = owned
    moment = NOW + timedelta(hours=1)
    adapter.now, adapter.quote_price = moment, '10.50'
    data = intraday_frame(moment, '10.50', relative=True,
                          oldest_quote_at=(moment - timedelta(seconds=27)).isoformat())
    clock = [0.0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    def delayed_quote(_):
        clock[0] += 5
    adapter.quote_hook = delayed_quote
    assert live.scan(data, moment)['orders_sent'] == 0
    pos = live.snapshot()['positions'][0]
    assert not pos['exit_latched']
    assert pos['intraday_reason'] == 'relative_quotes_stale_at_decision'
    assert pos['intraday_evidence']['relative_log_return'] is None
    assert pos['intraday_quote_check']['status'] == 'PASSED'


def test_trigger_confirmation_does_not_replace_post_preview_quote_guard(owned):
    live, adapter = owned
    moment = NOW + timedelta(hours=1)
    adapter.preview_hook = lambda _: setattr(adapter, 'quote_price', '10.02')
    assert scan(owned, moment, '9.30')['orders_sent'] == 0
    state = live.snapshot()
    assert state['positions'][0]['exit_reason'] == 'INTRADAY_ABSOLUTE_STOP'
    sell = next(i for i in state['intents'] if i['side'] == 'SELL')
    assert sell['status'] == 'ABANDONED' and sell['unsent_reason'] == 'CROSS_QUOTE_BLOCKED'
    assert len(adapter.submissions) == 1
    # A positively confirmed trigger stays pending; a new order must be checked.
    adapter.preview_hook = None
    assert scan(owned, moment + timedelta(seconds=30), '9.40')['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'


def test_intraday_stop_is_available_in_first_five_minutes_of_next_session(owned):
    live, adapter = owned
    moment = (NOW + timedelta(days=1)).replace(minute=31)
    data = packet(signal='2026-10-26', execute='2026-10-27')
    assert scan(owned, moment, '9.30', daily=data)['orders_sent'] == 1
    assert live.snapshot()['positions'][0]['exit_reason'] == 'INTRADAY_ABSOLUTE_STOP'
    assert adapter.submissions[-1]['side'] == 'SELL'


def test_unknown_submission_blocks_repeat_and_retains_ownership(owned):
    live, adapter = owned
    adapter.submit_hook = lambda _: {'status': 'UNKNOWN'}
    moment = NOW + timedelta(hours=1)
    assert scan(owned, moment, '9.30')['orders_sent'] == 1
    assert scan(owned, moment + timedelta(seconds=30), '9.30')['orders_sent'] == 0
    assert len(adapter.submissions) == 2
    assert live.snapshot()['positions'][0]['quantity'] == '4'
    assert 'UNKNOWN_INSTRUCTION' in live.snapshot()['readiness']['blockers']


def test_partial_sell_waits_for_cancel_confirmation_then_only_exits_remainder(owned):
    live, adapter = owned
    moment = NOW + timedelta(hours=1)
    scan(owned, moment, '9.30')
    sell = adapter.data['orders'][-1]
    adapter.fill(sell['id'], 2, price='9.29')
    assert scan(owned, moment + timedelta(seconds=30), '9.30')['orders_sent'] == 0
    assert live.snapshot()['positions'][0]['quantity'] == '2'
    sell['state'] = 'CANCELLED'
    assert scan(owned, moment + timedelta(minutes=1), '9.30')['orders_sent'] == 1
    assert adapter.submissions[-1]['quantity'] == '2'
    assert len(adapter.submissions) == 3


@pytest.mark.parametrize('equity', ['20', '0'])
def test_managed_sell_can_reduce_risk_even_when_buy_cap_is_below_one_share(tmp_path, equity):
    adapter = FakeAdapter()
    adapter.now = NOW
    live = LiveExecutionService(tmp_path / 'managed.sqlite3', adapter, write_enabled=True)
    live.prepare_strategy('reversal_v2', now=NOW)
    arm(live, NOW)
    assert live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)['orders_sent'] == 1
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    live.reconcile(NOW)
    adapter.data['account']['total_value'] = equity
    moment = NOW + timedelta(hours=1)
    adapter.now, adapter.quote_price = moment, '10.50'
    result = live.scan(intraday_frame(moment, '10.50', symbol='AAPL', relative=True,
                                    daily=managed_packet()), moment)
    assert result['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'
    assert adapter.submissions[-1]['quantity'] == '4'
    assert Decimal(live.snapshot()['configuration']['max_order_amount']) < Decimal('10.49')


def test_intraday_exit_never_touches_unmanaged_position(tmp_path):
    adapter = FakeAdapter()
    adapter.now = NOW
    adapter.data['positions'] = [{'symbol': 'XYZ', 'quantity': '2', 'available_quantity': '2',
                                  'average_price': '10'}]
    live = LiveExecutionService(tmp_path / 'unmanaged.sqlite3', adapter, write_enabled=True)
    live.reconcile(NOW)
    live.configure({**SCOPE, 'strategy_id': 'reversal_v2'}, PARAMS)
    arm(live, NOW)
    assert scan((live, adapter), NOW + timedelta(hours=1), '9.30')['orders_sent'] == 0
    assert adapter.submissions == adapter.quote_reads == []
    assert live.snapshot()['positions'] == []


@pytest.mark.parametrize('identity_key', ['config_hash', 'metadata_hash', 'quality'])
@pytest.mark.parametrize('fallback_price,expected_reason', [
    ('9.30', 'INTRADAY_ABSOLUTE_STOP'), ('10.30', 'INTRADAY_TRAILING_STOP'),
])
def test_mismatched_packet_blocks_relative_but_preserves_independent_price_exit(
        tmp_path, identity_key, fallback_price, expected_reason):
    adapter = FakeAdapter()
    adapter.now = NOW
    live = LiveExecutionService(tmp_path / 'identity.sqlite3', adapter, write_enabled=True)
    live.prepare_strategy('reversal_v2', now=NOW)
    arm(live, NOW)
    assert live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)['orders_sent'] == 1
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    live.reconcile(NOW)
    # The checkpoint is already complete through the preceding close, so the
    # pure relative calculation could otherwise accept an unauthenticated basket.
    assert live.snapshot()['positions'][0]['last_daily_session'] == '2026-10-23'
    bad_packet = managed_packet()
    bad_packet[identity_key] = 'blocked' if identity_key == 'quality' else 'different-policy-or-universe'
    moment = NOW + timedelta(hours=1)
    adapter.now, adapter.quote_price = moment, '10.50'
    data = intraday_frame(moment, '10.50', symbol='AAPL', relative=True, daily=bad_packet)
    assert live.scan(data, moment)['orders_sent'] == 0
    pos = live.snapshot()['positions'][0]
    assert not pos['exit_latched']
    assert pos['intraday_reason'] == 'intraday_packet_identity_mismatch'
    assert pos['intraday_evidence']['relative_log_return'] is None
    assert pos['intraday_peak_bid'] == '10.49'
    assert data['intraday_context']['AAPL']['relative_eligible'] is True  # caller evidence unchanged
    assert scan((live, adapter), moment + timedelta(seconds=30), fallback_price,
                symbol='AAPL', relative=True, daily=bad_packet)['orders_sent'] == 1
    assert live.snapshot()['positions'][0]['exit_reason'] == expected_reason


def test_dispatch_context_reaches_real_decision_and_two_source_execution(owned):
    from trading.reversal_dispatch import quote_frame
    from trading.intraday_market import REFERENCE_VERSION

    live, adapter = owned
    moment = NOW + timedelta(hours=2)
    adapter.now, adapter.quote_price = moment, '10.50'
    symbols = ['XYZ', 'P1', 'P2', 'P3', 'P4', 'P5']
    daily = packet()
    daily['intraday_reference_version'] = REFERENCE_VERSION
    daily['security_master'] = {s: {'security_id': 'id-' + s, 'issuer_id': s, 'industry': '35'}
                                for s in symbols}
    daily['intraday_reference'] = {
        s: {**daily['security_master'][s], 'session': '2026-10-23', 'raw_close': '10',
            'eligible': True, 'peer_symbols': [p for p in symbols if p != s]} for s in symbols}

    class Provider:
        feed = 'sip'

        def assets(self):
            return [{'symbol': s, 'tradable': True, 'status': 'active'} for s in symbols]

        def corporate_actions(self, selected, start, end):
            assert set(selected) == set(symbols)
            assert start < '2026-10-26' < end  # Provider filters process dates, not only ex-dates.
            return []

        def quotes(self, selected):
            assert set(selected) == set(symbols)
            return {s: {'bp': '10.49' if s == 'XYZ' else '9.99',
                        'ap': '10.51' if s == 'XYZ' else '10.01',
                        't': moment.isoformat(), 'bs': 10, 'as': 10} for s in selected}

    snapshot = live.snapshot()
    frame = quote_frame(Provider(), daily, snapshot['configuration'],
                        snapshot['positions'] + snapshot['broker_positions'], snapshot['intents'], now=moment)
    assert frame['intraday_context']['XYZ']['relative_eligible']
    assert live.scan(frame, moment)['orders_sent'] == 1
    assert live.snapshot()['positions'][0]['exit_reason'] == 'INTRADAY_REBOUND_TARGET'
    assert adapter.submissions[-1]['side'] == 'SELL'
