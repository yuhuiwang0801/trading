"""Pure portfolio allocation shared by live monitoring and historical replay."""
from decimal import Decimal, ROUND_DOWN
from .db import utc
from .strategy import D, DEFAULT_PARAMS

def validate_quote(quote, now, params=None):
    p = {**DEFAULT_PARAMS, **(params or {})}
    if not quote:
        return 'MISSING_QUOTE'
    if quote.get('quality', 'valid') != 'valid':
        return 'INVALID_QUOTE_QUALITY'
    try:
        age = (utc(now)-utc(quote['timestamp'])).total_seconds()
        bid, ask, last = D(quote['bid']), D(quote['ask']), D(quote['last'])
        if age < 0 or age > int(p['quote_max_age_seconds']):
            return 'STALE_QUOTE'
        if bid <= 0 or ask < bid or last <= 0:
            return 'INVALID_QUOTE'
        if (ask-bid)/((ask+bid)/2) > D(p['max_spread_pct']):
            return 'SPREAD_TOO_WIDE'
        if quote.get('tradable') is False or quote.get('halted') is True:
            return 'SECURITY_NOT_TRADABLE'
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return 'INVALID_QUOTE'
    return None

def size_entry(equity, cash_available, exposure, reserved_cash, position_count, price, atr, params=None, day_added=Decimal(0), daily_pnl=Decimal(0)):
    p = {**DEFAULT_PARAMS, **(params or {})}
    equity, cash_available, exposure, reserved_cash, price, atr, day_added, daily_pnl = map(D, (equity,cash_available,exposure,reserved_cash,price,atr,day_added,daily_pnl))
    def deny(reason):
        return {'allowed': False, 'quantity': Decimal(0), 'reason': reason, 'max_cost': Decimal(0)}
    if equity <= 0 or price <= 0 or atr <= 0:
        return deny('INVALID_SIZING_INPUT')
    if position_count >= int(p['max_positions']):
        return deny('MAX_POSITIONS')
    if daily_pnl <= -equity * D(p['daily_loss_pause_pct']):
        return deny('DAILY_LOSS_LIMIT')
    fee = D(p['fee_bps'])/10000
    unit_cost = price*(1+fee)
    allowance = min(equity*D(p['max_position_pct']), D(p['max_order_amount']),
                    max(Decimal(0),equity*D(p['max_exposure_pct'])-exposure-reserved_cash),
                    max(Decimal(0),cash_available-reserved_cash),
                    max(Decimal(0),D(p['max_daily_added'])-day_added-reserved_cash))
    risk_quantity = equity*D(p['risk_per_trade_pct'])/(atr*D(p['initial_stop_atr']))
    qty = min(risk_quantity,allowance/unit_cost).to_integral_value(rounding=ROUND_DOWN)
    if qty < 1:
        return deny('INSUFFICIENT_BUDGET_OR_BUYING_POWER')
    return {'allowed': True, 'quantity': qty, 'reason': 'APPROVED', 'max_cost': qty*unit_cost}
