"""First-party hosted sessions bound to verified Cloudflare Access identity."""
from dataclasses import dataclass
import base64
from http.cookies import SimpleCookie
import json
import secrets
from threading import Lock
import time
from urllib.parse import urlsplit

from big_dizzi.jwt_verify import fetch_json, verify_rs256


@dataclass
class Session:
    subject: str
    csrf: str
    created: float
    last_seen: float
    access_expires: float


class HostedSecurity:
    def __init__(self, config, *, clock=time.time, jwks=None):
        self.origin = config['public_origin'].rstrip('/')
        parts = urlsplit(self.origin)
        if parts.scheme != 'https' or not parts.hostname or parts.path or parts.query or parts.fragment:
            raise ValueError('invalid_public_origin')
        self.host = parts.netloc
        self.issuer = config['access_issuer'].rstrip('/')
        self.audience = config['access_audience']
        self.owner_sub = config['owner_sub']
        if not self.issuer.startswith('https://') or not self.audience or not self.owner_sub:
            raise ValueError('invalid_access_config')
        self.jwks_url = self.issuer + '/cdn-cgi/access/certs'
        self.jwks = jwks if jwks is not None else fetch_json(self.jwks_url)
        self.clock = clock
        self.sessions = {}
        self.requests = {}
        self.lock = Lock()
        self.idle_seconds = min(int(config.get('idle_seconds', 1800)), 3600)
        self.max_seconds = min(int(config.get('max_seconds', 28800)), 28800)

    def identity(self, assertion):
        if not assertion:
            raise ValueError('missing_access_token')
        try:
            encoded = assertion.split('.')[0]
            header = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if header.get('kid') not in {key.get('kid') for key in self.jwks['keys']}:
                self.jwks = fetch_json(self.jwks_url)
        except (ValueError, IndexError, KeyError, TypeError):
            raise ValueError('invalid_access_token') from None
        claims = verify_rs256(assertion, self.jwks, issuer=self.issuer, audience=self.audience, now=self.clock())
        if claims.get('type') != 'app' or claims['sub'] != self.owner_sub:
            raise ValueError('access_identity_denied')
        return claims

    def cookie_token(self, header):
        cookie = SimpleCookie()
        try:
            cookie.load(header or '')
            return cookie['__Host-dizzi'].value if '__Host-dizzi' in cookie else None
        except Exception:
            return None

    def create(self, assertion):
        claims = self.identity(assertion)
        now = self.clock()
        token = secrets.token_urlsafe(32)
        session = Session(claims['sub'], secrets.token_urlsafe(32), now, now, claims['exp'])
        with self.lock:
            self.sessions[token] = session
        return token, session

    def get(self, token, assertion):
        claims = self.identity(assertion)
        now = self.clock()
        with self.lock:
            session = self.sessions.get(token)
            if not session or session.subject != claims['sub'] or now >= min(session.access_expires, claims['exp'], session.created + self.max_seconds) or now - session.last_seen >= self.idle_seconds:
                self.sessions.pop(token, None)
                raise ValueError('session_expired')
            session.last_seen = now
            return session

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def allow_request(self, key, limit=60, interval=60):
        now = self.clock()
        with self.lock:
            starts, count = self.requests.get(key, (now, 0))
            if now - starts >= interval:
                starts, count = now, 0
            count += 1
            self.requests[key] = (starts, count)
            return count <= limit


COOKIE = '__Host-dizzi'
COOKIE_ATTRIBUTES = 'Path=/; Secure; HttpOnly; SameSite=Strict'
