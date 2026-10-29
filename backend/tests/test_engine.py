from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import select
from trading.engine import Engine
from trading.models import BrokerOrder, OrderIntent, Position, Account
from trading.paper import PaperBroker
from trading.strategy import exit_signal

START=datetime(2026,10,26,14,0,tzinfo=timezone.utc)

def frame(now=START,price='101.4',previous='100.5',quality='valid',symbols=('AAPL',),volume=10000):
    return {'source':'demo','quality':quality,'market_open':True,'session_date':str(now.date()),'market_filter':True,
            'candidates':[{'symbol':sym,'security_id':'security-'+sym,'name':sym,'close':'100','ma20':'98','ma60':'90','high20':'101','atr14':'2',
                           'return60':'.1','avg_dollar_volume':'90000000','rank':idx+1} for idx,sym in enumerate(symbols)],
            'quotes':{sym:{'bid':str(Decimal(price)-Decimal('.01')),'ask':str(Decimal(price)+Decimal('.01')),'last':price,'timestamp':now.isoformat(),'volume':volume} for sym in symbols},
            'bars':{sym:[{'timestamp':(now-timedelta(minutes=5)).isoformat(),'close':previous},{'timestamp':now.isoformat(),'close':price}] for sym in symbols}}

@pytest.fixture
def engine(tmp_path):
    return Engine('sqlite:///'+str(tmp_path/'test.db'))

def buy(engine,quantity=None):
    engine.scan(frame(),START)
    if quantity is not None:
        with engine.Session.begin() as s:
            order=s.scalar(select(BrokerOrder));intent=s.get(OrderIntent,order.intent_id)
            order.quantity=Decimal(quantity);intent.quantity=Decimal(quantity)
            intent.reserved_cash=Decimal(quantity)*order.limit_price*Decimal('1.0001')
    engine.scan(frame(START+timedelta(seconds=30)),START+timedelta(seconds=30))
    return engine.snapshot()

def test_subsequent_quote_fill_and_daily_dedup(engine):
    engine.scan(frame(),START)
    first=engine.snapshot()
    assert len(first['orders'])==1
    assert first['orders'][0]['status']=='ACCEPTED'
    assert first['fills']==[]
    assert Decimal(first['account']['reserved_cash'])>0
    engine.scan(frame(),START)
    assert engine.snapshot()['fills']==[]
    snap=buy(engine)
    assert len(snap['orders'])==1
    assert len(snap['fills'])==1
    assert snap['orders'][0]['status']=='FILLED'
    assert len(snap['positions'])==1
    assert snap['account']['reserved_cash']=='0'
    assert any(a['event']=='INTENT_PERSISTED' for a in snap['audit'])
    assert any(a['event']=='FILL_APPLIED' for a in snap['audit'])

def test_stale_data_and_stale_quote_block(engine):
    engine.scan(frame(quality='stale'),START)
    assert engine.snapshot()['orders']==[]
    data=frame(START+timedelta(minutes=10))
    data['quotes']['AAPL']['timestamp']=START.isoformat()
    engine.scan(data,START+timedelta(minutes=10))
    assert engine.snapshot()['orders']==[]
    assert engine.snapshot()['signals'][0]['risk_result']=='STALE_QUOTE'

def test_first_cross_and_no_future_bar(engine):
    data=frame(price='102',previous='101.5')
    engine.scan(data,START)
    assert not engine.snapshot()['signals']
    data=frame()
    data['bars']['AAPL'][-1]['timestamp']=(START+timedelta(minutes=5)).isoformat()
    engine.scan(data,START)
    assert not engine.snapshot()['orders']

def test_ranking_and_shared_cash_reservations(engine):
    engine.update_settings({'params':{'max_daily_added':'1000'}})
    engine.scan(frame(symbols=('AAPL','MSFT','GOOG','AMD','NVDA')),START)
    snap=engine.snapshot()
    # Allocation is sequential, with every earlier intent reservation included.
    assert len(snap['orders'])==1
    assert snap['orders'][0]['symbol']=='AAPL'
    reserved=Decimal(snap['account']['reserved_cash'])
    assert 0<reserved<=1000
    assert Decimal(snap['account']['buying_power'])+reserved==Decimal('10000')

def test_fill_idempotency(engine):
    snap=buy(engine)
    original=snap['fills'][0]
    assert engine.apply_fill(original) is False
    after=engine.snapshot()
    assert after['account']==snap['account']
    assert after['positions']==snap['positions']
    assert len(after['fills'])==1

def test_partial_cancel_race_retains_then_releases_only_remaining(engine):
    engine.scan(frame(),START)
    oid=engine.snapshot()['orders'][0]['id']
    later=START+timedelta(seconds=30)
    engine.scan(frame(later,volume=200),later)
    partial=engine.snapshot()
    assert partial['orders'][0]['status']=='PARTIALLY_FILLED'
    assert partial['orders'][0]['filled_quantity']=='2'
    before_reserve=Decimal(partial['account']['reserved_cash'])
    engine.cancel_order(oid,now=START+timedelta(seconds=60))
    assert Decimal(engine.snapshot()['account']['reserved_cash'])==before_reserve
    # Quote occurred before cancel request, but arrived during cancellation: still fills.
    raced=frame(START+timedelta(seconds=50),volume=100)
    engine.scan(raced,START+timedelta(seconds=65))
    snap=engine.snapshot()
    assert snap['orders'][0]['status']=='CANCELLED'
    assert snap['orders'][0]['filled_quantity']=='3'
    assert snap['positions'][0]['quantity']=='3'
    assert snap['account']['reserved_cash']=='0'
    assert len(snap['fills'])==2

def test_timeout_unknown_is_not_resubmitted_and_recovery_queries(engine,tmp_path):
    class TimeoutBroker(PaperBroker):
        calls=0
        accepted={}
        def submit_order(self,intent):
            self.calls+=1;self.accepted[intent['id']]={'id':'ack-'+intent['id'],'status':'ACCEPTED'}
            raise TimeoutError('accepted but response lost')
        def find_order(self,client_order_id):return self.accepted.get(client_order_id)
    broker=TimeoutBroker(engine.Session);engine.broker=broker
    engine.scan(frame(),START)
    assert engine.snapshot()['orders'][0]['status']=='UNKNOWN'
    assert Decimal(engine.snapshot()['account']['reserved_cash'])>0
    restarted=Engine(str(engine.db.url),broker=broker)
    assert restarted.recover()['ok'] is True
    assert restarted.snapshot()['orders'][0]['status']=='ACCEPTED'
    restarted.scan(frame(START+timedelta(seconds=30)),START+timedelta(seconds=30))
    assert broker.calls==1

def test_unknown_without_reliable_lookup_stays_reserved(engine):
    class OpaqueBroker(PaperBroker):
        calls=0
        def submit_order(self,intent):self.calls+=1;raise TimeoutError()
        def find_order(self,client_order_id):return None
    broker=OpaqueBroker(engine.Session);engine.broker=broker
    engine.scan(frame(),START)
    engine.scan(frame(START+timedelta(seconds=30)),START+timedelta(seconds=30))
    snap=engine.snapshot()
    assert snap['orders'][0]['status']=='UNKNOWN'
    assert snap['settings']['reconciliation_blocked'] is True
    assert Decimal(snap['account']['reserved_cash'])>0
    assert broker.calls==1

def test_pause_entries_keeps_exit_and_universe_removal_monitoring(engine):
    buy(engine)
    engine.control('pause_entries')
    now=START+timedelta(minutes=5)
    data=frame(now,price='95',previous='101.4')
    data['candidates']=[]
    engine.scan(data,now)
    orders=engine.snapshot()['orders']
    assert len(orders)==2
    assert any(o['side']=='SELL' and o['status']=='ACCEPTED' for o in orders)
    later=now+timedelta(seconds=30)
    engine.scan(frame(later,price='95',previous='95'),later)
    snap=engine.snapshot()
    assert snap['positions']==[]
    assert Decimal(snap['account']['unsettled_cash'])>0
    assert len(snap['fills'])==2

def test_ownership_stop_and_mode_boundaries(engine):
    buy(engine)
    with engine.Session.begin() as s:
        position=s.scalar(select(Position));position.managed=False
    now=START+timedelta(minutes=5)
    engine.scan(frame(now,price='90',previous='101.4'),now)
    assert len(engine.snapshot()['orders'])==1
    engine.adopt_position('AAPL','2','98')
    engine.control('stop_all')
    engine.scan(frame(now+timedelta(minutes=5),price='89',previous='90'),now+timedelta(minutes=5))
    assert len(engine.snapshot()['orders'])==1
    with pytest.raises(ValueError):engine.update_settings({'mode':'live'})
    with engine.Session.begin() as s:s.scalar(select(BrokerOrder)).owner='manual'
    with pytest.raises(ValueError):engine.cancel_order(engine.snapshot()['orders'][0]['id'])

def test_reconciliation_manual_change_detected_without_overwriting_ledger(engine):
    buy(engine)
    before=engine.snapshot()
    result=engine.reconcile({'account_id':'paper-usd','positions':[{'symbol':'AAPL','quantity':'100'}],'cash':'5'})
    assert result['ok'] is False
    assert engine.snapshot()['settings']['reconciliation_blocked'] is True
    assert engine.snapshot()['account']==before['account']
    assert engine.snapshot()['positions']==before['positions']

def test_trailing_protection_does_not_retroactively_trigger():
    position={'avg_price':'100','last_price':'110','entry_atr':'2','protection':'104','highest_close':'110','opened_at':START.isoformat(),'last_bar_at':(START+timedelta(minutes=10)).isoformat()}
    bars=[{'timestamp':(START+timedelta(minutes=5)).isoformat(),'close':'101'}, {'timestamp':(START+timedelta(minutes=10)).isoformat(),'close':'110'}, {'timestamp':(START+timedelta(minutes=15)).isoformat(),'close':'108'}]
    result=exit_signal(position,bars,'95',START+timedelta(minutes=15))
    assert result['triggered'] is False
    assert result['protection']==Decimal('104')

def test_lease_prevents_duplicate_worker(engine):
    assert engine._acquire('another-worker') is True
    try:
        assert engine.scan(frame(),START)['status']=='LOCKED'
        assert not engine.snapshot()['orders']
    finally:engine._release('another-worker')

def test_delayed_fill_after_cancel_does_not_resurrect_reservation(engine):
    engine.scan(frame(),START)
    oid=engine.snapshot()['orders'][0]['id']
    engine.cancel_order(oid,now=START+timedelta(seconds=15))
    engine.scan(frame(START+timedelta(seconds=30)),START+timedelta(seconds=30))
    assert engine.snapshot()['orders'][0]['status']=='CANCELLED'
    engine.apply_fill({'broker_fill_id':'late-execution','order_id':oid,'quantity':'1','price':'101.4','fee':'.01','timestamp':START+timedelta(seconds=10)})
    snap=engine.snapshot()
    assert snap['orders'][0]['status']=='CANCELLED'
    assert snap['positions'][0]['quantity']=='1'
    assert snap['account']['reserved_cash']=='0'

def test_recovery_never_submits_unattempted_stale_intent(engine):
    original_submit=engine._submit_intent
    engine._submit_intent=lambda intent_id:None
    engine.scan(frame(),START)
    assert engine.snapshot()['orders'][0]['status']=='PENDING_SUBMIT'
    engine._submit_intent=original_submit
    restarted=Engine(str(engine.db.url))
    restarted.recover()
    assert restarted.snapshot()['orders'][0]['status']=='CANCELLED'
    assert restarted.snapshot()['account']['reserved_cash']=='0'

def test_bad_settings_are_value_errors_and_rollback(engine):
    previous=engine.snapshot()['settings']
    for value in ('oops','NaN','Infinity','-1'):
        with pytest.raises(ValueError):engine.update_settings({'params':{'risk_per_trade_pct':value}})
    assert engine.snapshot()['settings']==previous

def test_audit_application_updates_are_forbidden(engine):
    from trading.models import Audit
    engine.control('pause_entries')
    with pytest.raises(ValueError):
        with engine.Session.begin() as s:
            s.scalar(select(Audit)).event='HISTORY_REWRITTEN'
    assert engine.snapshot()['audit'][0]['event']=='CONTROL_PAUSE_ENTRIES'

def test_simulated_sale_cash_settles_next_exchange_session(engine):
    buy(engine)
    now=START+timedelta(minutes=5)
    engine.scan(frame(now,price='95',previous='101.4'),now)
    engine.scan(frame(now+timedelta(seconds=30),price='95',previous='95'),now+timedelta(seconds=30))
    assert Decimal(engine.snapshot()['account']['unsettled_cash'])>0
    next_day=START+timedelta(days=1)
    engine.scan(frame(next_day,price='95',previous='95'),next_day)
    assert Decimal(engine.snapshot()['account']['unsettled_cash'])==0

def test_quote_refresh_cannot_reuse_same_closed_bar_liquidity(engine):
    engine.scan(frame(),START)
    for seconds in (30,60,90):
        now=START+timedelta(seconds=seconds)
        data=frame(now,volume=100)
        data['quotes']['AAPL']['volume_timestamp']=(START-timedelta(minutes=5)).isoformat()
        engine.scan(data,now)
    snap=engine.snapshot()
    assert snap['orders'][0]['filled_quantity']=='1'
    assert len(snap['fills'])==1
    now=START+timedelta(seconds=120)
    data=frame(now,volume=100)
    data['quotes']['AAPL']['volume_timestamp']=START.isoformat()
    engine.scan(data,now)
    assert engine.snapshot()['orders'][0]['filled_quantity']=='2'

def test_exit_latched_until_quote_recovers_without_new_closed_bar(engine):
    buy(engine)
    now=START+timedelta(minutes=5)
    data=frame(now,price='95',previous='101.4')
    data['quotes']['AAPL']['timestamp']=(now-timedelta(minutes=10)).isoformat()
    engine.scan(data,now)
    assert len(engine.snapshot()['orders'])==1
    # Same completed bar; only the quote freshness changes.
    data['quotes']['AAPL']['timestamp']=(now+timedelta(seconds=30)).isoformat()
    engine.scan(data,now+timedelta(seconds=30))
    snap=engine.snapshot()
    assert len(snap['orders'])==2
    assert sum(o['side']=='SELL' for o in snap['orders'])==1
    assert len([x for x in snap['signals'] if x['side']=='SELL'])==1

def test_entry_reassesses_unexpired_signal_when_quote_recovers(engine):
    data=frame()
    data['quotes']['AAPL']['timestamp']=(START-timedelta(minutes=10)).isoformat()
    engine.scan(data,START)
    assert not engine.snapshot()['orders']
    data['quotes']['AAPL']['timestamp']=(START+timedelta(seconds=30)).isoformat()
    engine.scan(data,START+timedelta(seconds=30))
    assert len(engine.snapshot()['orders'])==1
    assert len(engine.snapshot()['signals'])==1

def test_recovery_respects_another_workers_lease(engine):
    engine._acquire('active-worker')
    try:
        assert engine.recover()['status']=='RECOVERY_LOCKED'
        assert engine.recovered is False
    finally:engine._release('active-worker')

def test_expiry_is_exclusive_no_fill_at_expiry(engine):
    engine.scan(frame(),START)
    now=START+timedelta(seconds=300)
    engine.scan(frame(now),now)
    snap=engine.snapshot()
    assert snap['orders'][0]['status']=='EXPIRED'
    assert snap['fills']==[]

def test_preflight_rechecks_latest_settled_cash_before_submit(engine):
    original_submit=engine._submit_intent
    engine._submit_intent=lambda _:None
    engine.scan(frame(),START)
    intent_id=engine.snapshot()['intents'][0]['id']
    with engine.Session.begin() as s:
        # Fault injection: buying power changed after allocation but before submission.
        s.get(Account,'paper-usd').settled_cash=Decimal('1')
    engine._submit_intent=original_submit
    engine._submit_intent(intent_id)
    snap=engine.snapshot()
    assert snap['orders'][0]['status']=='REJECTED'
    assert snap['orders'][0]['message']=='LATEST_BUYING_POWER_INSUFFICIENT'
    assert snap['account']['reserved_cash']=='0'

def test_execution_rechecks_calendar_after_fetch_crosses_market_close(engine):
    now=datetime(2026,11,27,18,0,tzinfo=timezone.utc) # Thanksgiving Friday 13:00 ET early close
    data=frame(now)
    data['market_open']=True # Stale upstream open flag from a fetch before close.
    result=engine.scan(data,now)
    assert result['status']=='MARKET_CLOSED'
    assert not engine.snapshot()['orders']

def test_stale_session_candidate_pool_cannot_trade(engine):
    data=frame()
    data['session_date']='2026-10-23'
    assert engine.scan(data,START)['status']=='DEGRADED'
    assert not engine.snapshot()['orders']

def test_individual_quote_quality_blocks_despite_valid_frame(engine):
    data=frame()
    data['quotes']['AAPL']['quality']='invalid'
    engine.scan(data,START)
    assert engine.snapshot()['orders']==[]
    assert engine.snapshot()['signals'][0]['risk_result']=='INVALID_QUOTE_QUALITY'

def test_invalid_completed_bar_blocks_crossing(engine):
    data=frame()
    data['bars']['AAPL'][-1]['quality']='invalid'
    engine.scan(data,START)
    assert not engine.snapshot()['signals']
