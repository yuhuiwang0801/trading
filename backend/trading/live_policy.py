"""Trusted, code-defined execution scopes. This module never contacts a broker.

Dollar amounts are a view of current account equity, not user-entered limits or
authorization to use margin. Buying-power and reservation checks remain separate.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

from . import live_reversal as reversal
from . import intraday_reversal
from .strategy import DEFAULT_PARAMS, D

ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH = ROOT / 'data/reversal/alpaca_sip_20261027_proxy200_v2/security_master.csv'
IDENTITY_PATH = ROOT / 'data/reversal/v2_identity_review.json'
MODE = 'strategy_managed'
VERSION = 'strategy_managed_scope_v2'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def vetted_master():
    raw = MASTER_PATH.read_bytes()
    review = json.loads(IDENTITY_PATH.read_text())
    if hashlib.sha256(raw).hexdigest() != review['source_master_sha256']:
        raise ValueError('V2 vetted security master changed')
    excluded = set(review['excluded_security_ids'])
    rows = [row for row in csv.DictReader(raw.decode().splitlines()) if row['security_id'] not in excluded]
    if (len(rows) != 190 or len({r['symbol'] for r in rows}) != 190 or
            len({r['security_id'] for r in rows}) != 190 or
            any(not all(r.get(k) for k in ('symbol', 'security_id', 'issuer_id', 'industry')) for r in rows)):
        raise ValueError('V2 vetted security master is invalid')
    return {r['symbol']: {k: r[k] for k in ('symbol', 'security_id', 'issuer_id', 'industry')}
            for r in sorted(rows, key=lambda row: row['symbol'])}


def policy_for(strategy_id):
    """Return the policy from trusted files/code; request bodies cannot override it."""
    params = deepcopy(DEFAULT_PARAMS)
    policy = {'version': VERSION, 'strategy_id': strategy_id, 'sizing_basis': 'current_account_equity',
              'scope_mode': MODE, 'order_type': 'whole_share_limit', 'regular_session_only': True,
              'leverage_allowed': False, 'manual_positions_managed': False,
              'execution_params': params}
    if strategy_id == reversal.STRATEGY_ID:
        cfg = reversal.rules()
        master = vetted_master()
        policy.update(strategy_version=cfg['strategy_version'], strategy_config_hash=reversal.config_hash(),
                      universe_mode='fixed_vetted_universe', universe_count=len(master),
                      symbols=list(master), security_master=master,
                      universe_hash=fingerprint({'security_master': hashlib.sha256(MASTER_PATH.read_bytes()).hexdigest(),
                                                'identity_review': hashlib.sha256(IDENTITY_PATH.read_bytes()).hexdigest()}),
                      max_order_weight=str(cfg['max_security_weight']),
                      max_daily_added_weight=str(cfg['max_daily_buy_weight']),
                      max_security_weight=str(cfg['max_security_weight']),
                      max_issuer_weight=str(cfg['max_issuer_weight']),
                      max_industry_weight=str(cfg['max_industry_weight']),
                      max_positions=cfg['max_positions'], max_entries_per_day=cfg['max_entries_per_day'],
                      intraday_exit=intraday_reversal.rules(),
                      sell_uses_buy_budget=False,
                      buy_window='09:35–09:40 America/New_York',
                      sell_window='09:30–regular close intraday; daily exits from 09:35 America/New_York')
    elif strategy_id == reversal.TREND_ID:
        policy.update(strategy_version='trend_breakout_v1', strategy_config_hash=None,
                      universe_mode='eligible_active_us_equities', universe_count=None, symbols=[],
                      universe_hash=fingerprint({'kind': 'eligible_active_us_equities', 'minimum_price': params['minimum_price'],
                                                'min_listing_sessions': params['min_listing_sessions'],
                                                'min_avg_dollar_volume': params['min_avg_dollar_volume'],
                                                'max_candidates': params['max_candidates']}),
                      max_order_weight=str(params['max_position_pct']), max_daily_added_weight='0.30',
                      max_security_weight=str(params['max_position_pct']), max_issuer_weight=None,
                      max_industry_weight=None, max_positions=params['max_positions'], max_entries_per_day=None,
                      max_order_absolute=str(params['max_order_amount']), max_daily_added_absolute=str(params['max_daily_added']),
                      max_exposure_weight=str(params['max_exposure_pct']),
                      buy_window='regular session', sell_window='regular session')
    else:
        raise ValueError('Unknown live strategy')
    policy['policy_hash'] = fingerprint(policy)
    return policy


def limits_for(policy, account):
    equity = max(D('0'), D(account.get('total_value', '0')))
    order = equity * D(policy['max_order_weight'])
    daily = equity * D(policy['max_daily_added_weight'])
    if policy.get('max_order_absolute') is not None:
        order = min(order, D(policy['max_order_absolute']))
    if policy.get('max_daily_added_absolute') is not None:
        daily = min(daily, D(policy['max_daily_added_absolute']))
    available = max(D('0'), min(D(account.get(k, '0')) for k in
                               ('cash', 'buying_power', 'unleveraged_buying_power')))
    # Floor display caps to cents so the shown ceiling never overstates capacity.
    def money(value):
        from decimal import ROUND_DOWN
        return format(value.quantize(D('.01'), rounding=ROUND_DOWN), 'f')
    return {'max_order_amount': money(order), 'max_daily_added': money(daily),
            'max_positions': int(policy['max_positions']), 'sizing_equity': money(equity),
            'available_unleveraged_cash': money(available), 'current_buy_order_ceiling': money(min(order, available))}


def managed_configuration(strategy_id, account):
    policy = policy_for(strategy_id)
    return {'strategy_id': strategy_id, 'strategy_config_hash': policy['strategy_config_hash'],
            'scope_mode': MODE, 'policy_version': policy['version'], 'policy_hash': policy['policy_hash'],
            'universe_hash': policy['universe_hash'], 'strategy_policy': policy,
            'symbols': policy['symbols'], 'params': deepcopy(policy['execution_params']),
            **limits_for(policy, account)}


def matches(config):
    if config.get('scope_mode') != MODE:
        return True
    try:
        policy = policy_for(config['strategy_id'])
        return (config.get('policy_hash') == policy['policy_hash'] and
                config.get('policy_version') == policy['version'] and
                config.get('universe_hash') == policy['universe_hash'] and
                config.get('strategy_config_hash') == policy['strategy_config_hash'] and
                config.get('strategy_policy') == policy and config['symbols'] == policy['symbols'] and
                config['params'] == policy['execution_params'] and
                config['max_positions'] == policy['max_positions'])
    except (OSError, KeyError, ValueError, TypeError):
        return False


def effective_configuration(config, account):
    if config.get('scope_mode') != MODE:
        return config
    return {**config, **limits_for(config['strategy_policy'], account)}


def public_policy(policy):
    return {key: value for key, value in policy.items() if key not in {'security_master', 'execution_params', 'symbols'}}
