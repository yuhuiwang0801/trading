"""Platform strategy selection; selection never grants broker write authority."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_CEILING
from . import config
from .runtime import utcnow

STRATEGY_IDS = {'reversal_v2'}
RETIRED_STRATEGY_IDS = {'trend_breakout'}


def strategies():
    from .live_policy import policy_for, public_policy
    path = config.ROOT / 'config/reversal_research_v2.json'
    v2 = json.loads(path.read_text())
    items = [
        {'id': 'reversal_v2', 'name': 'V2 基本面过滤与反弹退出', 'version': v2['strategy_version'],
         'config_hash': hashlib.sha256(path.read_bytes()).hexdigest(),
         'description': '行业相对超跌、已披露基本面筛选、固定偏离修复80%退出。',
         'parameters': {key: v2[key] for key in ['formation_sessions', 'min_raw_price', 'min_history_sessions',
                        'candidate_fraction', 'max_positions', 'max_entries_per_day', 'max_security_weight',
                        'max_industry_weight', 'max_daily_buy_weight', 'entry_filters', 'dynamic_exit']},
         'execution_notice': '每日收盘后计算；下一交易日09:35–09:40 ET允许新买。退出于09:35后提交限价单，未成交继续按订单状态处理。研究日线开盘收益不等于实时成交收益。'}
    ]
    for item in items:
        item['execution_policy'] = public_policy(policy_for(item['id']))
    return items


def active_strategy(store):
    saved = store.get('active_strategy', {})
    selected = saved.get('id', 'reversal_v2')
    # Retire the old catalog selection without changing live authorization,
    # configuration, orders, or ownership. Those require a fresh V2 preparation.
    if selected in RETIRED_STRATEGY_IDS:
        selected = 'reversal_v2'
    if selected not in STRATEGY_IDS:
        raise ValueError('Unknown persisted strategy; no fallback execution is allowed')
    item = next(s for s in strategies() if s['id'] == selected)
    return {key: item[key] for key in ['id', 'name', 'version']} | {'selected_at': saved.get('selected_at')}


def runtime_view(packet):
    candidates = []
    for row in packet.get('candidates', []):
        candidates.append({key: row.get(key) for key in ['security_id', 'symbol', 'issuer_id', 'industry',
                           'score', 'entry_gap', 'raw_close', 'tr_close', 'rank', 'raw_return', 'peer_return']} |
                          {'relative_gap': row.get('entry_gap'), 'close': row.get('raw_close'),
                           'stock_return_5d': row.get('raw_return'), 'fundamental_status': 'passed'})
    return {'status': packet.get('status', 'not_ready'),
            'data_session': packet.get('data_session'), 'signal_session': packet.get('signal_session'),
            'expected_signal_session': packet.get('expected_signal_session'),
            'execute_session': packet.get('execute_session'), 'updated_at': packet.get('created_at'),
            'source': packet.get('source'), 'config_hash': packet.get('config_hash'),
            'execution_eligible': packet.get('execution_eligible', False),
            'blockers': packet.get('blocked_reasons', []), 'candidates': candidates,
            'candidate_count': len(candidates), 'universe_count': packet.get('universe_count', 190)}


def daily_data_ready(packet):
    """An elapsed entry window is normal; its verified closes still serve exits."""
    return packet.get('status') == 'ready' or (
        packet.get('status') == 'expired' and
        packet.get('blocked_reasons') == ['execution_window_expired'])


def entry_feasibility(packet, configuration, account):
    """Explain whole-share affordability using last closes, never a trade signal."""
    if (configuration.get('scope_mode') != 'strategy_managed' or
            configuration.get('strategy_id') != 'reversal_v2'):
        return None
    result = {'status': 'data_unavailable', 'basis': 'last_completed_close_proxy',
              'data_session': packet.get('data_session'), 'candidate_count': 0,
              'estimated_affordable_count': 0, 'execution_quote_required': True}
    if not daily_data_ready(packet):
        return result
    try:
        budget = max(Decimal(0), min(Decimal(str(configuration['max_order_amount'])),
                     Decimal(str(configuration['current_buy_order_ceiling'])),
                     Decimal(str((account or {})['execution_buying_power']))))
        params = configuration['params']
        fee = Decimal(str(params['fee_bps'])) / 10000
        offset = Decimal(str(params['limit_offset_bps'])) / 10000
        closes = [Decimal(str(row['raw_close'])) for row in packet.get('candidates', [])]
        if any(not close.is_finite() or close <= 0 for close in closes) or not budget.is_finite():
            return result
        affordable = sum((close * (1 + offset)).quantize(Decimal('.01'), rounding=ROUND_CEILING) *
                         (1 + fee) <= budget for close in closes)
        return {**result, 'status': ('no_candidates' if not closes else 'some_affordable_candidates'
                                    if affordable else 'no_affordable_candidates'),
                'candidate_count': len(closes), 'estimated_affordable_count': affordable,
                'max_buy_budget': str(budget), 'minimum_candidate_close': str(min(closes)) if closes else None}
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return result


def enqueue_refresh(store):
    pending = next((job for job in store.jobs('reversal_refresh') if job['status'] in {'queued', 'running'}), None)
    return pending or store.enqueue('reversal_refresh', {'strategy_id': 'reversal_v2', 'requested_at': utcnow().isoformat(),
                                                       'purpose': 'read_only_daily_data_and_signals'})


def retire_legacy_job(store, job):
    """Preserve queued legacy work as failed history without executing it."""
    if job and (job['kind'] in {'demo', 'backtest'} or
                (job['kind'] == 'scan' and job['payload'].get('source') == 'demo')):
        store.finish(job['id'], {'status': 'retired', 'error': 'STRATEGY_RETIRED',
                                'message': 'Trend strategy is retired; this queued job was not executed'}, 'failed')
        return True
    return False
