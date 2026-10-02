"""Narrow stdio MCP adapter for the approved DizziClient.

The Work tunnel and Big Dizzi app-server can each launch this narrow local stdio adapter.
This process never accepts a shell command, host, path, credential, or URL from MCP.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import re
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.little_dizzi import ClientError, DizziClient

PROTOCOL = '2025-06-18'
_ALLOWED = os.environ.get('DIZZI_ALLOWED_CAPABILITIES')
ALLOWED_CAPABILITIES = tuple(x for x in ('health', 'system_status', 'local_inference') if not _ALLOWED or x in _ALLOWED.split(','))
PARENT_TASK_ID = os.environ.get('DIZZI_PARENT_TASK_ID')
if PARENT_TASK_ID and not re.fullmatch(r'task-[0-9a-f]{32}', PARENT_TASK_ID):
    raise SystemExit('Invalid Big Dizzi parent task ID')
AUDIT = Path.home() / '.local/share/little-dizzi-connector/audit.jsonl'
TOOLS = [
    {'name': 'submit_dizzi_task', 'title': 'Submit approved Dizzi task',
     'description': 'Run one approved Little Dizzi capability and return its schema-validated, durable-readback-verified task record. Health and system status cover the AIOS guest only.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
                     'properties': {'capability': {'type': 'string', 'enum': list(ALLOWED_CAPABILITIES)},
                                    'instruction': {'type': 'string', 'maxLength': 4000}},
                     'required': ['capability']},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}},
    {'name': 'read_dizzi_task', 'title': 'Read Dizzi task',
     'description': 'Read a previously completed durable Little Dizzi task by task ID.',
     'inputSchema': {'type': 'object', 'additionalProperties': False,
                     'properties': {'task_id': {'type': 'string', 'pattern': '^task-[0-9a-f]{32}$'}},
                     'required': ['task_id']},
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}
]


def audit(event):
    AUDIT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(AUDIT.parent, 0o700)
    event['timestamp'] = dt.datetime.now(dt.timezone.utc).isoformat()
    event['origin'] = 'mcp_stdio_transport; caller identity not independently attested'
    fd = os.open(AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(event, separators=(',', ':')) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def invoke(name, args, client=None):
    client = client or DizziClient()
    if not isinstance(args, dict):
        raise ValueError('Arguments must be an object')
    if name == 'submit_dizzi_task':
        if set(args) - {'capability', 'instruction'} or args.get('capability') not in ALLOWED_CAPABILITIES:
            raise ValueError('Unsupported task arguments')
        instruction = args.get('instruction', '')
        if not isinstance(instruction, str) or len(instruction) > 4000:
            raise ValueError('Invalid instruction')
        if PARENT_TASK_ID:
            task_id = 'task-' + uuid4().hex
            audit({'tool': name, 'capability': args['capability'], 'task_id': task_id,
                   'parent_task_id': PARENT_TASK_ID, 'status': 'dispatch_started'})
            return client.submit(args['capability'], instruction, task_id=task_id)
        return client.submit(args['capability'], instruction)
    if name == 'read_dizzi_task':
        if set(args) != {'task_id'} or not isinstance(args['task_id'], str):
            raise ValueError('Invalid read arguments')
        return client.read(args['task_id'])
    raise ValueError('Unsupported tool')


def dispatch(message, client=None):
    method = message.get('method')
    ident = message.get('id')
    if ident is None:
        return None
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': ident, 'result': {
            'protocolVersion': PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'dizzi-work-connector', 'version': '0.1.0'},
            'instructions': 'Approved capabilities: ' + ', '.join(ALLOWED_CAPABILITIES) + '. Health and status describe the AIOS guest, not the wider homelab. Do not put secrets in inference instructions.'}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': ident, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': ident, 'result': {'tools': TOOLS}}
    if method == 'tools/call':
        params = message.get('params') or {}
        name = params.get('name')
        args = params.get('arguments') or {}
        event = {'tool': name if name in ('submit_dizzi_task', 'read_dizzi_task') else 'unsupported',
                 'capability': args.get('capability') if isinstance(args, dict) else None,
                 'parent_task_id': PARENT_TASK_ID}
        try:
            record = invoke(name, args, client)
            event.update({'task_id': record['task_id'], 'status': record['status'],
                          'durable_result_reference': record['task_id']})
            audit(event)
            return {'jsonrpc': '2.0', 'id': ident, 'result': {
                'content': [{'type': 'text', 'text': json.dumps(record, ensure_ascii=True)}],
                'structuredContent': {'record': record}, 'isError': False}}
        except (ClientError, ValueError) as exc:
            event.update({'task_id': getattr(exc, 'task_id', None), 'status': 'failed',
                          'failure': exc.code if isinstance(exc, ClientError) else 'invalid_arguments'})
            audit(event)
            failure = {'error': event['failure'], 'task_id': event['task_id']}
            return {'jsonrpc': '2.0', 'id': ident, 'result': {
                'content': [{'type': 'text', 'text': json.dumps(failure)}],
                'structuredContent': failure, 'isError': True}}
    return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}}


def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response = dispatch(message)
            if response is not None:
                print(json.dumps(response, separators=(',', ':')), flush=True)
        except Exception:
            # Never echo malformed input, credentials, or traceback into the MCP channel.
            print(json.dumps({'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32603, 'message': 'Connector internal error'}}), flush=True)


if __name__ == '__main__':
    main()
