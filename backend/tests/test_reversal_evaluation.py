import importlib.util
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from trading.reversal_evaluation import benchmark_and_uncertainty, signal_diagnostics


def benchmark_fixture(count=3):
    days = xcals.get_calendar('XNYS').sessions_in_range('2022-01-03', '2022-12-30').strftime('%Y-%m-%d')[:count]
    panel = pd.DataFrame({'session': days, 'symbol': 'SPY', 'tr_close': 100., 'adj_open': 100.})
    returns = np.full(count, .001)
    run = {'config': {'initial_cash': 100000}, 'equity': [
        {'session': day, 'nav': 100000 * float(np.prod(1 + returns[:i + 1])),
         'daily_return': float(returns[i]), 'exposure': .5}
        for i, day in enumerate(days)]}
    return run, panel


def test_benchmark_starts_at_first_open_and_uses_only_prior_exposure():
    run, panel = benchmark_fixture()
    panel['tr_close'] = [110., 121., 108.9]
    run['equity'][0]['exposure'] = .2
    run['equity'][1]['exposure'] = .5
    run['equity'][2]['exposure'] = .99
    result = benchmark_and_uncertainty(run, panel, repetitions=50)
    assert result['total_return'] == pytest.approx(.089)
    assert result['market_cash_return'] == pytest.approx(1.02 * .95 - 1)
    assert result['block_bootstrap']['20']['reason'] == 'insufficient_sessions'
    run['equity'][2]['exposure'] = 0
    assert benchmark_and_uncertainty(run, panel, repetitions=50)['market_cash_return'] == result['market_cash_return']


@pytest.mark.parametrize('invalid', ['missing', 'zero', 'infinite', 'interpolated'])
def test_benchmark_missing_invalid_or_interpolated_prices_are_never_filled(invalid):
    run, panel = benchmark_fixture()
    if invalid == 'missing':
        panel = panel.iloc[[0, 2]]
    elif invalid == 'zero':
        panel.loc[1, 'tr_close'] = 0
    elif invalid == 'infinite':
        panel.loc[1, 'adj_open'] = np.inf
    else:
        panel['interpolated'] = [False, True, False]
    result = benchmark_and_uncertainty(run, panel)
    assert result['status'] == 'missing_spy'
    assert result['total_return'] is None and result['invalid_sessions'] == 1


def test_block_bootstrap_is_deterministic_and_reports_constant_excess_correctly():
    run, panel = benchmark_fixture(160)
    one = benchmark_and_uncertainty(run, panel, repetitions=123, seed=7, blocks=(20,))
    two = benchmark_and_uncertainty(run, panel, repetitions=123, seed=7, blocks=(20,))
    assert one == two
    assert set(one['block_bootstrap']) == {'20'}
    interval = one['block_bootstrap']['20']
    assert interval['repetitions'] == 123
    for field in ('mean_daily_excess', 'lower_95', 'upper_95'):
        assert interval[field] == pytest.approx(.001)


@pytest.mark.parametrize('arguments', [{'repetitions': 0}, {'repetitions': -1}, {'blocks': [0]}, {'blocks': []}])
def test_invalid_bootstrap_configuration_fails_explicitly(arguments):
    run, panel = benchmark_fixture(160)
    with pytest.raises(ValueError, match='Bootstrap'):
        benchmark_and_uncertainty(run, panel, **arguments)


def test_signal_labels_start_after_signal_close_and_cannot_cross_split_end():
    days = xcals.get_calendar('XNYS').sessions_in_range('2023-01-03', '2023-01-31').strftime('%Y-%m-%d')[:10]
    records, scores = [], []
    for i, day in enumerate(days):
        for security in range(3):
            price = 100 * (1.01 + security * .01) ** i
            records.append({'session': day, 'security_id': str(security), 'adj_open': price, 'tr_close': price * 1.002})
            scores.append({'session': day, 'security_id': str(security), 'scorable': True,
                           'raw_score': 3-security, 'industry_score': 3-security,
                           'candidate_e00': security == 0, 'candidate_e01': security == 0})
    panel, scores = pd.DataFrame(records), pd.DataFrame(scores)
    result = signal_diagnostics(panel, scores, days[0], days[7])
    for experiment in ('E00', 'E01'):
        data = result[experiment]
        assert data['top_decile_decay_gross']['1'] == pytest.approx(.01)
        assert data['top_decile_decay_gross']['5'] == pytest.approx(1.01 ** 5 - 1)
        assert data['signal_close_to_next_open'] == pytest.approx(1.01 / 1.002 - 1)
        assert data['deciles'][0]['observations_5d'] == 2
        assert np.isnan(data['top_decile_decay_gross']['10'])
        assert data['open_to_0935'] is None
    panel.loc[panel.session > days[7], ['adj_open', 'tr_close']] *= 100
    changed = signal_diagnostics(panel, scores, days[0], days[7])
    assert changed['E01']['top_decile_decay_gross']['5'] == result['E01']['top_decile_decay_gross']['5']


def runner_module():
    path = Path(__file__).resolve().parents[2] / 'scripts/run_reversal_research.py'
    spec = importlib.util.spec_from_file_location('reversal_runner_for_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signal_diagnostics_allows_weekend_year_end_without_extending_labels():
    days = xcals.get_calendar('XNYS').sessions_in_range('2023-12-18', '2023-12-29').strftime('%Y-%m-%d')
    panel = pd.DataFrame({'session': days, 'security_id': 'a',
                          'adj_open': 100 * 1.01 ** np.arange(len(days)),
                          'tr_close': 100 * 1.01 ** np.arange(len(days))})
    scores = pd.DataFrame({'session': days, 'security_id': 'a', 'scorable': True,
                           'raw_score': 1., 'industry_score': 1.,
                           'candidate_e00': True, 'candidate_e01': True})
    friday = signal_diagnostics(panel, scores, '2023-12-18', '2023-12-29')
    sunday = signal_diagnostics(panel, scores, '2023-12-18', '2023-12-31')
    for experiment in ['E00', 'E01']:
        assert sunday[experiment]['top_decile_decay_gross']['5'] == friday[experiment]['top_decile_decay_gross']['5']
        assert sunday[experiment]['deciles'][0]['observations_5d'] == friday[experiment]['deciles'][0]['observations_5d']


def test_source_claim_requires_actual_history_overlap_and_quarantine_applies_to_each_partition():
    runner = runner_module()
    assert runner.dual_source_metadata(None)['status'] == 'unavailable'
    evidence = {'calls': [{'label': 'history', 'tool': 'get_equity_quotes', 'status': 'passed'}],
                'comparison': {'history': {'AAPL': {'valid_overlap': 10}}}}
    assert runner.dual_source_metadata(evidence)['status'] == 'unavailable'
    evidence['calls'][0]['tool'] = 'get_equity_historicals'
    result = runner.dual_source_metadata(evidence)
    assert result['status'] == 'sampled_history_compared' and result['sampled_symbols'] == ['AAPL']
    panel = pd.DataFrame({'security_id': ['cancelled', 'continuing']})
    for _ in ('development', 'reserved'):
        accepted = runner.apply_identity_quarantine(panel, {'excluded_security_ids': ['cancelled']})
        assert accepted.security_id.tolist() == ['continuing']


def test_public_copy_omits_full_ledger_and_internal_result_paths():
    runner = runner_module()
    summary = {'status': 'exploratory', 'data': {'source': 'Alpaca', 'internal_account': 'not public'},
               'runs': [{'experiment': 'E01', 'cost_bps': 10, 'result_file': 'private.json.gz',
                         'metrics': {'total_return': .02, 'cagr': .01, 'trades': ['not public']}}],
               'benchmark': {'total_return': .1, 'curve': ['not public']}, 'account': 'not public'}
    public = runner.public_summary(summary)
    assert 'account' not in public and 'result_file' not in public['runs'][0]
    assert 'trades' not in public['runs'][0]['metrics'] and 'curve' not in public['benchmark']
    assert public['data'] == {'source': 'Alpaca'}
