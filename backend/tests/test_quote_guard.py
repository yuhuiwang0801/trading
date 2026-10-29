from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from trading.quote_guard import cross_quote_check


NOW = datetime(2026, 10, 27, 14, 10, 30, tzinfo=timezone.utc)


def inputs(side='BUY'):
    return {
        'order': {'symbol': 'AAPL', 'side': side, 'quantity': '2', 'limit_price': '100'},
        'alpaca_quote': {'symbol': 'AAPL', 'timestamp': NOW - timedelta(seconds=1),
                         'bid': '100', 'ask': '100', 'last': '100', 'quality': 'valid'},
        'robinhood_quote': {'symbol': 'AAPL', 'bid_price': '100', 'ask_price': '100',
                            'venue_bid_time': NOW - timedelta(seconds=1),
                            'venue_ask_time': NOW - timedelta(seconds=1),
                            'state': 'active', 'has_traded': True},
        'now': NOW, 'params': {},
        'signal': {'price': '100', 'bar_time': NOW - timedelta(seconds=30)},
    }


def test_valid_comparison_is_json_safe_preserves_intent_and_redacts_unneeded_fields():
    request = inputs()
    request['order']['quantity'] = Decimal('2')
    request['order']['account_number'] = 'secret-account'
    request['robinhood_quote']['debug'] = 'sensitive broker body'
    request['signal']['reason'] = 'untrusted description'
    original = deepcopy(request)
    result = cross_quote_check(**request)
    assert result['status'] == 'PASSED'
    assert result['reasons'] == []
    assert result['thresholds']['quote_max_age_seconds'] == '30'
    assert result['order']['limit_notional'] == '200'
    assert request == original
    serialized = json.dumps(result, allow_nan=False)
    assert 'secret-account' not in serialized
    assert 'sensitive broker body' not in serialized
    assert 'untrusted description' not in serialized


@pytest.mark.parametrize('source,field,reason', [
    ('alpaca_quote', 'timestamp', 'ALPACA_TIMESTAMP_STALE'),
    ('robinhood_quote', 'venue_bid_time', 'ROBINHOOD_BID_TIMESTAMP_STALE'),
    ('robinhood_quote', 'venue_ask_time', 'ROBINHOOD_ASK_TIMESTAMP_STALE'),
])
def test_each_side_age_is_independent_and_strictly_capped_at_30_seconds(source, field, reason):
    request = inputs()
    request['params']['quote_max_age_seconds'] = 90
    request[source][field] = NOW - timedelta(seconds=30)
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request[source][field] -= timedelta(microseconds=1)
    assert reason in cross_quote_check(**request)['reasons']


def test_configured_shorter_quote_age_and_closed_bar_age_are_used():
    request = inputs()
    request['params'] = {'quote_max_age_seconds': 5, 'bar_max_age_seconds': 30}
    request['robinhood_quote']['venue_bid_time'] = NOW - timedelta(seconds=5)
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['robinhood_quote']['venue_bid_time'] -= timedelta(microseconds=1)
    assert 'ROBINHOOD_BID_TIMESTAMP_STALE' in cross_quote_check(**request)['reasons']
    request['robinhood_quote']['venue_bid_time'] = NOW
    request['signal']['bar_time'] -= timedelta(microseconds=1)
    assert 'SIGNAL_TIMESTAMP_STALE' in cross_quote_check(**request)['reasons']


@pytest.mark.parametrize('source,field,prefix', [
    ('alpaca_quote', 'timestamp', 'ALPACA_TIMESTAMP'),
    ('robinhood_quote', 'venue_bid_time', 'ROBINHOOD_BID_TIMESTAMP'),
    ('robinhood_quote', 'venue_ask_time', 'ROBINHOOD_ASK_TIMESTAMP'),
    ('signal', 'bar_time', 'SIGNAL_TIMESTAMP'),
])
@pytest.mark.parametrize('bad_time,ending', [
    (None, '_INVALID'), ('2026-10-27T14:10:29', '_INVALID'),
    (NOW.replace(tzinfo=None), '_INVALID'), ('bad secret timestamp', '_INVALID'),
    (NOW + timedelta(microseconds=1), '_FUTURE'),
])
def test_invalid_or_future_times_are_blocked_without_raw_text(source, field, prefix, bad_time, ending):
    request = inputs()
    request[source][field] = bad_time
    result = cross_quote_check(**request)
    assert result['status'] == 'BLOCKED'
    assert prefix + ending in result['reasons']
    assert 'bad secret timestamp' not in json.dumps(result)


@pytest.mark.parametrize('source,field', [
    ('alpaca_quote', 'bid'), ('alpaca_quote', 'ask'), ('alpaca_quote', 'last'),
    ('robinhood_quote', 'bid_price'), ('robinhood_quote', 'ask_price'),
    ('order', 'limit_price'), ('order', 'quantity'), ('signal', 'price'),
])
@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', 0, '-1', None, True])
def test_bad_prices_never_pass_or_escape_json(source, field, value):
    request = inputs()
    request[source][field] = value
    result = cross_quote_check(**request)
    assert result['status'] == 'BLOCKED'
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('source,field', [
    ('alpaca_quote', 'bid'), ('alpaca_quote', 'ask'), ('alpaca_quote', 'last'),
    ('alpaca_quote', 'timestamp'), ('alpaca_quote', 'quality'),
    ('robinhood_quote', 'symbol'), ('robinhood_quote', 'bid_price'),
    ('robinhood_quote', 'ask_price'), ('robinhood_quote', 'venue_bid_time'),
    ('robinhood_quote', 'venue_ask_time'), ('robinhood_quote', 'state'),
    ('robinhood_quote', 'has_traded'), ('signal', 'price'), ('signal', 'bar_time'),
])
def test_required_fields_cannot_be_omitted(source, field):
    request = inputs()
    del request[source][field]
    assert cross_quote_check(**request)['status'] == 'BLOCKED'


@pytest.mark.parametrize('source,field,value,code', [
    ('robinhood_quote', 'state', 'halted SECRET', 'ROBINHOOD_NOT_ACTIVE'),
    ('robinhood_quote', 'has_traded', False, 'ROBINHOOD_NOT_TRADED'),
    ('robinhood_quote', 'symbol', 'MSFT', 'ROBINHOOD_SYMBOL_MISMATCH'),
    ('alpaca_quote', 'symbol', 'MSFT', 'ALPACA_SYMBOL_MISMATCH'),
    ('alpaca_quote', 'quality', 'stale SECRET', 'ALPACA_INVALID_QUOTE_QUALITY'),
    ('alpaca_quote', 'halted', True, 'ALPACA_SECURITY_NOT_TRADABLE'),
    ('alpaca_quote', 'tradable', False, 'ALPACA_SECURITY_NOT_TRADABLE'),
    ('signal', 'complete', False, 'SIGNAL_INCOMPLETE'),
    ('signal', 'quality', 'invalid SECRET', 'SIGNAL_INVALID_QUALITY'),
    ('signal', 'symbol', 'MSFT', 'SIGNAL_SYMBOL_MISMATCH'),
])
def test_security_and_signal_quality_checks(source, field, value, code):
    request = inputs()
    request[source][field] = value
    result = cross_quote_check(**request)
    assert code in result['reasons']
    assert 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('source,bid_field,ask_field,prefix', [
    ('alpaca_quote', 'bid', 'ask', 'ALPACA'),
    ('robinhood_quote', 'bid_price', 'ask_price', 'ROBINHOOD'),
])
def test_crossed_and_wide_books_block_but_spread_boundary_passes(source, bid_field, ask_field, prefix):
    request = inputs()
    request[source][bid_field], request[source][ask_field] = '99.75', '100.25'
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request[source][ask_field] = '100.2501'
    assert prefix + '_SPREAD_TOO_WIDE' in cross_quote_check(**request)['reasons']
    request[source][bid_field], request[source][ask_field] = '100.01', '100'
    assert prefix + '_CROSSED_BOOK' in cross_quote_check(**request)['reasons']


def test_both_feed_sides_have_independent_inclusive_deviation_bounds():
    request = inputs()
    request['robinhood_quote'].update(bid_price='100.5', ask_price='100.5')
    request['order']['limit_price'] = '100.5'
    request['signal']['price'] = '100.5'
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['robinhood_quote']['ask_price'] = '100.50001'
    result = cross_quote_check(**request)
    assert 'ASK_FEED_DEVIATION' in result['reasons']
    assert 'BID_FEED_DEVIATION' not in result['reasons']
    request['robinhood_quote'].update(bid_price='100.50001', ask_price='100.50001')
    assert 'BID_FEED_DEVIATION' in cross_quote_check(**request)['reasons']


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_limit_deviation_bound_is_inclusive_and_original_limit_never_changes(side):
    request = inputs(side)
    request['order']['limit_price'] = '100.5'
    request['signal']['price'] = '100.5'
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['order']['limit_price'] = '100.50001'
    original = deepcopy(request)
    result = cross_quote_check(**request)
    assert 'LIMIT_PRICE_DEVIATION' in result['reasons']
    assert result['order']['limit_price'] == '100.50001'
    assert request == original


def test_buy_signal_ask_and_limit_bounds_are_inclusive_and_independent():
    request = inputs()
    request['alpaca_quote'].update(bid='100.5', ask='100.5')
    request['robinhood_quote'].update(bid_price='100.5', ask_price='100.5')
    request['order']['limit_price'] = '100.5'
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['robinhood_quote']['ask_price'] = '100.50001'
    result = cross_quote_check(**request)
    assert 'SIGNAL_ASK_DEVIATION' in result['reasons']
    assert 'SIGNAL_LIMIT_DEVIATION' not in result['reasons']
    request['order']['limit_price'] = '100.50001'
    assert 'SIGNAL_LIMIT_DEVIATION' in cross_quote_check(**request)['reasons']


def test_sell_ignores_old_trigger_price_and_time_without_bypassing_book_checks():
    request = inputs('SELL')
    request['signal'] = {'price': '9999', 'bar_time': 'bad old time', 'complete': False}
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['signal'] = None
    assert cross_quote_check(**request)['status'] == 'PASSED'
    request['robinhood_quote']['venue_bid_time'] = NOW - timedelta(seconds=31)
    assert 'ROBINHOOD_BID_TIMESTAMP_STALE' in cross_quote_check(**request)['reasons']
    request['order']['side'] = 'BUY'
    assert 'SIGNAL_MISSING' in cross_quote_check(**request)['reasons']


@pytest.mark.parametrize('side,limit', [('BUY', '99.9'), ('SELL', '100.1')])
def test_valid_resting_limit_does_not_need_to_cross_broker_book(side, limit):
    request = inputs(side)
    request['order']['limit_price'] = limit
    assert cross_quote_check(**request)['status'] == 'PASSED'


@pytest.mark.parametrize('field,value', [
    ('quote_max_age_seconds', 0), ('bar_max_age_seconds', -1),
    ('max_spread_pct', 'NaN'), ('max_price_deviation_pct', -1),
    ('quote_max_age_seconds', '0.5'), ('bar_max_age_seconds', '1.5'),
    ('max_spread_pct', '1.01'), ('max_price_deviation_pct', '1.01'),
])
def test_invalid_thresholds_fail_closed(field, value):
    request = inputs()
    request['params'][field] = value
    result = cross_quote_check(**request)
    assert result['status'] == 'BLOCKED'
    assert 'PARAMETERS_INVALID' in result['reasons']


def test_aware_timezone_offsets_are_compared_as_instants_and_alpaca_symbol_is_optional():
    request = inputs()
    del request['alpaca_quote']['symbol']
    request['robinhood_quote']['venue_bid_time'] = '2026-10-27T10:10:29-04:00'
    assert cross_quote_check(**request)['status'] == 'PASSED'


@pytest.mark.parametrize('field,value', [
    ('now', '2026-10-27T14:10:30'), ('order', []), ('alpaca_quote', []),
    ('robinhood_quote', None), ('signal', []), ('params', ['bad private config']),
])
def test_malformed_inputs_return_blocked_instead_of_raising(field, value):
    request = inputs()
    request[field] = value
    result = cross_quote_check(**request)
    assert result['status'] == 'BLOCKED'
    assert 'bad private config' not in json.dumps(result, allow_nan=False)
