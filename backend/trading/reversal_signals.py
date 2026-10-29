"""Causal E00/E01 signals for an explicitly labelled daily research proxy.

Monthly membership is published after month end, for the next month. All
calculations use a complete XNYS session index; missing bars are never filled.
The caller is responsible for supplying honest security/industry provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import exchange_calendars as xcals
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalConfig:
    formation_sessions: int = 5
    universe_size: int = 500
    min_history_sessions: int = 252
    liquidity_sessions: int = 60
    min_raw_price: float = 5.0
    min_other_peers: int = 5
    candidate_fraction: float = 0.1


def prepare_signals(panel: pd.DataFrame, config: SignalConfig | None = None):
    """Return (panel with effective eligibility, all daily scores, coverage).

    ``industry`` must be stable in this proxy. A changing classification requires
    an effective-dated implementation and is rejected rather than backfilled.
    SPY rows are retained for valuation but never enter the stock universe.
    """
    c = config or SignalConfig()
    required = {'session', 'symbol', 'security_id', 'issuer_id', 'industry',
                'open', 'close', 'volume', 'tr_close', 'adj_open'}
    if not required.issubset(panel):
        raise ValueError(f'Missing columns: {sorted(required - set(panel))}')
    p = panel.copy()
    p['session'] = pd.to_datetime(p['session']).dt.normalize()
    if p.duplicated(['session', 'security_id']).any():
        raise ValueError('Duplicate session/security identity')
    if p[['security_id', 'issuer_id']].isna().any().any():
        raise ValueError('Missing permanent security or issuer identity')
    for field in ('industry', 'issuer_id'):
        if (p.groupby('security_id')[field].nunique() > 1).any():
            raise ValueError(f'Changing {field} requires point-in-time implementation')
    start, end = p.session.min(), p.session.max()
    cal = xcals.get_calendar('XNYS', start=start, end=end)
    sessions = cal.sessions_in_range(start, end).tz_localize(None)
    if not p.session.isin(sessions).all():
        raise ValueError('Non-session row in daily input')
    ids = sorted(p.security_id.unique())
    def wide(field):
        return p.pivot(index='session', columns='security_id', values=field).reindex(index=sessions, columns=ids)
    raw, volume, tr = wide('close'), wide('volume'), wide('tr_close')
    valid = (raw > 0) & (volume > 0) & (tr > 0) & np.isfinite(raw) & np.isfinite(tr)
    if 'interpolated' in p:
        valid &= ~wide('interpolated').fillna(False).astype(bool)
    tr = tr.where(valid)
    meta = p.sort_values('session').groupby('security_id').first()
    industries = meta.industry.reindex(ids).fillna('').astype(str)
    ordinary = meta.symbol.reindex(ids).ne('SPY') & industries.ne('')
    if 'instrument_type' in meta:
        ordinary &= meta.instrument_type.reindex(ids).eq('common_stock')
    liquidity = (raw * volume).where(valid).rolling(c.liquidity_sessions, min_periods=c.liquidity_sessions).median()
    observed_history = valid.cumsum()
    membership = pd.DataFrame(False, index=sessions, columns=ids)
    active: set[str] = set()
    for i, day in enumerate(sessions):
        if active:
            membership.loc[day, list(active)] = True
        next_day = sessions[i + 1] if i + 1 < len(sessions) else None
        if next_day is not None and next_day.month != day.month:
            mask = (ordinary & valid.loc[day] & raw.loc[day].ge(c.min_raw_price)
                    & observed_history.loc[day].ge(c.min_history_sessions) & liquidity.loc[day].notna())
            ranking = pd.DataFrame({'liquidity': liquidity.loc[day, mask]})
            ranking['security_id'] = ranking.index
            ranking.index.name = None
            ranking = ranking.sort_values(['liquidity', 'security_id'], ascending=[False, True])
            active = set(ranking.head(c.universe_size).security_id)

    daily = tr.div(tr.shift(1)).sub(1)
    own_return = tr.div(tr.shift(c.formation_sessions)).sub(1)
    own_valid = valid.rolling(c.formation_sessions + 1).sum().eq(c.formation_sessions + 1)
    peer_daily = pd.DataFrame(np.nan, index=sessions, columns=ids)
    peer_count = pd.DataFrame(0, index=sessions, columns=ids)
    # Each day's comparison set uses THAT day's effective membership. Removing
    # the stock itself depends on whether it was a valid member on that day.
    for industry in sorted(set(industries) - {''}):
        cols = industries[industries.eq(industry)].index
        eligible_returns = daily.loc[:, cols].where(membership.loc[:, cols])
        total, count = eligible_returns.sum(axis=1), eligible_returns.count(axis=1)
        for sid in cols:
            self_valid = eligible_returns[sid].notna()
            n = count - self_valid.astype(int)
            mean = (total - eligible_returns[sid].fillna(0)).div(n.where(n > 0))
            peer_daily[sid] = mean.where(n >= c.min_other_peers)
            peer_count[sid] = n
    peer_return = (1 + peer_daily).rolling(c.formation_sessions, min_periods=c.formation_sessions).apply(np.prod, raw=True) - 1
    common_valid = membership & own_valid & own_return.notna() & peer_return.notna()
    all_scores, coverage = [], []
    for day in sessions:
        members = list(membership.columns[membership.loc[day]])
        scored = list(common_valid.columns[common_valid.loc[day]])
        rows = pd.DataFrame(index=scored)
        rows['security_id'] = rows.index
        rows['raw_score'] = -own_return.loc[day, scored]
        rows['industry_score'] = -(own_return.loc[day, scored] - peer_return.loc[day, scored])
        for label, field in [('e00', 'raw_score'), ('e01', 'industry_score')]:
            order = rows.sort_values([field, 'security_id'], ascending=[False, True]).index
            rows[f'rank_{label}'] = pd.Series(range(1, len(order) + 1), index=order)
            k = math.ceil(c.candidate_fraction * len(rows))
            rows[f'candidate_{label}'] = rows[f'rank_{label}'].le(k) & rows[field].gt(0)
        for sid in members:
            row = {'session': day.date().isoformat(), 'security_id': sid,
                   'symbol': meta.loc[sid, 'symbol'], 'issuer_id': meta.loc[sid, 'issuer_id'],
                   'industry': industries[sid], 'scorable': sid in rows.index,
                   'raw_return': float(own_return.loc[day, sid]),
                   'peer_return': float(peer_return.loc[day, sid]),
                   'min_peer_count': int(peer_count[sid].loc[:day].tail(c.formation_sessions).min())}
            if sid in rows.index:
                row.update(rows.loc[sid].drop('security_id').to_dict())
                row['exclusion_reason'] = None
            else:
                row['exclusion_reason'] = 'missing_formation_bars' if not own_valid.loc[day, sid] else 'fewer_than_5_valid_peers_or_missing_returns'
            all_scores.append(row)
        coverage.append({'session': day.date().isoformat(), 'universe_count': len(members),
                         'scorable_count': len(scored), 'unscorable_count': len(members) - len(scored),
                         'e00_candidate_count': int(rows.candidate_e00.sum()),
                         'e01_candidate_count': int(rows.candidate_e01.sum())})
    eligibility = membership.stack().rename('eligible')
    eligibility.index.names = ['session', 'security_id']
    p = p.drop(columns=['eligible'], errors='ignore').merge(eligibility.reset_index(), on=['session', 'security_id'], how='left')
    p['session'] = p.session.dt.strftime('%Y-%m-%d')
    scores = pd.DataFrame(all_scores)
    return p, scores, pd.DataFrame(coverage)


def candidates_for(scores: pd.DataFrame, experiment: str) -> pd.DataFrame:
    if experiment not in {'E00', 'E01'}:
        raise ValueError('Only preregistered E00/E01 are supported')
    field = 'raw_score' if experiment == 'E00' else 'industry_score'
    if scores.empty:
        return pd.DataFrame(columns=['signal_session', 'security_id', 'score'])
    selected = scores[scores[f'candidate_{experiment.lower()}'].fillna(False).astype(bool)].copy()
    selected = selected.rename(columns={'session': 'signal_session', field: 'score'})
    return selected[['signal_session', 'security_id', 'score']].sort_values(
        ['signal_session', 'score', 'security_id'], ascending=[True, False, True])
