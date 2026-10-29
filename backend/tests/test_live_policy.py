"""Strategy-owned commissioning and risk caps; all broker actions are synthetic."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from trading import live_policy
from trading.live_execution import LiveExecutionService
from test_live_execution import FakeAdapter, PARAMS, START, arm, frame
from test_live_reversal import NOW, packet, vframe


def managed_packet(symbols=('AAPL',)):
    result = packet(symbols)
    policy = live_policy.policy_for('reversal_v2')
    result['metadata_hash'] = policy['universe_hash']
    result['security_master'] = deepcopy(policy['security_master'])
    for candidate in result['candidates']:
        if candidate['symbol'] in result['security_master']:
            candidate.update(result['security_master'][candidate['symbol']])
    return result


@pytest.fixture
def service(tmp_path):
    adapter = FakeAdapter()
    adapter.now = NOW
    live = LiveExecutionService(tmp_path / 'live.sqlite3', adapter, write_enabled=True)
    return live, adapter


def prepare(service, strategy='reversal_v2', now=NOW):
    live, adapter = service
    adapter.now = now
    return live.prepare_strategy(strategy, now=now)


def buy(service):
    prepare(service)
    live, adapter = service
    arm(live, NOW)
    result = live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW, PARAMS)
    assert result['orders_sent'] == 1
    return live, adapter


def test_v2_scope_uses_full_vetted_universe_without_user_fields_and_no_writes(service):
    live, adapter = service
    result = live.prepare_strategy('reversal_v2', params={'untrusted': 'ignored'}, now=NOW)
    cfg = result['configuration']
    assert cfg['scope_mode'] == 'strategy_managed'
    assert len(cfg['symbols']) == cfg['strategy_policy']['universe_count'] == 190
    assert cfg['max_order_amount'] == '50.00' and cfg['max_daily_added'] == '200.00'
    assert cfg['max_positions'] == 20
    assert cfg['strategy_policy']['max_entries_per_day'] == 4
    assert cfg['strategy_policy']['max_industry_weight'] == '0.25'
    assert 'security_master' not in cfg['strategy_policy']
    assert not result['control']['armed']
    assert adapter.previews == adapter.submissions == adapter.cancellations == []
    assert result['positions'] == result['intents'] == result['orders'] == result['fills'] == []


def test_account_caps_update_without_changing_reviewed_policy_revision(service):
    live, adapter = service
    before = prepare(service)['configuration']
    adapter.data['account'].update(total_value='2000', cash='2000', buying_power='2000', unleveraged_buying_power='2000')
    live.reconcile(NOW)
    after = live.snapshot()['configuration']
    assert after['max_order_amount'] == '100.00' and after['max_daily_added'] == '400.00'
    assert after['revision'] == before['revision']
    assert prepare(service)['configuration']['revision'] == before['revision']
    arm(live, NOW)
    result = live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW, PARAMS)
    assert result['orders_sent'] == 1
    assert Decimal(adapter.submissions[0]['quantity']) == 9
    intent = live.snapshot()['intents'][0]
    assert intent['policy_hash'] == after['policy_hash']


def test_cash_capacity_does_not_allow_margin_or_fractional_shares(service):
    live, adapter = service
    adapter.data['account'].update(cash='15', buying_power='1000', unleveraged_buying_power='15')
    cfg = prepare(service)['configuration']
    assert cfg['current_buy_order_ceiling'] == '15.00'
    arm(live, NOW)
    live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)
    assert adapter.submissions[0]['quantity'] == '1'


@pytest.mark.parametrize('cash,equity,blocker', [('0', '1000', 'NO_UNLEVERAGED_BUYING_POWER'),
                                                ('1000', '0', 'STRATEGY_CAPITAL_UNAVAILABLE')])
def test_no_capital_is_explicitly_blocked(service, cash, equity, blocker):
    live, adapter = service
    adapter.data['account'].update(cash=cash, total_value=equity)
    result = prepare(service)
    assert blocker in result['readiness']['blockers']
    with pytest.raises(ValueError): arm(live, NOW)
    assert not adapter.submissions


@pytest.mark.parametrize('key,value', [('symbols', ['XYZ']), ('policy_hash', 'changed'), ('max_positions', 999)])
def test_mutated_policy_configuration_cannot_arm(service, key, value):
    live, _ = service
    prepare(service)
    with live._locked() as db:
        cfg = live._get(db, 'configuration')
        live._put(db, 'configuration', {**cfg, key: value})
        db.commit()
    assert 'STRATEGY_POLICY_CHANGED' in live.readiness(NOW)['blockers']
    with pytest.raises(ValueError): arm(live, NOW)


@pytest.mark.parametrize('phase', ['preview', 'final_adapter_gate'])
def test_policy_change_during_execution_never_submits(service, monkeypatch, phase):
    live, adapter = service
    prepare(service)
    arm(live, NOW)
    original = live_policy.policy_for
    def change(*_):
        monkeypatch.setattr(live_policy, 'policy_for', lambda sid: {**original(sid), 'policy_hash': 'new_policy'})
    if phase == 'preview': adapter.preview_hook = change
    else: adapter.account_delay_hook = change
    result = live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)
    assert result['orders_sent'] == 0
    assert not adapter.submissions
    assert live.snapshot()['intents'][0]['status'] == 'ABANDONED'


def test_v2_packet_must_match_vetted_universe_and_identity(service):
    live, adapter = service
    prepare(service)
    arm(live, NOW)
    daily = managed_packet()
    daily['metadata_hash'] = 'different_pool'
    assert live.scan(vframe(symbols=('AAPL',), daily=daily), NOW)['status'] == 'REVERSAL_UNIVERSE_MISMATCH'
    daily = managed_packet()
    daily['security_master']['AAPL']['security_id'] = 'different_security'
    assert live.scan(vframe(symbols=('AAPL',), daily=daily), NOW)['orders_sent'] == 0
    assert not adapter.submissions


def test_trend_uses_eligible_candidates_and_frozen_defaults(service):
    live, adapter = service
    cfg = prepare(service, 'trend_breakout', START)['configuration']
    assert cfg['symbols'] == []
    assert cfg['strategy_policy']['universe_mode'] == 'eligible_active_us_equities'
    assert cfg['max_order_amount'] == '100.00'
    assert cfg['max_daily_added'] == '300.00'
    arm(live, START)
    daily = frame(START)
    assert live.scan(daily, START, PARAMS)['orders_sent'] == 0  # no affirmative eligibility
    daily['candidates'][0]['tradable'] = True
    assert live.scan(daily, START, PARAMS)['orders_sent'] == 1
    assert Decimal(adapter.submissions[0]['quantity']) == 9


def manual_change(live, adapter):
    live.reconcile(NOW)
    adapter.data['positions'] = [{'symbol': 'AAPL', 'quantity': '2', 'available_quantity': '2', 'average_price': '10'}]
    order = adapter.add_order({'ref_id': 'manual_ref', 'symbol': 'AAPL', 'side': 'BUY', 'quantity': '2', 'limit_price': '10'})
    order.update(state='FILLED', filled_quantity='2', agent_initiated=False)
    live.reconcile(NOW)
    return order


def test_first_strategy_setup_can_recapture_completed_manual_activity_without_adopting(service):
    live, adapter = service
    manual_change(live, adapter)
    assert set(live.readiness(NOW)['blockers']) >= {'EXTERNAL_ORDER_ACTIVITY', 'EXTERNAL_POSITION_ACTIVITY'}
    result = prepare(service)
    assert result['readiness']['ready_to_arm']
    assert result['positions'] == [] and result['broker_positions'][0]['symbol'] == 'AAPL'
    audit = next(row for row in result['audit'] if row['event'] == 'PRISTINE_BASELINE_REFRESHED')
    assert audit['detail']['positions_adopted'] == 0
    assert not adapter.submissions


@pytest.mark.parametrize('reason', ['automated_history', 'open_order', 'pending_approval', 'unknown_order', 'other_block'])
def test_pristine_baseline_refresh_refuses_unresolved_or_nonpristine_state(service, reason):
    live, adapter = service
    order = manual_change(live, adapter)
    if reason == 'automated_history':
        with live._locked() as db:
            live._save(db, 'live_positions', 'OLD', {'symbol': 'OLD', 'quantity': '0'})
            db.commit()
    elif reason == 'open_order': order['state'] = 'SUBMITTED'
    elif reason == 'unknown_order': order['state'] = 'VENDOR_UNKNOWN'
    elif reason == 'pending_approval':
        adapter.data['approvals'] = [{'approval_id': 'manual_approval', 'state': 'PENDING'}]
    else:
        with live._locked() as db:
            live._block(db, 'FILL_LEDGER_MISMATCH', NOW)
            db.commit()
    result = prepare(service)
    assert 'EXTERNAL_POSITION_ACTIVITY' in result['readiness']['blockers']
    assert not any(r['event'] == 'PRISTINE_BASELINE_REFRESHED' for r in result['audit'])


def test_stale_account_cannot_configure_or_recapture_baseline(service):
    live, adapter = service
    manual_change(live, adapter)
    with pytest.raises(ValueError): live.prepare_strategy('reversal_v2', now=NOW + timedelta(minutes=2))
    assert 'EXTERNAL_POSITION_ACTIVITY' in live.readiness(NOW)['blockers']
    assert not live.snapshot()['configuration']['configured']


def test_pending_same_policy_can_be_prepared_after_restart_but_not_changed(service):
    live, adapter = buy(service)
    revision = live.snapshot()['configuration']['revision']
    live.recover(NOW)
    prepared = prepare(service)
    assert prepared['configuration']['revision'] == revision
    assert prepared['readiness']['ready_to_arm']
    with pytest.raises(ValueError): prepare(service, 'trend_breakout')
    assert len(adapter.submissions) == 1
    arm(live, NOW)
    assert live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)['orders_sent'] == 0


def test_zero_cash_owned_position_can_rearm_for_exit_without_new_buy(service):
    live, adapter = buy(service)
    adapter.fill(adapter.data['orders'][0]['id'], 4, price='10')
    live.reconcile(NOW)
    live.disarm()
    adapter.data['account'].update(cash='0', buying_power='0', unleveraged_buying_power='0')
    prepared = prepare(service)
    assert prepared['readiness']['ready_to_arm']
    arm(live, NOW)
    assert live.scan(vframe(symbols=('AAPL',), daily=managed_packet()), NOW)['orders_sent'] == 0
    assert len(adapter.submissions) == 1

