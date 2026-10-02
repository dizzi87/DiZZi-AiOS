"""Small RS256/JWKS verifier shared by SIWC and hosted Access identity."""
import base64
import json
import time
from urllib import request
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes


def _decode(part):
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))


def fetch_json(url):
    if not url.startswith('https://'):
        raise ValueError('https_required')
    with request.urlopen(request.Request(url, headers={'Accept': 'application/json'}), timeout=10) as response:
        return json.load(response)


def verify_rs256(token, jwks, *, issuer, audience, nonce=None, now=None):
    try:
        header_part, payload_part, signature_part = token.split('.')
        header = json.loads(_decode(header_part))
        claims = json.loads(_decode(payload_part))
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
            raise ValueError('invalid_algorithm')
        keys = [key for key in jwks['keys'] if key.get('kid') == header['kid'] and key.get('kty') == 'RSA' and key.get('use', 'sig') == 'sig']
        if len(keys) != 1:
            raise ValueError('unknown_key')
        key = keys[0]
        public = rsa.RSAPublicNumbers(int.from_bytes(_decode(key['e']), 'big'), int.from_bytes(_decode(key['n']), 'big')).public_key()
        public.verify(_decode(signature_part), (header_part + '.' + payload_part).encode(), padding.PKCS1v15(), hashes.SHA256())
        current = time.time() if now is None else now
        if claims.get('iss') != issuer or audience not in ([claims.get('aud')] if isinstance(claims.get('aud'), str) else claims.get('aud', [])):
            raise ValueError('invalid_issuer_or_audience')
        if not isinstance(claims.get('exp'), (int, float)) or claims['exp'] <= current:
            raise ValueError('expired')
        if not isinstance(claims.get('iat'), (int, float)) or claims['iat'] > current + 60:
            raise ValueError('invalid_issued_at')
        if claims.get('nbf', 0) > current + 60:
            raise ValueError('not_yet_valid')
        if not isinstance(claims.get('sub'), str) or not claims['sub']:
            raise ValueError('missing_subject')
        if nonce is not None and claims.get('nonce') != nonce:
            raise ValueError('invalid_nonce')
        return claims
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, base64.binascii.Error, ValueError) as exc:
        raise ValueError('invalid_identity_token') from exc
    except Exception as exc:
        raise ValueError('invalid_identity_token') from exc
