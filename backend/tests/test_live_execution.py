from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import threading
import time
import uuid

import pytest

from trading.live_execution import LiveExecutionService, ARM_CONFIRMATION
from trading.mcp_broker import LiveExecutionDisabled
from trading.robinhood_adapter import RobinhoodTradingAdapter
from trading.strategy import DEFAULT_PARAMS


START = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
NUMBER = 'private-account-0001'
PARAMS = {**DEFAULT_PARAMS, 'max_position_pct': '0.5', 'max_exposure_pct': '0.9',
          'risk_per_trade_pct': '0.02', 'max_order_amount': '100', 'max_daily_added': '200',
          'max_positions': 3}
SCOPE = {'symbols': ['XYZ'], 'max_order_amount': '100', 'max_daily_added': '200', 'max_positions': 3}
REQUEST = {'symbol': 'XYZ', 'side': 'BUY', 'quantity': '1', 'limit_price': '10.02'}


class FakeAdapter:
    _proposal_matches = staticmethod(RobinhoodTradingAdapter._proposal_matches)

    def __init__(self):
        self.now = START
        self.write_enabled = True
        self.data = {'account_number': NUMBER, 'compatible': True,
                     'account': {'masked_number': '••••0001', 'type': 'cash', 'account_type': 'individual',
                                 'currency': 'USD', 'cash': '1000', 'buying_power': '1000',
                                 'unleveraged_buying_power': '1000', 'total_value': '1000', 'approval_required': False},
                     'positions': [], 'orders': [], 'fills': [], 'approvals': []}
        self.previews = []
        self.submissions = []
        self.cancellations = []
        self.preview_hook = None
        self.submit_hook = None
        self.account_delay_hook = None
        self.preview_status = 'READY'
        self.fail_read = False
        self.quote_reads = []
        self.quote_hook = None
        self.quote_price = '10.02'
        self.quote_patch = {}

    def read_snapshot(self, approval_ids=None):
        if self.fail_read:
            raise RuntimeError('sensitive broker detail')
        return {**deepcopy(self.data), 'checked_at': self.now.isoformat()}

    def preview(self, order):
        self.previews.append(deepcopy(order))
        if self.preview_hook:
            self.preview_hook(order)
        return {'status': self.preview_status, 'order': deepcopy(order), 'checked_at': self.now.isoformat(),
                'errors': [], 'warnings': [], 'approval_required': False}

    def submit(self, order):
        # Like the real adapter: an account read may delay its final write gate.
        if self.account_delay_hook:
            self.account_delay_hook()
        if self.write_enabled() is not True:
            raise LiveExecutionDisabled('Local write gate closed')
        self.submissions.append(deepcopy(order))
        if self.submit_hook:
            return self.submit_hook(order)
        result = self.add_order(order)
        return {'status': 'SUBMITTED', 'broker_order_id': result['id'], 'approval_id': None,
                'ref_id': order['ref_id']}

    def fresh_quote(self, symbol):
        self.quote_reads.append(symbol)
        if self.quote_hook:
            self.quote_hook(symbol)
        return {'symbol': symbol, 'bid_price': str(Decimal(self.quote_price) - Decimal('.01')),
                'ask_price': str(Decimal(self.quote_price) + Decimal('.01')),
                'venue_bid_time': self.now.isoformat(), 'venue_ask_time': self.now.isoformat(),
                'state': 'active', 'has_traded': True, **deepcopy(self.quote_patch)}

    def add_order(self, order, ident=None, ref=None):
        result = {'id': ident or str(uuid.uuid4()), 'ref_id': ref or order['ref_id'],
                  'symbol': order['symbol'], 'side': order['side'], 'quantity': str(order['quantity']),
                  'filled_quantity': '0', 'limit_price': str(order['limit_price']), 'state': 'SUBMITTED',
                  'created_at': self.now.isoformat(), 'updated_at': self.now.isoformat(),
                  'type': 'limit', 'time_in_force': 'gfd', 'market_hours': 'regular_hours',
                  'trigger': 'immediate', 'agent_initiated': True}
        self.data['orders'].append(result)
        return result

    def cancel(self, account_number, order_id):
        assert self.write_enabled() is True
        self.cancellations.append(order_id)
        return {'status': 'CANCEL_PENDING', 'broker_order_id': order_id}

    def fill(self, order_id, quantity, price='10', fee='0', ident=None):
        order = next(o for o in self.data['orders'] if o['id'] == order_id)
        quantity = Decimal(str(quantity))
        order['filled_quantity'] = str(Decimal(order['filled_quantity']) + quantity)
        order['state'] = 'FILLED' if Decimal(order['filled_quantity']) == Decimal(order['quantity']) else 'PARTIALLY_FILLED'
        fill = {'id': ident or str(uuid.uuid4()), 'order_id': order_id, 'symbol': order['symbol'],
                'side': order['side'], 'quantity': str(quantity), 'price': price, 'fees': fee,
                'timestamp': self.now.isoformat()}
        self.data['fills'].append(fill)
        position = next((p for p in self.data['positions'] if p['symbol'] == order['symbol']), None)
        if not position:
            position = {'symbol': order['symbol'], 'quantity': '0', 'available_quantity': '0', 'average_price': price}
            self.data['positions'].append(position)
        sign = 1 if order['side'] == 'BUY' else -1
        position['quantity'] = str(Decimal(position['quantity']) + sign * quantity)
        position['available_quantity'] = position['quantity']
        account = self.data['account']
        account['cash'] = str(Decimal(account['cash']) - sign * quantity * Decimal(price) - Decimal(fee))
        account['buying_power'] = account['unleveraged_buying_power'] = account['cash']
        return fill


def frame(now=START, price='10.02', previous='9.99', symbols=('XYZ',), **patch):
    return {'source': 'alpaca', 'quality': 'valid', 'market_open': True,
            'session_date': str(now.date()), 'market_filter': True,
            'candidates': [{'symbol': s, 'high20': '10', 'atr14': '.1', 'ma20': '9.50', 'rank': i}
                           for i, s in enumerate(symbols)],
            'bars': {s: [{'timestamp': (now-timedelta(minutes=5)).isoformat(), 'close': previous},
                         {'timestamp': now.isoformat(), 'close': price}] for s in symbols},
            'quotes': {s: {'timestamp': now.isoformat(), 'last': price, 'bid': str(Decimal(price)-Decimal('.01')),
                           'ask': str(Decimal(price)+Decimal('.01')), 'quality': 'valid'} for s in symbols}, **patch}


@pytest.fixture
def setup(tmp_path):
    adapter = FakeAdapter()
    service = LiveExecutionService(tmp_path/'live.sqlite3', adapter, write_enabled=True)
    service.reconcile(START)
    service.configure(SCOPE, PARAMS)
    return service, adapter


def arm(service, now=START):
    revision = service.snapshot()['configuration']['revision']
    return service.arm(ARM_CONFIRMATION, revision, now)


def buy(setup):
    service, adapter = setup
    arm(service)
    result = service.scan(frame(), START, PARAMS)
    assert result['orders_sent'] == 1
    return service, adapter


def test_default_is_disabled_and_preview_never_creates_instruction(tmp_path):
    adapter = FakeAdapter()
    service = LiveExecutionService(tmp_path/'live.sqlite3', adapter)
    report = service.preview(REQUEST, START)
    assert report['status'] == 'READY' and report['scope_matched'] is False
    assert adapter.submissions == adapter.cancellations == []
    assert service.scan(frame(), START)['status'] == 'DISARMED'
    assert 'SERVER_WRITE_DISABLED' in service.readiness(START)['blockers']
    snap = service.snapshot()
    assert snap['intents'] == snap['orders'] == snap['fills'] == snap['positions'] == []
    assert NUMBER not in json.dumps(snap)
    assert NUMBER not in json.dumps(report)
    assert (tmp_path/'live.sqlite3').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('scope', [
    {**SCOPE, 'symbols': []}, {**SCOPE, 'symbols': ['xyz']}, {**SCOPE, 'symbols': ['XYZ', 'XYZ']},
    {**SCOPE, 'max_order_amount': '0'}, {**SCOPE, 'max_daily_added': 'NaN'},
    {**SCOPE, 'max_positions': '1.1'}, {**SCOPE, 'account_number': NUMBER}])
def test_invalid_scope_refused(setup, scope):
    service, _ = setup
    with pytest.raises(ValueError): service.configure(scope)


@pytest.mark.parametrize('patch', [{'quantity': '0'}, {'quantity': '.5'}, {'limit_price': '1.001'},
                                  {'side': 'SELL_SHORT'}, {'account_number': NUMBER}])
def test_invalid_preview_requests_cannot_reach_adapter(setup, patch):
    service, adapter = setup
    count = len(adapter.previews)
    with pytest.raises(ValueError): service.preview({**REQUEST, **patch}, START)
    assert len(adapter.previews) == count


def test_arm_requires_exact_confirmation_revision_and_fresh_account_without_manual_preview(setup):
    service, adapter = setup
    with pytest.raises(ValueError): service.arm('yes', 1, START)
    with pytest.raises(ValueError): service.arm(ARM_CONFIRMATION, 0, START)
    with pytest.raises(ValueError): service.arm(ARM_CONFIRMATION, 1, START+timedelta(minutes=6))
    service.configure({**SCOPE, 'max_order_amount': '90'})
    assert service.readiness(START)['ready_to_arm']
    assert adapter.previews == []
    assert arm(service)['control']['armed'] is True
    with pytest.raises(ValueError): service.configure(SCOPE)
    service.disarm()
    assert not service.snapshot()['control']['armed'] and adapter.submissions == []


@pytest.mark.parametrize('size', [{'dollar_amount':'10.00'}, {'quantity':'0.125'}])
def test_market_preview_is_optional_and_does_not_change_automatic_order_type(setup, size):
    service, adapter = setup
    result = service.preview({'symbol':'XYZ','side':'BUY','type':'market',**size}, START)
    assert result['status'] == 'READY' and not result['scope_matched']
    assert result['preview_only'] is True
    assert result['automatic_order_type'] == 'whole_share_limit'
    assert service.readiness(START)['ready_to_arm']
    snapshot = service.snapshot()
    assert snapshot['intents'] == snapshot['orders'] == snapshot['fills'] == snapshot['positions'] == []
    assert not adapter.submissions and not adapter.cancellations
    assert arm(service)['control']['armed'] is True
    assert service.scan(frame(), START)['orders_sent'] == 1
    assert adapter.previews[-1].get('type', 'limit') == 'limit'
    assert Decimal(adapter.submissions[-1]['quantity']) == Decimal(adapter.submissions[-1]['quantity']).to_integral_value()


@pytest.mark.parametrize('patch', [
    {'type':'market','dollar_amount':'0.99'},
    {'type':'market','dollar_amount':'1.001'},
    {'type':'market','dollar_amount':'NaN'},
    {'type':'market','quantity':'0.0000001'},
    {'type':'market','quantity':'0.5','limit_price':'10.00'},
    {'type':'market','quantity':'0.5','dollar_amount':'10.00'},
])
def test_invalid_market_preview_does_not_call_adapter_preview(setup, patch):
    service, adapter = setup
    count = len(adapter.previews)
    with pytest.raises(ValueError):
        service.preview({'symbol':'XYZ','side':'BUY',**patch}, START)
    assert len(adapter.previews) == count and not adapter.submissions


@pytest.mark.parametrize('patch', [{'source': 'demo'}, {'quality': 'stale'}, {'session_date': '2026-10-25'},
                                  {'market_open': False}])
def test_market_data_gates(setup, patch):
    service, adapter = setup
    arm(service)
    service.scan(frame(**patch), START)
    assert adapter.submissions == []


def test_actual_exchange_calendar_overrides_open_frame(setup):
    service, adapter = setup
    adapter.now = START.replace(hour=11)
    service.reconcile(adapter.now)
    service.preview(REQUEST, adapter.now)
    arm(service, adapter.now)
    assert service.scan(frame(adapter.now), adapter.now)['status'] == 'MARKET_CLOSED'
    assert not adapter.submissions


@pytest.mark.parametrize('change', ['stale_quote', 'invalid_quote', 'stale_bar', 'no_cross', 'filter', 'wrong_symbol'])
def test_no_submission_without_fresh_strategy_signal(setup, change):
    service, adapter = setup
    arm(service)
    data = frame()
    if change == 'stale_quote': data['quotes']['XYZ']['timestamp'] = (START-timedelta(minutes=5)).isoformat()
    if change == 'invalid_quote': data['quotes']['XYZ']['quality'] = 'invalid'
    if change == 'stale_bar': data['bars']['XYZ'][-1]['timestamp'] = (START-timedelta(minutes=15)).isoformat()
    if change == 'no_cross': data['bars']['XYZ'][-2]['close'] = '10.01'
    if change == 'filter': data['market_filter'] = False
    if change == 'wrong_symbol': data = frame(symbols=('UNAUTHORIZED',))
    service.scan(data, START)
    assert not adapter.submissions


def test_intent_committed_before_write_and_daily_signal_dedup(setup):
    service, adapter = setup
    def verify(order):
        with sqlite3.connect(service.path) as db:
            intent = json.loads(db.execute('SELECT payload FROM live_intents').fetchone()[0])
        assert intent['status'] == 'SUBMITTING' and intent['ref_id'] == order['ref_id']
        row = adapter.add_order(order)
        return {'status': 'SUBMITTED', 'broker_order_id': row['id']}
    adapter.submit_hook = verify
    buy(setup)
    service.scan(frame(), START)
    assert len(adapter.submissions) == 1
    assert Decimal(service.snapshot()['intents'][0]['reserved_cash']) > 0
    assert service.snapshot()['positions'] == []


def test_real_buying_power_never_uses_paper_ten_thousand(setup):
    service, adapter = setup
    adapter.data['account'].update(cash='5', buying_power='5', unleveraged_buying_power='5', total_value='5')
    service.reconcile(START)
    arm(service)
    service.scan(frame(), START)
    assert not adapter.submissions
    assert service.snapshot()['account']['cash'] == '5'


def test_existing_positions_remain_unmanaged_and_constrain_allocation(tmp_path):
    adapter = FakeAdapter()
    adapter.data['positions'] = [{'symbol': 'OLD', 'quantity': '5', 'available_quantity': '5', 'average_price': '100'}]
    adapter.data['account'].update(cash='25.58', buying_power='25.58', unleveraged_buying_power='25.58', total_value='104')
    service = LiveExecutionService(tmp_path/'live.sqlite3', adapter, True)
    service.reconcile(START)
    service.configure({**SCOPE, 'symbols': ['OLD']}, PARAMS)
    service.preview({**REQUEST, 'symbol': 'OLD'}, START)
    arm(service)
    service.scan(frame(symbols=('OLD',), price='9.01'), START)
    assert not adapter.submissions
    assert service.snapshot()['positions'] == []
    assert service.snapshot()['broker_positions'][0]['quantity'] == '5'


def test_preview_warning_blocks_execution(setup):
    service, adapter = setup
    arm(service)
    adapter.preview_status = 'BLOCKED'
    service.scan(frame(), START)
    assert not adapter.submissions
    assert service.snapshot()['intents'][0]['status'] == 'PREVIEW_BLOCKED'
    assert service.snapshot()['intents'][0]['reserved_cash'] == '0'


def test_timeout_stays_unknown_without_retry_across_restart(setup):
    service, adapter = setup
    adapter.submit_hook = lambda order: (_ for _ in ()).throw(TimeoutError())
    buy(setup)
    assert service.snapshot()['intents'][0]['status'] == 'UNKNOWN'
    assert not service.snapshot()['control']['armed']
    restarted = LiveExecutionService(service.path, FakeAdapter(), True)
    restarted.recover(START)
    assert restarted.snapshot()['intents'][0]['status'] == 'UNKNOWN'
    assert 'UNKNOWN_INSTRUCTION' in restarted.readiness(START)['blockers']
    with pytest.raises(ValueError): arm(restarted)
    restarted.scan(frame(), START)
    assert len(adapter.submissions) == 1 and not restarted.adapter.submissions


def test_unknown_resolves_only_by_positive_broker_reference(setup):
    service, adapter = setup
    def lost_response(order):
        adapter.add_order(order)
        raise TimeoutError()
    adapter.submit_hook = lost_response
    buy(setup)
    service.reconcile(START)
    snap = service.snapshot()
    assert snap['intents'][0]['status'] == 'SUBMITTED'
    assert 'UNKNOWN_INSTRUCTION' not in snap['readiness']['blockers']
    assert snap['control']['armed'] is False
    assert len(adapter.submissions) == 1


def test_fill_is_idempotent_and_owned_position_uses_execution_price(setup):
    service, adapter = buy(setup)
    order = adapter.data['orders'][0]
    adapter.fill(order['id'], order['quantity'], price='10.01', fee='.09')
    service.reconcile(START)
    first = service.snapshot()
    service.reconcile(START)
    second = service.snapshot()
    assert len(second['fills']) == 1 and first['positions'] == second['positions']
    assert second['intents'][0]['status'] == 'FILLED' and second['intents'][0]['reserved_cash'] == '0'
    assert second['positions'][0]['quantity'] == order['quantity']
    assert Decimal(second['positions'][0]['avg_price']) > Decimal('10.01')


@pytest.mark.parametrize('change', ['new_order', 'position', 'amended_order', 'edited_approval'])
def test_external_activity_blocks_without_taking_ownership(setup, change):
    service, adapter = setup
    arm(service)
    if change == 'new_order':
        adapter.add_order({**REQUEST, 'ref_id': str(uuid.uuid4())})
    elif change == 'position':
        adapter.data['positions'] = [{'symbol': 'MANUAL', 'quantity': '1', 'available_quantity': '1', 'average_price': '1'}]
    elif change == 'amended_order':
        buy_result = service.scan(frame(), START)
        assert buy_result['orders_sent'] == 1
        adapter.data['orders'][0]['market_hours'] = 'all_day_hours'
    else:
        adapter.data['approvals'] = [{'approval_id': 'external', 'state': 'AWAITING_APPROVAL',
                                     'asset_class': 'ASSET_CLASS_EQUITY', 'is_edited': False}]
    service.reconcile(START)
    assert service.snapshot()['control']['armed'] is False
    assert service.snapshot()['positions'] == []
    assert service.snapshot()['readiness']['blockers']


def test_approval_pending_is_not_an_order_then_new_reference_resolves(setup):
    service, adapter = setup
    approval_id = str(uuid.uuid4())
    def awaiting(order):
        adapter.data['approvals'] = [{'approval_id': approval_id, 'state': 'AWAITING_APPROVAL',
                                     'asset_class': 'ASSET_CLASS_EQUITY', 'is_edited': False,
                                     'proposed_order': None, 'order_id': None}]
        return {'status': 'AWAITING_APPROVAL', 'approval_id': approval_id, 'broker_order_id': None}
    adapter.submit_hook = awaiting
    buy(setup)
    service.reconcile(START)
    snap = service.snapshot()
    assert snap['intents'][0]['status'] == 'AWAITING_APPROVAL'
    assert snap['orders'] == snap['fills'] == snap['positions'] == []
    assert 'APPROVAL_CHANGED' not in snap['readiness']['blockers']
    adapter.data['approvals'][0]['state'] = 'APPROVED'
    order = adapter.add_order(adapter.submissions[0], ref=approval_id)
    adapter.fill(order['id'], order['quantity'])
    service.reconcile(START)
    snap = service.snapshot()
    assert snap['intents'][0]['status'] == 'FILLED'
    assert snap['intents'][0]['approval_id'] == approval_id
    assert len(snap['fills']) == 1 and len(adapter.submissions) == 1
    assert 'EXTERNAL_ORDER_ACTIVITY' not in snap['readiness']['blockers']


def test_restart_always_disarms_without_resubmitting_known_order(setup):
    service, adapter = buy(setup)
    service.recover(START)
    assert service.snapshot()['control']['armed'] is False
    assert service.snapshot()['intents'][0]['status'] == 'SUBMITTED'
    service.scan(frame(), START)
    assert len(adapter.submissions) == 1


def test_ttl_cancel_is_owned_once_and_retains_reservations(setup):
    service, adapter = buy(setup)
    adapter.now = START + timedelta(seconds=301)
    service.scan(frame(adapter.now), adapter.now)
    snap = service.snapshot()
    assert len(adapter.cancellations) == 1
    assert snap['intents'][0]['status'] == 'CANCEL_PENDING'
    assert Decimal(snap['intents'][0]['reserved_cash']) > 0
    service.scan(frame(adapter.now), adapter.now)
    assert len(adapter.cancellations) == 1
    order = adapter.data['orders'][0]
    adapter.fill(order['id'], '2')
    order['state'] = 'CANCELLED'
    service.reconcile(adapter.now)
    snap = service.snapshot()
    assert snap['intents'][0]['reserved_cash'] == '0'
    assert snap['positions'][0]['quantity'] == '2'


def test_exit_only_system_quantity_and_uses_sellable_quantity(setup):
    service, adapter = buy(setup)
    order = adapter.data['orders'][0]
    adapter.fill(order['id'], order['quantity'])
    service.reconcile(START)
    adapter.now = START + timedelta(minutes=5)
    adapter.data['positions'][0]['available_quantity'] = '2'
    adapter.quote_price = '9.40'
    service.scan(frame(adapter.now, price='9.40', previous='10'), adapter.now)
    assert len(adapter.submissions) == 2
    assert adapter.submissions[1]['side'] == 'SELL' and adapter.submissions[1]['quantity'] == '2'


def test_disarm_during_preview_stops_before_actual_write(setup):
    service, adapter = setup
    arm(service)
    entered, release = threading.Event(), threading.Event()
    def pause_preview(order):
        entered.set()
        assert release.wait(5)
    adapter.preview_hook = pause_preview
    scan_thread = threading.Thread(target=lambda: service.scan(frame(), START))
    scan_thread.start()
    assert entered.wait(5)
    stopped_generation = service._stop_generation()
    stop_thread = threading.Thread(target=service.disarm)
    stop_thread.start()
    deadline = time.monotonic() + 3
    while service._stop_generation() == stopped_generation and time.monotonic() < deadline:
        time.sleep(.01)
    assert service._stop_generation() != stopped_generation
    release.set()
    scan_thread.join(5)
    stop_thread.join(5)
    assert not scan_thread.is_alive() and not stop_thread.is_alive()
    assert not adapter.submissions
    assert not service.snapshot()['control']['armed']


@pytest.mark.parametrize('delay_phase', ['preview', 'adapter_account'])
def test_delayed_preview_or_account_read_cannot_use_stale_market_frame(setup, monkeypatch, delay_phase):
    service, adapter = setup
    arm(service)
    clock = [0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    def delay(*args): clock[0] += 120
    if delay_phase == 'preview': adapter.preview_hook = delay
    else: adapter.account_delay_hook = delay
    service.scan(frame(), START)
    assert not adapter.submissions


def test_changed_strategy_parameters_disarm_and_require_new_scope_review(setup):
    service, adapter = setup
    arm(service)
    result = service.scan(frame(), START, {**PARAMS, 'risk_per_trade_pct': '.01'})
    assert result['reason'] == 'STRATEGY_PARAMETERS_CHANGED' and not adapter.submissions
    service.configure(SCOPE, {**PARAMS, 'risk_per_trade_pct': '.01'})
    assert 'STRATEGY_PARAMETERS_CHANGED' not in service.readiness(START)['blockers']
    assert service.readiness(START)['ready_to_arm']
    assert not service.snapshot()['control']['armed']


def test_read_failure_is_redacted_and_recovery_does_not_rearm(setup):
    service, adapter = setup
    arm(service)
    adapter.fail_read = True
    service.reconcile(START)
    snap = service.snapshot()
    assert 'ACCOUNT_READ_FAILED' in snap['readiness']['blockers']
    assert 'sensitive broker detail' not in json.dumps(snap)
    adapter.fail_read = False
    service.reconcile(START)
    assert 'ACCOUNT_READ_FAILED' not in service.readiness(START)['blockers']
    assert not service.snapshot()['control']['armed']


def test_reservations_enforce_daily_budget_across_multiple_symbols(setup):
    service, adapter = setup
    limits = {**SCOPE, 'symbols': ['XYZ', 'ABC', 'DEF'], 'max_daily_added': '100'}
    service.configure(limits, PARAMS)
    service.preview(REQUEST, START)
    arm(service)
    service.scan(frame(symbols=('XYZ', 'ABC', 'DEF')), START)
    reserved = sum(Decimal(i['reserved_cash']) for i in service.snapshot()['intents'])
    assert reserved <= 100
    assert len(adapter.submissions) == 1


def test_all_real_positions_count_against_position_limit(setup):
    service, adapter = setup
    # Start a fresh baseline with three legacy holdings, none managed by the app.
    for symbol in ('ONE', 'TWO', 'THREE'):
        adapter.data['positions'].append({'symbol': symbol, 'quantity': '1', 'available_quantity': '1', 'average_price': '1'})
    broker = FakeAdapter()
    broker.data = deepcopy(adapter.data)
    fresh = LiveExecutionService(service.path.with_name('fresh.sqlite3'), broker, True)
    fresh.reconcile(START)
    fresh.configure(SCOPE, PARAMS)
    fresh.preview(REQUEST, START)
    arm(fresh)
    fresh.scan(frame(), START)
    assert not broker.submissions and not fresh.snapshot()['positions']
    assert any(a['event'] == 'ENTRY_RISK_BLOCKED' and a['detail']['reason'] == 'MAX_POSITIONS'
               for a in fresh.snapshot()['audit'])


def test_account_daily_loss_blocks_entries(setup):
    service, adapter = setup
    adapter.data['account']['total_value'] = '960'
    service.reconcile(START)
    arm(service)
    service.scan(frame(), START)
    assert not adapter.submissions
    assert any(a['event'] == 'ENTRY_RISK_BLOCKED' and a['detail']['reason'] == 'DAILY_LOSS_LIMIT'
               for a in service.snapshot()['audit'])


def test_second_order_uses_elapsed_time_from_first_submission(setup, monkeypatch):
    service, adapter = setup
    service.configure({**SCOPE, 'symbols': ['XYZ', 'ABC']}, PARAMS)
    service.preview(REQUEST, START)
    arm(service)
    clock = [0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    def slow_response(order):
        row = adapter.add_order(order)
        clock[0] += 120
        return {'status': 'SUBMITTED', 'broker_order_id': row['id']}
    adapter.submit_hook = slow_response
    service.scan(frame(symbols=('XYZ', 'ABC')), START)
    assert len(adapter.submissions) == 1
    assert any(i['status'] == 'ABANDONED' for i in service.snapshot()['intents'])


def test_pending_approval_timeout_blocks_and_never_autoapproves(setup):
    service, adapter = setup
    approval_id = str(uuid.uuid4())
    def pending(order):
        adapter.data['approvals'] = [{'approval_id': approval_id, 'state': 'AWAITING_APPROVAL',
                                     'asset_class': 'ASSET_CLASS_EQUITY', 'is_edited': False,
                                     'proposed_order': None, 'order_id': None}]
        return {'status': 'AWAITING_APPROVAL', 'approval_id': approval_id}
    adapter.submit_hook = pending
    buy(setup)
    adapter.now = START + timedelta(seconds=301)
    service.scan(frame(adapter.now), adapter.now)
    assert 'STALE_PENDING_APPROVAL' in service.snapshot()['readiness']['blockers']
    assert not adapter.cancellations and len(adapter.submissions) == 1
    adapter.data['approvals'][0]['state'] = 'DECLINED'
    service.reconcile(adapter.now)
    assert 'STALE_PENDING_APPROVAL' not in service.snapshot()['readiness']['blockers']
    assert service.snapshot()['intents'][0]['status'] == 'REJECTED'
    assert not service.snapshot()['control']['armed']


def test_changed_fill_id_content_blocks_without_reapplying(setup):
    service, adapter = buy(setup)
    order = adapter.data['orders'][0]
    fill = adapter.fill(order['id'], order['quantity'])
    service.reconcile(START)
    quantity = service.snapshot()['positions'][0]['quantity']
    fill['price'] = '9'
    service.reconcile(START)
    assert 'EXECUTION_CHANGED' in service.snapshot()['readiness']['blockers']
    assert service.snapshot()['positions'][0]['quantity'] == quantity


def test_closed_position_new_entry_has_fresh_protection_lifecycle(setup):
    service, adapter = buy(setup)
    original = adapter.data['orders'][0]
    adapter.fill(original['id'], original['quantity'])
    service.reconcile(START)
    adapter.now = START + timedelta(minutes=5)
    adapter.quote_price = '9.40'
    service.scan(frame(adapter.now, price='9.40'), adapter.now)
    sell = adapter.data['orders'][1]
    adapter.fill(sell['id'], sell['quantity'], price='9.40')
    service.reconcile(adapter.now)
    assert not service.snapshot()['positions']
    adapter.now = START + timedelta(days=1)
    adapter.quote_price = '10.02'
    data = frame(adapter.now)
    data['candidates'][0]['atr14'] = '.2'
    service.scan(data, adapter.now)
    second = adapter.data['orders'][2]
    adapter.fill(second['id'], second['quantity'], price='10.02')
    service.reconcile(adapter.now)
    position = service.snapshot()['positions'][0]
    assert position['entry_atr'] == '0.2' and position['opened_at'] == adapter.now.isoformat()
    assert position['last_bar_at'] is None and position['exit_latched'] is False
    assert Decimal(position['protection']) == Decimal('9.62')


def test_cross_quote_is_fresh_after_preview_and_evidence_precedes_write(setup):
    service, adapter = setup
    arm(service)
    events = []
    adapter.preview_hook = lambda order: events.append('preview')
    adapter.quote_hook = lambda symbol: events.append('quote')
    def submit(order):
        events.append('submit')
        with sqlite3.connect(service.path) as db:
            intent = json.loads(db.execute('SELECT payload FROM live_intents').fetchone()[0])
        assert intent['status'] == 'SUBMITTING'
        assert intent['quote_check']['status'] == 'PASSED'
        assert intent['quote_check']['signal']['price'] == '10.02'
        assert order['limit_price'] == intent['quote_check']['order']['limit_price'] == '10.04'
        return {'status': 'SUBMITTED', 'broker_order_id': adapter.add_order(order)['id']}
    adapter.submit_hook = submit
    assert service.scan(frame(), START)['orders_sent'] == 1
    assert events == ['preview', 'quote', 'submit']
    report = service.snapshot()['last_quote_check']
    assert report['status'] == 'PASSED' and report['reasons'] == []
    assert NUMBER not in json.dumps(report)


@pytest.mark.parametrize('patch,reason', [
    ({'venue_bid_time': (START-timedelta(seconds=31)).isoformat()}, 'ROBINHOOD_BID_TIMESTAMP_STALE'),
    ({'venue_ask_time': (START+timedelta(seconds=10)).isoformat()}, 'ROBINHOOD_ASK_TIMESTAMP_FUTURE'),
    ({'venue_ask_time': None}, 'ROBINHOOD_ASK_TIMESTAMP_INVALID'),
    ({'bid_price': '9.90', 'ask_price': '9.92'}, 'BID_FEED_DEVIATION'),
    ({'bid_price': '9.99', 'ask_price': '10.06'}, 'ROBINHOOD_SPREAD_TOO_WIDE'),
    ({'symbol': 'ABC'}, 'ROBINHOOD_SYMBOL_MISMATCH'),
    ({'ask_price': 'NaN'}, 'ROBINHOOD_ASK_INVALID'),
])
def test_cross_quote_blocks_release_reservations_and_allow_new_check(setup, patch, reason):
    service, adapter = setup
    arm(service)
    adapter.quote_patch = patch
    assert service.scan(frame(), START)['orders_sent'] == 0
    snap = service.snapshot()
    intent = snap['intents'][0]
    assert intent['status'] == 'ABANDONED' and intent['unsent_reason'] == 'CROSS_QUOTE_BLOCKED'
    assert intent['reserved_cash'] == intent['reserved_quantity'] == '0'
    assert reason in snap['last_quote_check']['reasons']
    assert snap['control']['armed'] and not adapter.submissions
    with sqlite3.connect(service.path) as db:
        row = db.execute('SELECT signal_key,payload FROM live_intents').fetchone()
    assert row[0] == json.loads(row[1])['signal_key'] != intent['original_signal_key']
    assert intent['original_signal_key'] == 'entry:XYZ:2026-10-26'
    adapter.now += timedelta(seconds=1)
    adapter.quote_patch = {}
    assert service.scan(frame(adapter.now), adapter.now)['orders_sent'] == 1
    service.scan(frame(adapter.now), adapter.now)
    assert len(adapter.submissions) == 1 and len(adapter.quote_reads) == 2
    assert adapter.submissions[0]['ref_id'] != intent['ref_id']


def test_failed_quote_read_is_redacted_and_recoverable(setup):
    service, adapter = setup
    arm(service)
    adapter.quote_hook = lambda symbol: (_ for _ in ()).throw(RuntimeError('secret-token-in-upstream-error'))
    assert service.scan(frame(), START)['orders_sent'] == 0
    snap = service.snapshot()
    assert 'ROBINHOOD_QUOTE_UNAVAILABLE' in snap['last_quote_check']['reasons']
    assert 'secret-token-in-upstream-error' not in json.dumps(snap)
    adapter.quote_hook = None
    assert service.scan(frame(), START)['orders_sent'] == 1


@pytest.mark.parametrize('phase', ['preview', 'quote', 'account'])
def test_thirty_second_guard_includes_every_network_delay_and_can_retry(setup, monkeypatch, phase):
    service, adapter = setup
    arm(service)
    clock = [0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    def delay(*args): clock[0] += 31
    if phase == 'preview': adapter.preview_hook = delay
    elif phase == 'quote': adapter.quote_hook = delay
    else: adapter.account_delay_hook = delay
    assert service.scan(frame(), START)['orders_sent'] == 0
    snap = service.snapshot()
    assert not adapter.submissions and snap['control']['armed']
    assert snap['intents'][0]['status'] == 'ABANDONED'
    assert snap['intents'][0]['reserved_cash'] == '0'
    assert 'ALPACA_TIMESTAMP_STALE' in snap['last_quote_check']['reasons']
    assert 'UNKNOWN_INSTRUCTION' not in snap['readiness']['blockers']
    adapter.preview_hook = adapter.quote_hook = adapter.account_delay_hook = None
    adapter.now = START + timedelta(seconds=31)
    assert service.scan(frame(adapter.now), adapter.now)['orders_sent'] == 1


def test_sell_cross_quote_failure_preserves_latch_and_retries_same_bar(setup):
    service, adapter = buy(setup)
    order = adapter.data['orders'][0]
    adapter.fill(order['id'], order['quantity'])
    service.reconcile(START)
    adapter.now += timedelta(minutes=5)
    data = frame(adapter.now, price='9.40')
    assert service.scan(data, adapter.now)['orders_sent'] == 0
    snap = service.snapshot()
    assert snap['positions'][0]['exit_latched'] is True
    assert snap['intents'][0]['status'] == 'ABANDONED'
    assert snap['intents'][0]['reserved_quantity'] == '0'
    adapter.quote_price = '9.40'
    assert service.scan(data, adapter.now)['orders_sent'] == 1
    assert adapter.submissions[-1]['side'] == 'SELL'
    assert service.snapshot()['last_quote_check']['signal'] == {'required': False}


@pytest.mark.parametrize('phase', ['quote', 'account'])
def test_stop_during_new_read_stages_prevents_submission(setup, phase):
    service, adapter = setup
    arm(service)
    if phase == 'quote': adapter.quote_hook = lambda symbol: service._stop_now()
    else: adapter.account_delay_hook = service._stop_now
    assert service.scan(frame(), START)['orders_sent'] == 0
    assert not adapter.submissions
    intent = service.snapshot()['intents'][0]
    assert intent['status'] == 'ABANDONED' and intent['reserved_cash'] == '0'


def test_cancel_does_not_depend_on_cross_quote_availability(setup):
    service, adapter = buy(setup)
    adapter.quote_hook = lambda symbol: (_ for _ in ()).throw(RuntimeError('no quotes'))
    adapter.now += timedelta(seconds=301)
    service.scan(frame(adapter.now), adapter.now)
    assert len(adapter.cancellations) == 1 and len(adapter.quote_reads) == 1


@pytest.mark.parametrize('crash_state', ['PREPARED', 'PREVIEWED', 'SUBMITTING'])
def test_recovery_distinguishes_unsent_from_possibly_sent_keys(setup, crash_state):
    service, adapter = setup
    arm(service)
    def crash(*args): raise KeyboardInterrupt('simulated crash')
    if crash_state == 'PREPARED': adapter.preview_hook = crash
    elif crash_state == 'PREVIEWED': adapter.quote_hook = crash
    else: adapter.account_delay_hook = crash
    with pytest.raises(KeyboardInterrupt): service.scan(frame(), START)
    adapter.preview_hook = adapter.quote_hook = adapter.account_delay_hook = None
    assert not adapter.submissions
    snap = service.recover(START)
    assert not snap['control']['armed']
    intent = snap['intents'][0]
    if crash_state == 'SUBMITTING':
        assert intent['status'] == 'UNKNOWN' and Decimal(intent['reserved_cash']) > 0
        assert intent['signal_key'] == 'entry:XYZ:2026-10-26'
        assert 'UNKNOWN_INSTRUCTION' in snap['readiness']['blockers']
        assert service.scan(frame(), START)['orders_sent'] == 0
    else:
        assert intent['status'] == 'ABANDONED' and intent['reserved_cash'] == '0'
        assert ':unsent:' in intent['signal_key']
        arm(service)
        assert service.scan(frame(), START)['orders_sent'] == 1


@pytest.mark.parametrize('phase', ['INTENT_PERSISTED', 'EXECUTION_QUOTE_CHECK', 'SUBMISSION_ATTEMPT_STARTED'])
def test_audit_and_persistence_time_never_resets_quote_age(setup, monkeypatch, phase):
    service, adapter = setup
    arm(service)
    clock = [0]
    monkeypatch.setattr('trading.live_execution.time.monotonic', lambda: clock[0])
    original = service._audit
    delayed = [False]
    def slow_persistence(db, event, *args):
        original(db, event, *args)
        if event == phase and not delayed[0]:
            clock[0] += 31
            delayed[0] = True
    monkeypatch.setattr(service, '_audit', slow_persistence)
    assert service.scan(frame(), START)['orders_sent'] == 0
    assert not adapter.submissions
    snap = service.snapshot()
    assert snap['intents'][0]['status'] == 'ABANDONED'
    assert 'ALPACA_TIMESTAMP_STALE' in snap['last_quote_check']['reasons']


def test_transient_preview_failure_does_not_consume_the_signal(setup):
    service, adapter = setup
    arm(service)
    adapter.preview_status = 'BLOCKED'
    assert service.scan(frame(), START)['orders_sent'] == 0
    first = service.snapshot()['intents'][0]
    assert first['status'] == 'PREVIEW_BLOCKED' and first['reserved_cash'] == '0'
    assert ':unsent:' in first['signal_key'] and not adapter.quote_reads
    adapter.preview_status = 'READY'
    assert service.scan(frame(), START)['orders_sent'] == 1
    assert len(adapter.submissions) == 1


def test_prepare_reads_account_saves_scope_once_and_never_previews_or_arms(tmp_path):
    adapter = FakeAdapter()
    service = LiveExecutionService(tmp_path/'live.sqlite3', adapter, write_enabled=True)
    result = service.prepare(SCOPE, PARAMS, START)
    assert result['configuration']['revision'] == 1
    assert result['readiness']['ready_to_arm'] and not result['control']['armed']
    assert result['preview'] is None
    assert result['intents'] == result['orders'] == result['fills'] == result['positions'] == []
    assert adapter.previews == adapter.submissions == adapter.cancellations == []
    assert not service._may_write()
    adapter.now += timedelta(seconds=1)
    again = service.prepare({**SCOPE, 'max_order_amount': '100.00'},
                            {**PARAMS, 'risk_per_trade_pct': '0.020'}, adapter.now)
    assert again['configuration'] == result['configuration']
    assert again['checked_at'] == adapter.now.isoformat()
    assert len([e for e in again['audit'] if e['event'] == 'SCOPE_CONFIGURED']) == 1
    arm(service, adapter.now)
    adapter.preview_status = 'BLOCKED'
    assert service.scan(frame(adapter.now), adapter.now,
                        {**PARAMS, 'risk_per_trade_pct': '0.020'})['orders_sent'] == 0
    assert len(adapter.previews) == 1 and not adapter.submissions


@pytest.mark.parametrize('scope,params', [
    ({**SCOPE, 'symbols': []}, PARAMS),
    ({**SCOPE, 'symbols': ['bad']}, PARAMS),
    ({**SCOPE, 'max_order_amount': 'NaN'}, PARAMS),
    ({**SCOPE, 'max_positions': True}, PARAMS),
    ({**SCOPE, 'account_number': 'injected'}, PARAMS),
    (SCOPE, {**PARAMS, 'max_positions': 1.5}),
    (SCOPE, {**PARAMS, 'unexpected': 1}),
])
@pytest.mark.parametrize('armed', [False, True])
def test_invalid_prepare_preserves_control_configuration_and_never_reads(setup, monkeypatch, scope, params, armed):
    service, adapter = setup
    if armed:
        arm(service)
    before = service.snapshot()
    generation = service._stop_generation()
    reads = []
    monkeypatch.setattr(adapter, 'read_snapshot', lambda **kwargs: reads.append(kwargs))
    with pytest.raises(ValueError):
        service.prepare(scope, params, START)
    after = service.snapshot()
    assert after['configuration'] == before['configuration']
    assert after['control'] == before['control']
    assert after['audit'] == before['audit']
    assert service._stop_generation() == generation
    assert reads == []


def test_valid_prepare_refuses_armed_state_before_account_read(setup, monkeypatch):
    service, adapter = setup
    arm(service)
    before = service.snapshot()
    reads = []
    monkeypatch.setattr(adapter, 'read_snapshot', lambda **kwargs: reads.append(kwargs))
    with pytest.raises(ValueError, match='Disarm'):
        service.prepare({**SCOPE, 'max_order_amount': '90'}, PARAMS, START)
    after = service.snapshot()
    assert after['configuration'] == before['configuration']
    assert after['control'] == before['control']
    assert reads == [] and service._may_write()


def test_prepare_read_failure_never_reuses_old_account_to_change_scope(setup):
    service, adapter = setup
    before = service.snapshot()['configuration']
    adapter.fail_read = True
    with pytest.raises(ValueError, match='scope was not changed'):
        service.prepare({**SCOPE, 'max_order_amount': '90'}, PARAMS, START)
    snapshot = service.snapshot()
    assert snapshot['configuration'] == before
    assert not snapshot['control']['armed']
    assert 'ACCOUNT_READ_FAILED' in snapshot['readiness']['blockers']
    assert 'sensitive broker detail' not in json.dumps(snapshot)
    adapter.fail_read = False
    assert service.prepare({**SCOPE, 'max_order_amount': '90'}, PARAMS, START)['configuration']['revision'] == 2


def test_prepare_read_is_serialized_and_concurrent_stop_cannot_be_overwritten(setup, monkeypatch):
    service, adapter = setup
    entered, release = threading.Event(), threading.Event()
    original = adapter.read_snapshot
    def paused_read(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(adapter, 'read_snapshot', paused_read)
    prepared, stopped = [], []
    prepare_thread = threading.Thread(target=lambda: prepared.append(service.prepare(SCOPE, PARAMS, START)))
    prepare_thread.start()
    assert entered.wait(5)
    generation = service._stop_generation()
    stop_thread = threading.Thread(target=lambda: stopped.append(service.disarm()))
    stop_thread.start()
    deadline = time.monotonic() + 3
    while service._stop_generation() == generation and time.monotonic() < deadline:
        time.sleep(.01)
    assert service._stop_generation() != generation
    assert not service._may_write()
    release.set()
    prepare_thread.join(5)
    stop_thread.join(5)
    assert not prepare_thread.is_alive() and not stop_thread.is_alive()
    assert len(prepared) == len(stopped) == 1
    snapshot = service.snapshot()
    assert snapshot['configuration']['revision'] == 1
    assert snapshot['control']['armed'] is False
    assert snapshot['control']['reason'] == 'USER_DISARMED'
    assert adapter.previews == adapter.submissions == adapter.cancellations == []


def test_prepare_does_not_release_unknown_or_drop_managed_scope(setup):
    service, adapter = setup
    adapter.submit_hook = lambda order: (_ for _ in ()).throw(TimeoutError())
    buy(setup)
    before = service.snapshot()
    with pytest.raises(ValueError, match='existing live instructions'):
        service.prepare({**SCOPE, 'symbols': ['OTHER']}, PARAMS, START)
    after = service.snapshot()
    assert after['configuration'] == before['configuration']
    assert after['intents'][0]['status'] == 'UNKNOWN'
    assert after['intents'][0]['signal_key'] == before['intents'][0]['signal_key']
    assert after['intents'][0]['reserved_cash'] == before['intents'][0]['reserved_cash']


def test_prepare_can_reconfirm_restored_parameters_without_revision_churn(setup):
    service, _ = setup
    arm(service)
    service.scan(frame(), START, {**PARAMS, 'risk_per_trade_pct': '.01'})
    assert 'STRATEGY_PARAMETERS_CHANGED' in service.readiness(START)['blockers']
    result = service.prepare(SCOPE, PARAMS, START)
    assert result['configuration']['revision'] == 1
    assert result['readiness']['ready_to_arm'] and not result['control']['armed']
