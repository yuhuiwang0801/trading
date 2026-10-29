import httpx
import pytest
from jsonschema import ValidationError
from trading.mcp_broker import MCPClient, RobinhoodMCPBroker, MCPAuthenticationRequired, MCPUnavailable, LiveExecutionDisabled

class Tokens:
    def get(self): return ''

def client(handler):
    return MCPClient(token_provider=Tokens(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

def test_discovery_uses_real_schema_and_pagination():
    seen = []
    def handler(request):
        import json
        body = json.loads(request.content); seen.append(body)
        if body['method'] == 'initialize': result = {'protocolVersion': '2025-03-26'}
        elif body['method'] == 'notifications/initialized': return httpx.Response(202)
        elif body['method'] == 'tools/list':
            result = {'tools': [{'name': 'actual_balance_v2', 'annotations': {'readOnlyHint':True}, 'inputSchema': {'type':'object','properties':{'acct':{'type':'string'}},'required':['acct'],'additionalProperties':False}}]}
        else: result = {'content':[{'type':'text','text':'{}'}]}
        return httpx.Response(200, json={'jsonrpc':'2.0','id':body.get('id'),'result':result}, headers={'Mcp-Session-Id':'session'})
    c = client(handler); c.discover()
    broker = RobinhoodMCPBroker(c, {'balances':{'tool':'actual_balance_v2','arguments':{'acct':'$account_id'},'verified':True}})
    assert broker.get_account('acct1')['content']
    with pytest.raises(ValidationError): c.call_discovered('actual_balance_v2', {'guessed':'wrong'})
    with pytest.raises(MCPUnavailable): c.call_discovered('get_balance_guessed', {})
    assert sum(v['method']=='tools/call' for v in seen) == 1

def test_live_writes_locked_even_with_mapping():
    c = client(lambda request: pytest.fail('No network request allowed'))
    b = RobinhoodMCPBroker(c, {'submit':{'tool':'anything','verified':True}})
    with pytest.raises(LiveExecutionDisabled): b.submit_order({'symbol':'AAPL'})
    with pytest.raises(LiveExecutionDisabled): b.cancel_order('123')

def test_auth_failure_is_clear_no_schema_fabrication():
    c = client(lambda request: httpx.Response(401, headers={'WWW-Authenticate':'Bearer resource_metadata="https://agent.robinhood.com/.well-known/oauth-protected-resource"'}, text='secret-not-for-logs'))
    with pytest.raises(MCPAuthenticationRequired) as e: c.discover()
    assert 'secret' not in str(e.value)
    assert not c.tools
    assert c.oauth_resource_metadata.startswith('https://agent.robinhood.com/')

def test_timeout_is_not_retried():
    count = []
    def handler(request):
        count.append(1); raise httpx.ReadTimeout('sensitive data')
    c = client(handler)
    with pytest.raises(MCPUnavailable): c.discover()
    assert len(count)==1

def test_sse_response():
    def handler(request):
        import json
        body = json.loads(request.content)
        if body['method']=='notifications/initialized': return httpx.Response(202)
        result = {'protocolVersion':'2025-03-26'} if body['method']=='initialize' else {'tools':[]}
        return httpx.Response(200, text='event: message\ndata: '+json.dumps({'jsonrpc':'2.0','id':body['id'],'result':result})+'\n\n',headers={'content-type':'text/event-stream'})
    assert client(handler).discover()['tools']==[]


def test_partial_discovery_failure_cannot_show_connected():
    import json
    def handler(request):
        body=json.loads(request.content)
        if body['method']=='initialize':return httpx.Response(200,json={'result':{'protocolVersion':'2025-03-26'}})
        if body['method']=='notifications/initialized':return httpx.Response(202)
        return httpx.Response(503)
    c=client(handler)
    with pytest.raises(MCPUnavailable):c.discover()
    assert not c.initialized and not c.tools and c.discovery_time is None


def test_read_mapping_cannot_be_bound_to_a_write_tool():
    c=client(lambda request:pytest.fail('Unsafe mapping must not reach network'))
    c.tools={'write_tool':{'name':'write_tool','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':False,'destructiveHint':True}}}
    b=RobinhoodMCPBroker(c,{'balances':{'tool':'write_tool','arguments':{},'verified':True}})
    with pytest.raises(MCPUnavailable):b.get_account('test')


def test_untrusted_endpoint_blocked_before_token_read_or_network():
    class SecretTokens:
        def get(self): pytest.fail('Never read credentials for an untrusted host')
    c = MCPClient(url='https://untrusted.example/mcp', token_provider=SecretTokens(),
                  http_client=httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail('No HTTP request'))))
    with pytest.raises(MCPUnavailable): c.discover()


def test_auth_failure_invalidates_previously_discovered_session():
    c = client(lambda request:httpx.Response(401, text='secret'))
    c.initialized = True; c.session_id = 'session'; c.discovery_time = 'old'
    c.tools = {'read':{'inputSchema':{'type':'object'}}}
    with pytest.raises(MCPAuthenticationRequired): c.call_discovered('read', {})
    assert not c.initialized and not c.tools and c.session_id is None and c.discovery_time is None


def test_redirect_is_not_followed_with_credentials():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={'location':'https://untrusted.example'})
    with pytest.raises(MCPUnavailable): client(handler).discover()
    assert len(requests) == 1
