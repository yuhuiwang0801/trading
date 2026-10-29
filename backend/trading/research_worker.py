"""Separate research queue: a long replay cannot block the market monitor."""
import fcntl
import os
import signal
import time
from . import config
from .runtime import RuntimeStore, utcnow
from .alpaca_verify import run_verification
from .strategy_registry import active_strategy, enqueue_refresh, daily_data_ready, retire_legacy_job


def schedule_reversal_refresh(store, now=None):
    """Daily close data work is independent of the executable quote worker."""
    now = now or utcnow()
    if config.SOURCE != 'alpaca' or active_strategy(store)['id'] != 'reversal_v2':
        return None
    from .reversal_runtime import expected_signal_session, load_runtime_packet
    expected = expected_signal_session(now)
    packet = load_runtime_packet(now=now, data_dir=config.DATA / 'reversal' / 'runtime_v2')
    if daily_data_ready(packet) and packet.get('data_session') == expected:
        return None
    last = store.get('reversal_auto_refresh', {})
    if last.get('target_session') == expected:
        try:
            from .calendar import as_utc
            if (now - as_utc(last['attempted_at'])).total_seconds() < 1800:
                return None
        except (ValueError, TypeError, KeyError):
            pass
    job = enqueue_refresh(store)
    store.put('reversal_auto_refresh', {'target_session': expected, 'attempted_at': now.isoformat(), 'job_id': job['id']})
    return job

def main(once=False):
    lock=open(config.DATA/'research.lock','w')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('A research worker is already running on this host')
    store=RuntimeStore(config.DATABASE_URL)
    store.recover_jobs(['backtest', 'market_verify', 'reversal_refresh'])
    running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    next_schedule_check = 0.0
    while running:
        store.put('research_worker',{'pid':os.getpid(),'heartbeat':utcnow().isoformat(),'status':'idle'})
        try:
            if time.monotonic() >= next_schedule_check:
                schedule_reversal_refresh(store)
                next_schedule_check = time.monotonic() + 60
        except Exception as exc:
            store.put('reversal_scheduler_error', {'checked_at': utcnow().isoformat(), 'error': type(exc).__name__})
        job=store.claim(['backtest', 'market_verify', 'reversal_refresh'])
        if job:
            store.put('research_worker',{'pid':os.getpid(),'heartbeat':utcnow().isoformat(),'status':'running','job_id':job['id']})
            try:
                if retire_legacy_job(store, job):
                    pass
                elif job['kind'] == 'reversal_refresh':
                    from .reversal_runtime import build_runtime_packet
                    from .strategy_registry import runtime_view
                    result = build_runtime_packet(data_dir=config.DATA / 'reversal' / 'runtime_v2')
                    store.finish(job['id'], runtime_view(result), 'completed' if daily_data_ready(result) else 'failed')
                else:
                    store.finish(job['id'], run_verification(store))
            except Exception as exc:
                store.finish(job['id'],{'error':type(exc).__name__,'message':'Research run failed; no trading action performed'},'failed')
        if once: break
        time.sleep(1)
    store.put('research_worker',{'pid':os.getpid(),'heartbeat':utcnow().isoformat(),'status':'stopped'})

if __name__=='__main__':main()
