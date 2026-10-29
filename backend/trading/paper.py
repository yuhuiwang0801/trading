"""Internal broker contract and deterministic, durable paper execution.

The paper broker uses a later quote, conservative price/slippage, and a documented
per-quote liquidity cap. It never treats submission or a touched bar as a fill.
"""
from typing import Protocol, runtime_checkable
from decimal import Decimal, ROUND_DOWN
from sqlalchemy import select
from .models import Account, Position, BrokerOrder, Fill, OrderIntent
from .strategy import D
from .db import utc, stamp
from .risk import validate_quote

ACTIVE = {'PENDING_SUBMIT','SUBMITTING','PENDING_APPROVAL','UNKNOWN','ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING'}
TERMINAL = {'FILLED','CANCELLED','REJECTED','EXPIRED'}

@runtime_checkable
class BrokerAdapter(Protocol):
    mode: str
    capabilities: dict
    def get_accounts(self) -> list[dict]: ...
    def get_account(self, account_id: str) -> dict: ...
    def get_positions(self, account_id: str) -> list[dict]: ...
    def get_orders(self, account_id: str) -> list[dict]: ...
    def get_fills(self, account_id: str) -> list[dict]: ...
    def get_tradability(self, symbol: str) -> dict: ...
    def preflight(self, intent: dict) -> dict: ...
    def submit_order(self, intent: dict) -> dict: ...
    def cancel_order(self, broker_order_id: str) -> dict: ...
    def find_order(self, client_order_id: str) -> dict | None: ...

def row_dict(row):
    return {c.name: str(getattr(row,c.name)) if isinstance(getattr(row,c.name),Decimal) else getattr(row,c.name) for c in row.__table__.columns}

class PaperBroker:
    mode = 'paper'
    capabilities = {'client_order_id':True,'fractional':False,'approval':False,'order_push':False,'replace':False,'native_protective_orders':False,'settlement':'simulated T+1 exchange session'}
    def __init__(self, session_factory):
        self.Session = session_factory
    def get_accounts(self):
        return [{'id':'paper-usd','currency':'USD','readable':True,'tradable':True,'mode':'paper'}]
    def get_account(self, account_id):
        with self.Session() as s:
            account=s.get(Account,account_id)
            if not account: raise ValueError('Account not found')
            reserved=sum((i.reserved_cash for i in s.scalars(select(OrderIntent).where(OrderIntent.account_id==account_id,OrderIntent.status.in_(ACTIVE)))),Decimal(0))
            return {**row_dict(account),'reserved_cash':str(reserved),
                    'buying_power':str(max(Decimal(0),account.settled_cash-reserved)),
                    'buying_power_includes_reservations':True,'margin_enabled':False,
                    'restrictions':['Unsettled simulated sale proceeds unavailable until T+1']}
    def get_positions(self, account_id):
        with self.Session() as s:
            return [row_dict(x) for x in s.scalars(select(Position).where(Position.account_id==account_id)) if x.quantity>0]
    def get_orders(self, account_id):
        with self.Session() as s:
            return [row_dict(x) for x in s.scalars(select(BrokerOrder).where(BrokerOrder.account_id==account_id))]
    def get_fills(self, account_id):
        with self.Session() as s:
            return [row_dict(x) for x in s.scalars(select(Fill).where(Fill.account_id==account_id))]
    def get_tradability(self,symbol):
        return {'symbol':symbol,'tradable':True,'fractionable':False,'source':'paper_simulation'}
    def preflight(self,intent):
        if intent.get('mode')!='paper' or intent.get('account_id')!='paper-usd' or D(intent['quantity'])<=0 or D(intent['limit_price'])<=0:
            return {'allowed':False,'reason':'PAPER_ACCOUNT_SCOPE_OR_INVALID_ORDER'}
        with self.Session() as s:
            active=list(s.scalars(select(OrderIntent).where(OrderIntent.account_id=='paper-usd',OrderIntent.status.in_(ACTIVE))))
            if intent['side']=='BUY':
                account=s.get(Account,'paper-usd')
                if sum((i.reserved_cash for i in active),Decimal(0))>account.settled_cash:
                    return {'allowed':False,'reason':'LATEST_BUYING_POWER_INSUFFICIENT'}
            elif intent['side']=='SELL':
                position=s.scalar(select(Position).where(Position.account_id=='paper-usd',Position.symbol==intent['symbol']))
                held=position.quantity if position else Decimal(0)
                if sum((i.reserved_quantity for i in active if i.symbol==intent['symbol']),Decimal(0))>held:
                    return {'allowed':False,'reason':'LATEST_SELLABLE_QUANTITY_INSUFFICIENT'}
            else:return {'allowed':False,'reason':'INVALID_SIDE'}
        return {'allowed':True,'reason':'Paper account scope, settled cash and sellable quantity verified'}
    def submit_order(self,intent):
        if not self.preflight(intent)['allowed']:
            return {'status':'REJECTED','message':'Paper adapter refuses non-paper account or invalid order'}
        return {'id':'paper-'+intent['id'],'status':'ACCEPTED'}
    def cancel_order(self,broker_order_id):
        # Confirmation is deliberately deferred to the next synchronization cycle;
        # a subsequent market event may still fill before cancel is acknowledged.
        return {'id':broker_order_id,'status':'CANCEL_PENDING'}
    def find_order(self,client_order_id):
        with self.Session() as s:
            row = s.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id==client_order_id))
            if row and row.status not in {'SUBMITTING','UNKNOWN','PENDING_SUBMIT'} and row.broker_id:
                return {'id':row.broker_id,'status':row.status,'filled_quantity':str(row.filled_quantity)}
            return None
    def match(self,order,quote,now,params):
        if order.status not in {'ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING'}:
            return None
        if validate_quote(quote,now,params):
            return None
        quote_at=stamp(quote['timestamp'])
        if quote.get('volume_timestamp') and utc(quote['volume_timestamp'])>utc(quote_at):
            return None
        if utc(quote_at)<=utc(order.submitted_at) or (order.last_match_at and utc(quote_at)<=utc(order.last_match_at)):
            return None
        if order.cancel_requested_at and utc(quote_at)>utc(order.cancel_requested_at):
            return None
        if utc(quote_at)>=utc(order.expires_at):
            return None
        spread_price=D(quote['ask'] if order.side=='BUY' else quote['bid'])
        slip=D(params['slippage_bps'])/10000
        price=spread_price*(1+slip if order.side=='BUY' else 1-slip)
        if (order.side=='BUY' and price>order.limit_price) or (order.side=='SELL' and price<order.limit_price):
            return None
        liquidity=max(Decimal(0),D(quote.get('volume',0))*D(params['participation_rate'])-D(quote.get('_paper_liquidity_consumed',0)))
        quantity=min(order.quantity-order.filled_quantity,liquidity.to_integral_value(rounding=ROUND_DOWN))
        if quantity<=0:
            return None
        liquidity_key=stamp(quote.get('volume_timestamp',quote['timestamp']))
        return {'broker_fill_id':order.id+':'+quote_at+'|liquidity:'+liquidity_key,'order_id':order.id,'quantity':quantity,'price':price,'fee':quantity*price*D(params['fee_bps'])/10000,'timestamp':quote_at}
