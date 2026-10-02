"""Loopback-only Big Dizzi UI/API. Run as python -m big_dizzi.server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import fcntl
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from big_dizzi.core import audit
from big_dizzi import brain
from big_dizzi.service import Orchestrator
from big_dizzi.hosted_security import HostedSecurity, COOKIE, COOKIE_ATTRIBUTES

GOAL_ID = r'goal-[0-9a-f]{32}'
SECRET_SHAPE = re.compile(r'(?i)(?:-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}\b|\bgh[pousr]_[A-Za-z0-9_]{12,}\b|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)')


def safe_connections(data):
    if not isinstance(data, dict) or not isinstance(data.get('connections'), list):
        raise ValueError('invalid_connections_registry')
    rows = []
    for item in data['connections']:
        if not isinstance(item, dict):
            raise ValueError('invalid_connection')
        row = {key: item.get(key) for key in ('id', 'tool', 'status', 'health')}
        if any(not isinstance(value, str) or not value for value in row.values()):
            raise ValueError('invalid_connection_fields')
        if row['status'] not in ('configured', 'indirect', 'planned', 'unavailable'):
            raise ValueError('invalid_connection_status')
        row['auth_status'] = 'not configured' if str(item.get('auth_status', '')).lower() in ('not connected', 'not configured', '') else 'configured'
        row['capabilities'] = item.get('capabilities', [])
        values = [*row.values()]
        if not isinstance(row['capabilities'], list) or len(row['capabilities']) > 20:
            raise ValueError('invalid_connection_capabilities')
        values = [v for v in values if isinstance(v, str)] + row['capabilities']
        if any(not isinstance(v, str) or len(v) > 300 or SECRET_SHAPE.search(v) for v in values):
            raise ValueError('unsafe_connection_value')
        rows.append(row)
    return {'connections': rows}


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config['provider']['name'] not in ('codex', 'openai'):
        raise ValueError('unsupported_provider')
    if config.get('hosted') and (config['provider']['name'] != 'codex' or config['provider'].get('auth_mode') != 'siwc'):
        raise ValueError('hosted_requires_siwc')
    models = config['provider']['models']
    if config['provider'].get('collaboration_mode') not in (None, 'plan'):
        raise ValueError('invalid_collaboration_mode')
    if set(models) != ({'plan_goal', 'reason', 'research', 'engineer', 'summarise', 'health'} if config['provider']['name'] == 'codex' else {'plan_goal', 'reason', 'research', 'engineer', 'summarise'}) or not all(isinstance(v, str) and v.strip() for v in models.values()):
        raise ValueError('incomplete_capability_map')
    if not 1 <= config.get('max_active_goals', 2) <= 4:
        raise ValueError('invalid_concurrency')
    for key, value in config['provider'].get('max_output_tokens', {}).items():
        if key not in models or type(value) is not int or not 1 <= value <= 8000:
            raise ValueError('invalid_output_limit')
    brain._selected(config)
    return config


def make_handler(service, hosted_security=None):
    static = Path(__file__).parent / 'static'
    hosted = hosted_security or (HostedSecurity(service.config['hosted']) if service.config.get('hosted') else None)

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, code, raw, mime, *, artifact=False, cookie=None):
            self.send_response(code)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            if hosted:
                self.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
            if cookie:
                self.send_header('Set-Cookie', cookie)
            csp = "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
            if artifact:
                csp = "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
            self.send_header('Content-Security-Policy', csp)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def send_json(self, code, value):
            self.send_bytes(code, json.dumps(value).encode(), 'application/json')

        def local_request(self, *, mutation=False):
            allowed = {hosted.host} if hosted else {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host', '')
            if host not in allowed:
                self.send_json(403, {'error': 'untrusted_host'})
                return False
            origin = self.headers.get('Origin')
            expected_origin = hosted.origin if hosted else 'http://' + host
            if ((hosted and mutation and origin != expected_origin) or (origin and origin != expected_origin) or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send_json(403, {'error': 'cross_origin_request'})
                return False
            if mutation and self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.send_json(415, {'error': 'json_required'})
                return False
            if hosted:
                client_key = self.client_address[0]
                if not hosted.allow_request(client_key, limit=120):
                    self.send_json(429, {'error': 'rate_limited'})
                    return False
                if self.path in ('/login', '/signed-out') and not mutation:
                    return True
                token = hosted.cookie_token(self.headers.get('Cookie'))
                try:
                    self.session = hosted.get(token, self.headers.get('Cf-Access-Jwt-Assertion'))
                except ValueError:
                    if self.path == '/' and not mutation:
                        self.send_response(303)
                        self.send_header('Location', '/login')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                    else:
                        self.send_json(401, {'error': 'authentication_required'})
                    return False
                if mutation and (not self.headers.get('X-CSRF-Token') or not __import__('hmac').compare_digest(self.headers['X-CSRF-Token'], self.session.csrf)):
                    self.send_json(403, {'error': 'csrf_rejected'})
                    return False
            return True

        def do_GET(self):
            if not self.local_request():
                return
            path = urlparse(self.path).path
            if hosted and path == '/signed-out':
                self.send_bytes(200, b'<!doctype html><html><meta charset="utf-8"><title>Signed out</title><p>Big Dizzi session ended.</p><a href="/login">Continue to Big Dizzi</a></html>', 'text/html')
            elif hosted and path == '/login':
                try:
                    token, _ = hosted.create(self.headers.get('Cf-Access-Jwt-Assertion'))
                except ValueError:
                    self.send_json(401, {'error': 'authentication_required'})
                    return
                hosted.logout(hosted.cookie_token(self.headers.get('Cookie')))
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Set-Cookie', COOKIE + '=' + token + '; ' + COOKIE_ATTRIBUTES)
                self.end_headers()
            elif path == '/api/session' and hosted:
                self.send_json(200, {'csrf': self.session.csrf, 'subject': self.session.subject})
            elif path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                self.send_bytes(200, (static / name).read_bytes(), {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name])
            elif path in ('/brain', '/brain/'):
                self.send_bytes(200, (static / 'brain' / 'index.html').read_bytes(), 'text/html')
            elif path in ('/brain/app.js', '/brain/style.css'):
                name = path.rsplit('/', 1)[-1]
                self.send_bytes(200, (static / 'brain' / name).read_bytes(), 'text/javascript' if name.endswith('.js') else 'text/css')
            elif path == '/api/graph':
                graph, _ = brain.build(service.config)
                self.send_json(200, graph)
            elif path == '/api/node':
                from urllib.parse import parse_qs
                identity = parse_qs(urlparse(self.path).query).get('id', [''])[0]
                result = brain.note(service.config, identity)
                self.send_json(200 if result else 404, result or {'error': 'not_found'})
            elif path == '/api/reveal':
                self.send_json(404, {'error': 'unavailable'})
            elif path == '/api/connections':
                target = Path(service.config['core_root']) / '50-Projects' / 'Dizzi-AIOS' / 'CONNECTIONS.json'
                data = json.loads(target.read_text(encoding='utf-8'))
                try:
                    self.send_json(200, safe_connections(data))
                except ValueError:
                    self.send_json(503, {'error': 'connections_unavailable'})
            elif path == '/api/goals':
                self.send_json(200, service.store.recent())
            elif re.fullmatch('/api/goals/' + GOAL_ID + '/artifact', path):
                identity = path.split('/')[3]
                goal = service.store.get(identity)
                target = Path(service.config['state_dir']) / 'artifacts' / identity / 'index.html'
                if goal and goal['artifact'] and target.is_file():
                    raw = target.read_bytes()
                    expected = goal['artifact'].get('preview_sha256') or goal['artifact'].get('sha256')
                    if hashlib.sha256(raw).hexdigest() == expected:
                        self.send_bytes(200, raw, 'text/html', artifact=True)
                    else:
                        self.send_json(409, {'error': 'artifact_hash_mismatch'})
                else:
                    self.send_json(404, {'error': 'artifact_not_found'})
            elif re.fullmatch('/api/goals/' + GOAL_ID + '/artifact-bundle', path):
                identity = path.split('/')[3]
                goal = service.store.get(identity)
                target = Path(service.config['state_dir']) / 'artifacts' / identity / 'engineering.zip'
                if goal and goal['artifact'] and target.is_file():
                    raw = target.read_bytes()
                    if hashlib.sha256(raw).hexdigest() == goal['artifact'].get('bundle_sha256'):
                        self.send_bytes(200, raw, 'application/zip')
                    else:
                        self.send_json(409, {'error': 'artifact_hash_mismatch'})
                else:
                    self.send_json(404, {'error': 'artifact_not_found'})
            elif re.fullmatch('/api/goals/' + GOAL_ID + '/export', path):
                goal = service.store.get(path.split('/')[3])
                self.send_json(200 if goal else 404, goal or {'error': 'not_found'})
            elif re.fullmatch('/api/goals/' + GOAL_ID, path):
                goal = service.store.get(path.rsplit('/', 1)[-1])
                self.send_json(200 if goal else 404, goal or {'error': 'not_found'})
            elif path == '/api/system':
                self.send_json(200, {'core_audit': audit(service.config['core_root']),
                                     'cloud_configured': service.config['provider']['name'] == 'codex' or bool(os.environ.get(service.config['provider'].get('api_key_env', 'OPENAI_API_KEY'))),
                                     'local_status': 'Check Dizzi-AIOS health for a live guest reading', 'usage_cost': None,
                                     'provider': service.config['provider']['name'], 'models': service.config['provider']['models']})
            else:
                self.send_json(404, {'error': 'not_found'})

        def do_POST(self):
            if not self.local_request(mutation=True):
                return
            path = urlparse(self.path).path
            if hosted and path == '/api/logout':
                hosted.logout(hosted.cookie_token(self.headers.get('Cookie')))
                self.send_bytes(200, b'{}', 'application/json', cookie=COOKIE + '=; Max-Age=0; ' + COOKIE_ATTRIBUTES)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 20_000:
                    raise ValueError('invalid_request_size')
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('json_object_required')
                if path == '/api/goals':
                    goal_id = service.submit(payload['instruction'])
                    self.send_json(202, {'id': goal_id})
                elif re.fullmatch('/api/goals/' + GOAL_ID + '/requests/request-[0-9a-f]{32}', path):
                    parts = path.split('/')
                    service.store.respond_runtime_request(parts[3], parts[5], payload, actor=self.session.subject if hosted else 'local-owner')
                    self.send_json(200, service.store.get(parts[3]))
                elif re.fullmatch('/api/goals/' + GOAL_ID + '/continue', path):
                    identity = path.split('/')[3]
                    service.continue_goal(identity, payload['instruction'])
                    self.send_json(202, service.store.get(identity))
                elif re.fullmatch('/api/goals/' + GOAL_ID + '/cancel', path):
                    identity = path.split('/')[3]
                    service.cancel(identity)
                    self.send_json(202, service.store.get(identity))
                elif re.fullmatch('/api/goals/' + GOAL_ID + '/approvals/approval-[0-9a-f]{32}', path):
                    parts = path.split('/')
                    service.store.decide_approval(parts[3], parts[5], payload['decision'], actor=self.session.subject if hosted else 'local-owner')
                    self.send_json(200, service.store.get(parts[3]))
                else:
                    self.send_json(404, {'error': 'not_found'})
            except (ValueError, KeyError, TypeError):
                self.send_json(400, {'error': 'Invalid request or action not currently available'})
            except TimeoutError:
                self.send_json(408, {'error': 'request_timeout'})

    return Handler


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path(__file__).parent / 'config.json'))
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    os.umask(0o077)
    service = Orchestrator(load_config(args.config))
    instance_lock = open(Path(service.config['state_dir']) / 'server.lock', 'a')
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('This state directory already has a running Big Dizzi server') from None
    # Bind first: a second invocation must never "recover" a still-running server.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service))
    service.store.recover_interrupted()
    service.reconcile_interrupted()
    print(f'Big Dizzi: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
