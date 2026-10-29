"""Independent Streamable HTTP MCP discovery and schema-bound adapter.

No broker method or argument name is assumed. Read-only methods require an explicit
mapping to a discovered schema. Phase one deliberately cannot submit live orders.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from threading import RLock
from typing import Any
import httpx
from jsonschema import validate
from .robinhood_oauth import RobinhoodOAuth, RobinhoodOAuthError

OPERATIONS = ('accounts', 'balances', 'positions', 'orders', 'fills', 'tradability',
              'preview', 'submit', 'cancel')
WRITE_OPERATIONS = {'submit', 'cancel'}
MCP_ENDPOINT = 'https://agent.robinhood.com/mcp/trading'
TOKEN_ENDPOINT = 'https://api.robinhood.com/oauth2/token/'

class MCPUnavailable(RuntimeError):
    pass

class MCPAuthenticationRequired(MCPUnavailable):
    pass

class LiveExecutionDisabled(MCPUnavailable):
    pass

class TokenProvider:
    """Reads a rotated server token file, or refreshes using explicitly configured OAuth.

    Tokens never leave this class except into the HTTP Authorization header. Refresh
    persistence is the operator's secret-store responsibility; no token is logged.
    """
    def __init__(self, oauth=None, *, allow_legacy=True):
        self.oauth = oauth or RobinhoodOAuth()
        self.allow_legacy = allow_legacy
        self.access_token = os.getenv('ROBINHOOD_ACCESS_TOKEN', '')
        self.refresh_token = os.getenv('ROBINHOOD_REFRESH_TOKEN', '')
        self.expires_at = 0.0

    def get(self, force_refresh=False) -> str:
        # A platform OAuth grant is independent of grants held by other MCP clients.
        try:
            if self.oauth.status().get('configured'):
                return self.oauth.get_token()
        except RobinhoodOAuthError:
            raise MCPAuthenticationRequired('Robinhood authorization needs renewal; reconnect required') from None
        if not self.allow_legacy:
            return ''
        token_file = os.getenv('ROBINHOOD_TOKEN_FILE')
        if token_file:
            path = Path(token_file)
            if path.stat().st_mode & 0o077:
                raise MCPUnavailable('Token file must have permission 0600')
            return path.read_text().strip()
        if force_refresh or (self.expires_at and time.time() >= self.expires_at - 60):
            url = os.getenv('ROBINHOOD_OAUTH_TOKEN_URL')
            if not url or not self.refresh_token:
                raise MCPAuthenticationRequired('OAuth renewal unavailable; reconnect required')
            if url != TOKEN_ENDPOINT:
                raise MCPUnavailable('OAuth token endpoint must be the official Robinhood endpoint')
            try:
                r = httpx.post(url, data={'grant_type': 'refresh_token', 'refresh_token': self.refresh_token,
                                       'client_id': os.getenv('ROBINHOOD_OAUTH_CLIENT_ID', '')}, timeout=15, follow_redirects=False)
                r.raise_for_status()
                payload = r.json()
                self.access_token = payload['access_token']
                self.refresh_token = payload.get('refresh_token', self.refresh_token)
                self.expires_at = time.time() + int(payload.get('expires_in', 1800))
            except Exception:
                raise MCPAuthenticationRequired('OAuth renewal failed; execution must remain paused') from None
        return self.access_token

class MCPClient:
    def __init__(self, url=None, token_provider=None, http_client=None):
        self.url = url or os.getenv('ROBINHOOD_MCP_URL', 'https://agent.robinhood.com/mcp/trading')
        self.tokens = token_provider or TokenProvider()
        self.http = http_client or httpx.Client(timeout=15, follow_redirects=False)
        self.session_id = None
        self.protocol_version = '2025-03-26'
        self.request_id = 0
        self.tools: dict[str, dict] = {}
        self.initialized = False
        self.discovery_time = None
        self.last_error = None
        self.oauth_resource_metadata = None
        self.lock = RLock()

    def reset(self):
        self.session_id = None
        self.initialized = False
        self.tools = {}
        self.discovery_time = None

    def _rpc(self, method: str, params=None, notification=False) -> Any:
        if self.url != MCP_ENDPOINT:
            raise MCPUnavailable('MCP endpoint must be the official Robinhood endpoint')
        self.request_id += 1
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        try:
            token = self.tokens.get()
        except MCPAuthenticationRequired:
            self.reset()
            self.last_error = 'authorization_required'
            raise
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        if method != 'initialize':
            headers['MCP-Protocol-Version'] = self.protocol_version
        body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification:
            body['id'] = self.request_id
        # No blind retry: POST may be a non-idempotent broker action.
        try:
            response = self.http.post(self.url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError):
            self.last_error = 'transport_uncertain'
            raise MCPUnavailable('MCP transport uncertain; reconcile before any retry') from None
        if response.status_code in (401, 403):
            # Keep only the standard public metadata URL, never the response body.
            import re
            match = re.search(r'resource_metadata="(https://[^" ]+)"', response.headers.get('www-authenticate', ''))
            if match:
                metadata = match.group(1)
                if metadata in {'https://agent.robinhood.com/.well-known/oauth-protected-resource',
                                 'https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading'}:
                    self.oauth_resource_metadata = metadata
            self.reset()
            self.last_error = 'authorization_required'
            raise MCPAuthenticationRequired('Robinhood authorization required; no tools discovered')
        if response.status_code == 404 and self.session_id:
            self.reset()
            self.last_error = 'session_expired'
            raise MCPUnavailable('MCP session expired; rediscover before retrying read operations')
        if response.status_code >= 300:
            raise MCPUnavailable(f'MCP returned HTTP {response.status_code}; response redacted')
        self.session_id = response.headers.get('mcp-session-id', self.session_id)
        if notification or response.status_code == 202:
            return {}
        if 'text/event-stream' in response.headers.get('content-type', ''):
            messages = []
            for event in response.text.replace('\r\n', '\n').split('\n\n'):
                data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                if data:
                    messages.append(json.loads(data))
            payload = next((m for m in messages if m.get('id') == body['id']), None)
            if payload is None:
                raise MCPUnavailable('MCP stream ended without response; result unknown')
        else:
            payload = response.json()
        if payload.get('error'):
            raise MCPUnavailable(f'MCP protocol error {payload["error"].get("code")}; details redacted')
        return payload.get('result', {})

    def discover(self) -> dict:
        with self.lock:
            self.reset()
            try:
                result = self._discover()
                self.initialized = True
                return result
            except Exception:
                self.reset()
                raise

    def _discover(self) -> dict:
        self.tools = {}
        result = self._rpc('initialize', {'protocolVersion': self.protocol_version, 'capabilities': {},
                            'clientInfo': {'name': 'northstar-trading', 'version': '0.1.0'}})
        self.protocol_version = result.get('protocolVersion', self.protocol_version)
        self._rpc('notifications/initialized', notification=True)
        cursor = None
        seen = set()
        for _ in range(100):
            result = self._rpc('tools/list', {'cursor': cursor} if cursor else {})
            for tool in result.get('tools', []):
                if isinstance(tool.get('name'), str) and isinstance(tool.get('inputSchema'), dict):
                    self.tools[tool['name']] = tool
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise MCPUnavailable('Repeated MCP discovery cursor')
            seen.add(cursor)
        else:
            raise MCPUnavailable('MCP discovery exceeded pagination bound')
        self.discovery_time = datetime.now(timezone.utc).isoformat()
        self.last_error = None
        return {'discovered_at': self.discovery_time, 'tools': list(self.tools.values())}

    def call_discovered(self, name: str, arguments: dict):
        with self.lock:
            if not self.initialized or name not in self.tools:
                raise MCPUnavailable('Tool is absent from the authenticated discovery result')
            validate(arguments, self.tools[name]['inputSchema'])
            result = self._rpc('tools/call', {'name': name, 'arguments': arguments})
            if result.get('isError'):
                raise MCPUnavailable('MCP tool reported failure; details redacted')
            return result

class RobinhoodMCPBroker:
    """Internal API mapped only after actual tool schemas have been inspected.

    Mapping file: {operation: {tool: 'discovered name', arguments: {actualParam:
    '$account_id'}, verified: true}}. Response normalization requires phase-two
    account-specific fixtures. Unknown response enums never become fill records.
    """
    def __init__(self, client=None, mapping=None):
        self.client = client or MCPClient()
        if mapping is None:
            path = os.getenv('ROBINHOOD_CAPABILITY_MAP')
            mapping = json.loads(Path(path).read_text()) if path else {}
        self.mapping = mapping
        self.account_id = os.getenv('ROBINHOOD_EXECUTION_ACCOUNT')

    def capabilities(self) -> dict:
        return {'name': 'Robinhood Trading MCP', 'endpoint': self.client.url,
                'connected': self.client.initialized, 'discovered_at': self.client.discovery_time,
                'live_enabled': False, 'execution_account_bound': False,
                'execution_account_configured': bool(self.account_id),
                'approval_policy': 'broker_enforced', 'last_error': self.client.last_error,
                'operations': {op: {'mapped': op in self.mapping,
                                   'discovered': self.mapping.get(op, {}).get('tool') in self.client.tools,
                                   'verified': self.mapping.get(op, {}).get('verified', False),
                                   'enabled': False if op in WRITE_OPERATIONS else bool(self.client.initialized and self.mapping.get(op, {}).get('verified') and self.mapping.get(op, {}).get('tool') in self.client.tools)}
                               for op in OPERATIONS},
                'unknown_capabilities': ['client_order_id', 'order_push', 'fractional_rules', 'sandbox', 'replace_order', 'fill_cursor'],
                'blockers': ['Phase-two response normalization and account-scope verification not commissioned',
                             'Authenticated tools/list and OAuth renewal must be validated with the user account'],
                'token_configured': self._token_configured(),
                'credentials': 'server_only'}

    def _token_configured(self):
        tokens = self.client.tokens
        if hasattr(tokens, 'oauth') and tokens.oauth.status().get('authorized'):
            return True
        return bool(getattr(tokens, 'allow_legacy', False) and
                    (os.getenv('ROBINHOOD_ACCESS_TOKEN') or os.getenv('ROBINHOOD_TOKEN_FILE')))

    def discover(self):
        return self.client.discover()

    def _invoke(self, operation, **values):
        if operation in WRITE_OPERATIONS:
            raise LiveExecutionDisabled('Live execution is locked in phase one; no broker write was sent')
        binding = self.mapping.get(operation)
        if not binding or not binding.get('verified'):
            raise MCPUnavailable(f'{operation} requires a reviewed mapping to a discovered schema')
        discovered = self.client.tools.get(binding.get('tool'), {})
        annotations = discovered.get('annotations', {})
        # Phase one permits discovery and explicitly read-only mapped calls only.
        # Hints are an additional fail-closed check, not proof of account authority.
        if annotations.get('readOnlyHint') is not True or annotations.get('destructiveHint') is True:
            raise MCPUnavailable('Phase-one mapped calls require a discovered read-only, non-destructive tool')
        values.setdefault('account_id', self.account_id)
        def resolve(value):
            if isinstance(value, str) and value.startswith('$'):
                if value[1:] not in values or values[value[1:]] is None:
                    raise MCPUnavailable(f'Missing normalized input {value[1:]}')
                return values[value[1:]]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value
        return self.client.call_discovered(binding['tool'], resolve(binding.get('arguments', {})))

    def accounts(self): return self._invoke('accounts')
    def balances(self, account_id=None): return self._invoke('balances', account_id=account_id or self.account_id)
    def positions(self, account_id=None): return self._invoke('positions', account_id=account_id or self.account_id)
    def orders(self, account_id=None, **filters): return self._invoke('orders', account_id=account_id or self.account_id, **filters)
    def fills(self, account_id=None, **filters): return self._invoke('fills', account_id=account_id or self.account_id, **filters)
    def tradability(self, symbol): return self._invoke('tradability', symbol=symbol)
    def preview(self, intent): return self._invoke('preview', intent=intent)
    def submit(self, intent): return self._invoke('submit', intent=intent)
    def cancel(self, order_id): return self._invoke('cancel', order_id=order_id)

    # BrokerAdapter protocol: return raw evidence until phase-two normalization is commissioned.
    def get_accounts(self): return self.accounts()
    def get_account(self, account_id): return self.balances(account_id)
    def get_positions(self, account_id): return self.positions(account_id)
    def get_orders(self, account_id): return self.orders(account_id)
    def get_fills(self, account_id): return self.fills(account_id)
    def get_tradability(self, symbol): return self.tradability(symbol)
    def preflight(self, intent): return self.preview(intent)
    def submit_order(self, intent): return self.submit(intent)
    def cancel_order(self, broker_order_id): return self.cancel(broker_order_id)
    def find_order(self, client_order_id):
        raise MCPUnavailable('Client order ID support has not been discovered or verified')
