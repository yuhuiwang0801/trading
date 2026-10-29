"""Transactional orchestration for paper monitoring and future broker adapters.

Intent persistence precedes external submission. UNKNOWN retains reservations and
is reconciled before any further entry; a lost response is NEVER blindly retried.
"""
from __future__ import annotations
import time
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from .db import Base, database, utc, stamp
from .models import (Account, Settings, Candidate, Signal, OrderIntent, BrokerOrder,
                     Fill, Position, Settlement, Audit, Run, Lease, SchemaVersion, SessionEquity)
from .strategy import DEFAULT_PARAMS, D, entry_signal, exit_signal, closed_bars, limit_price_for
from .risk import validate_quote, size_entry
from .paper import PaperBroker, BrokerAdapter, ACTIVE, TERMINAL, row_dict

ZERO = Decimal('0')
NY = ZoneInfo('America/New_York')

def uid():
    return uuid.uuid4().hex

def jsonable(value):
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,dict): return {str(k):jsonable(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [jsonable(v) for v in value]
    if hasattr(value,'isoformat'): return value.isoformat()
    return value

class Engine:
    def __init__(self, database_url=None, broker: BrokerAdapter | None=None):
        self.db, self.Session = database(database_url)
        Base.metadata.create_all(self.db)
        with self.Session.begin() as s:
            if not s.get(Account,'paper-usd'):
                s.add(Account(id='paper-usd',cash=D('10000'),settled_cash=D('10000'),realized_pnl=ZERO,initial_equity=D('10000')))
            if not s.get(Settings,1): s.add(Settings(id=1,params=dict(DEFAULT_PARAMS)))
            if not s.get(SchemaVersion,1): s.add(SchemaVersion(version=1))
            if not s.get(Lease,'scan'): s.add(Lease(id='scan',owner='',expires_at='1970-01-01T00:00:00.000000+00:00'))
        self.broker = broker or PaperBroker(self.Session)
        self.recovered = False

    def _audit(self,s,event,detail=None,category='business',**links):
        allowed={'actor','account_id','symbol','run_id','signal_id','intent_id','order_id','version'}
        s.add(Audit(id=uid(),event=event,category=category,detail=jsonable(detail or {}),**{k:v for k,v in links.items() if k in allowed}))

    def _balance(self,s):
        account=s.get(Account,'paper-usd')
        positions=[x for x in s.scalars(select(Position).where(Position.account_id=='paper-usd')) if x.quantity>0]
        intents=list(s.scalars(select(OrderIntent).where(OrderIntent.account_id=='paper-usd',OrderIntent.status.in_(ACTIVE))))
        reserved=sum((i.reserved_cash for i in intents),ZERO)
        exposure=sum((p.quantity*p.last_price for p in positions),ZERO)
        unrealized=sum((p.quantity*(p.last_price-p.avg_price) for p in positions),ZERO)
        return {**row_dict(account),'equity':str(account.cash+exposure),'exposure':str(exposure),
                'reserved_cash':str(reserved),'buying_power':str(max(ZERO,account.settled_cash-reserved)),
                'unsettled_cash':str(account.cash-account.settled_cash),'unrealized_pnl':str(unrealized),
                'account_type':'paper_cash','settlement_model':'T+1 exchange session simulation',
                'buying_power_includes_reservations':True}

    def snapshot(self):
        with self.Session() as s:
            latest=s.scalar(select(Candidate.session_date).order_by(Candidate.session_date.desc()).limit(1))
            candidates=[{**x.data,'id':x.id,'session_date':x.session_date} for x in s.scalars(select(Candidate).where(Candidate.session_date==latest))] if latest else []
            positions=[]
            for p in s.scalars(select(Position).where(Position.account_id=='paper-usd')):
                if p.quantity<=0: continue
                reserved=sum((i.reserved_quantity for i in s.scalars(select(OrderIntent).where(OrderIntent.symbol==p.symbol,OrderIntent.side=='SELL',OrderIntent.status.in_(ACTIVE)))),ZERO)
                positions.append({**row_dict(p),'market_value':str(p.quantity*p.last_price),'unrealized_pnl':str(p.quantity*(p.last_price-p.avg_price)),
                                  'available_quantity':str(max(ZERO,p.quantity-reserved)), 'reserved_quantity':str(reserved),'protection_location':'local_service'})
            def rows(model,key,limit=250):
                return [row_dict(x) for x in s.scalars(select(model).order_by(key.desc()).limit(limit))]
            return {'account':self._balance(s),'settings':row_dict(s.get(Settings,1)),
                    'candidates':sorted(candidates,key=lambda c:int(c.get('rank',999))), 'positions':positions,
                    'signals':rows(Signal,Signal.created_at),'intents':rows(OrderIntent,OrderIntent.created_at),
                    'orders':rows(BrokerOrder,BrokerOrder.submitted_at),'fills':rows(Fill,Fill.timestamp),
                    'audit':rows(Audit,Audit.timestamp,500),'runs':rows(Run,Run.started_at,50),
                    'strategy_validation':'PENDING_VALIDATION','broker_capabilities':self.broker.capabilities}

    def set_candidates(self,candidates,session_date):
        with self.Session.begin() as s:
            self._set_candidates(s,candidates,session_date)
        return self.snapshot()['candidates']

    def _set_candidates(self,s,candidates,session_date):
        before={x.symbol:x for x in s.scalars(select(Candidate).where(Candidate.session_date==session_date))}
        previous_date=s.scalar(select(Candidate.session_date).where(Candidate.session_date<session_date).order_by(Candidate.session_date.desc()).limit(1))
        previous={x.symbol for x in s.scalars(select(Candidate).where(Candidate.session_date==previous_date))} if not before and previous_date else set(before)
        changed=False
        symbols=set()
        for item in candidates:
            item=jsonable(item); symbol=item['symbol']; symbols.add(symbol)
            if symbol in before:
                if before[symbol].data != item: before[symbol].data=item;changed=True
            else:
                s.add(Candidate(id=uid(),session_date=session_date,symbol=symbol,security_id=str(item.get('security_id',symbol)),data=item));changed=True
        for symbol in set(before)-symbols:
            s.delete(before[symbol]);changed=True
        if changed:
            self._audit(s,'CANDIDATE_POOL_UPDATED',{'session_date':session_date,'added':sorted(symbols-previous),'removed':sorted(previous-symbols),'count':len(symbols),'indicators':candidates})

    def update_settings(self,patch):
        with self.Session.begin() as s:
            settings=s.get(Settings,1); before=row_dict(settings)
            permitted={'mode','params','paused_entries','stopped'}
            unknown=set(patch)-permitted-set(DEFAULT_PARAMS)
            if unknown: raise ValueError('Unknown settings: '+','.join(sorted(unknown)))
            if 'mode' in patch:
                mode=patch['mode']
                if mode=='observation': mode='observe'
                if mode not in {'paper','observe'}: raise ValueError('Live execution unavailable: account authorization and verified MCP capabilities required')
                settings.mode=mode
            values={**patch.get('params',{}),**{k:v for k,v in patch.items() if k in DEFAULT_PARAMS}}
            unknown=set(values)-set(DEFAULT_PARAMS)
            if unknown: raise ValueError('Unknown strategy parameters: '+','.join(unknown))
            params={**settings.params,**values}
            for k,v in params.items():
                if k=='regular_session_only':
                    if v is not True: raise ValueError('Extended hours not enabled in this release')
                elif not D(v).is_finite() or D(v)<=0: raise ValueError(k+' must be finite and positive')
                elif (k.endswith('_pct') or k=='participation_rate') and D(v)>1: raise ValueError(k+' must be <= 1')
            for k in ('max_positions','max_candidates','min_listing_sessions','scan_interval_seconds','quote_max_age_seconds','bar_max_age_seconds','order_ttl_seconds','max_consecutive_failures'):
                if D(params[k]) != D(params[k]).to_integral_value(): raise ValueError(k+' must be an integer')
                params[k]=int(params[k])
            if D(params['max_exposure_pct'])>1: raise ValueError('No leverage permitted')
            settings.params=jsonable(params)
            for flag in ('paused_entries','stopped'):
                if flag in patch:
                    if type(patch[flag]) is not bool: raise ValueError(flag+' must be boolean')
                    setattr(settings,flag,patch[flag])
            settings.version+=1
            self._audit(s,'SETTINGS_CHANGED',{'before':before,'after':row_dict(settings)},actor='user',version=settings.version)
        return self.snapshot()['settings']

    def control(self,action):
        mapping={'pause_entries':('paused_entries',True),'resume_entries':('paused_entries',False),'stop_all':('stopped',True),'resume_all':('stopped',False)}
        with self.Session.begin() as s:
            settings=s.get(Settings,1)
            if action in mapping:
                key,val=mapping[action]; setattr(settings,key,val)
                if action=='resume_all': settings.emergency=False
            elif action=='emergency': settings.stopped=True;settings.paused_entries=True;settings.emergency=True
            elif action!='cancel_system': raise ValueError('Unknown control action')
            self._audit(s,'CONTROL_'+action.upper(),{'settings':row_dict(settings)},actor='user')
        if action in {'emergency','cancel_system'}:
            with self.Session() as s:
                ids=list(s.scalars(select(BrokerOrder.id).where(BrokerOrder.owner=='system',BrokerOrder.status.in_(ACTIVE))))
            for order_id in ids: self.cancel_order(order_id)
        return self.snapshot()

    def mark_signal(self,signal_id,status):
        status=status.upper()
        if status not in {'READ','IGNORED','EXPIRED'}: raise ValueError('Signal status must be READ, IGNORED or EXPIRED')
        with self.Session.begin() as s:
            signal=s.get(Signal,signal_id)
            if not signal: raise ValueError('Signal not found')
            before=signal.status;signal.status=status
            self._audit(s,'SIGNAL_STATUS_CHANGED',{'before':before,'after':status},signal_id=signal_id,actor='user')
        return {'id':signal_id,'status':status}

    def cancel_order(self,order_id,now=None):
        with self.Session.begin() as s:
            order=s.get(BrokerOrder,order_id,with_for_update=True)
            if not order: raise ValueError('Order not found')
            if order.owner!='system': raise ValueError('Refusing to cancel a manually owned order')
            if order.status in TERMINAL: return row_dict(order)
            if not order.broker_id or order.status=='UNKNOWN':
                self._audit(s,'CANCEL_AWAITING_RECONCILIATION',{'reason':'Submission cannot be confirmed; reservations retained'},order_id=order.id)
                return row_dict(order)
            order.status='CANCEL_PENDING';order.cancel_requested_at=stamp(now)
            s.get(OrderIntent,order.intent_id).status='CANCEL_PENDING'
            self._audit(s,'CANCEL_REQUESTED',{'reserved_until_final_confirmation':True},order_id=order.id,intent_id=order.intent_id)
            broker_id=order.broker_id
        try:
            result=self.broker.cancel_order(broker_id)
        except Exception as exc:
            result={'status':'CANCEL_PENDING','message':type(exc).__name__}
        with self.Session.begin() as s:
            order=s.get(BrokerOrder,order_id,with_for_update=True)
            if result.get('status') in {'CANCELLED','FILLED','EXPIRED'}:
                # A FILLED status without the fill ledger must not release quantities.
                if result['status']=='FILLED' and order.filled_quantity<order.quantity:
                    s.get(Settings,1).reconciliation_blocked=True
                    order.message='Broker reports filled; awaiting verified fills'
                else: self._terminal(s,order,result['status'])
            self._audit(s,'CANCEL_RESPONSE',result,order_id=order_id)
            return row_dict(order)

    def _terminal(self,s,order,status):
        order.status=status
        intent=s.get(OrderIntent,order.intent_id);intent.status=status;intent.reserved_cash=ZERO;intent.reserved_quantity=ZERO
        if order.side=='SELL':
            position=s.scalar(select(Position).where(Position.symbol==order.symbol,Position.account_id==order.account_id))
            if position: position.exit_status=status
        self._audit(s,'ORDER_'+status,{'filled_quantity':order.filled_quantity,'unfilled_quantity':order.quantity-order.filled_quantity},order_id=order.id,intent_id=order.intent_id,symbol=order.symbol)

    def _acquire(self,owner):
        # Wall-clock lease independent of historical replay timestamps.
        now=utc();expiry=stamp(now+timedelta(minutes=3))
        with self.Session.begin() as s:
            result=s.execute(update(Lease).where(Lease.id=='scan',Lease.expires_at<stamp(now)).values(owner=owner,expires_at=expiry))
            return result.rowcount==1

    def _release(self,owner):
        with self.Session.begin() as s:
            s.execute(update(Lease).where(Lease.id=='scan',Lease.owner==owner).values(expires_at='1970-01-01T00:00:00.000000+00:00',owner=''))

    def recover(self, _lease_owner=None):
        owner=_lease_owner or 'recovery-'+uid()
        if _lease_owner is None and not self._acquire(owner):
            return {'ok':False,'status':'RECOVERY_LOCKED','unresolved_orders':[],'discrepancies':[]}
        try:
            return self._recover_locked()
        finally:
            if _lease_owner is None:self._release(owner)

    def _recover_locked(self):
        with self.Session.begin() as s:
            # A durable intent without an attempted submission is safe to cancel. Re-evaluate
            # market/risk using a fresh signal; never submit a stale recovered request.
            unsubmitted=list(s.scalars(select(BrokerOrder).where(BrokerOrder.status=='PENDING_SUBMIT')))
            for order in unsubmitted:
                self._terminal(s,order,'CANCELLED')
                self._audit(s,'RECOVERY_UNSUBMITTED_CANCELLED',{'reason':'No broker submission attempted; fresh market/risk evaluation required'},order_id=order.id)
            interrupted=list(s.scalars(select(BrokerOrder).where(BrokerOrder.status=='SUBMITTING')))
            for order in interrupted:
                order.status='UNKNOWN';s.get(OrderIntent,order.intent_id).status='UNKNOWN'
                self._audit(s,'RECOVERY_SUBMISSION_UNKNOWN',{},order_id=order.id,intent_id=order.intent_id)
        result=self.reconcile()
        self.recovered=True
        return result

    def reconcile(self,snapshot=None):
        """Resolve unknown submissions by stable ID; external snapshots are read-only evidence.

        Paper fills remain the authoritative paper ledger. External mismatches block
        risk and are recorded without overwriting historical balances.
        """
        with self.Session() as s:
            pending=[row_dict(x) for x in s.scalars(select(BrokerOrder).where(BrokerOrder.status.in_({'UNKNOWN','SUBMITTING'})))]
        unresolved=[]
        for item in pending:
            try: observed=self.broker.find_order(item['client_order_id'])
            except Exception: observed=None
            with self.Session.begin() as s:
                order=s.get(BrokerOrder,item['id'])
                if observed and observed.get('status') in ACTIVE|TERMINAL and observed.get('id'):
                    order.broker_id=observed['id']
                    status=observed['status']
                    if status=='FILLED' and order.filled_quantity<order.quantity:
                        order.status='UNKNOWN';order.message='Broker reports filled: awaiting verified fills';unresolved.append(order.id)
                    else:
                        order.status=status;s.get(OrderIntent,order.intent_id).status=status
                        if status in TERMINAL:self._terminal(s,order,status)
                    self._audit(s,'SUBMISSION_RECONCILED',observed,order_id=order.id)
                else: unresolved.append(order.id)
        discrepancies=[]
        if snapshot is not None:
            current=self.snapshot()
            if str(snapshot.get('account_id','paper-usd'))!='paper-usd': discrepancies.append('ACCOUNT_SCOPE_MISMATCH')
            external={p['symbol']:D(p['quantity']) for p in snapshot.get('positions',[])}
            local={p['symbol']:D(p['quantity']) for p in current['positions']}
            if external!=local: discrepancies.append('POSITION_DIFFERENCE_OR_MANUAL_TRADE')
            if 'cash' in snapshot and D(snapshot['cash'])!=D(current['account']['cash']): discrepancies.append('CASH_DIFFERENCE')
            if snapshot.get('orders'):
                known={o['broker_id'] for o in current['orders']}
                if any(o.get('id') not in known for o in snapshot['orders']): discrepancies.append('EXTERNAL_ORDER_DETECTED')
        with self.Session.begin() as s:
            settings=s.get(Settings,1)
            # An unexplained external discrepancy remains blocked until explicit reconciled evidence.
            if snapshot is not None: settings.reconciliation_blocked=bool(discrepancies or unresolved)
            elif unresolved: settings.reconciliation_blocked=True
            elif pending and not unresolved:
                # Resolving an order must not clear a separate manual-trade discrepancy.
                external_blocked=False
                for audit in s.scalars(select(Audit).where(Audit.event=='RECONCILIATION').order_by(Audit.timestamp.desc())):
                    if audit.detail.get('snapshot') is not None:
                        external_blocked=bool(audit.detail.get('discrepancies'));break
                settings.reconciliation_blocked=external_blocked
            if pending or snapshot is not None:
                self._audit(s,'RECONCILIATION',{'unresolved_orders':unresolved,'discrepancies':discrepancies,'snapshot':snapshot},category='system')
        return {'ok':not(unresolved or discrepancies),'unresolved_orders':unresolved,'discrepancies':discrepancies}

    def adopt_position(self,symbol,atr,ma20):
        atr,ma20=D(atr),D(ma20)
        if atr<=0 or ma20<=0: raise ValueError('Positive entry ATR and MA20 are required')
        with self.Session.begin() as s:
            position=s.scalar(select(Position).where(Position.symbol==symbol,Position.account_id=='paper-usd'))
            if not position or position.quantity<=0: raise ValueError('Position not found in bound paper account')
            before=row_dict(position);position.managed=True;position.strategy='trend_breakout';position.entry_atr=atr;position.ma20=ma20
            position.protection=max(position.protection,position.avg_price-D(s.get(Settings,1).params['initial_stop_atr'])*atr)
            self._audit(s,'POSITION_ADOPTED',{'before':before,'after':row_dict(position)},actor='user',symbol=symbol)
        return self.snapshot()

    def apply_fill(self,fill):
        with self.Session.begin() as s:
            return self._apply_fill(s,fill)

    def _apply_fill(self,s,fill):
        fill_id=str(fill['broker_fill_id'])
        if s.scalar(select(Fill.id).where(Fill.broker_fill_id==fill_id)): return False
        # Serialize fills for the account as well as the order (PostgreSQL row locks;
        # SQLite transactions are serialized with BEGIN IMMEDIATE).
        account=s.get(Account,'paper-usd',with_for_update=True)
        order=s.get(BrokerOrder,fill['order_id'],with_for_update=True)
        if s.scalar(select(Fill.id).where(Fill.broker_fill_id==fill_id)): return False
        if not order: raise ValueError('Fill has no known order')
        if order.mode!='paper' or order.account_id!='paper-usd': raise ValueError('Fill account/mode mismatch')
        qty,price,fee=D(fill['quantity']),D(fill['price']),D(fill.get('fee','0'))
        if not all(x.is_finite() for x in (qty,price,fee)) or qty<=0 or price<=0 or fee<0: raise ValueError('Invalid fill numbers')
        if qty>order.quantity-order.filled_quantity: raise ValueError('Fill exceeds remaining quantity')
        intent=s.get(OrderIntent,order.intent_id)
        position=s.scalar(select(Position).where(Position.account_id==order.account_id,Position.symbol==order.symbol))
        prior_terminal=order.status if order.status in TERMINAL else None
        before_position=row_dict(position) if position else None
        before_cash=account.cash
        realized=ZERO
        if order.side=='BUY':
            cost=qty*price+fee
            if cost>account.settled_cash: raise ValueError('Verified paper fill exceeds settled cash')
            account.cash-=cost;account.settled_cash-=cost;account.realized_pnl-=fee;realized=-fee
            atr=D(intent.data['atr']);ma20=D(intent.data['ma20'])
            if not position:
                position=Position(id=uid(),account_id=order.account_id,symbol=order.symbol,security_id=intent.data.get('security_id',order.symbol),
                    quantity=ZERO,avg_price=ZERO,last_price=price,entry_atr=atr,ma20=ma20,highest_close=price,
                    protection=price-D(intent.data.get('initial_stop_atr','2'))*atr,managed=True,strategy='trend_breakout',opened_at=stamp(fill['timestamp']),updated_at=stamp(fill['timestamp']))
                s.add(position)
            if position.quantity==0:
                position.avg_price=ZERO;position.entry_atr=atr;position.ma20=ma20;position.highest_close=price
                position.protection=price-D(intent.data.get('initial_stop_atr','2'))*atr
                position.opened_at=stamp(fill['timestamp']);position.last_bar_at=stamp(fill['timestamp'])
                position.managed=True;position.exit_status='MONITORING'
            position.avg_price=(position.avg_price*position.quantity+price*qty)/(position.quantity+qty)
            position.quantity+=qty;position.last_price=price
            position.protection=max(position.protection,position.avg_price-D(intent.data.get('initial_stop_atr','2'))*position.entry_atr)
            position.updated_at=stamp(fill['timestamp'])
        else:
            if not position or qty>position.quantity: raise ValueError('Fill exceeds held quantity')
            proceeds=qty*price-fee;realized=qty*(price-position.avg_price)-fee
            position.quantity-=qty;position.last_price=price;position.updated_at=stamp(fill['timestamp'])
            account.cash+=proceeds;account.realized_pnl+=realized
            if position.quantity==0:position.exit_status='CLOSED'
        record=Fill(id=uid(),broker_fill_id=fill_id,order_id=order.id,account_id=order.account_id,symbol=order.symbol,side=order.side,
                    quantity=qty,price=price,fee=fee,timestamp=stamp(fill['timestamp']),realized_pnl=realized)
        s.add(record);s.flush()
        if order.side=='SELL':
            import exchange_calendars as xcals
            import pandas as pd
            calendar=xcals.get_calendar('XNYS')
            trade_date=utc(fill['timestamp']).astimezone(NY).date()
            session=calendar.date_to_session(pd.Timestamp(trade_date),direction='next')
            settle_date=str(calendar.next_session(session).date())
            s.add(Settlement(id=uid(),fill_id=record.id,account_id=order.account_id,amount=qty*price-fee,settle_date=settle_date))
        order.filled_quantity+=qty;order.last_match_at=stamp(fill['timestamp'])
        remaining=order.quantity-order.filled_quantity
        if remaining==0:
            self._terminal(s,order,'FILLED')
        elif prior_terminal:
            # A delayed verified fill may arrive after final cancellation. Preserve
            # final unfilled cancellation and do not resurrect its released reserve.
            self._terminal(s,order,prior_terminal)
        else:
            order.status='CANCEL_PENDING' if order.cancel_requested_at else 'PARTIALLY_FILLED';intent.status=order.status
            intent.reserved_cash=remaining*order.limit_price*(1+D(intent.data.get('fee_bps','1'))/10000) if order.side=='BUY' else ZERO
            intent.reserved_quantity=remaining if order.side=='SELL' else ZERO
        self._audit(s,'FILL_APPLIED',{'fill':row_dict(record),'cash_before':before_cash,'cash_after':account.cash,'position_before':before_position,'position_after':row_dict(position)},
                    order_id=order.id,intent_id=intent.id,signal_id=intent.signal_id,symbol=order.symbol,run_id=intent.run_id,version=intent.version)
        return True

    def _sync_paper(self,s,data,now):
        session_date=data.get('session_date',str(utc(now).astimezone(NY).date()))
        account=s.get(Account,'paper-usd')
        for settlement in s.scalars(select(Settlement).where(Settlement.settled==False,Settlement.settle_date<=session_date)):
            account.settled_cash+=settlement.amount;settlement.settled=True
            self._audit(s,'CASH_SETTLED',{'amount':settlement.amount,'fill_id':settlement.fill_id,'settle_date':settlement.settle_date})
        settings=s.get(Settings,1)
        for order in list(s.scalars(select(BrokerOrder).where(BrokerOrder.status.in_({'ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING'}),BrokerOrder.mode=='paper'))):
            if data.get('quality')=='valid' and data.get('market_open') and isinstance(self.broker,PaperBroker):
                quote=data.get('quotes',{}).get(order.symbol)
                if quote:
                    quote=dict(quote)
                    liquidity_key='|liquidity:'+stamp(quote.get('volume_timestamp',quote['timestamp']))
                    consumed=sum((f.quantity for f in s.scalars(select(Fill).where(Fill.symbol==order.symbol)) if f.broker_fill_id.endswith(liquidity_key)),ZERO)
                    quote['_paper_liquidity_consumed']=consumed
                fill=self.broker.match(order,quote,now,settings.params)
                if fill:self._apply_fill(s,fill)
            if order.status=='CANCEL_PENDING': self._terminal(s,order,'CANCELLED')
            elif order.status not in TERMINAL and utc(now)>=utc(order.expires_at):
                # Paper TTL is an exchange-side simulated DAY/timeout cancellation acknowledgment.
                self._terminal(s,order,'EXPIRED')
        for signal in s.scalars(select(Signal).where(Signal.status.in_({'NEW','READ'}),Signal.expires_at<stamp(now))):
            signal.status='EXPIRED'
        for position in s.scalars(select(Position)):
            quote=data.get('quotes',{}).get(position.symbol)
            if data.get('quality')=='valid' and not validate_quote(quote,now,settings.params):position.last_price=D(quote['last'])

    def _signal(self,s,symbol,side,key,event,run_id,now):
        existing=s.scalar(select(Signal).where(Signal.dedup_key==key))
        if existing:return existing,False
        settings=s.get(Settings,1)
        signal=Signal(id=uid(),dedup_key=key,account_id='paper-usd',symbol=symbol,side=side,status='NEW',reason=event['reason'],risk_result='PENDING',
                      run_id=run_id,version=settings.version,created_at=stamp(now),expires_at=stamp(utc(now)+timedelta(seconds=int(settings.params['order_ttl_seconds']))),evidence=jsonable(event))
        s.add(signal);s.flush()
        self._audit(s,'SIGNAL_GENERATED',event,category='decision',signal_id=signal.id,symbol=symbol,run_id=run_id,version=settings.version)
        return signal,True

    def _record_risk(self,s,signal,reason,run_id):
        if signal.risk_result!=reason:
            signal.risk_result=reason
            self._audit(s,'RISK_DECISION',{'result':reason},category='decision',signal_id=signal.id,symbol=signal.symbol,run_id=run_id,version=signal.version)

    def _new_intent(self,s,signal,quantity,limit_price,now,run_id,metadata):
        settings=s.get(Settings,1)
        key=signal.dedup_key
        if s.scalar(select(OrderIntent.id).where(OrderIntent.dedup_key==key)):return None
        reserve=quantity*limit_price*(1+D(settings.params['fee_bps'])/10000) if signal.side=='BUY' else ZERO
        intent=OrderIntent(id=uid(),dedup_key=key,signal_id=signal.id,account_id='paper-usd',symbol=signal.symbol,side=signal.side,quantity=quantity,
                           limit_price=limit_price,reserved_cash=reserve,reserved_quantity=quantity if signal.side=='SELL' else ZERO,status='PENDING_SUBMIT',mode='paper',
                           version=settings.version,run_id=run_id,created_at=stamp(now),data=jsonable({**metadata,'fee_bps':settings.params['fee_bps'],
                           'initial_stop_atr':settings.params['initial_stop_atr']}))
        s.add(intent);s.flush()
        order=BrokerOrder(id=uid(),intent_id=intent.id,account_id=intent.account_id,client_order_id=intent.id,symbol=intent.symbol,side=intent.side,
                          quantity=quantity,filled_quantity=ZERO,limit_price=limit_price,status='PENDING_SUBMIT',owner='system',mode='paper',submitted_at=stamp(now),
                          expires_at=stamp(utc(now)+timedelta(seconds=int(settings.params['order_ttl_seconds']))))
        s.add(order);signal.status='LINKED'
        self._audit(s,'INTENT_PERSISTED',{'intent':row_dict(intent),'order_id':order.id},signal_id=signal.id,intent_id=intent.id,order_id=order.id,symbol=signal.symbol,run_id=run_id,version=settings.version)
        return intent.id

    def _submit_intent(self,intent_id):
        # First transaction makes the intent and SUBMITTING state durable before I/O.
        with self.Session.begin() as s:
            intent=s.get(OrderIntent,intent_id)
            if not intent or intent.status!='PENDING_SUBMIT':return
            settings=s.get(Settings,1)
            order=s.scalar(select(BrokerOrder).where(BrokerOrder.intent_id==intent_id))
            if settings.stopped or settings.emergency or settings.mode!='paper' or (intent.side=='BUY' and (settings.paused_entries or settings.reconciliation_blocked)):
                self._terminal(s,order,'CANCELLED');return
            if getattr(self.broker,'mode',None)!='paper':
                self._terminal(s,order,'REJECTED');raise ValueError('Paper intent may only use a paper broker')
            order.status='SUBMITTING';intent.status='SUBMITTING'
            request=row_dict(intent)
            self._audit(s,'ORDER_SUBMISSION_STARTED',{'request':request},intent_id=intent_id,order_id=order.id,signal_id=intent.signal_id,run_id=intent.run_id)
        try:
            preflight=self.broker.preflight(request)
            result=self.broker.submit_order(request) if preflight.get('allowed') else {'status':'REJECTED','message':preflight.get('reason','Preflight rejected')}
        except Exception as exc:
            # Network error is not evidence that a broker failed to accept an order.
            result={'status':'UNKNOWN','message':type(exc).__name__+'; reconcile before any retry'}
        with self.Session.begin() as s:
            intent=s.get(OrderIntent,intent_id)
            order=s.scalar(select(BrokerOrder).where(BrokerOrder.intent_id==intent_id))
            status=result.get('status','UNKNOWN')
            if status not in ACTIVE|TERMINAL:status='UNKNOWN'
            if status=='FILLED':status='UNKNOWN' # verified fills, never status alone, move the ledger
            if status in {'ACCEPTED','PARTIALLY_FILLED','PENDING_APPROVAL'} and not result.get('id'):
                status='UNKNOWN';result['message']='No stable broker order ID returned; reconcile required'
            order.status=status;intent.status=status;order.message=result.get('message','')
            order.broker_id=result.get('id')
            if status in TERMINAL:self._terminal(s,order,status)
            if status=='UNKNOWN':s.get(Settings,1).reconciliation_blocked=True
            self._audit(s,'ORDER_SUBMISSION_RESULT',result,intent_id=intent_id,order_id=order.id,signal_id=intent.signal_id,run_id=intent.run_id,symbol=intent.symbol)

    def scan(self,data,now=None,run_id=None):
        now=utc(now);run_id=run_id or uid();started=time.monotonic()
        if not self._acquire(run_id):return {'status':'LOCKED','run_id':run_id}
        try:
            # Recheck the exchange session at execution time; a fetch started just
            # before close must not carry an outdated open flag beyond the close.
            from .calendar import session_bounds
            execution_day=str(now.astimezone(NY).date())
            bounds=session_bounds(execution_day)
            data=dict(data)
            data['market_open']=bool(data.get('market_open') and bounds and bounds[0]<=now<bounds[1])
            if data.get('session_date',execution_day)!=execution_day:data['quality']='stale'
            if not self.recovered:self.recover(_lease_owner=run_id)
            else:self.reconcile()
            intents=[]
            with self.Session.begin() as s:
                if s.get(Run,run_id):return {'status':'DUPLICATE_RUN','run_id':run_id}
                session_date=data.get('session_date',str(now.astimezone(NY).date()))
                run=Run(id=run_id,started_at=stamp(now),status='RUNNING',data_source=data.get('source','unknown'),quality=data.get('quality','unknown'),coverage=len(data.get('quotes',{})))
                s.add(run)
                settings=s.get(Settings,1);params=settings.params
                baseline=s.get(SessionEquity,session_date)
                if not baseline:
                    baseline=SessionEquity(session_date=session_date,opening_equity=D(self._balance(s)['equity']));s.add(baseline)
                self._sync_paper(s,data,now)
                self._set_candidates(s,data.get('candidates',[]),session_date)
                valid=data.get('quality')=='valid' and bool(data.get('market_open'))
                if not valid:
                    run.status='DEGRADED' if data.get('quality')!='valid' else 'MARKET_CLOSED'
                    run.message='New instructions blocked: '+('invalid/stale data' if data.get('quality')!='valid' else 'outside regular session')
                    self._audit(s,'SCAN_DATA_GATE',{'quality':data.get('quality'),'market_open':data.get('market_open'),'source':data.get('source')},category='system',run_id=run_id)
                else:
                    candidates={c['symbol']:c for c in data.get('candidates',[])}
                    # Account holdings remain monitored even after leaving the daily candidate pool.
                    for position in list(s.scalars(select(Position).where(Position.account_id=='paper-usd'))):
                        if position.quantity<=0:continue
                        indicator=candidates.get(position.symbol) or data.get('holding_indicators',{}).get(position.symbol)
                        if indicator and indicator.get('ma20'):position.ma20=D(indicator['ma20'])
                        if not position.managed:continue
                        bars=closed_bars(data.get('bars',{}).get(position.symbol,[]),now)
                        quote=data.get('quotes',{}).get(position.symbol)
                        if not bars or (now-utc(bars[-1]['timestamp'])).total_seconds()>int(params['bar_max_age_seconds']):continue
                        event=exit_signal({**row_dict(position),'params':params},bars,position.ma20,now)
                        before_line=position.protection;position.highest_close=event['highest_close'];position.protection=event['protection']
                        position.last_bar_at=stamp(bars[-1]['timestamp'])
                        if position.protection!=before_line:
                            self._audit(s,'PROTECTION_RAISED',{'before':before_line,'after':position.protection,'bar_time':position.last_bar_at},symbol=position.symbol,run_id=run_id)
                        latched=s.scalar(select(Signal).where(Signal.symbol==position.symbol,Signal.side=='SELL',
                            Signal.status.in_({'NEW','READ'}),Signal.created_at>=position.opened_at,Signal.expires_at>stamp(now)).order_by(Signal.created_at.desc()))
                        if not event['triggered'] and not latched:continue
                        position.exit_status='TRIGGERED'
                        if latched:
                            signal=latched
                        else:
                            key='exit:'+position.id+':'+event['bar_time']
                            signal,new=self._signal(s,position.symbol,'SELL',key,event,run_id,now)
                            if not new:continue
                        block=validate_quote(quote,now,params)
                        if settings.stopped or settings.emergency:block='ALL_SUBMISSIONS_STOPPED'
                        elif settings.mode=='observe':block='OBSERVATION_ONLY'
                        reserved=sum((i.reserved_quantity for i in s.scalars(select(OrderIntent).where(OrderIntent.symbol==position.symbol,OrderIntent.side=='SELL',OrderIntent.status.in_(ACTIVE)))),ZERO)
                        quantity=position.quantity-reserved
                        if quantity<=0:block='SELL_QUANTITY_RESERVED'
                        if block:self._record_risk(s,signal,block,run_id);continue
                        limit=limit_price_for('SELL',quote,params)
                        self._record_risk(s,signal,'APPROVED',run_id)
                        intent_id=self._new_intent(s,signal,quantity,limit,now,run_id,{'atr':position.entry_atr,'ma20':position.ma20,'security_id':position.security_id})
                        if intent_id:intents.append(intent_id);position.exit_status='ORDER_PENDING'
                    for candidate in sorted(data.get('candidates',[]),key=lambda c:int(c.get('rank',999))):
                        symbol=candidate['symbol'];bars=closed_bars(data.get('bars',{}).get(symbol,[]),now)
                        if len(bars)<2 or (now-utc(bars[-1]['timestamp'])).total_seconds()>int(params['bar_max_age_seconds']):continue
                        event=entry_signal(candidate,bars,now)
                        if not event:continue
                        key='entry:paper-usd:'+symbol+':'+session_date
                        signal,new=self._signal(s,symbol,'BUY',key,event,run_id,now)
                        if not new and (signal.status not in {'NEW','READ'} or utc(signal.expires_at)<=now):continue
                        block=validate_quote(data.get('quotes',{}).get(symbol),now,params)
                        if not data.get('market_filter'):block='SPY_MARKET_FILTER'
                        if candidate.get('tradable') is False:block='SECURITY_NOT_TRADABLE'
                        if settings.stopped or settings.emergency:block='ALL_SUBMISSIONS_STOPPED'
                        elif settings.paused_entries:block='ENTRIES_PAUSED'
                        elif settings.reconciliation_blocked:block='RECONCILIATION_REQUIRED'
                        elif settings.mode=='observe':block='OBSERVATION_ONLY'
                        position=s.scalar(select(Position).where(Position.account_id=='paper-usd',Position.symbol==symbol))
                        if position and position.quantity>0:block='POSITION_ALREADY_HELD'
                        active_buys=list(s.scalars(select(OrderIntent).where(OrderIntent.side=='BUY',OrderIntent.status.in_(ACTIVE))))
                        if any(i.symbol==symbol for i in active_buys):block='BUY_ALREADY_PENDING'
                        if block:self._record_risk(s,signal,block,run_id);continue
                        quote=data['quotes'][symbol]
                        limit=limit_price_for('BUY',quote,params)
                        if abs(limit-event['price'])/event['price']>D(params['max_price_deviation_pct']):
                            self._record_risk(s,signal,'MAX_PRICE_DEVIATION',run_id);continue
                        balance=self._balance(s)
                        day_fills=[f for f in s.scalars(select(Fill).where(Fill.side=='BUY')) if str(utc(f.timestamp).astimezone(NY).date())==session_date]
                        day_added=sum((f.quantity*f.price+f.fee for f in day_fills),ZERO)
                        positions=[p for p in s.scalars(select(Position)) if p.quantity>0]
                        count=len({p.symbol for p in positions}|{i.symbol for i in active_buys})
                        sizing=size_entry(balance['equity'],balance['settled_cash'],balance['exposure'],balance['reserved_cash'],count,limit,event['atr'],params,
                                          day_added=day_added,daily_pnl=D(balance['equity'])-baseline.opening_equity)
                        self._record_risk(s,signal,sizing['reason'],run_id)
                        if not sizing['allowed']:continue
                        intent_id=self._new_intent(s,signal,sizing['quantity'],limit,now,run_id,{'atr':event['atr'],'ma20':candidate['ma20'],'security_id':candidate.get('security_id',symbol),'signal_bar_time':event['bar_time']})
                        if intent_id:intents.append(intent_id)
                    run.status='SUCCESS';settings.failures=0
                run.completed_at=stamp();run.duration_ms=int((time.monotonic()-started)*1000)
                self._audit(s,'SCAN_COMPLETE',{'status':run.status,'coverage':run.coverage,'new_intents':len(intents),'source':run.data_source},category='system',run_id=run_id)
            for intent_id in intents:self._submit_intent(intent_id)
            return {'run_id':run_id,'status':run.status,'new_intents':len(intents),'duration_ms':int((time.monotonic()-started)*1000)}
        except Exception as exc:
            with self.Session.begin() as s:
                settings=s.get(Settings,1);settings.failures+=1
                if settings.failures>=int(settings.params['max_consecutive_failures']):settings.paused_entries=True
                self._audit(s,'SCAN_FAILED',{'error_type':type(exc).__name__,'message':str(exc)[:300]},category='system',run_id=run_id)
                failed=s.get(Run,run_id)
                if failed:failed.status='FAILED';failed.message=type(exc).__name__;failed.completed_at=stamp()
            raise
        finally:self._release(run_id)
