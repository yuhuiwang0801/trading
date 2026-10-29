"""Predeclared V2 entry screening and causal peer residuals.

Passing this screen means no detected risk in the covered data. It does not
establish that a price move is irrational. No parameter is selected by returns.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import exchange_calendars as xcals


def boolean(series):
    return series.map(lambda value: str(value).lower() in {'true', '1', '1.0'} if pd.notna(value) else False)


def screen_fundamentals(features: pd.DataFrame, rules: dict) -> pd.DataFrame:
    required = {'session', 'security_id', 'fundamental_status', 'filing_history_complete', 'events_known',
                'revenue_yoy', 'net_income', 'net_income_yoy',
                'earnings_age_sessions', 'risk_event_age_sessions'}
    if not required <= set(features):
        raise ValueError(f'Missing point-in-time fields: {sorted(required - set(features))}')
    f = features.copy()
    if f.duplicated(['session', 'security_id']).any():
        raise ValueError('Duplicate fundamental feature key')
    for name in ['revenue_yoy', 'net_income', 'net_income_yoy', 'earnings_age_sessions', 'risk_event_age_sessions']:
        f[name] = pd.to_numeric(f[name], errors='coerce')
    numbers = f[['revenue_yoy', 'net_income', 'net_income_yoy']]
    known = f.fundamental_status.eq('known') & numbers.notna().all(axis=1) & np.isfinite(numbers).all(axis=1)
    history = boolean(f.filing_history_complete) & boolean(f.events_known)
    f['financial_known'] = known
    f['events_known'] = history
    f['revenue_deterioration'] = known & f.revenue_yoy.lt(rules['minimum_revenue_yoy'])
    f['income_deterioration'] = known & f.net_income_yoy.lt(rules['minimum_net_income_yoy'])
    f['loss_making'] = known & f.net_income.le(0) & rules['require_positive_net_income']
    f['recent_risk_event'] = f.risk_event_age_sessions.ge(0) & f.risk_event_age_sessions.lt(rules['risk_event_cooldown_sessions'])
    f['earnings_cooldown'] = f.earnings_age_sessions.ge(0) & f.earnings_age_sessions.lt(rules['earnings_cooldown_sessions'])
    # Known bad news remains bad even when coverage for other filings is missing.
    f['fundamental_block'] = f[['revenue_deterioration', 'income_deterioration', 'loss_making', 'recent_risk_event']].any(axis=1)
    f['fundamental_known'] = known | f.recent_risk_event
    f['screen_pass'] = known & history & ~f.fundamental_block & ~f.earnings_cooldown
    f['risk_class'] = np.select([f.fundamental_block, ~known | ~history, f.earnings_cooldown],
                               ['disclosed_risk', 'unknown', 'earnings_cooldown'], default='no_detected_risk')
    return f


def peer_residuals(panel: pd.DataFrame, min_other_peers: int = 5) -> pd.DataFrame:
    """Daily stock-minus-self-excluded-peer log return on the full calendar.

    Membership must already be effective on each date, from prepare_signals.
    No missing price or return is filled; holding exits need a complete path.
    """
    p = panel.copy()
    p['session'] = pd.to_datetime(p.session).dt.normalize()
    if p.duplicated(['session', 'security_id']).any():
        raise ValueError('Duplicate residual panel key')
    cal = xcals.get_calendar('XNYS', start=p.session.min(), end=p.session.max())
    dates = cal.sessions_in_range(p.session.min(), p.session.max()).tz_localize(None)
    ids = sorted(p.security_id.unique())
    def wide(name):
        return p.pivot(index='session', columns='security_id', values=name).reindex(index=dates, columns=ids)
    price, volume, tr, member = wide('close'), wide('volume'), wide('tr_close'), wide('eligible').fillna(False).astype(bool)
    valid = price.gt(0) & volume.gt(0) & tr.gt(0) & np.isfinite(price) & np.isfinite(tr)
    if 'interpolated' in p:
        valid &= ~wide('interpolated').fillna(False).astype(bool)
    tr = tr.where(valid)
    daily = tr.div(tr.shift(1)).sub(1)
    industries = p.groupby('security_id').industry.first().reindex(ids).fillna('').astype(str)
    residual = pd.DataFrame(np.nan, index=dates, columns=ids)
    for industry in sorted(set(industries) - {''}):
        cols = industries[industries.eq(industry)].index
        returns = daily.loc[:, cols].where(member.loc[:, cols])
        total, count = returns.sum(axis=1), returns.count(axis=1)
        for sid in cols:
            n = count - returns[sid].notna().astype(int)
            peer = (total - returns[sid].fillna(0)).div(n.where(n >= min_other_peers))
            residual[sid] = np.log1p(daily[sid]) - np.log1p(peer)
    residual.index.name = 'session'
    residual.columns.name = 'security_id'
    result = residual.stack(future_stack=True).rename('residual_log_return').reset_index()
    result['session'] = result.session.dt.strftime('%Y-%m-%d')
    return result


def prepare_refinement(panel, scores, fundamentals, rules, min_other_peers=5):
    f = screen_fundamentals(fundamentals, rules)
    features = peer_residuals(panel, min_other_peers).merge(
        f[['session', 'security_id', 'fundamental_block', 'fundamental_known']],
        on=['session', 'security_id'], how='left', validate='one_to_one')
    features[['fundamental_block', 'fundamental_known']] = features[['fundamental_block', 'fundamental_known']].fillna(False).astype(bool)
    # Keep original top decile and rank; screening never backfills worse ranks.
    s = scores.merge(f.drop(columns=['symbol', 'issuer_id'], errors='ignore'),
                     on=['session', 'security_id'], how='left', validate='one_to_one')
    s['entry_gap'] = np.log1p(s.peer_return) - np.log1p(s.raw_return)
    s['base_candidate'] = boolean(s.candidate_e01)
    s['negative_stock_return'] = s.raw_return.lt(0) if rules['require_negative_stock_return'] else True
    s['sufficient_gap'] = s.entry_gap.ge(rules['minimum_relative_log_gap'])
    s['candidate_v2'] = (s.base_candidate & s.negative_stock_return & s.sufficient_gap & boolean(s.screen_pass))
    s['entry_rejection_reason'] = np.select([
        ~s.base_candidate, ~s.negative_stock_return, ~s.sufficient_gap,
        s.risk_class.fillna('unknown').eq('unknown'),
        s.risk_class.eq('disclosed_risk'), s.risk_class.eq('earnings_cooldown')],
        ['outside_original_top_decile', 'stock_not_down', 'relative_gap_below_threshold',
         'fundamental_unknown', 'disclosed_fundamental_risk', 'earnings_cooldown'], default='passed')
    return s, features


def refined_candidates(scores, filtered=False):
    rows = scores.loc[scores.candidate_v2 if filtered else scores.base_candidate].copy()
    rows = rows.rename(columns={'session': 'signal_session', 'industry_score': 'score'})
    return rows[['signal_session', 'security_id', 'score', 'entry_gap']].sort_values(
        ['signal_session', 'score', 'security_id'], ascending=[True, False, True])
