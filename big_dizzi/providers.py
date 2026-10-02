"""The sole cloud adapter. The orchestrator depends on capabilities and records."""
import json
import os
from urllib import request, error

USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'context_tokens')


class ProviderError(Exception):
    def __init__(self, code, message, observation=None):
        super().__init__(message)
        self.code = code
        self.observation = observation


class OpenAIProvider:
    runtime = 'openai-responses'

    def __init__(self, config):
        self.config = config

    def call(self, record):
        key = os.environ.get(self.config['api_key_env'])
        if not key:
            raise ProviderError('cloud_provider_not_configured', 'Cloud provider is not configured')
        payload = {'model': record['request']['model']['id'], 'input': record['request']['instruction'],
                   'max_output_tokens': record['request']['limits']['max_output_tokens'], 'store': False}
        req = request.Request('https://api.openai.com/v1/responses', json.dumps(payload).encode(),
                              {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
        try:
            with request.urlopen(req, timeout=record['request']['limits']['max_duration_seconds']) as response:
                data = json.loads(response.read(2_000_001))
        except error.HTTPError as exc:
            # Never persist a raw provider body, request, key or transport exception.
            code = {401: 'provider_authentication_failed', 403: 'provider_access_denied',
                    429: 'provider_rate_limited'}.get(exc.code, 'provider_http_error')
            raise ProviderError(code, f'Cloud provider returned HTTP {exc.code}; no automatic replay') from None
        except (OSError, ValueError):
            raise ProviderError('provider_outcome_uncertain', 'Cloud response was not received; no automatic replay') from None
        counts = data.get('usage') or {}
        detail_in = counts.get('input_tokens_details') or {}
        detail_out = counts.get('output_tokens_details') or {}
        observed = {'input_tokens': counts.get('input_tokens'), 'output_tokens': counts.get('output_tokens'),
                    'cache_read_tokens': detail_in.get('cached_tokens'), 'cache_write_tokens': detail_in.get('cache_write_tokens'),
                    'reasoning_tokens': detail_out.get('reasoning_tokens'), 'context_tokens': None}
        observed = {k: v if type(v) is int and v >= 0 else None for k, v in observed.items()}
        model = data.get('model') if isinstance(data.get('model'), str) and data['model'] else None
        provider_id = data.get('id') if isinstance(data.get('id'), str) else None
        parts = [part['text'] for item in data.get('output', []) if isinstance(item, dict)
                 for part in item.get('content', []) if part.get('type') == 'output_text' and isinstance(part.get('text'), str)]
        observation = (observed, model, provider_id)
        if data.get('status') != 'completed' or not parts:
            raise ProviderError('cloud_response_incomplete', 'Cloud response did not complete', observation)
        return '\n'.join(parts), *observation


def create_provider(config):
    if config['name'] != 'openai':
        raise ValueError('unsupported_provider')
    return OpenAIProvider(config)
