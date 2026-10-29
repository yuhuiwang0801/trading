"""Diagnostics for daily proxy research. No diagnostic selects a new strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd


def benchmark_and_uncertainty(run, panel, repetitions=2000, seed=20261027, blocks=(10, 20, 60)):
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions <= 0:
        raise ValueError('Bootstrap repetitions must be a positive integer')
    if not blocks or any(isinstance(block, bool) or not isinstance(block, int) or block <= 0 for block in blocks):
        raise ValueError('Bootstrap blocks must be positive integer session counts')
    equity = pd.DataFrame(run['equity']).set_index('session')
    spy = panel[panel.symbol.eq('SPY')].set_index('session').reindex(equity.index)
    prices = spy[['tr_close', 'adj_open']].apply(pd.to_numeric, errors='coerce')
    invalid = prices.isna().any(axis=1) | ~np.isfinite(prices).all(axis=1) | prices.le(0).any(axis=1)
    if 'interpolated' in spy:
        invalid |= spy.interpolated.fillna(False).astype(bool)
    if invalid.any():
        return {'status': 'missing_spy', 'total_return': None,
                'reason': 'Missing, invalid or interpolated benchmark observations',
                'invalid_sessions': int(invalid.sum())}
    spy[['tr_close', 'adj_open']] = prices
    # Buy-and-hold comparison begins at the first evaluation session's open.
    spy_nav = spy.tr_close / spy.adj_open.iloc[0]
    daily_spy = spy.tr_close.pct_change(fill_method=None)
    daily_spy.iloc[0] = spy.tr_close.iloc[0] / spy.adj_open.iloc[0] - 1
    # A transparent descriptive risk comparator, not a tradable matched basket.
    # Prior-close exposure avoids using today's realized investment weight.
    market_cash_returns = daily_spy * equity.exposure.shift(1).fillna(0)
    market_cash_nav = (1 + market_cash_returns).cumprod()
    excess = equity.daily_return - market_cash_returns
    intervals = {}
    for block in blocks:
        values = excess.to_numpy(dtype=float)
        n = len(values)
        if n < 2 * block:
            intervals[str(block)] = {'reason': 'insufficient_sessions'}
            continue
        rng = np.random.default_rng(seed)
        count = int(np.ceil(n / block))
        indices = (rng.integers(0, n, size=(repetitions, count, 1)) + np.arange(block)) % n
        means = values[indices.reshape(repetitions, -1)[:, :n]].mean(axis=1)
        lo, hi = np.quantile(means, [.025, .975])
        intervals[str(block)] = {'mean_daily_excess': float(values.mean()),
                                  'lower_95': float(lo), 'upper_95': float(hi),
                                  'repetitions': repetitions}
    return {'status': 'descriptive_proxy', 'total_return': float(spy_nav.iloc[-1] - 1),
            'market_cash_return': float(market_cash_nav.iloc[-1] - 1),
            'strategy_minus_spy_percentage_points': float(equity.nav.iloc[-1] / run['config']['initial_cash'] - spy_nav.iloc[-1]),
            'mean_daily_excess': float(excess.mean()), 'block_bootstrap': intervals,
            'matched_control_status': 'unavailable_no_point_in_time_size_beta_industry_history',
            'limitations': ['SPY is a vendor-adjusted total-return proxy before benchmark trading costs.',
                            'Market+cash uses lagged closing exposure and zero cash yield; no intraday exposure or rebalancing costs.',
                            'Confidence intervals measure sampling uncertainty only, not survivorship/classification/data-model bias.'],
            'curve': [{'session': d, 'spy': float(spy_nav.loc[d]),
                       'market_cash': float(market_cash_nav.loc[d])} for d in equity.index]}


def signal_diagnostics(panel, scores, start, end):
    """Forward open-to-open labels strictly contained within a named split."""
    import exchange_calendars as xcals
    # A calendar built exactly through a weekend ends at Friday, making the
    # requested Sunday out of bounds. Padding does not extend label coverage:
    # the session range and forward-price matrix still stop at `end` below.
    cal = xcals.get_calendar('XNYS', start=panel.session.min(), end=pd.Timestamp(end) + pd.Timedelta(days=7))
    dates = cal.sessions_in_range(panel.session.min(), end).strftime('%Y-%m-%d')
    opens = panel.pivot(index='session', columns='security_id', values='adj_open').reindex(dates)
    closes = panel.pivot(index='session', columns='security_id', values='tr_close').reindex(dates)
    future = {h: opens.shift(-h - 1).div(opens.shift(-1)).sub(1) for h in [1, 5, 10, 21]}
    overnight = opens.shift(-1).div(closes).sub(1)
    s = scores[scores.scorable & scores.session.ge(start) & scores.session.le(end)].copy()
    for h, frame in future.items():
        stacked = frame.stack().rename(f'forward_{h}').reset_index()
        stacked.columns = ['session', 'security_id', f'forward_{h}']
        s = s.merge(stacked, on=['session', 'security_id'], how='left')
    stack = overnight.stack().rename('overnight').reset_index()
    stack.columns = ['session', 'security_id', 'overnight']
    s = s.merge(stack, on=['session', 'security_id'], how='left')
    result = {}
    for exp, score in [('E00', 'raw_score'), ('E01', 'industry_score')]:
        rank = s.groupby('session')[score].rank(ascending=False, method='first')
        size = s.groupby('session')[score].transform('size')
        s['decile'] = np.minimum(10, np.floor((rank - 1) * 10 / size).astype(int) + 1)
        groups = []
        for decile, rows in s.groupby('decile'):
            # Date-first aggregation gives each signal date equal weight.
            means = rows.groupby('session')[[f'forward_{h}' for h in future]].mean().mean()
            groups.append({'decile': int(decile), 'observations_5d': int(rows.forward_5.notna().sum()),
                           **{key: float(value) for key, value in means.items()}})
        top = s[s[f'candidate_{exp.lower()}'].eq(True)]
        decay = {str(h): float(top.groupby('session')[f'forward_{h}'].mean().mean()) for h in future}
        ic = []
        for _, rows in s.dropna(subset=['forward_5']).groupby('session'):
            if len(rows) > 2:
                value = rows[score].rank().corr(rows.forward_5.rank())
                if pd.notna(value):
                    ic.append(value)
        result[exp] = {'deciles': groups, 'top_decile_decay_gross': decay,
                       'mean_daily_rank_ic_5d': float(np.nanmean(ic)) if ic else None,
                       'signal_close_to_next_open': float(top.groupby('session').overnight.mean().mean()),
                       'open_to_0935': None,
                       'label': 'Idealized gross signal diagnostics; overlapping labels, no portfolio, no costs; decile 1 is highest reversal score.'}
    return result
