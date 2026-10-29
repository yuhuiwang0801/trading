"""HTTP/UI boundary. Trading and research continue in independent worker processes."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from . import config
from .calendar import market_state
from .engine import Engine
from .market import MarketService
from .mcp_broker import RobinhoodMCPBroker, MCPClient, TokenProvider, MCPUnavailable
from .robinhood_oauth import RobinhoodOAuth, RobinhoodOAuthError
from .robinhood_verify import verify_robinhood, _VerificationFailure
from .runtime import RuntimeStore, utcnow
from .live_runtime import create_live_service, LIVE_CONTROL_VERSION, live_control_identity
from .strategy_registry import strategies, active_strategy, runtime_view, enqueue_refresh, daily_data_ready, entry_feasibility


class ControlRequest(BaseModel):
    action: str = Field(max_length=40)
class SignalRequest(BaseModel):
    status: str = Field(max_length=20)
class AdoptRequest(BaseModel):
    atr: str
    ma20: str
class LoginRequest(BaseModel):
    token: str = Field(max_length=1024)
class ConnectRequest(BaseModel):
    pass

class LiveConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbols: list[str] = Field(min_length=1, max_length=30)
    max_order_amount: str = Field(min_length=1, max_length=32)
    max_daily_added: str = Field(min_length=1, max_length=32)
    max_positions: int = Field(ge=1, le=30, strict=True)

class ArmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: str = Field(max_length=64)
    configuration_revision: int = Field(ge=1, strict=True)

class StrategySelectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy_id: Literal['reversal_v2']


class OAuthAccessLogFilter(logging.Filter):
    """Never put an authorization code or state in Uvicorn access logs."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split('?', 1)[0] == '/broker/oauth/callback':
                args[2] = '/broker/oauth/callback'
                record.args = tuple(args)
        return True


logging.getLogger('uvicorn.access').addFilter(OAuthAccessLogFilter())


def create_app(database_url=None, demo_database_url=None, source=None, *, oauth=None, broker=None, live_service=None):
    primary = database_url or config.DATABASE_URL
    demo_db = demo_database_url or config.DEMO_DATABASE_URL
    selected_source = source or config.SOURCE
    if selected_source not in {'demo', 'alpaca'}:
        raise ValueError('MARKET_SOURCE must be demo or alpaca')
    store = RuntimeStore(primary)
    engine = Engine(demo_db if selected_source == 'demo' else primary)
    market = MarketService.from_env()
    oauth = oauth or RobinhoodOAuth()
    broker = broker or RobinhoodMCPBroker(client=MCPClient(token_provider=TokenProvider(oauth, allow_legacy=False)))
    live = live_service or create_live_service(primary, client=broker.client, oauth=oauth)
    access_token = os.getenv('APP_ACCESS_TOKEN', '')
    session_token = secrets.token_urlsafe(32)
    allowed_origins = os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173').split(',')

    @asynccontextmanager
    async def lifespan(app):
        if os.getenv('API_HOST', '127.0.0.1') not in {'127.0.0.1', 'localhost', '::1'} and not access_token:
            raise RuntimeError('APP_ACCESS_TOKEN required when API_HOST is not loopback')
        # API and worker restarts both revoke prior activation. Existing orders
        # remain visible for read reconciliation; restart never submits or cancels.
        live.disarm()
        yield

    app = FastAPI(title='Northstar Trading API', version='0.1.0', lifespan=lifespan,
                  description='Paper execution and auditable research. Live execution not commissioned.')
    app.state.engine = engine
    app.state.store = store
    app.state.market = market
    app.state.broker = broker
    app.state.robinhood_oauth = oauth
    app.state.live = live
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True,
                       allow_methods=['GET','POST','PATCH'], allow_headers=['Content-Type','Authorization'])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv('ALLOWED_HOSTS','localhost,127.0.0.1,testserver').split(','))

    @app.middleware('http')
    async def guard(request: Request, call_next):
        if request.url.path.startswith('/api'):
            if not access_token and request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
                return JSONResponse({'detail':'Remote API access requires server authentication'}, status_code=403)
            origin = request.headers.get('origin')
            same_origin = str(request.base_url).rstrip('/')
            if request.method in {'POST','PATCH','DELETE','PUT'} and origin and origin not in allowed_origins and origin != same_origin:
                return JSONResponse({'detail':'Origin is not allowed'}, status_code=403)
            if access_token and request.url.path not in {'/api/session', '/api/health'} and request.method != 'OPTIONS':
                bearer = request.headers.get('Authorization', '').removeprefix('Bearer ')
                session = request.cookies.get('northstar_session', '')
                if not (hmac.compare_digest(bearer, access_token) or hmac.compare_digest(session, session_token)):
                    return JSONResponse({'detail':'Authentication required'}, status_code=401)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api') or request.url.path == '/broker/oauth/callback':
            response.headers['Cache-Control'] = 'no-store'
        if request.url.path == '/broker/oauth/callback':
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)
    @app.exception_handler(KeyError)
    async def missing_item(request, exc):
        return JSONResponse({'detail':'Record not found'}, status_code=404)
    @app.exception_handler(MCPUnavailable)
    async def mcp_unavailable(request, exc):
        return JSONResponse({'detail':str(exc), 'live_enabled':False}, status_code=503)
    @app.exception_handler(RobinhoodOAuthError)
    async def oauth_unavailable(request, exc):
        return JSONResponse({'detail':'Robinhood authorization could not be completed. Reconnect from settings.',
                             'live_enabled':False}, status_code=503)

    @app.exception_handler(_VerificationFailure)
    async def invalid_broker_contract(request, exc):
        return JSONResponse({'detail':exc.message, 'code':exc.code}, status_code=409)

    @app.post('/api/session')
    def login(body: LoginRequest, request: Request, response: Response):
        if access_token and not hmac.compare_digest(body.token, access_token):
            raise HTTPException(401, 'Invalid access token')
        response.set_cookie('northstar_session', session_token, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', max_age=8*3600)
        return {'authenticated':True}

    @app.get('/api/health')
    def health():
        heartbeat = store.get('worker', {})
        return {'status':'ok', 'version':'0.1.0', 'source':selected_source,
                'worker':heartbeat.get('status', 'not_started'),
                'live_enabled':bool(live.snapshot().get('control', {}).get('armed'))}

    def worker_status():
        result = store.get('worker', {'status':'not_started', 'heartbeat':None})
        if result.get('heartbeat'):
            try:
                age = (utcnow() - datetime.fromisoformat(result['heartbeat'])).total_seconds()
                result = {**result, 'heartbeat_age_seconds':round(age,1),
                          'status':result.get('status') if 0 <= age <= 75 else 'offline'}
            except (ValueError, TypeError):
                result = {**result, 'heartbeat_age_seconds':None, 'status':'offline'}
        return result

    def provider_status():
        if selected_source == 'demo':
            return {'source':'demo','provider':'synthetic','quality':'synthetic','configured':True,
                    'feed':'deterministic replay','label':'演示回放 / SYNTHETIC REPLAY',
                    'real_time':False,'profitability':'NOT_VALIDATED','scope':'8 synthetic securities',
                    'limitations':['Synthetic prices only. No investment performance claim.']}
        return {**market.status(), **store.get('market_status', {}), 'source':'alpaca',
                'quality':store.get('last_market_scan', {}).get('quality','unavailable')}

    def current_reversal_packet():
        from .reversal_runtime import load_runtime_packet
        return load_runtime_packet(data_dir=config.DATA / 'reversal' / 'runtime_v2')

    def strategy_state():
        selected = active_strategy(store)
        live_snapshot = live.snapshot()
        packet = current_reversal_packet()
        pending = next((job for job in store.jobs('reversal_refresh') if job['status'] in {'queued', 'running'}), None)
        return {'active_strategy': selected, 'strategies': strategies(), 'runtime': runtime_view(packet),
                'refresh_job': pending, 'live_armed': bool(live_snapshot.get('control', {}).get('armed')),
                'authorized_symbols': live_snapshot.get('configuration', {}).get('symbols', []),
                'selection_enables_live': False}

    @app.get('/api/strategies')
    def get_strategies():
        return strategy_state()

    @app.post('/api/strategies/select')
    def select_strategy(body: StrategySelectionRequest):
        # The live ledger validates armed state, pending instructions and owned
        # holdings under its own lock; local selection sends no broker requests.
        live.select_strategy(body.strategy_id)
        store.put('active_strategy', {'id': body.strategy_id, 'selected_at': utcnow().isoformat()})
        if body.strategy_id == 'reversal_v2' and selected_source == 'alpaca':
            enqueue_refresh(store)
        return strategy_state()

    @app.post('/api/strategies/refresh', status_code=202)
    def refresh_strategy(body: ConnectRequest):
        if active_strategy(store)['id'] != 'reversal_v2':
            raise HTTPException(409, '当前策略不是V2；请使用原策略候选池更新')
        if selected_source != 'alpaca':
            raise HTTPException(409, 'V2需要Alpaca真实历史行情')
        return enqueue_refresh(store)

    @app.get('/api/state')
    def state():
        snapshot = engine.snapshot()
        jobs = store.jobs('backtest')
        frame = store.get('demo_last_frame', {}) if selected_source == 'demo' else store.get('last_market_scan', {})
        selected = active_strategy(store)
        if selected['id'] == 'reversal_v2':
            snapshot['candidates'] = runtime_view(current_reversal_packet())['candidates']
        return {**snapshot, 'active_strategy':selected, 'market':market_state(), 'provider':provider_status(), 'worker':worker_status(),
                'market_verification':store.get('market_verification'),
                'research_worker':store.get('research_worker', {}), 'backtests':jobs,
                'jobs':store.jobs(), 'equity_curve':store.get('demo_equity', []) if selected_source == 'demo' else store.get('paper_equity', []),
                'demo_clock':frame.get('timestamp') if selected_source == 'demo' else None,
                'data_timestamp':frame.get('timestamp'), 'account_namespace':'demo' if selected_source=='demo' else 'real_data_paper',
                'live_execution':live_status(),
                'strategy_validation':'待验证 / NOT VALIDATED',
                'protection_notice':'Protection rules run locally and require the worker online; no protective order is resting at a broker.',
                'pending_broker_orders':[o for o in snapshot['orders'] if o['status'] not in ['FILLED','CANCELED','CANCELLED','REJECTED','EXPIRED']]}

    @app.get('/api/market/status')
    def get_market_status(): return {**provider_status(), 'calendar':market_state()}

    @app.get('/api/market/verification')
    def get_market_verification(): return store.get('market_verification')

    @app.post('/api/market/verify', status_code=202)
    def verify_market():
        pending = next((job for job in store.jobs('market_verify') if job['status'] in {'queued', 'running'}), None)
        return pending or store.enqueue('market_verify', {'source':'alpaca'})

    @app.get('/api/broker/capabilities')
    def get_capabilities(): return broker.capabilities()

    def connection_state():
        connection = oauth.status()
        capabilities = broker.capabilities()
        if capabilities.get('last_error') == 'authorization_required':
            connection = {**connection, 'authorized':False, 'blocker':'authorization_required'}
        verification = store.get('robinhood_verification') if connection['authorized'] else None
        if verification:
            try:
                age = max(0, (utcnow() - datetime.fromisoformat(verification['checked_at'].replace('Z', '+00:00'))).total_seconds())
            except (KeyError, ValueError, TypeError):
                age = None
            verification = {**verification, 'age_seconds':round(age, 1) if age is not None else None,
                            'stale':age is None or age > 300}
        if not connection['authorized']:
            stage = 'authorization_required'
        elif (capabilities['connected'] and verification and verification.get('account_verified')
              and verification.get('status') != 'failed'
              and not any(check.get('status') == 'failed' for check in verification.get('checks', []))):
            stage = 'account_verified'
        elif capabilities['connected']:
            stage = 'schema_review_required'
        else:
            stage = 'discovery_required'
        blockers = list(verification.get('blockers', [])) if verification else ['execution_account_unverified', 'order_reconciliation_unverified']
        if stage != 'account_verified': blockers.insert(0, stage)
        if verification and verification.get('stale'): blockers.append('account_snapshot_stale')
        return {'connection':connection, 'capabilities':capabilities, 'verification':verification,
                'readiness':{'stage':stage, 'live_enabled':False,
                             'tool_count':len(broker.client.tools),
                             'blockers':list(dict.fromkeys(blockers))}}

    @app.get('/api/broker/connection')
    def get_connection(): return connection_state()

    def require_robinhood():
        if not oauth.status().get('authorized'):
            raise HTTPException(409, '请先连接 Robinhood')

    def live_worker_blockers():
        worker = worker_status()
        blockers = []
        age = worker.get('heartbeat_age_seconds')
        if (worker.get('status') not in {'running', 'degraded'} or
                not isinstance(age, (int, float)) or not 0 <= age <= 75):
            blockers.append('worker_offline')
        if (type(worker.get('live_control_version')) is not int or
                worker['live_control_version'] != LIVE_CONTROL_VERSION):
            blockers.append('worker_restart_required')
        if worker.get('live_control_id') != live_control_identity(live):
            blockers.append('worker_ledger_mismatch')
        if worker.get('source') != 'alpaca':
            blockers.append('worker_source_mismatch')
        return blockers

    def live_status():
        snapshot = live.snapshot()
        readiness = snapshot.get('readiness', {})
        blockers = list(readiness.get('blockers', []))
        if selected_source != 'alpaca': blockers.append('real_market_source_required')
        if not oauth.status().get('authorized'): blockers.append('authorization_required')
        blockers.extend(live_worker_blockers())
        configured = snapshot.get('configuration', {})
        selected = active_strategy(store)
        packet = {}
        if configured.get('strategy_id', 'trend_breakout') != selected['id']:
            blockers.append('selected_strategy_scope_mismatch')
        if selected['id'] == 'reversal_v2':
            packet = current_reversal_packet()
            if not daily_data_ready(packet) and not v2_exit_recovery(snapshot):
                blockers.append('reversal_daily_data_not_ready')
        armed = bool(snapshot.get('control', {}).get('armed'))
        account = snapshot.get('account') or {}
        return {**snapshot, 'enabled':armed, 'selected_strategy':selected,
                'entry_feasibility':entry_feasibility(packet, configured, account),
                'activation_available':readiness.get('server_write_enabled') is True,
                'activation_mode':'ui_confirmation',
                'armed':armed, 'mode':'live' if armed else 'dry_run',
                'config':configured,
                'configuration_revision':configured.get('revision', 0),
                'account_masked':account.get('masked_number'),
                'last_preview':snapshot.get('preview'), 'last_run':snapshot.get('last_scan'),
                'last_quote_check':snapshot.get('last_quote_check'),
                'orders_sent':len(snapshot.get('orders', [])),
                'readiness':{**readiness, 'ready':bool(readiness.get('ready_to_arm', readiness.get('ready', False))) and not blockers,
                             'ready_to_arm':bool(readiness.get('ready_to_arm', readiness.get('ready', False))) and not blockers,
                             'blockers':list(dict.fromkeys(blockers))},
                'activation_confirmation':'ENABLE LIVE AUTO TRADING',
                'restart_requires_reactivation':True}

    @app.get('/api/live')
    def get_live(): return live_status()

    def v2_exit_recovery(snapshot):
        # Daily outages must not prevent an explicitly re-armed worker from
        # servicing a latched exit or the independent calendar holding cap.
        terminal = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED', 'PREVIEW_BLOCKED', 'ABANDONED'}
        return bool(snapshot.get('positions') or
                    any(i.get('status') not in terminal for i in snapshot.get('intents', [])))

    @app.get('/api/live/workspace')
    def get_live_workspace():
        from .live_workspace import build_live_workspace
        return build_live_workspace(live.snapshot(), authorized=oauth.status().get('authorized') is True)

    @app.post('/api/live/verify')
    def verify_live(body: ConnectRequest):
        require_robinhood()
        live.reconcile()
        return live_status()

    @app.post('/api/live/preview', include_in_schema=False)
    def preview_live():
        # The retired manual tool must not issue even a broker preview. Automatic
        # execution retains its own mandatory adapter preview and risk checks.
        raise HTTPException(410, '手动订单预览已移除；自动交易会自行完成券商预检')

    @app.post('/api/live/config')
    def configure_live(body: LiveConfigRequest):
        require_robinhood()
        live.configure({**body.model_dump(), 'strategy_id':active_strategy(store)['id']}, params=engine.snapshot()['settings']['params'])
        return live_status()

    @app.post('/api/live/prepare')
    def prepare_live(body: LiveConfigRequest):
        require_robinhood()
        if selected_source != 'alpaca':
            raise HTTPException(409, '实盘需要已接入的真实行情')
        live.prepare({**body.model_dump(), 'strategy_id':active_strategy(store)['id']}, params=engine.snapshot()['settings']['params'])
        return live_status()

    @app.post('/api/live/prepare-strategy')
    def prepare_selected_strategy(body: StrategySelectionRequest):
        """Prepare trusted strategy policy; this endpoint never grants write authority."""
        require_robinhood()
        if selected_source != 'alpaca':
            raise HTTPException(409, '实盘需要已接入的真实行情')
        if body.strategy_id != active_strategy(store)['id']:
            raise HTTPException(409, '策略选择已变化，请重新选择并检查')
        live.prepare_strategy(body.strategy_id)
        return live_status()

    @app.post('/api/live/arm')
    def arm_live(body: ArmRequest):
        require_robinhood()
        if selected_source != 'alpaca':
            raise HTTPException(409, '实盘需要已接入的真实行情')
        if not live.readiness().get('server_write_enabled'):
            raise HTTPException(409, '当前部署不支持实盘启用')
        worker_blocks = live_worker_blockers()
        if worker_blocks:
            raise HTTPException(409, '后台执行服务尚未就绪，请刷新检查并查看开启条件')
        selected = active_strategy(store)
        if live.snapshot().get('configuration', {}).get('strategy_id', 'trend_breakout') != selected['id']:
            raise HTTPException(409, '当前策略与已确认交易范围不一致，请重新检查交易设置')
        if (selected['id'] == 'reversal_v2' and not daily_data_ready(current_reversal_packet())
                and not v2_exit_recovery(live.snapshot())):
            raise HTTPException(409, 'V2最新日线与基本面资料尚未就绪')
        live.arm(body.confirmation, body.configuration_revision)
        return live_status()

    @app.post('/api/live/disarm')
    def disarm_live(body: ConnectRequest):
        live.disarm()
        return live_status()

    @app.post('/api/broker/connect')
    def connect_broker(body: ConnectRequest, request: Request, response: Response):
        # The registered callback is fixed, never built from an untrusted Host header.
        if str(request.base_url).rstrip('/') != 'http://127.0.0.1:8010':
            raise HTTPException(409, 'Open http://127.0.0.1:8010/#settings to connect Robinhood')
        result = oauth.start('http://127.0.0.1:8010/broker/oauth/callback')
        response.set_cookie('northstar_robinhood_state', result['state'], httponly=True,
                            secure=False, samesite='lax', max_age=480, path='/broker/oauth/callback')
        return {'authorization_url':result['authorization_url']}

    @app.get('/broker/oauth/callback', include_in_schema=False)
    def broker_callback(request: Request):
        state = request.query_params.get('state', '')
        cookie = request.cookies.get('northstar_robinhood_state', '')
        result = 'authorization_failed'
        if (request.client and request.client.host in {'127.0.0.1', '::1', 'testclient'}
                and state and cookie and state.isascii() and cookie.isascii()
                and hmac.compare_digest(state, cookie)
                and not request.query_params.get('error')):
            try:
                live.disarm()
                with broker.client.lock:
                    oauth.complete(request.query_params.get('code', ''), state,
                                   request.query_params.get('iss', ''))
                    broker.client.reset()
                    broker.client.last_error = None
                    store.put('robinhood_verification', None)
                result = 'connected'
                store.put('robinhood_connection', {'status':'authorized', 'at':utcnow().isoformat()})
            except RobinhoodOAuthError:
                pass
        if result != 'connected' and state and cookie and state.isascii() and cookie.isascii() and hmac.compare_digest(state, cookie):
            oauth.cancel_pending(state)
        redirect = RedirectResponse('/?broker='+result+'#settings', status_code=303)
        redirect.delete_cookie('northstar_robinhood_state', path='/broker/oauth/callback')
        return redirect

    @app.post('/api/broker/disconnect')
    def disconnect_broker(body: ConnectRequest):
        live.disarm()
        with broker.client.lock:
            oauth.disconnect()
            broker.client.reset()
            broker.client.last_error = None
            store.put('robinhood_verification', None)
        store.put('robinhood_connection', {'status':'disconnected', 'at':utcnow().isoformat()})
        return connection_state()

    @app.post('/api/broker/discover')
    def discover():
        with broker.client.lock:
            store.put('robinhood_verification', None)
            try:
                result = broker.discover()
                store.put('mcp_discovery', {'status':'discovered', 'at':utcnow().isoformat(), 'tools':result['tools']})
                return {'discovered_at':result['discovered_at'], 'tool_count':len(result['tools']), **connection_state()}
            except MCPUnavailable as exc:
                store.put('mcp_discovery', {'status':'not_authorized_or_unavailable','at':utcnow().isoformat(), 'error':str(exc)})
                raise

    @app.post('/api/broker/verify')
    def verify_broker(body: ConnectRequest):
        # One coherent, read-only snapshot. It never touches the paper ledger,
        # binds an execution account, or changes any broker approval setting.
        with broker.client.lock:
            if not oauth.status().get('authorized'):
                raise HTTPException(409, 'Connect Robinhood before verifying the account')
            store.put('robinhood_verification', None)
            try:
                report = verify_robinhood(broker.client)
            except Exception:
                raise HTTPException(503, 'Robinhood account verification failed; reconnect or retry') from None
            store.put('robinhood_verification', report)
            if broker.client.initialized:
                store.put('mcp_discovery', {'status':'discovered', 'at':utcnow().isoformat(),
                                           'tools':list(broker.client.tools.values())})
            return report

    @app.post('/api/control')
    def control(body: ControlRequest): return engine.control(body.action)

    @app.patch('/api/settings')
    def settings(patch: dict): return engine.update_settings(patch)

    @app.post('/api/orders/{order_id}/cancel')
    def cancel(order_id: str): return engine.cancel_order(order_id)

    @app.patch('/api/signals/{signal_id}')
    def signal(signal_id: str, body: SignalRequest): return engine.mark_signal(signal_id, body.status)

    @app.post('/api/positions/{symbol}/adopt')
    def adopt(symbol: str, body: AdoptRequest): return engine.adopt_position(symbol, body.atr, body.ma20)

    @app.post('/api/scan', status_code=202)
    def scan():
        if selected_source != 'alpaca':
            raise HTTPException(410, '趋势演示已下架；V2需要Alpaca真实历史行情')
        return enqueue_refresh(store)

    @app.post('/api/demo/run', include_in_schema=False)
    def demo():
        raise HTTPException(410, '趋势演示已下架')

    @app.get('/api/backtests')
    def backtests(): return store.jobs('backtest')

    @app.get('/api/research/reversal/latest')
    def reversal_research_latest():
        # This view only reads a published research artifact. It never starts a
        # run, changes strategy settings, or touches either execution ledger.
        directory = config.DATA / 'reversal' / 'latest'
        try:
            report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'status': 'not_run', 'runs': [], 'report_available': False}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return JSONResponse({'status': 'unavailable', 'runs': [],
                                 'detail': 'Research report could not be read'}, status_code=503)
        summary = report.get('summary', report) if isinstance(report, dict) else None
        if not isinstance(summary, dict):
            return JSONResponse({'status': 'unavailable', 'runs': [],
                                 'detail': 'Research report has an invalid format'}, status_code=503)
        fields = ('status', 'title', 'period', 'data', 'benchmark', 'limitations', 'generated_at')
        result = {key: summary[key] for key in fields if key in summary}
        metrics = ('total_return', 'max_drawdown', 'cagr', 'sharpe', 'ending_equity')
        result['runs'] = [
            {'experiment': run.get('experiment'), 'cost_bps': run.get('cost_bps'),
             'metrics': {key: run['metrics'][key] for key in metrics if key in run['metrics']}}
            for run in summary.get('runs', [])
            if isinstance(run, dict) and isinstance(run.get('metrics'), dict)
        ] if isinstance(summary.get('runs', []), list) else []
        result['report_available'] = (directory / 'report.html').is_file()
        return result

    @app.get('/api/research/reversal/report', response_class=FileResponse)
    def reversal_research_report():
        path = config.DATA / 'reversal' / 'latest' / 'report.html'
        if not path.is_file():
            raise HTTPException(404, 'Research report has not been generated')
        return FileResponse(path, media_type='text/html')

    @app.post('/api/backtests', include_in_schema=False)
    def backtest():
        raise HTTPException(410, '趋势回测已下架；历史记录仍可查询')

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str):
        result = next((j for j in store.jobs() if j['id']==job_id), None)
        if result is None: raise HTTPException(404, 'Job not found')
        return result

    @app.get('/api/audit/export')
    def export():
        # Export the complete ledger, not only the bounded UI snapshot.
        from sqlalchemy import select
        from .models import Audit
        from .paper import row_dict
        with engine.Session() as s:
            rows = [row_dict(row) for row in s.scalars(select(Audit).order_by(Audit.timestamp))]
        data = '\n'.join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
        return Response(data, media_type='application/x-ndjson', headers={'Content-Disposition':'attachment; filename="northstar-audit.jsonl"'})

    @app.get('/api/events')
    async def events(request: Request):
        async def stream():
            while not await request.is_disconnected():
                yield 'data: '+json.dumps({'type':'refresh','timestamp':utcnow().isoformat()})+'\n\n'
                await asyncio.sleep(3)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering':'no'})

    dist = config.ROOT / 'frontend' / 'dist'
    if (dist / 'assets').exists(): app.mount('/assets', StaticFiles(directory=dist/'assets'), name='assets')
    @app.get('/{path:path}', include_in_schema=False)
    def spa(path: str):
        if path.startswith('api/'): raise HTTPException(404, 'Endpoint not found')
        candidate = (dist/path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file(): return FileResponse(candidate)
        if (dist/'index.html').exists(): return FileResponse(dist/'index.html')
        return JSONResponse({'message':'Build frontend with npm --prefix frontend run build, or run Vite on :5173', 'api_docs':'/docs'})
    return app

app = create_app()
