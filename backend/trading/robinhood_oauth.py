"""Robinhood OAuth for this server; no brokerage or trading operations.

OAuth endpoints are fixed to the official discovery result. Authorization uses
PKCE, an expiring process-local state and an exact loopback callback. Credentials
are private JSON on disk; public methods other than get_token never return them.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx


RESOURCE = 'https://agent.robinhood.com/mcp/trading'
ISSUER = RESOURCE
AUTHORIZATION_ENDPOINT = 'https://robinhood.com/oauth'
REGISTRATION_ENDPOINT = 'https://agent.robinhood.com/oauth/trading/register'
TOKEN_ENDPOINT = 'https://api.robinhood.com/oauth2/token/'
REDIRECT_URI = 'http://127.0.0.1:8010/broker/oauth/callback'
SCOPE = 'internal'
STATE_LIFETIME_SECONDS = 8 * 60
REFRESH_MARGIN_SECONDS = 60


class RobinhoodOAuthError(RuntimeError):
    """A fixed, non-sensitive message safe to present through the HTTP API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _valid_string(value, maximum=16384):
    return (isinstance(value, str) and bool(value) and len(value) <= maximum
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (OverflowError, ValueError):
        return None


def _state_matches(value, expected):
    return isinstance(value, str) and value.isascii() and hmac.compare_digest(value, expected)


class RobinhoodOAuth:
    def __init__(self, http_client=None, storage_path=None, clock=None,
                 allowed_redirect_uri=REDIRECT_URI):
        self.http = http_client or httpx.Client(timeout=15, follow_redirects=False)
        self.storage_path = Path(storage_path) if storage_path else Path(__file__).resolve().parents[2] / 'data' / 'robinhood-oauth.json'
        self.clock = clock or time.time
        self.allowed_redirect_uri = allowed_redirect_uri
        # The override exists for local tests/deployments, never for a request.
        parsed = urlsplit(allowed_redirect_uri)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path != '/broker/oauth/callback'):
            raise RobinhoodOAuthError('invalid_redirect', 'OAuth callback must be the configured loopback callback')
        self._pending = None
        self._mutex = threading.RLock()

    @contextmanager
    def _locked_storage(self):
        """Serialize rotation across API processes sharing this credential file."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.storage_path.with_name(self.storage_path.name + '.lock')
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise RobinhoodOAuthError('credential_storage_unavailable', 'OAuth credential storage is unavailable') from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
                raise RobinhoodOAuthError('credential_file_permissions', 'OAuth credential files must be owned by this user with permission 0600')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError:
                raise RobinhoodOAuthError('credential_storage_unavailable', 'OAuth credential storage is unavailable') from None
            yield
        finally:
            os.close(descriptor)

    def _read(self):
        try:
            descriptor = os.open(self.storage_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError:
            raise RobinhoodOAuthError('credential_storage_unavailable', 'OAuth credential storage is unavailable') from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
                raise RobinhoodOAuthError('credential_file_permissions', 'OAuth credential files must be owned by this user with permission 0600')
            if metadata.st_size > 1024 * 1024:
                raise RobinhoodOAuthError('credential_file_invalid', 'OAuth credential file is invalid; reconnect required')
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                descriptor = None
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError, OSError):
            raise RobinhoodOAuthError('credential_file_invalid', 'OAuth credential file is invalid; reconnect required') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, data):
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.robinhood-oauth-', dir=self.storage_path.parent)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, separators=(',', ':'), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.storage_path)
            temporary = None
            directory = os.open(self.storage_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise RobinhoodOAuthError('credential_storage_unavailable', 'OAuth credentials could not be saved securely; reconnect required') from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _request(self, endpoint, *, data=None, payload=None):
        # Never accept endpoint URLs from an OAuth response, request or token file.
        if endpoint not in {REGISTRATION_ENDPOINT, TOKEN_ENDPOINT}:
            raise RobinhoodOAuthError('untrusted_endpoint', 'OAuth endpoint is not trusted')
        try:
            kwargs = {'data': data} if data is not None else {'json': payload}
            response = self.http.post(endpoint, **kwargs, headers={'Accept': 'application/json'},
                                      follow_redirects=False, timeout=15)
        except (httpx.HTTPError, OSError):
            raise RobinhoodOAuthError('oauth_transport_failed', 'Robinhood authorization request failed; reconnect required') from None
        if response.status_code not in {200, 201}:
            raise RobinhoodOAuthError('oauth_request_rejected', 'Robinhood rejected the authorization request; reconnect required')
        try:
            result = response.json()
        except (ValueError, UnicodeError):
            raise RobinhoodOAuthError('oauth_response_invalid', 'Robinhood authorization response was invalid; reconnect required') from None
        if not isinstance(result, dict) or result.get('error'):
            raise RobinhoodOAuthError('oauth_response_invalid', 'Robinhood authorization response was invalid; reconnect required')
        return result

    def _registration(self, existing):
        if (_valid_string(existing.get('client_id'), 2048)
                and existing.get('registered_redirect_uri') == self.allowed_redirect_uri):
            return existing
        metadata = {'client_name': 'Northstar Trading', 'redirect_uris': [self.allowed_redirect_uri],
                    'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                    'token_endpoint_auth_method': 'none'}
        result = self._request(REGISTRATION_ENDPOINT, payload=metadata)
        grants = result.get('grant_types', metadata['grant_types'])
        if (not _valid_string(result.get('client_id'), 2048) or result.get('client_secret')
                or result.get('token_endpoint_auth_method', 'none') != 'none'
                or result.get('redirect_uris', metadata['redirect_uris']) != metadata['redirect_uris']
                or not isinstance(grants, list) or not all(isinstance(grant, str) for grant in grants)
                or not {'authorization_code', 'refresh_token'}.issubset(grants)
                or result.get('response_types', ['code']) != ['code']):
            raise RobinhoodOAuthError('registration_invalid', 'Robinhood client registration did not confirm the requested public client')
        registered = {'client_id': result['client_id'], 'registered_redirect_uri': self.allowed_redirect_uri,
                      'registered_at': self.clock()}
        self._write(registered)
        return registered

    def _current_pending(self):
        if self._pending and self.clock() >= self._pending['expires_at']:
            self._pending = None
        return self._pending

    def status(self):
        """Inspect local state only. A refreshable grant is authorized, not verified."""
        with self._mutex:
            pending = bool(self._current_pending())
            try:
                with self._locked_storage():
                    record = self._read()
            except RobinhoodOAuthError as exc:
                return {'authorized': False, 'configured': False, 'expires_at': None,
                        'refresh_available': False, 'authorization_pending': pending,
                        'token_expired': True, 'blocker': exc.code}
            configured = bool(_valid_string(record.get('client_id'), 2048)
                              and record.get('registered_redirect_uri') == self.allowed_redirect_uri)
            expires = _number(record.get('expires_at')) or 0.0
            expired = expires <= self.clock()
            refresh = bool(_valid_string(record.get('refresh_token')) and not record.get('refresh_blocked')
                           and not record.get('refresh_in_flight'))
            authorized = bool(configured and _valid_string(record.get('access_token'))
                              and not record.get('refresh_blocked') and not record.get('refresh_in_flight')
                              and (not expired or refresh))
            try:
                expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else None
            except (ValueError, OverflowError, OSError):
                expires_at = None
                authorized = False
                expired = True
            return {'authorized': authorized, 'configured': configured, 'expires_at': expires_at,
                    'refresh_available': refresh, 'authorization_pending': pending,
                    'token_expired': expired,
                    'blocker': None if authorized else ('authorization_required' if configured else 'client_not_registered')}

    def start(self, redirect_uri):
        if redirect_uri != self.allowed_redirect_uri:
            raise RobinhoodOAuthError('invalid_redirect', 'OAuth callback does not match the configured callback')
        with self._mutex, self._locked_storage():
            registration = self._registration(self._read())
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
            self._pending = {'state': state, 'verifier': verifier, 'expires_at': self.clock() + STATE_LIFETIME_SECONDS,
                             'client_id': registration['client_id'], 'redirect_uri': redirect_uri}
            query = {'response_type': 'code', 'client_id': registration['client_id'], 'redirect_uri': redirect_uri,
                     'scope': SCOPE, 'state': state, 'code_challenge': challenge,
                     'code_challenge_method': 'S256', 'resource': RESOURCE}
            return {'authorization_url': AUTHORIZATION_ENDPOINT + '?' + urlencode(query), 'state': state}

    def cancel_pending(self, state):
        with self._mutex:
            pending = self._current_pending()
            if pending and _state_matches(state, pending['state']):
                self._pending = None
                return True
            return False

    def _tokens(self, result, existing):
        access_token = result.get('access_token')
        token_type = result.get('token_type')
        expires_in = _number(result.get('expires_in'))
        refresh_token = result.get('refresh_token', existing.get('refresh_token'))
        scope = result.get('scope', SCOPE)
        if (not _valid_string(access_token) or not isinstance(token_type, str) or token_type.lower() != 'bearer'
                or expires_in is None or expires_in <= 0
                or not isinstance(scope, str) or SCOPE not in scope.split()
                or (refresh_token is not None and not _valid_string(refresh_token))):
            raise RobinhoodOAuthError('token_response_invalid', 'Robinhood token response was invalid; reconnect required')
        expires_at = self.clock() + expires_in
        try:
            datetime.fromtimestamp(expires_at, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise RobinhoodOAuthError('token_response_invalid', 'Robinhood token response was invalid; reconnect required') from None
        record = {**existing, 'access_token': access_token, 'token_type': 'Bearer',
                  'expires_at': expires_at, 'scope': SCOPE, 'refresh_blocked': False, 'refresh_in_flight': False}
        if refresh_token:
            record['refresh_token'] = refresh_token
        return record

    def complete(self, code, state, issuer):
        with self._mutex:
            pending = self._current_pending()
            if not pending or not _state_matches(state, pending['state']):
                raise RobinhoodOAuthError('invalid_state', 'Authorization state is missing, expired or invalid; start again')
            # Correctly correlated callbacks are single-use even if exchange fails.
            self._pending = None
            if issuer != ISSUER:
                raise RobinhoodOAuthError('invalid_issuer', 'Authorization issuer did not match Robinhood; start again')
            if not _valid_string(code, 8192):
                raise RobinhoodOAuthError('invalid_code', 'Authorization code is missing or invalid; start again')
            with self._locked_storage():
                record = self._read()
                if record.get('client_id') != pending['client_id'] or record.get('registered_redirect_uri') != pending['redirect_uri']:
                    raise RobinhoodOAuthError('client_changed', 'OAuth client changed during authorization; start again')
                result = self._request(TOKEN_ENDPOINT, data={'grant_type': 'authorization_code', 'code': code,
                    'redirect_uri': pending['redirect_uri'], 'client_id': pending['client_id'],
                    'code_verifier': pending['verifier'], 'resource': RESOURCE})
                # A new grant must not inherit a refresh token from an older grant.
                new_grant = {key: value for key, value in record.items() if key in {'client_id', 'registered_redirect_uri', 'registered_at'}}
                self._write(self._tokens(result, new_grant))
            return self.status()

    def get_token(self):
        """Internal bearer retrieval, including a single serialized refresh attempt."""
        with self._mutex, self._locked_storage():
            record = self._read()
            if (not _valid_string(record.get('client_id'), 2048)
                    or record.get('registered_redirect_uri') != self.allowed_redirect_uri
                    or record.get('refresh_blocked') or record.get('refresh_in_flight')
                    or not _valid_string(record.get('access_token'))):
                raise RobinhoodOAuthError('authorization_required', 'Robinhood authorization is required')
            expires_at = _number(record.get('expires_at'))
            if expires_at is None:
                raise RobinhoodOAuthError('authorization_required', 'Robinhood authorization is required')
            if self.clock() < expires_at - REFRESH_MARGIN_SECONDS:
                return record['access_token']
            if not _valid_string(record.get('refresh_token')):
                # A still-valid non-refreshable token may be used until its expiry.
                if self.clock() < expires_at:
                    return record['access_token']
                raise RobinhoodOAuthError('authorization_required', 'Robinhood authorization expired; reconnect required')
            # Persist uncertainty BEFORE sending the rotating credential. A process
            # crash after the server accepted it must not replay it after restart.
            self._write({**record, 'refresh_blocked': True, 'refresh_in_flight': True})
            try:
                result = self._request(TOKEN_ENDPOINT, data={'grant_type': 'refresh_token',
                    'refresh_token': record['refresh_token'], 'client_id': record['client_id'], 'resource': RESOURCE})
                rotated = self._tokens(result, record)
                self._write(rotated)
            except RobinhoodOAuthError:
                # A timeout may already have rotated the token. Never retry it
                # blindly from the next worker tick or from another process.
                self._write({**record, 'refresh_blocked': True, 'refresh_in_flight': False})
                raise RobinhoodOAuthError('authorization_required', 'Robinhood authorization renewal failed; reconnect required') from None
            return rotated['access_token']

    def disconnect(self):
        """Clear only this platform's OAuth credentials; no server-side revocation."""
        with self._mutex, self._locked_storage():
            self._pending = None
            try:
                self.storage_path.unlink(missing_ok=True)
            except OSError:
                raise RobinhoodOAuthError('credential_storage_unavailable', 'OAuth credentials could not be removed') from None
        return self.status()
