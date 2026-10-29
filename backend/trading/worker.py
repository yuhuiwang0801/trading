"""Independent 30-second market worker. Never depends on an open browser."""
from __future__ import annotations
import fcntl
import os
import signal
import time
from . import config
from .engine import Engine
from .runtime import RuntimeStore, utcnow
from .market import MarketService
from .live_runtime import create_live_service, LIVE_CONTROL_VERSION, live_control_identity
from .strategy_registry import active_strategy, daily_data_ready, retire_legacy_job


def scan_cycle(engine, market, live_market, live, store):
    """Run V2 only; the legacy live_market argument is retained for old callers."""
    snapshot = engine.snapshot()
    params = snapshot['settings']['params']
    selected = active_strategy(store)['id']
    live_snapshot = live.snapshot()
    from .reversal_runtime import load_runtime_packet
    from .reversal_dispatch import quote_frame
    from .strategy_registry import runtime_view
    packet = load_runtime_packet(data_dir=config.DATA / 'reversal' / 'runtime_v2')
    configured_strategy = live_snapshot.get('configuration', {}).get('strategy_id', 'trend_breakout')
    if configured_strategy != 'reversal_v2':
        # Retiring a strategy does not transfer its positions/authorization to V2.
        # Reconcile existing activity for visibility, but never issue new writes.
        if live_snapshot.get('intents') or live_snapshot.get('positions'):
            live.reconcile()
        live_result = {'status': 'STRATEGY_RETIRED', 'orders_sent': 0,
                       'read_only_sync': True, 'reason': 'legacy_strategy_requires_v2_preparation'}
    elif live_snapshot.get('control', {}).get('armed'):
        frame = quote_frame(market.provider, packet, live_snapshot['configuration'],
                            live_snapshot.get('positions', []) + live_snapshot.get('broker_positions', []),
                            live_snapshot.get('intents', []))
        live_result = live.scan(frame, now=utcnow(), params=params)
        store.put('last_live_market_scan', frame)
    elif live_snapshot.get('intents') or live_snapshot.get('positions'):
        live.reconcile()
        live_result = {'status': 'DISARMED', 'orders_sent': 0, 'read_only_sync': True}
    else:
        live_result = {'status': 'DISARMED', 'orders_sent': 0}
    status = runtime_view(packet)
    store.put('live_worker_scan', {**live_result, 'strategy_id': selected, 'checked_at': utcnow().isoformat()})
    store.put('last_market_scan', {'source': 'alpaca', 'strategy_id': selected,
              'quality': 'valid' if daily_data_ready(packet) else 'unavailable',
              'timestamp': packet.get('created_at'), 'data_session': packet.get('data_session'),
              'candidates': status['candidates'], 'reason': status['blockers']})
    store.put('market_status', {'source': 'alpaca', 'signal_feed': 'sip', 'feed': market.provider.feed,
              'daily_runtime': status, 'strategy_id': selected})
    return {'status': 'SUCCESS' if daily_data_ready(packet) else 'DEGRADED',
            'strategy_id': selected, 'live': live_result, 'candidate_count': len(status['candidates'])}


def main(once=False):
    lock = open(config.DATA / 'worker.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('A market worker is already running on this host')
    store = RuntimeStore(config.DATABASE_URL)
    engine = Engine(config.DEMO_DATABASE_URL if config.SOURCE == 'demo' else config.DATABASE_URL)
    market = MarketService.from_env()
    live = create_live_service(config.DATABASE_URL)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store.recover_jobs(['scan','demo'])
    recovery = engine.recover()
    # Both API lifespan and worker startup revoke activation; neither resumes it.
    live_recovery = live.recover()
    store.put('live_worker_recovery', {'armed':False, 'readiness':live_recovery.get('readiness', {})})
    next_scan = 0.0
    last_success = None
    last_error = None
    store.put('worker_recovery', recovery)
    while running:
        now = utcnow()
        job = store.claim(['scan','demo'])
        info = {'pid':os.getpid(), 'heartbeat':now.isoformat(), 'status':'running', 'source':config.SOURCE,
                'strategy_id':active_strategy(store)['id'],
                'live_write_enabled':live._may_write(),
                'live_control_version':LIVE_CONTROL_VERSION, 'live_control_id':live_control_identity(live),
                'last_success':last_success, 'last_error':last_error, 'interval_seconds':config.SCAN_INTERVAL,
                'independent_process':True, 'recovered':bool(recovery.get('ok'))}
        store.put('worker', info)
        try:
            retired = retire_legacy_job(store, job)
            if retired:
                last_error = 'STRATEGY_RETIRED'
            elif job and job['payload'].get('source', config.SOURCE) != config.SOURCE:
                raise ValueError('Queued job source differs from this worker account namespace')
            elif config.SOURCE=='alpaca' and (job or time.monotonic() >= next_scan):
                result = scan_cycle(engine, market, None, live, store)
                if result['status']=='SUCCESS':
                    last_success=utcnow().isoformat()
                    last_error=None
                elif result['status']=='DEGRADED': last_error='market_data_degraded'
                elif result['status']=='MARKET_CLOSED': last_error=None
                if job: store.finish(job['id'], result)
                next_scan = time.monotonic() + max(5, int(engine.snapshot()['settings']['params'].get('scan_interval_seconds',config.SCAN_INTERVAL)))
            elif job:
                store.finish(job['id'], {'error':'STRATEGY_RETIRED', 'message':'No scan performed: V2 requires Alpaca data'}, 'failed')
        except Exception as exc:
            last_error = type(exc).__name__
            if job: store.finish(job['id'], {'error':last_error,'message':'Inspect audit for the failed run'}, 'failed')
        store.put('worker', {**info, 'heartbeat':utcnow().isoformat(), 'last_success':last_success,
                            'last_error':last_error, 'status':'degraded' if last_error else ('source_unavailable' if config.SOURCE!='alpaca' else 'running')})
        if once: break
        # One-second interruptible tick. Only scan on configured schedule or queued work.
        time.sleep(1)
    remaining = engine.snapshot()['orders']
    store.put('worker', {'pid':os.getpid(),'heartbeat':utcnow().isoformat(),'status':'stopped','source':config.SOURCE,
                        'last_success':last_success,'independent_process':True,
                        'remaining_orders':[o['id'] for o in remaining if o['status'] not in {'FILLED','CANCELLED','REJECTED','EXPIRED'}],
                        'notice':'Service stop does not cancel or liquidate broker positions. Local protection is offline.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true')
    main(parser.parse_args().once)
