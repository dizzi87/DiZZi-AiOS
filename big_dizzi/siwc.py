"""OpenAI OSS Sign in with ChatGPT, with app-owned credentials only.

No Codex desktop auth file, API key, browser cookie or token URL is read.
"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
from threading import Lock
import time
from urllib import parse, request
from uuid import uuid4
import webbrowser

from big_dizzi.jwt_verify import fetch_json, verify_rs256

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
APP_ID = 'big_dizzi'
AGENT = APP_ID  # Must match app-server initialize.clientInfo.name.
PLAN_SCOPE = 'chatgpt.tokens.use.direct'


def _open_browser(url):
    if webbrowser.open(url):
        return True
    # WSL may lack a Linux x-scheme handler while its Windows browser can
    # reach the loopback callback. This still opens the user's system browser.
    for path in ('/mnt/c/Program Files/Google/Chrome/Application/chrome.exe',
                 '/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'):
        if Path(path).is_file():
            subprocess.Popen([path, url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def _post(url, values):
    body = parse.urlencode(values).encode()
    with request.urlopen(request.Request(url, body, {'Content-Type': 'application/x-www-form-urlencoded'}), timeout=20) as response:
        return json.load(response) if response.headers.get('Content-Type', '').startswith('application/json') else None


class Credentials:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        if self.directory.is_symlink():
            raise ValueError('credential_directory_symlink')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.lock = Lock()
        self.host_path = self.directory / 'host.json'
        self.account_path = self.directory / 'account.json'

    def _read(self, path):
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError('credential_file_symlink')
        if path.stat().st_mode & 0o077:
            raise ValueError('insecure_credential_permissions')
        return json.loads(path.read_text())

    def _write(self, path, value):
        temporary = self.directory / ('.' + path.name + '.' + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def host_id(self):
        with self.lock:
            saved = self._read(self.host_path)
            if saved:
                return saved['ext_agent_host_id']
            value = 'urn:uuid:' + str(uuid4())
            self._write(self.host_path, {'ext_agent_host_id': value})
            return value

    def account(self):
        return self._read(self.account_path)

    def _discovery(self):
        metadata = fetch_json(ISSUER + '/.well-known/openid-configuration')
        if metadata.get('issuer') != ISSUER or not metadata.get('jwks_uri', '').startswith(ISSUER + '/'):
            raise ValueError('invalid_openai_discovery')
        return metadata

    def _validate(self, response, client_id, nonce, previous=None):
        if response.get('token_type', '').lower() != 'bearer' or not response.get('access_token') or not response.get('refresh_token') or not response.get('id_token'):
            raise ValueError('incomplete_token_response')
        metadata = self._discovery()
        identity = verify_rs256(response['id_token'], fetch_json(metadata['jwks_uri']), issuer=ISSUER, audience=client_id, nonce=nonce)
        if previous and (identity['sub'] != previous['subject'] or client_id != previous['client_id']):
            raise ValueError('account_switch_rejected')
        granted = set(response.get('scope', '').split())
        if PLAN_SCOPE not in granted or 'resource.invoke' not in granted or 'offline_access' not in granted:
            raise ValueError('siwc_plan_usage_not_granted')
        return {'issuer': ISSUER, 'subject': identity['sub'], 'email': identity.get('email'), 'client_id': client_id,
                'ext_agent_host_id': self.host_id(), 'id_token': response['id_token'], 'access_token': response['access_token'],
                'refresh_token': response['refresh_token'], 'scopes': sorted(granted),
                'expires_at': int(time.time()) + int(response['expires_in']), 'earliest_refresh_at': response.get('earliest_refresh_at')}

    def sign_in(self, *, open_browser=True, timeout=180):
        previous = self.account()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        captured = {}
        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                if parse.urlsplit(self.path).path != '/auth/callback':
                    self.send_error(404); return
                captured.update({k: v[0] for k, v in parse.parse_qs(parse.urlsplit(self.path).query).items()})
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'Big Dizzi sign-in returned. You may close this tab.')
            def log_message(self, *_):
                pass
        with HTTPServer(('127.0.0.1', 0), Callback) as listener:
            listener.timeout = timeout
            callback = 'http://127.0.0.1:' + str(listener.server_port) + '/auth/callback'
            client_id = previous['client_id'] if previous else 'dynamic_agent_client'
            params = {'client_id': client_id, 'ext_agent_host_id': self.host_id(), 'response_type': 'code',
                      'redirect_uri': callback, 'scope': SCOPES, 'resource': RESOURCE, 'state': state, 'nonce': nonce,
                      'code_challenge_method': 'S256', 'code_challenge': challenge}
            if not previous:
                params['agent_name_hint'] = AGENT
            url = AUTHORIZE + '?' + parse.urlencode(params)
            if not open_browser:
                raise ValueError('interactive_browser_required')
            if not _open_browser(url):
                raise ValueError('browser_open_failed')
            listener.handle_request()
        if not secrets.compare_digest(captured.get('state', ''), state) or captured.get('error') or not captured.get('code'):
            raise ValueError('siwc_authorization_failed')
        issued = captured.get('client_id', client_id)
        if previous and issued != client_id:
            raise ValueError('siwc_client_mismatch')
        if not previous and (not issued.startswith('oaiapp_') or issued == client_id):
            raise ValueError('siwc_registration_incomplete')
        response = _post(TOKEN, {'grant_type': 'authorization_code', 'client_id': issued, 'code': captured['code'],
                                 'code_verifier': verifier, 'redirect_uri': callback, 'resource': RESOURCE})
        record = self._validate(response, issued, nonce, previous)
        with self.lock:
            self._write(self.account_path, record)
        return {'status': 'SIWC PLAN USAGE PROVEN', 'client_id': issued, 'subject': record['subject']}

    def access_token(self):
        with self.lock:
            record = self.account()
            if not record or PLAN_SCOPE not in record['scopes']:
                raise ValueError('siwc_plan_usage_not_granted')
            if record['expires_at'] <= time.time() + 120:
                response = _post(TOKEN, {'grant_type': 'refresh_token', 'client_id': record['client_id'],
                                         'refresh_token': record['refresh_token'], 'resource': RESOURCE})
                if not response or not response.get('access_token') or not response.get('refresh_token'):
                    raise ValueError('siwc_refresh_failed')
                granted = set(response.get('scope', '').split())
                if PLAN_SCOPE not in granted or 'resource.invoke' not in granted:
                    raise ValueError('siwc_plan_usage_not_granted')
                record.update(access_token=response['access_token'], refresh_token=response['refresh_token'],
                              expires_at=int(time.time()) + int(response['expires_in']), scopes=sorted(granted))
                self._write(self.account_path, record)
            return record['access_token']

    def logout(self):
        host_id = self.host_id()
        with self.lock:
            record = self.account()
            if not record:
                return True
            confirmed = False
            try:
                metadata = self._discovery()
                endpoint = metadata['revocation_endpoint']
                if not endpoint.startswith(ISSUER + '/'):
                    raise ValueError('invalid_revocation_endpoint')
                _post(endpoint, {'token': record['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': record['client_id']})
                confirmed = True
            finally:
                # Keep the non-secret registration mapping and host ID for later sign-in.
                self._write(self.account_path, {'client_id': record['client_id'], 'subject': record['subject'],
                                                'ext_agent_host_id': host_id})
            return confirmed


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Big Dizzi ChatGPT-plan sign-in; no API key')
    parser.add_argument('action', choices=('sign-in', 'refresh', 'logout', 'status'))
    parser.add_argument('--credentials-dir', default=str(Path.home() / '.config' / 'big-dizzi' / 'siwc'))
    args = parser.parse_args()
    credentials = Credentials(args.credentials_dir)
    if args.action == 'sign-in':
        result = credentials.sign_in()
        print(result['status'])
    elif args.action == 'refresh':
        credentials.access_token()
        print('SIWC token refreshed or still valid; no inference claim')
    elif args.action == 'logout':
        try:
            confirmed = credentials.logout()
        except Exception:
            print('Local credentials cleared; remote revocation unconfirmed')
        else:
            print('Local credentials cleared; remote revocation ' + ('confirmed' if confirmed else 'not needed'))
    else:
        account = credentials.account() or {}
        print('SIWC plan scope saved: ' + str(PLAN_SCOPE in account.get('scopes', [])))


if __name__ == '__main__':
    main()
