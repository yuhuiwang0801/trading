import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from trading.robinhood_oauth import (
    AUTHORIZATION_ENDPOINT, ISSUER, REDIRECT_URI, REGISTRATION_ENDPOINT, RESOURCE,
    STATE_LIFETIME_SECONDS, TOKEN_ENDPOINT, RobinhoodOAuth, RobinhoodOAuthError,
)


class Clock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now


def service(tmp_path, handler=None, clock=None):
    calls = []

    def transport(request):
        calls.append(request)
        if handler:
            return handler(request)
        if str(request.url) == REGISTRATION_ENDPOINT:
            return httpx.Response(201, json={'client_id': 'public-client', 'token_endpoint_auth_method': 'none', 'redirect_uris': [REDIRECT_URI]})
        assert str(request.url) == TOKEN_ENDPOINT
        return httpx.Response(200, json={'access_token': 'private-access', 'refresh_token': 'private-refresh', 'token_type': 'Bearer', 'expires_in': 1800})

    oauth = RobinhoodOAuth(http_client=httpx.Client(transport=httpx.MockTransport(transport)),
                          storage_path=tmp_path / 'oauth.json', clock=clock or Clock())
    return oauth, calls


def authorize(oauth):
    started = oauth.start(REDIRECT_URI)
    result = oauth.complete('private-code', started['state'], ISSUER)
    assert result['authorized']
    return result


def test_pkce_registration_exchange_and_private_persistence(tmp_path):
    oauth, calls = service(tmp_path)
    started = oauth.start(REDIRECT_URI)
    parsed = urlsplit(started['authorization_url'])
    assert parsed.scheme + '://' + parsed.netloc + parsed.path == AUTHORIZATION_ENDPOINT
    query = parse_qs(parsed.query)
    assert query['resource'] == [RESOURCE]
    assert query['scope'] == ['internal']
    assert query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == [REDIRECT_URI]
    assert 'verifier' not in json.dumps(started)
    registration = json.loads(calls[0].content)
    assert registration == {'client_name': 'Northstar Trading', 'redirect_uris': [REDIRECT_URI],
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'], 'token_endpoint_auth_method': 'none'}
    assert oauth.status()['authorization_pending']
    public = oauth.complete('private-code', started['state'], ISSUER)
    form = parse_qs(calls[1].content.decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == [challenge]
    assert form['code'] == ['private-code']
    assert form['resource'] == [RESOURCE]
    assert form['client_id'] == ['public-client']
    assert not public['authorization_pending']
    assert public['refresh_available'] and public['configured'] and public['authorized']
    assert 'private-' not in json.dumps(public)
    assert oauth.get_token() == 'private-access'
    assert len(calls) == 2
    assert os.stat(oauth.storage_path).st_mode & 0o777 == 0o600
    persisted = json.loads(oauth.storage_path.read_text())
    assert persisted['refresh_token'] == 'private-refresh'
    assert 'verifier' not in persisted and 'state' not in persisted
    assert not list(tmp_path.glob('.robinhood-oauth-*'))


@pytest.mark.parametrize('redirect', ['http://localhost:8010/broker/oauth/callback',
    REDIRECT_URI + '?next=https://evil.example', 'https://evil.example/callback',
    'http://127.0.0.1:8011/broker/oauth/callback'])
def test_request_cannot_override_callback(tmp_path, redirect):
    oauth, calls = service(tmp_path)
    with pytest.raises(RobinhoodOAuthError, match='callback'):
        oauth.start(redirect)
    assert calls == []


def test_reuses_registration_but_replaces_pending_state(tmp_path):
    oauth, calls = service(tmp_path)
    first = oauth.start(REDIRECT_URI)
    second = oauth.start(REDIRECT_URI)
    assert first['state'] != second['state'] and len(calls) == 1
    with pytest.raises(RobinhoodOAuthError):
        oauth.complete('private-code', first['state'], ISSUER)
    assert oauth.status()['authorization_pending']
    oauth.complete('private-code', second['state'], ISSUER)
    with pytest.raises(RobinhoodOAuthError):
        oauth.complete('private-code', second['state'], ISSUER)
    assert len(calls) == 2


@pytest.mark.parametrize('issuer', [None, '', 'https://evil.example', ISSUER + '/'])
def test_issuer_is_exact_and_callback_single_use(tmp_path, issuer):
    oauth, calls = service(tmp_path)
    started = oauth.start(REDIRECT_URI)
    with pytest.raises(RobinhoodOAuthError, match='issuer'):
        oauth.complete('private-code', started['state'], issuer)
    with pytest.raises(RobinhoodOAuthError, match='state'):
        oauth.complete('private-code', started['state'], ISSUER)
    assert len(calls) == 1


def test_expired_state_and_process_restart_need_fresh_authorization(tmp_path):
    clock = Clock()
    oauth, calls = service(tmp_path, clock=clock)
    started = oauth.start(REDIRECT_URI)
    clock.now += STATE_LIFETIME_SECONDS
    with pytest.raises(RobinhoodOAuthError, match='expired'):
        oauth.complete('private-code', started['state'], ISSUER)
    assert not oauth.status()['authorization_pending']
    restarted, _ = service(tmp_path, clock=clock)
    with pytest.raises(RobinhoodOAuthError, match='state'):
        restarted.complete('private-code', started['state'], ISSUER)
    assert len(calls) == 1


def test_cancel_pending_requires_matching_state(tmp_path):
    oauth, _ = service(tmp_path)
    started = oauth.start(REDIRECT_URI)
    assert not oauth.cancel_pending('wrong')
    assert oauth.status()['authorization_pending']
    assert oauth.cancel_pending(started['state'])
    assert not oauth.status()['authorization_pending']


def test_non_ascii_state_is_invalid_without_consuming_legitimate_state(tmp_path):
    oauth, calls = service(tmp_path)
    oauth.start(REDIRECT_URI)
    with pytest.raises(RobinhoodOAuthError, match='state'):
        oauth.complete('private-code', '非ASCII状态', ISSUER)
    assert not oauth.cancel_pending('非ASCII状态')
    assert oauth.status()['authorization_pending']
    assert len(calls) == 1


def test_refresh_rotation_is_persisted_and_status_does_not_refresh(tmp_path):
    clock = Clock()
    oauth, calls = service(tmp_path, clock=clock)
    authorize(oauth)
    clock.now += 1801
    status = oauth.status()
    assert status['authorized'] and status['token_expired'] and status['refresh_available']
    assert len(calls) == 2

    def refresh(request):
        assert str(request.url) == TOKEN_ENDPOINT
        form = parse_qs(request.content.decode())
        assert form['grant_type'] == ['refresh_token']
        assert form['refresh_token'] == ['private-refresh']
        return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                                        'token_type': 'bearer', 'expires_in': 3600})

    oauth.http = httpx.Client(transport=httpx.MockTransport(refresh))
    assert oauth.get_token() == 'rotated-access'
    record = json.loads(oauth.storage_path.read_text())
    assert record['refresh_token'] == 'rotated-refresh'
    restarted, calls_after = service(tmp_path, clock=clock)
    assert restarted.get_token() == 'rotated-access'
    assert not calls_after


def test_file_lock_prevents_double_refresh_across_service_instances(tmp_path):
    clock = Clock()
    oauth, _ = service(tmp_path, clock=clock)
    authorize(oauth)
    clock.now += 1800
    refresh_calls = []
    entered = threading.Event()
    release = threading.Event()

    def refresh(request):
        refresh_calls.append(request)
        entered.set()
        assert release.wait(2)
        return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                                        'token_type': 'Bearer', 'expires_in': 1800})

    first, _ = service(tmp_path, handler=refresh, clock=clock)
    second, _ = service(tmp_path, handler=refresh, clock=clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result_one = pool.submit(first.get_token)
        assert entered.wait(2)
        result_two = pool.submit(second.get_token)
        release.set()
        assert result_one.result() == result_two.result() == 'rotated-access'
    assert len(refresh_calls) == 1


def test_process_crash_during_refresh_blocks_replay_after_restart(tmp_path):
    clock = Clock()
    oauth, _ = service(tmp_path, clock=clock)
    authorize(oauth)
    clock.now += 1800

    def crash(request):
        persisted = json.loads(oauth.storage_path.read_text())
        assert persisted['refresh_in_flight'] and persisted['refresh_blocked']
        raise SystemExit('simulated process death after request reached server')

    oauth.http = httpx.Client(transport=httpx.MockTransport(crash))
    with pytest.raises(SystemExit):
        oauth.get_token()
    restarted, calls = service(tmp_path, clock=clock)
    with pytest.raises(RobinhoodOAuthError, match='authorization is required'):
        restarted.get_token()
    assert not calls and not restarted.status()['authorized']


@pytest.mark.parametrize('response', ['timeout', 'rejected', 'invalid'])
def test_uncertain_or_failed_refresh_is_not_retried_and_is_redacted(tmp_path, response):
    clock = Clock()
    oauth, _ = service(tmp_path, clock=clock)
    authorize(oauth)
    clock.now += 1800
    calls = []

    def fail(request):
        calls.append(request)
        if response == 'timeout':
            raise httpx.ReadTimeout('private-refresh and other secrets')
        if response == 'rejected':
            return httpx.Response(400, json={'error': 'invalid_grant', 'error_description': 'private-refresh'})
        return httpx.Response(200, json={'access_token': 'private-response', 'expires_in': 'invalid'})

    oauth.http = httpx.Client(transport=httpx.MockTransport(fail))
    for _ in range(2):
        with pytest.raises(RobinhoodOAuthError) as failure:
            oauth.get_token()
        assert 'private-' not in str(failure.value)
    assert len(calls) == 1
    assert not oauth.status()['authorized']
    assert not oauth.status()['refresh_available']


def test_redirect_responses_never_forward_credentials(tmp_path):
    oauth, _ = service(tmp_path)
    authorize(oauth)
    oauth.clock.now += 1800
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={'Location': 'https://evil.example/steal'}, text='private-secret')

    oauth.http = httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True)
    with pytest.raises(RobinhoodOAuthError):
        oauth.get_token()
    assert calls == [TOKEN_ENDPOINT]


def test_untrusted_endpoint_is_rejected_before_network(tmp_path):
    oauth, calls = service(tmp_path)
    with pytest.raises(RobinhoodOAuthError, match='trusted'):
        oauth._request('https://api.robinhood.com.evil.example/oauth2/token/', data={'refresh_token': 'private-secret'})
    assert calls == []


@pytest.mark.parametrize('metadata', [{'client_id': ''}, {'client_id': 'client', 'client_secret': 'private-secret'},
    {'client_id': 'client', 'redirect_uris': ['https://evil.example']},
    {'client_id': 'client', 'grant_types': None}, {'client_id': 'client', 'grant_types': [{}]},
    {'client_id': 'client', 'token_endpoint_auth_method': 'client_secret_post'}])
def test_registration_must_confirm_public_client(tmp_path, metadata):
    oauth, _ = service(tmp_path, handler=lambda request: httpx.Response(201, json=metadata))
    with pytest.raises(RobinhoodOAuthError) as failure:
        oauth.start(REDIRECT_URI)
    assert 'private-secret' not in str(failure.value)
    assert not oauth.storage_path.exists()


@pytest.mark.parametrize('update', [{'expires_in': 10 ** 400}, {'expires_in': 1e300}, {'expires_in': True},
    {'scope': 'unrelated'}, {'scope': None}, {'token_type': 'mac'}, {'refresh_token': ''}])
def test_invalid_token_metadata_does_not_store_credentials(tmp_path, update):
    oauth, _ = service(tmp_path)
    started = oauth.start(REDIRECT_URI)
    result = {'access_token': 'private-secret', 'token_type': 'Bearer', 'expires_in': 1800, **update}
    oauth.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result)))
    with pytest.raises(RobinhoodOAuthError) as failure:
        oauth.complete('private-code', started['state'], ISSUER)
    assert 'private-secret' not in str(failure.value)
    assert 'access_token' not in json.loads(oauth.storage_path.read_text())
    assert not oauth.status()['authorized']


def test_insecure_token_file_and_symlink_are_rejected(tmp_path):
    oauth, calls = service(tmp_path)
    oauth.storage_path.write_text('{"access_token":"private-secret"}')
    oauth.storage_path.chmod(0o644)
    assert oauth.status()['blocker'] == 'credential_file_permissions'
    with pytest.raises(RobinhoodOAuthError):
        oauth.get_token()
    oauth.storage_path.unlink()
    target = tmp_path / 'target.json'
    target.write_text('{}')
    target.chmod(0o600)
    oauth.storage_path.symlink_to(target)
    with pytest.raises(RobinhoodOAuthError):
        oauth.get_token()
    assert not calls


def test_new_grant_does_not_retain_previous_refresh_token(tmp_path):
    oauth, _ = service(tmp_path)
    authorize(oauth)
    started = oauth.start(REDIRECT_URI)
    oauth.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'access_token': 'new-grant-access', 'token_type': 'Bearer', 'expires_in': 1800})))
    assert not oauth.complete('private-code', started['state'], ISSUER)['refresh_available']
    assert 'refresh_token' not in json.loads(oauth.storage_path.read_text())


def test_disconnect_clears_only_local_credentials_and_pending_state(tmp_path):
    oauth, calls = service(tmp_path)
    authorize(oauth)
    oauth.start(REDIRECT_URI)
    before = len(calls)
    result = oauth.disconnect()
    assert not result['authorized'] and not result['configured'] and not result['authorization_pending']
    assert not oauth.storage_path.exists()
    assert len(calls) == before
    with pytest.raises(RobinhoodOAuthError):
        oauth.get_token()


def test_status_is_safe_before_registration_and_does_not_call_network(tmp_path):
    oauth, calls = service(tmp_path)
    assert oauth.status() == {'authorized': False, 'configured': False, 'expires_at': None,
        'refresh_available': False, 'authorization_pending': False, 'token_expired': True,
        'blocker': 'client_not_registered'}
    assert not calls
