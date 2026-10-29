import numpy as np
import pandas as pd
import exchange_calendars as xcals

from trading.reversal_signals import SignalConfig, prepare_signals, candidates_for


def panel():
    days = xcals.get_calendar('XNYS').sessions_in_range('2023-01-03', '2023-03-15')
    rows = []
    for j in range(7):
        for i, day in enumerate(days):
            price = 100 * (1 + (j - 3) * .001) ** i
            rows.append(dict(session=day.strftime('%Y-%m-%d'), symbol=f'S{j}',
                             security_id=f'id{j}', issuer_id=f'issuer{j}', industry='35',
                             open=price, close=price, volume=100000, tr_close=price, adj_open=price))
    return pd.DataFrame(rows)


def config():
    return SignalConfig(min_history_sessions=6, liquidity_sessions=6)


def test_month_end_membership_is_not_effective_until_next_month():
    p, scores, _ = prepare_signals(panel(), config())
    assert not p.loc[p.session.le('2023-01-31'), 'eligible'].any()
    assert p.loc[p.session.eq('2023-02-01'), 'eligible'].all()
    assert scores.session.min() == '2023-02-01'


def test_industry_return_excludes_self_and_compounds_daily_peer_means():
    _, scores, _ = prepare_signals(panel(), config())
    r = scores[(scores.session == '2023-02-10') & (scores.security_id == 'id0')].iloc[0]
    peer_daily = np.mean([(j - 3) * .001 for j in range(1, 7)])
    assert np.isclose(r.peer_return, (1 + peer_daily) ** 5 - 1)
    assert np.isclose(r.raw_return, .997 ** 5 - 1)
    assert np.isclose(r.industry_score, r.peer_return - r.raw_return)
    e00 = candidates_for(scores, 'E00')
    e01 = candidates_for(scores, 'E01')
    assert e00.groupby('signal_session').size().max() == 1
    assert e01.groupby('signal_session').size().max() == 1


def test_future_prices_do_not_change_past_signals_or_membership():
    source = panel()
    p1, s1, c1 = prepare_signals(source, config())
    source.loc[source.session.gt('2023-02-15'), ['open', 'close', 'tr_close', 'adj_open']] *= 3
    p2, s2, c2 = prepare_signals(source, config())
    pd.testing.assert_frame_equal(s1[s1.session.le('2023-02-15')], s2[s2.session.le('2023-02-15')])
    pd.testing.assert_frame_equal(c1[c1.session.le('2023-02-15')], c2[c2.session.le('2023-02-15')])
    assert p1[p1.session.le('2023-02-15')].eligible.equals(p2[p2.session.le('2023-02-15')].eligible)


def test_missing_peer_bars_are_not_filled_and_exclude_both_experiments():
    source = panel()
    source = source[~((source.session == '2023-02-07') & source.security_id.isin(['id1', 'id2']))]
    _, scores, _ = prepare_signals(source, config())
    target = scores[(scores.session == '2023-02-10') & (scores.security_id == 'id0')].iloc[0]
    assert not target.scorable
    assert target.min_peer_count == 4
    for experiment in ['E00', 'E01']:
        candidates = candidates_for(scores, experiment)
        assert candidates[(candidates.signal_session == '2023-02-10') & (candidates.security_id == 'id0')].empty
