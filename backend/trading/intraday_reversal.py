"""Pure V2 intraday exits; call only after independent executable-quote checks.

The daily signal/return path is immutable. Each intraday relative observation is
rebuilt from that path and the current frozen peer basket, never accumulated
between polls. These quote-based rules are NOT the historical daily backtest.
"""
from copy import deepcopy
import json
from pathlib import Path

from .calendar import as_utc, iso, market_state, previous_session
from .strategy import D

CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config/reversal_intraday_v1.json'


def rules():
    cfg = json.loads(CONFIG_PATH.read_text())
    if cfg.get('version') != 'v2_intraday_exit_v1' or type(cfg.get('enabled')) is not bool:
        raise ValueError('Invalid intraday policy')
    for key in ('absolute_stop_loss', 'rebound_fraction', 'trailing_activation_pct', 'trailing_drawdown_pct'):
        if isinstance(cfg.get(key), bool) or not 0 < D(cfg[key]) < 1:
            raise ValueError('Invalid intraday threshold')
    if not 0 < D(cfg['relative_stop_multiple']) <= 10:
        raise ValueError('Invalid intraday relative stop')
    if D(cfg['trailing_drawdown_pct']) >= D(cfg['trailing_activation_pct']):
        raise ValueError('Trailing drawdown must be smaller than activation')
    if type(cfg.get('quote_max_age_seconds')) is not int or not 0 < cfg['quote_max_age_seconds'] <= 30:
        raise ValueError('Invalid intraday freshness bound')
    return cfg


def update_position(position, packet, frame, now, *, confirmed_bid):
    """Observe a dual-source-verified bid, then latch the first valid exit.

    Missing peers disable relative exits only. Missing corporate-action/price
    basis evidence disables all NEW price triggers. A previously verified exit
    remains pending. The execution layer must revalidate before each submission.
    """
    pos = deepcopy(position)
    cfg = rules()
    if not cfg['enabled']:
        pos['intraday_status'] = 'disabled'
        return pos
    if pos.get('exit_latched'):
        pos['intraday_status'] = 'exit_pending'
        return pos
    clock = as_utc(now)
    calendar = market_state(clock)
    day = calendar['session_date']

    def blocked(reason):
        pos.update(intraday_status='blocked', intraday_reason=reason)
        return pos

    if not calendar['is_open']:
        return blocked('outside_regular_session')
    if pos.get('strategy_id') != 'reversal_v2' or pos.get('entry_session', day) > day:
        return blocked('position_identity_invalid')
    context = (frame.get('intraday_context') or {}).get(pos['symbol'], {})
    try:
        if context.get('session') != day or not 0 <= (clock - as_utc(context['as_of'])).total_seconds() <= cfg['quote_max_age_seconds']:
            return blocked('intraday_context_stale')
        if context.get('price_basis_valid') is not True or context.get('corporate_actions_checked') is not True:
            return blocked(context.get('price_reason') or context.get('reason') or 'price_basis_unavailable')
        bid, cost, quantity = D(confirmed_bid), D(pos['avg_price']), D(pos['quantity'])
        if isinstance(confirmed_bid, bool) or min(bid, cost, quantity) <= 0:
            return blocked('position_price_invalid')
        if clock < as_utc(pos['opened_at']):
            return blocked('quote_precedes_position')
        if pos.get('intraday_last_checked_at') and clock < as_utc(pos['intraday_last_checked_at']):
            return blocked('out_of_order_observation')
        peak = max(cost, bid, D(pos.get('intraday_peak_bid', cost)))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return blocked('intraday_context_invalid')

    price_return = bid / cost - 1
    trailing_active = bool(pos.get('intraday_trailing_active')) or peak / cost - 1 >= D(cfg['trailing_activation_pct'])
    trailing_level = peak * (1 - D(cfg['trailing_drawdown_pct'])) if trailing_active else None
    relative = None
    relative_reason = context.get('reason') or 'relative_context_unavailable'
    # The daily replay must reach the exact previous close. A future/old packet
    # cannot silently move the anchor, even if quotes themselves are current.
    previous = previous_session(day)
    try:
        relative_quotes_fresh = 0 <= (clock - as_utc(context['oldest_quote_at'])).total_seconds() <= cfg['quote_max_age_seconds']
    except (KeyError, TypeError, ValueError, ArithmeticError):
        relative_quotes_fresh = False
    if (context.get('relative_eligible') is True and context.get('reference_session') == previous
            and packet.get('signal_session') == previous and pos.get('last_daily_session') == previous
            and pos.get('relative_path_complete') is True and relative_quotes_fresh):
        try:
            reference, peer, gap = D(context['stock_previous_close']), D(context['peer_return']), D(pos['entry_gap'])
            if reference <= 0 or peer <= -1 or gap <= 0:
                raise ValueError('Invalid relative basis')
            relative = D(pos['cumulative_relative_log_return']) + (bid / reference).ln() - (1 + peer).ln()
            relative_reason = None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            relative_reason = 'relative_basis_invalid'
    elif context.get('relative_eligible') is True:
        relative_reason = 'daily_relative_path_incomplete' if relative_quotes_fresh else 'relative_quotes_stale_at_decision'

    evidence = {'version': cfg['version'], 'observed_at': iso(clock), 'session': day,
                'reference_session': context.get('reference_session'), 'confirmed_bid': str(bid),
                'oldest_quote_at': context.get('oldest_quote_at'),
                'cost_basis': str(cost), 'price_return': str(price_return), 'peak_bid': str(peak),
                'trailing_active': trailing_active,
                'trailing_level': str(trailing_level) if trailing_level is not None else None,
                'relative_log_return': str(relative) if relative is not None else None,
                'relative_blocker': relative_reason, 'peer_count': context.get('peer_count', 0),
                'price_basis': 'verified_fill_cost_and_current_bid_not_total_return',
                'corporate_action_limitations': context.get('corporate_action_limitations')}
    reason = None
    if price_return <= -D(cfg['absolute_stop_loss']):
        reason = 'INTRADAY_ABSOLUTE_STOP'
    elif relative is not None and relative <= -D(pos['entry_gap']) * D(cfg['relative_stop_multiple']):
        reason = 'INTRADAY_RELATIVE_STOP'
    elif trailing_active and bid <= trailing_level:
        reason = 'INTRADAY_TRAILING_STOP'
    elif relative is not None and relative >= D(pos['entry_gap']) * D(cfg['rebound_fraction']):
        reason = 'INTRADAY_REBOUND_TARGET'
    pos.update(intraday_status='triggered' if reason else 'watching', intraday_reason=reason or relative_reason,
               intraday_peak_bid=str(peak), intraday_trailing_active=trailing_active,
               intraday_last_checked_at=iso(clock), intraday_evidence=evidence, last_price=str(bid))
    if reason:
        pos.update(exit_latched=True, exit_reason=reason, exit_signal_session=day,
                   exit_due_session=day, exit_triggered_at=iso(clock),
                   exit_signal_total_return=None,  # Intraday cash-price P&L is not total return.
                   exit_signal_price_return=str(price_return),
                   exit_signal_relative_log_return=str(relative) if relative is not None else None)
    return pos
