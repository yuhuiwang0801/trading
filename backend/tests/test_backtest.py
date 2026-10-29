import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.backtest import ReplayBacktest
from trading.calendar import iso


def fixture_dataset():
    base = datetime(2026, 10, 26, 14, tzinfo=timezone.utc)
    candidate = {'symbol': 'ABC', 'security_id': 'test-id', 'name': 'Synthetic ABC',
                 'high20': '100', 'atr14': '2', 'ma20': '95', 'ma60': '90', 'rank': 1,
                 'known_at': '2026-10-23T20:00:00Z', 'asof_session': '2026-10-23', 'tradable': True}
    # Two closed bars confirm entry, later quotes permit fill, later closed bar exits.
    definitions = [(0, '99.90', [('13:55', '99')]),
                   (30, '100.10', [('13:55','99'), ('14:00','100.1')]),
                   (60, '100.10', [('13:55','99'), ('14:00','100.1')]),
                   (330, '110', [('14:00','100.1'), ('14:05','110')]),
                   (630, '103', [('14:05','110'), ('14:10','103')]),
                   (660, '103', [('14:05','110'), ('14:10','103')])]
    snapshots = []
    for seconds, price, bar_values in definitions:
        now = base + timedelta(seconds=seconds)
        value = Decimal(price)
        stamp = iso(now)
        snapshots.append({'timestamp': stamp, 'session_date': '2026-10-26', 'quality': 'valid',
                          'market_open': True, 'market_filter': True, 'candidates': [candidate],
                          'quotes': {'ABC': {'timestamp': stamp, 'bid': str(value-Decimal('.01')),
                                            'ask': str(value+Decimal('.01')), 'last': str(value), 'volume': '100000', 'tradable': True}},
                          'bars': {'ABC': [{'timestamp': f'2026-10-26T{minute}:00Z', 'close': close} for minute, close in bar_values]}})
    return {'kind': 'synthetic', 'source': 'demo', 'version': 'unit-test-v1', 'granularity': '5Min', 'snapshots': snapshots}


def test_daily_data_never_claims_intraday_validation():
    result = ReplayBacktest().run({'granularity': '1Day', 'kind': 'historical'})
    assert result['status'] == 'blocked' and result['metrics'] is None
    assert 'INTRADAY_DATA_REQUIRED' in result['reason']


def test_sequential_round_trip_costs_and_reproducibility():
    dataset = fixture_dataset()
    first = ReplayBacktest().run(dataset)
    second = ReplayBacktest().run(dataset)
    assert first['status'] == 'completed' and first['validation_status'] == 'software_replay_only'
    assert not first['profitability_validated']
    assert first['metrics'] == second['metrics']
    assert first['fills'] == second['fills']
    assert [fill['side'] for fill in first['fills']] == ['buy','sell']
    assert all(fill['timestamp'] > fill['signal_timestamp'] for fill in first['fills'])
    assert Decimal(first['metrics']['fees']) > 0
    assert first['metrics']['trade_count'] == 1
    assert Decimal(first['metrics']['final_equity']) > 10000
    assert first['metrics']['sharpe_daily'] is None


def test_future_bar_does_not_change_earlier_decisions_or_fill():
    baseline = fixture_dataset()
    future = copy.deepcopy(baseline)
    for frame in future['snapshots'][:3]:
        frame['bars']['ABC'].append({'timestamp': '2026-10-26T20:00:00Z', 'close': '9999999'})
    left, right = ReplayBacktest().run(baseline), ReplayBacktest().run(future)
    assert left['fills'] == right['fills']
    assert left['metrics'] == right['metrics']


def test_lookahead_daily_snapshot_blocked():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['candidates'][0]['known_at'] = '2026-10-26T15:00:00Z'
    result = ReplayBacktest().run(dataset)
    assert result['status'] == 'blocked' and result['reason'] == 'LOOKAHEAD_DAILY_INDICATORS'


def test_future_quote_blocked():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['quotes']['ABC']['timestamp'] = '2026-10-26T14:20:00Z'
    assert ReplayBacktest().run(dataset)['reason'] == 'FUTURE_QUOTE_IN_SNAPSHOT'


def test_no_liquidity_no_fill_and_not_bar_touch():
    dataset = fixture_dataset()
    for frame in dataset['snapshots']:
        frame['quotes']['ABC']['volume'] = '0'
    result = ReplayBacktest().run(dataset)
    assert result['fills'] == []
    assert result['metrics']['final_equity'] == '10000'


def test_signal_quote_is_not_fill_and_partial_volume_is_respected():
    dataset = fixture_dataset()
    for frame in dataset['snapshots']:
        frame['quotes']['ABC']['volume'] = '100'
    result = ReplayBacktest().run(dataset)
    assert result['fills'][0]['timestamp'] == '2026-10-26T14:01:00Z'
    assert result['fills'][0]['quantity'] == '1'
    assert len([intent for intent in result['intents'] if intent['side'] == 'buy']) == 1


def test_final_test_seal_rejects_retuning_across_instances(tmp_path):
    dataset = fixture_dataset()
    dataset['kind'], dataset['source'] = 'historical', 'test-fixture'
    dataset['splits'] = {'train':['2026-10-22','2026-10-22'], 'validation':['2026-10-23','2026-10-23'], 'test':['2026-10-26','2026-10-26']}
    lock_path = str(tmp_path/'research-seals.json')
    first = ReplayBacktest(lock_path=lock_path).run(dataset, split='test')
    second = ReplayBacktest(lock_path=lock_path).run(dataset, {'risk_per_trade_pct': '.003'}, split='test')
    assert first['status'] == 'completed'
    assert second['status'] == 'blocked' and 'FINAL_TEST_SEALED' in second['reason']


def test_overlapping_splits_rejected():
    dataset = fixture_dataset()
    dataset['splits'] = {'train':['2026-10-22','2026-10-24'], 'validation':['2026-10-23','2026-10-25'], 'test':['2026-10-26','2026-10-26']}
    result = ReplayBacktest().run(dataset)
    assert result['reason'] == 'SPLITS_MUST_BE_CHRONOLOGICAL_AND_NONOVERLAPPING'


def test_split_rebases_positions_before_new_prices_and_preserves_economics():
    baseline = fixture_dataset()
    dataset = copy.deepcopy(baseline)
    dataset['corporate_action_price_basis'] = 'as_of_snapshot'
    dataset['corporate_actions'] = [{'id':'split-1','type':'split','symbol':'ABC','effective_at':'2026-10-26T14:02:00Z','ratio':'2'}]
    for frame in dataset['snapshots'][3:]:
        quote = frame['quotes']['ABC']
        for field in ['bid','ask','last']:
            quote[field] = str(Decimal(quote[field])/2)
        frame['candidates'] = copy.deepcopy(frame['candidates'])
        for field in ['high20','atr14','ma20','ma60']:
            frame['candidates'][0][field] = str(Decimal(frame['candidates'][0][field])/2)
        for bar in frame['bars']['ABC']:
            bar['close'] = str(Decimal(bar['close'])/2)
    original, adjusted = ReplayBacktest().run(baseline), ReplayBacktest().run(dataset)
    assert adjusted['status'] == 'completed'
    assert Decimal(adjusted['fills'][-1]['quantity']) == 2*Decimal(original['fills'][-1]['quantity'])
    assert Decimal(adjusted['metrics']['final_equity']) == Decimal(original['metrics']['final_equity'])


def test_dividend_entitlement_and_payable_date_are_separate():
    baseline = fixture_dataset()
    dataset = copy.deepcopy(baseline)
    dataset['corporate_action_price_basis'] = 'as_of_snapshot'
    dataset['corporate_actions'] = [{'id':'div-1','type':'cash_dividend','symbol':'ABC','ex_at':'2026-10-26T14:02:00Z','pay_at':'2026-10-26T14:07:00Z','cash_per_share':'1'}]
    original, adjusted = ReplayBacktest().run(baseline), ReplayBacktest().run(dataset)
    quantity = Decimal(original['fills'][0]['quantity'])
    assert Decimal(adjusted['metrics']['final_equity']) - Decimal(original['metrics']['final_equity']) == quantity
    events = adjusted['corporate_action_events']
    assert events[0]['type'] == 'dividend_entitlement'
    assert events[1]['type'] == 'dividend_payment'
    assert events[0]['effective_at'] < events[1]['effective_at']
    assert adjusted['dividend_receivables'] == {}


def test_symbol_change_preserves_position_and_cash_delisting_requires_explicit_terms():
    dataset = fixture_dataset()
    dataset['corporate_action_price_basis'] = 'as_of_snapshot'
    dataset['corporate_actions'] = [{'id':'rename-1','type':'symbol_change','symbol':'ABC','new_symbol':'XYZ','effective_at':'2026-10-26T14:02:00Z'}]
    for frame in dataset['snapshots'][3:]:
        frame['quotes']['XYZ'] = frame['quotes'].pop('ABC')
        frame['bars']['XYZ'] = frame['bars'].pop('ABC')
        frame['candidates'] = copy.deepcopy(frame['candidates'])
        frame['candidates'][0]['symbol'] = 'XYZ'
    result = ReplayBacktest().run(dataset)
    assert [fill['symbol'] for fill in result['fills']] == ['ABC','XYZ']
    assert result['metrics']['open_positions'] == 0
    dataset = fixture_dataset()
    dataset['corporate_action_price_basis'] = 'as_of_snapshot'
    dataset['corporate_actions'] = [{'id':'delist-1','type':'cash_delisting','symbol':'ABC','effective_at':'2026-10-26T14:02:00Z','cash_per_share':'80'}]
    result = ReplayBacktest().run(dataset)
    assert result['metrics']['open_positions'] == 0
    assert result['trades'][0]['corporate_action_id'] == 'delist-1'
    assert [fill['side'] for fill in result['fills']] == ['buy']


def test_unsupported_corporate_action_is_blocked_not_silently_ignored():
    dataset = fixture_dataset()
    dataset['corporate_action_price_basis'] = 'as_of_snapshot'
    dataset['corporate_actions'] = [{'id':'merger-1','type':'unknown_merger','symbol':'ABC'}]
    result = ReplayBacktest().run(dataset)
    assert result['status'] == 'blocked' and result['reason'].startswith('UNSUPPORTED_CORPORATE_ACTION')


def test_repeated_quote_observation_cannot_reuse_bar_volume():
    dataset = fixture_dataset()
    dataset['snapshots'].insert(3, copy.deepcopy(dataset['snapshots'][2]))
    dataset['snapshots'][3]['timestamp'] = '2026-10-26T14:01:30Z'
    dataset['snapshots'][3]['quotes']['ABC']['timestamp'] = '2026-10-26T14:01:30Z'
    for frame in dataset['snapshots']:
        frame['quotes']['ABC']['volume'] = '100'
        frame['quotes']['ABC']['volume_timestamp'] = '2026-10-26T14:00:00Z'
    result = ReplayBacktest().run(dataset)
    buys = [fill for fill in result['fills'] if fill['side'] == 'buy']
    assert len(buys) == 1 and buys[0]['quantity'] == '1'


def test_cash_sale_proceeds_are_unsettled_until_next_session():
    result = ReplayBacktest().run(fixture_dataset())
    last = result['equity'][-1]
    assert Decimal(last['unsettled_cash']) > 0
    assert Decimal(last['cash']) > Decimal(last['settled_cash'])
    assert result['unsettled_proceeds'][0]['session_date'] == '2026-10-27'


def test_nonfinite_market_input_blocks_replay():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['quotes']['ABC']['bid'] = 'NaN'
    result = ReplayBacktest().run(dataset)
    assert result['status'] == 'blocked' and 'finite' in result['reason'].lower()


def test_demo_engine_and_replay_match_every_equity_point_and_fill():
    from trading.demo import dataset, run_demo
    from trading.engine import Engine
    from trading.calendar import as_utc
    engine = Engine('sqlite:///:memory:')
    online = run_demo(engine)
    replay = ReplayBacktest().run(dataset(), split='train')
    state = engine.snapshot()
    assert online['status'] == replay['status'] == 'completed'
    assert len(online['equity_curve']) == len(replay['equity']) == 8
    for observed, replayed in zip(online['equity_curve'], replay['equity']):
        assert as_utc(observed['timestamp']) == as_utc(replayed['timestamp'])
        assert Decimal(observed['equity']) == Decimal(replayed['equity'])
        assert Decimal(observed['cash']) == Decimal(replayed['cash'])
    for field in ['equity', 'cash', 'settled_cash', 'unsettled_cash']:
        assert Decimal(state['account'][field]) == Decimal(replay['equity'][-1][field])
    def normalize(fill):
        return (as_utc(fill['timestamp']), fill['symbol'], fill['side'].upper(),
                Decimal(fill['quantity']), Decimal(fill['price']), Decimal(fill['fee']))
    assert sorted(map(normalize, state['fills'])) == sorted(map(normalize, replay['fills']))
    assert len(replay['fills']) == 4
    assert Decimal(replay['metrics']['net_return']) < 0  # Actual fixture outcome, never a fabricated profit.


def test_prior_day_bars_published_before_current_open_are_allowed():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['candidates'][0]['known_at'] = '2026-10-26T13:00:00Z'
    assert ReplayBacktest().run(dataset)['status'] == 'completed'


def test_old_daily_snapshot_is_rejected_even_when_publication_time_is_old():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['candidates'][0]['asof_session'] = '2026-10-22'
    assert ReplayBacktest().run(dataset)['reason'] == 'STALE_DAILY_INDICATORS'


def test_max_price_deviation_changes_admission_but_never_ignores_limit():
    dataset = fixture_dataset()
    for frame in dataset['snapshots'][1:3]:
        frame['quotes']['ABC'].update({'bid':'102.99','ask':'103.01','last':'103'})
    conservative = ReplayBacktest().run(dataset)
    allowed = ReplayBacktest().run(dataset, {'max_price_deviation_pct': '.05'})
    assert not any(fill['side']=='buy' for fill in conservative['fills'])
    assert any(decision['reason']=='MAX_PRICE_DEVIATION' for decision in conservative['decisions'])
    assert any(fill['side']=='buy' for fill in allowed['fills'])
    intents = {intent['intent_id']: intent for intent in allowed['intents']}
    assert all(Decimal(fill['price']) <= Decimal(intents[fill['intent_id']]['limit_price'])
               for fill in allowed['fills'] if fill['side']=='buy')


def test_stale_frame_never_matches_an_old_order_or_creates_new_intents():
    dataset = fixture_dataset()
    for frame in dataset['snapshots'][2:]:
        frame['quality'] = 'stale'
    result = ReplayBacktest().run(dataset)
    assert len(result['intents']) == 1 and not result['fills']


def test_extended_session_bar_in_imported_dataset_is_rejected():
    dataset = fixture_dataset()
    dataset['snapshots'][0]['bars']['ABC'][0]['timestamp'] = '2026-10-26T13:25:00Z'
    result = ReplayBacktest().run(dataset)
    assert result['status'] == 'blocked' and result['reason'] == 'EXTENDED_SESSION_BAR_NOT_ALLOWED'
