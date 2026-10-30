from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import pytest

from trading.live_workspace import build_live_workspace
from test_live_api import setup


NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def snapshot():
    return {'checked_at': STAMP,
        'account': {'masked_number': '••••0001', 'currency': 'USD', 'total_value': '1500.00',
            'cash': '500.00', 'buying_power': '480.00', 'unleveraged_buying_power': '470.00',
            'execution_buying_power': '450.00', 'local_reserved_cash': '20.00'},
        'broker_positions': [{'symbol': 'AAPL', 'quantity': '5', 'available_quantity': '4',
                              'average_price': '200.00'}],
        'positions': [], 'orders': [], 'intents': [], 'fills': [], 'audit': [],
        'last_scan': {'status': 'DISARMED', 'checked_at': STAMP, 'orders_sent': 0,
                      'cancel_requests': 0, 'blockers': []}}


def intent(ident='intent-1', **patch):
    return {'id': ident, 'ref_id': 'ref-' + ident, 'symbol': 'AAPL', 'side': 'BUY',
        'status': 'UNKNOWN', 'quantity': '1', 'filled_quantity': '0', 'limit_price': '201.00',
        'reserved_cash': '201.00', 'reserved_quantity': '0', 'created_at': STAMP,
        'expires_at': (NOW + timedelta(minutes=5)).isoformat(), 'broker_order_id': None,
        'approval_id': None, 'evidence': {'atr': '2.0', 'ma20': '190.0',
                                       'signal_price': '200.75', 'signal_bar_time': STAMP}, **patch}


def test_intraday_exit_evidence_is_visible_but_untrusted_payloads_are_not():
    source = snapshot()
    source['positions'] = [{'symbol': 'AAPL', 'quantity': '5', 'avg_price': '200',
        'intraday_status': 'triggered', 'exit_reason': 'INTRADAY_TRAILING_STOP',
        'intraday_peak_bid': '206', 'intraday_trailing_active': True,
        'intraday_evidence': {'confirmed_bid': '202.9', 'peak_bid': '206', 'trailing_active': True,
                             'observed_at': STAMP, 'peer_count': 5, 'secret': 'must-not-pass'},
        'intraday_quote_check': {'status': 'PASSED', 'secret': 'must-not-pass'}}]
    row = build_live_workspace(source, now=NOW)['positions'][0]
    assert row['exit_reason'] == 'INTRADAY_TRAILING_STOP'
    assert row['intraday_peak_bid'] == '206'
    assert row['intraday_evidence']['confirmed_bid'] == '202.9'
    assert row['intraday_quote_check'] == {'status': 'PASSED'}
    assert 'must-not-pass' not in json.dumps(row)


def test_known_live_account_is_separate_from_paper_and_no_pnl_is_invented():
    source = snapshot()
    before = deepcopy(source)
    result = build_live_workspace(source, now=NOW)
    assert result['source'] == 'robinhood' and result['ledger'] == 'isolated_live'
    assert result['account_status'] == 'fresh'
    assert result['account']['equity'] == result['account']['total_value'] == '1500.00'
    assert result['account']['reserved_cash'] == '20.00'
    assert result['account']['execution_buying_power'] == '450.00'
    assert result['account']['unrealized_pnl'] is None
    assert result['account']['realized_pnl'] is None
    assert result['account']['exposure_pct'] is None
    assert result['equity_curve'] == result['signals'] == []
    assert result['runs'][0]['status'] == 'DISARMED'
    assert source == before
    result['account']['equity'] = '999'
    result['positions'][0]['quantity'] = '999'
    assert source == before


@pytest.mark.parametrize('source', [None, {}, {'account': {}}, {'account': {'total_value': '10000'}}])
def test_unverified_account_is_unknown_not_default_cash_or_equity(source):
    result = build_live_workspace(source, now=NOW)
    assert result['account_status'] == 'unavailable'
    for key in ('equity', 'total_value', 'cash', 'buying_power', 'reserved_cash', 'market_value'):
        assert result['account'][key] is None
    assert result['positions'] == []


def test_revoked_authorization_hides_cached_account_amounts_and_positions():
    result = build_live_workspace(snapshot(), authorized=False, now=NOW)
    assert result['account_status'] == 'unauthorized'
    assert result['account']['equity'] is None and result['account']['masked_number'] is None
    assert result['positions'] == []


@pytest.mark.parametrize('seconds,status', [(0, 'fresh'), (60, 'fresh'), (61, 'stale'), (-1, 'stale')])
def test_snapshot_freshness_is_explicit_without_replacing_its_timestamp(seconds, status):
    result = build_live_workspace(snapshot(), now=NOW + timedelta(seconds=seconds))
    assert result['account_status'] == status
    assert result['account']['age_seconds'] == seconds
    assert result['checked_at'] == STAMP
    assert result['account']['equity'] == '1500.00'


def test_external_position_is_read_only_and_not_adopted():
    result = build_live_workspace(snapshot(), now=NOW)
    row = result['positions'][0]
    assert row['quantity'] == '5' and row['available_quantity'] == '4'
    assert row['avg_price'] == '200.00'
    assert row['managed_quantity'] == '0' and row['unmanaged_quantity'] == '5'
    assert row['management'] == 'unmanaged' and row['managed'] is False
    assert row['read_only'] is True and row['can_adopt'] is False
    assert row['mark_price'] is row['last_price'] is row['unrealized_pnl'] is row['market_value'] is None
    assert row['protection'] is row['ma20'] is None


@pytest.mark.parametrize('managed,ownership,external', [('2', 'partial', '3'), ('5', 'managed', '0')])
def test_system_ownership_is_annotated_without_overwriting_broker_total_or_cost(managed, ownership, external):
    source = snapshot()
    source['positions'] = [{'symbol': 'AAPL', 'quantity': managed, 'avg_price': '202',
        'protection': '198', 'ma20': '190', 'entry_atr': '2', 'highest_close': '204',
        'opened_at': STAMP, 'last_bar_at': STAMP, 'last_price': '203'}]
    row = build_live_workspace(source, now=NOW)['positions'][0]
    assert row['quantity'] == '5' and row['avg_price'] == '200.00'
    assert row['management'] == ownership and row['managed_quantity'] == managed
    assert row['unmanaged_quantity'] == external and row['protection_quantity'] == managed
    assert row['managed_avg_price'] == '202' and row['protection'] == '198'
    assert row['managed_reference_price'] == '203' and row['mark_price'] is None
    assert row['managed'] is True and row['can_adopt'] is False


@pytest.mark.parametrize('local', ['6', 'NaN', None])
def test_mismatched_managed_quantity_does_not_claim_control_of_broker_position(local):
    source = snapshot()
    source['positions'] = [{'symbol': 'AAPL', 'quantity': local, 'protection': '198'}]
    result = build_live_workspace(source, now=NOW)
    row = result['positions'][0]
    assert row['quantity'] == '5' and row['management'] == 'mismatch'
    assert not row['managed'] and row['read_only'] and row['unmanaged_quantity'] is None
    assert row['protection'] is None and result['position_reconciliation']


def test_orphan_system_position_is_diagnostic_not_an_invented_broker_holding():
    source = snapshot()
    source['positions'] = [{'symbol': 'MSFT', 'quantity': '3', 'avg_price': '100'}]
    result = build_live_workspace(source, now=NOW)
    assert [row['symbol'] for row in result['positions']] == ['AAPL']
    assert result['position_reconciliation'] == [{'symbol': 'MSFT', 'reason': 'MANAGED_POSITION_NOT_IN_BROKER_SNAPSHOT'}]


def test_duplicate_broker_rows_do_not_double_the_displayed_holding():
    source = snapshot()
    source['broker_positions'].append(deepcopy(source['broker_positions'][0]))
    result = build_live_workspace(source, now=NOW)
    assert len(result['positions']) == 1
    assert result['positions'][0]['quantity'] == '5'
    assert result['positions'][0]['management'] == 'mismatch'


@pytest.mark.parametrize('link', ['intent_id', 'broker_order_id', 'ref_id', 'approval_id'])
def test_broker_order_supersedes_corresponding_intent_without_duplicate(link):
    source = snapshot()
    local = intent()
    order = {'id': 'order-1', 'symbol': 'AAPL', 'side': 'BUY', 'state': 'PARTIALLY_FILLED',
        'quantity': '1', 'filled_quantity': '0.5', 'limit_price': '201.00', 'created_at': STAMP,
        'updated_at': STAMP, 'owner': 'system'}
    if link == 'intent_id': order['intent_id'] = local['id']
    elif link == 'broker_order_id': local['broker_order_id'] = order['id']
    elif link == 'ref_id': order['ref_id'] = local['ref_id']
    else:
        local['approval_id'] = 'approval-1'
        order['ref_id'] = 'approval-1'
    source['intents'], source['orders'] = [local], [order, deepcopy(order)]
    result = build_live_workspace(source, now=NOW)
    assert len(result['orders']) == 1 and len(result['intents']) == 1
    row = result['orders'][0]
    assert row['id'] == row['broker_order_id'] == 'order-1'
    assert row['intent_id'] == 'intent-1' and row['status'] == 'PARTIALLY_FILLED'
    assert row['intent_status'] == 'UNKNOWN' and row['filled_quantity'] == '0.5'
    assert row['source_kind'] == 'broker_order' and row['can_cancel'] is False


@pytest.mark.parametrize('status,terminal', [('UNKNOWN', False), ('PREVIEW_BLOCKED', True),
    ('ABANDONED', True), ('AWAITING_APPROVAL', False), ('SUBMITTING', False),
    ('ACCEPTED', False), ('CONFIRMED', False), ('QUEUED', False), ('FAILED', True)])
def test_unmatched_instructions_remain_visible_with_their_exact_lifecycle(status, terminal):
    source = snapshot()
    source['intents'] = [intent(status=status)]
    row = build_live_workspace(source, now=NOW)['orders'][0]
    assert row['status'] == status and row['terminal'] is terminal
    assert row['source_kind'] == 'intent' and row['intent_id'] == row['id'] == 'intent-1'
    assert row['ref_id'] == 'ref-intent-1' and row['order_id'] is None
    assert row['evidence']['signal_price'] == '200.75'


def test_system_fills_and_audit_are_allowlisted_and_deduplicated():
    source = snapshot()
    fill = {'id': 'fill-1', 'order_id': 'order-1', 'intent_id': 'intent-1', 'symbol': 'AAPL',
        'side': 'BUY', 'quantity': '1', 'price': '200.5', 'fees': '.01', 'timestamp': STAMP}
    source['fills'] = [fill, deepcopy(fill)]
    source['audit'] = [{'sequence': 1, 'timestamp': STAMP, 'event': 'EXECUTION_QUOTE_CHECK',
        'detail': {'intent_id': 'intent-1', 'status': 'BLOCKED', 'reasons': ['ASK_FEED_DEVIATION'],
                   'order': {'symbol': 'AAPL', 'quantity': '1', 'limit_price': '201'},
                   'robinhood': {'bid': '200', 'ask': '201', 'ask_at': STAMP}}}]
    result = build_live_workspace(source, now=NOW)
    assert len(result['fills']) == 1 and result['fills'][0]['order_id'] == 'order-1'
    assert result['audit'][0]['symbol'] == 'AAPL'
    assert result['audit'][0]['detail']['reasons'] == ['ASK_FEED_DEVIATION']


def test_nested_account_credentials_and_untrusted_text_are_never_copied():
    source = snapshot()
    secret = {'account_number': 'private-account-number', 'access_token': 'private-access-token',
        'refresh_token': 'private-refresh-token', 'guide': 'private-untrusted-guide',
        'raw': {'password': 'private-password'}, 'message': 'private-freeform-message'}
    source.update(secret)
    source['account'].update(secret)
    source['broker_positions'][0].update(secret)
    local = intent(**secret)
    local['evidence'].update(secret)
    source['intents'] = [local]
    source['audit'] = [{'sequence': 1, 'timestamp': STAMP, 'event': 'INTENT_PERSISTED',
        'detail': {**secret, 'order': {**secret, 'symbol': 'AAPL'}}}]
    result = build_live_workspace(source, now=NOW)
    rendered = json.dumps(result)
    assert 'private-' not in rendered
    assert 'account_number' not in rendered and 'access_token' not in rendered
    assert result['account']['masked_number'] == '••••0001'


@pytest.mark.parametrize('invalid', ['NaN', 'Infinity', '-Infinity', True, {}, '1e1000'])
def test_invalid_account_amounts_are_unknown_and_json_safe(invalid):
    source = snapshot()
    source['account']['total_value'] = invalid
    result = build_live_workspace(source, now=NOW)
    assert result['account']['equity'] is None
    json.dumps(result, allow_nan=False)


def test_workspace_api_only_reads_local_snapshot_and_does_not_mutate_or_call_broker(setup):
    app, live, remote, oauth = setup
    with TestClient(app) as client:
        before = live.snapshot()
        paper_before = app.state.engine.snapshot()
        response = client.get('/api/live/workspace')
        assert response.status_code == 200
        result = response.json()
        assert result['account_status'] == 'unavailable'
        assert result['account']['equity'] is None and result['orders'] == []
        assert remote.calls == []
        assert live.snapshot() == before
        assert app.state.engine.snapshot() == paper_before
        oauth.authorized = False
        assert client.get('/api/live/workspace').json()['account_status'] == 'unauthorized'
        assert remote.calls == []


def test_workspace_api_projects_preexisting_synthetic_broker_snapshot_without_new_reads(setup):
    app, live, remote, _ = setup
    # The fixture client is synthetic; this creates only an isolated test mirror.
    live.reconcile(NOW)
    with TestClient(app) as client:
        calls_before = deepcopy(remote.calls)
        before = live.snapshot()
        response = client.get('/api/live/workspace')
        assert response.status_code == 200
        result = response.json()
        assert result['account']['equity'] == before['account']['total_value']
        assert result['account']['masked_number'] == '••••0001'
        assert len(result['positions']) == 1
        assert result['positions'][0]['management'] == 'unmanaged'
        assert result['orders'] == result['fills'] == []  # External account history is not system activity.
        assert remote.calls == calls_before and live.snapshot() == before
