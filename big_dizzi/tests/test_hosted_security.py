import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from threading import Thread
from urllib import request, error
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

from big_dizzi.jwt_verify import verify_rs256
from big_dizzi.hosted_security import HostedSecurity
from big_dizzi.siwc import Credentials, PLAN_SCOPE
from big_dizzi.server import make_handler, safe_connections


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {'keys': [{'kid':'test', 'kty':'RSA', 'use':'sig', 'n':b64(numbers.n.to_bytes(256,'big')), 'e':b64(numbers.e.to_bytes(3,'big'))}]}
        self.now = int(time.time())
        self.config = {'public_origin':'https://dizzi.example.test','access_issuer':'https://team.cloudflareaccess.com',
                       'access_audience':'app-aud','owner_sub':'owner','idle_seconds':30,'max_seconds':300}
        self.security = HostedSecurity(self.config, clock=lambda:self.now, jwks=self.jwks)

    def token(self, **changes):
        claims = {'iss':self.config['access_issuer'], 'aud':['app-aud'], 'sub':'owner', 'type':'app',
                  'iat':self.now, 'nbf':self.now, 'exp':self.now+200}
        claims.update(changes)
        header = b64(json.dumps({'alg':'RS256','kid':'test'}).encode())
        body = b64(json.dumps(claims).encode())
        message = (header+'.'+body).encode()
        signature = self.key.sign(message,padding.PKCS1v15(),hashes.SHA256())
        return message.decode()+'.'+b64(signature)

    def test_signed_owner_creates_bounded_session_and_forgery_fails(self):
        assertion = self.token()
        self.assertEqual(self.security.identity(assertion)['sub'],'owner')
        session_id, session = self.security.create(assertion)
        self.assertEqual(self.security.get(session_id, assertion).subject, 'owner')
        self.assertEqual(len(session.csrf), 43)
        for forged in (self.token(sub='other'), self.token(aud=['other']), self.token(exp=self.now-1), assertion[:-2]+'aa'):
            with self.assertRaises(ValueError):self.security.identity(forged)
        self.now += 31
        with self.assertRaises(ValueError):self.security.get(session_id, assertion)

    def test_logout_and_rate_limit(self):
        assertion = self.token()
        session_id, _ = self.security.create(assertion)
        self.security.logout(session_id)
        with self.assertRaises(ValueError):self.security.get(session_id, assertion)
        self.assertTrue(self.security.allow_request('remote', limit=1))
        self.assertFalse(self.security.allow_request('remote', limit=1))

    def test_siwc_credentials_are_private_rotate_and_reject_missing_scope(self):
        with tempfile.TemporaryDirectory() as root:
            creds = Credentials(root)
            host = creds.host_id()
            self.assertEqual(host, creds.host_id())
            self.assertTrue(host.startswith('urn:uuid:'))
            record = {'client_id':'oaiapp_test','subject':'owner','access_token':'old','refresh_token':'refresh',
                      'scopes':[PLAN_SCOPE], 'expires_at':0}
            creds._write(creds.account_path, record)
            self.assertEqual(creds.account_path.stat().st_mode & 0o077, 0)
            with patch('big_dizzi.siwc._post',return_value={'access_token':'new','refresh_token':'next', 'expires_in':3600,
                                                             'scope':PLAN_SCOPE}):
                self.assertEqual(creds.access_token(), 'new')
            self.assertEqual(creds.account()['refresh_token'], 'next')
            creds._write(creds.account_path,{**record,'scopes':[]})
            with self.assertRaisesRegex(ValueError,'siwc_plan_usage_not_granted'):creds.access_token()

    def test_siwc_oidc_and_granted_scope_are_both_required(self):
        with tempfile.TemporaryDirectory() as root:
            creds = Credentials(root)
            response = {'token_type':'Bearer','access_token':'access','refresh_token':'refresh',
                        'id_token':self.token(iss='https://auth.openai.com',aud='oaiapp_test',nonce='nonce'),
                        'scope':'openid offline_access resource.invoke '+PLAN_SCOPE,'expires_in':3600}
            with patch.object(creds,'_discovery',return_value={'jwks_uri':'https://auth.openai.com/test'}),\
                 patch('big_dizzi.siwc.fetch_json',return_value=self.jwks):
                self.assertEqual(creds._validate(response,'oaiapp_test','nonce')['subject'],'owner')
                with self.assertRaises(ValueError):creds._validate(response,'oaiapp_test','wrong')
                response['scope'] = 'openid offline_access resource.invoke'
                with self.assertRaisesRegex(ValueError,'siwc_plan_usage_not_granted'):
                    creds._validate(response,'oaiapp_test','nonce')

    def test_oidc_nonce_and_signature_required(self):
        jwt = self.token(nonce='expected')
        self.assertEqual(verify_rs256(jwt,self.jwks,issuer=self.config['access_issuer'],audience='app-aud',nonce='expected')['sub'],'owner')
        with self.assertRaises(ValueError):verify_rs256(jwt,self.jwks,issuer=self.config['access_issuer'],audience='app-aud',nonce='wrong')

    def test_connections_expose_status_only_and_reject_secret_values(self):
        item = {'id':'example','tool':'Example','status':'configured','health':'Healthy',
                'auth_status':'credential stored outside app','capabilities':['status'],
                'mechanism':'private connector details','scope':'private path'}
        public = safe_connections({'connections':[item]})['connections'][0]
        self.assertEqual(public['auth_status'],'configured')
        self.assertNotIn('mechanism',public)
        self.assertNotIn('scope',public)
        item['health'] = 'token=secret-value'
        with self.assertRaises(ValueError):safe_connections({'connections':[item]})

    def test_http_auth_csrf_logout_and_approval_boundary(self):
        service = Mock()
        service.config = {'hosted':self.config}
        service.submit.return_value = 'goal-' + 'a'*32
        server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(service, self.security))
        Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        assertion = self.token()
        self.assertEqual(self.security.identity(assertion)['sub'],'owner')
        headers = {'Host':'dizzi.example.test','Cf-Access-Jwt-Assertion':assertion}
        def call(path, *, extra=None, body=None):
            return request.urlopen(request.Request(base+path, body, {**headers,**(extra or {})}, method='POST' if body is not None else 'GET'))
        with self.assertRaises(error.HTTPError) as caught:call('/api/session')
        self.assertEqual(caught.exception.code,401)
        with self.assertRaises(error.HTTPError) as caught:call('/login',extra={'Cf-Access-Jwt-Assertion':'forged'})
        self.assertEqual(caught.exception.code,401)
        connection = HTTPConnection('127.0.0.1',server.server_port)
        connection.request('GET','/login',headers=headers)
        login = connection.getresponse()
        self.assertEqual(login.status,303)
        cookie_header = login.getheader('Set-Cookie')
        cookie = cookie_header.split(';',1)[0]
        self.assertIn('Secure',cookie_header)
        self.assertIn('HttpOnly',cookie_header)
        login.read();connection.close()
        authed = {'Cookie':cookie}
        with call('/api/session',extra=authed) as response:csrf=json.load(response)['csrf']
        for extra in ({'Cookie':cookie,'Content-Type':'application/json','Origin':self.config['public_origin']},
                      {'Cookie':cookie,'Content-Type':'application/json','Origin':'https://evil.test','X-CSRF-Token':csrf}):
            with self.assertRaises(error.HTTPError) as caught:call('/api/goals',extra=extra,body=b'{"instruction":"hello"}')
            self.assertEqual(caught.exception.code,403)
        with call('/api/goals',extra={**authed,'Content-Type':'application/json','Origin':self.config['public_origin'],'X-CSRF-Token':csrf},body=b'{"instruction":"hello"}') as response:
            self.assertEqual(response.status,202)
        service.submit.assert_called_once()
        service.store.decide_approval.assert_not_called()
        with call('/api/logout',extra={**authed,'Content-Type':'application/json','Origin':self.config['public_origin'],'X-CSRF-Token':csrf},body=b'{}'):
            pass
        with self.assertRaises(error.HTTPError) as caught:call('/api/session',extra=authed)
        self.assertEqual(caught.exception.code,401)


if __name__ == '__main__':unittest.main()
