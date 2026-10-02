"""Standard-library validator for the exact portable v0 schema vocabulary.

This intentionally is not a general JSON Schema implementation. Unknown schema
keywords fail closed so a future schema revision cannot silently weaken checks.
"""
from datetime import datetime
import json
from pathlib import Path
import re

SCHEMA_PATH = Path(__file__).resolve().parent / 'schemas/task-result-v0.schema.json'
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


class ContractError(ValueError):
    pass


def validate(value, schema=None, *, root=None, path='$'):
    schema = SCHEMA if schema is None else schema
    root = SCHEMA if root is None else root
    supported = {'$schema', '$id', 'title', 'description', '$defs', '$ref', 'type',
                 'additionalProperties', 'required', 'properties', 'const',
                 'enum', 'anyOf', 'minLength', 'minimum', 'items', 'uniqueItems', 'format'}
    if set(schema) - supported:
        raise ContractError('Unsupported schema vocabulary')
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise ContractError('Unsupported schema reference')
        target = root
        for name in ref[2:].split('/'):
            target = target[name.replace('~1', '/').replace('~0', '~')]
        validate(value, target, root=root, path=path)
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                validate(value, option, root=root, path=path)
                break
            except ContractError:
                pass
        else:
            raise ContractError(path + ': no permitted shape')
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'null': value is None,
               'integer': isinstance(value, int) and not isinstance(value, bool)}
    if types and not any(matches.get(kind, False) for kind in types):
        raise ContractError(path + ': invalid type')
    if 'const' in schema and value != schema['const']:
        raise ContractError(path + ': invalid constant')
    if 'enum' in schema and value not in schema['enum']:
        raise ContractError(path + ': invalid enum')
    if isinstance(value, dict):
        if set(schema.get('required', [])) - value.keys():
            raise ContractError(path + ': missing fields')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and value.keys() - properties.keys():
            raise ContractError(path + ': unknown fields')
        for name, item in value.items():
            if name in properties:
                validate(item, properties[name], root=root, path=path + '.' + name)
    if isinstance(value, list):
        if schema.get('uniqueItems') and any(item in value[:i] for i, item in enumerate(value)):
            raise ContractError(path + ': duplicate items')
        for item in value:
            if 'items' in schema:
                validate(item, schema['items'], root=root, path=path + '[]')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            raise ContractError(path + ': empty string')
        if schema.get('format') == 'date-time':
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value):
                raise ContractError(path + ': invalid timestamp')
            try:
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            except (ValueError, OverflowError):
                raise ContractError(path + ': invalid timestamp') from None
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get('minimum', value):
        raise ContractError(path + ': below minimum')
