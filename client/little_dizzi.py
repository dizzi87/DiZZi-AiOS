"""Provider-neutral, approved local interface to the bounded Little Dizzi gateway."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib import error, request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract_validation import validate

CAPABILITIES = ('health', 'system_status', 'local_inference')
DEFAULT_FILES = Path.home() / '.local/share/little-dizzi-bootstrap'
TASK_ID = re.compile(r'task-[0-9a-f]{32}\Z')


class ClientError(Exception):
    def __init__(self, code, message, task_id=None):
        super().__init__(message)
        self.code, self.task_id = code, task_id


def _exchange(port, token, path, payload=None, timeout=5):
    headers = {'Authorization': 'Bearer ' + token}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = request.Request(f'http://127.0.0.1:{port}{path}',
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers=headers, method='GET' if payload is None else 'POST')
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        try:
            detail = json.load(exc).get('error', 'http_error')
        except (ValueError, AttributeError):
            detail = 'http_error'
        raise ClientError(detail, f'Gateway returned HTTP {exc.code}: {detail}') from None


@contextmanager
def _tunnel(host, known_hosts, identity, token):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    command = ['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=' + str(known_hosts), '-o', 'IdentitiesOnly=yes',
               '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=8',
               '-i', str(identity), '-L', f'127.0.0.1:{port}:127.0.0.1:8765', host]
    tunnel = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise ClientError('connection_failed', 'Approved tunnel exited before gateway readiness')
            try:
                health = _exchange(port, token, '/v0/health', timeout=1)
                if health.get('gateway') == 'ok':
                    yield port
                    return
                raise ClientError('invalid_gateway', 'Unexpected gateway health response')
            except (OSError, TimeoutError):
                time.sleep(.2)
        raise ClientError('connection_timeout', 'Approved gateway did not become ready')
    finally:
        if tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()


class DizziClient:
    """One task call; configuration and credentials remain local to this approved client."""
    def __init__(self, *, files=DEFAULT_FILES, ssh_host=None):
        self.files = Path(files)
        self.ssh_host = ssh_host or os.environ.get('DIZZI_LITTLE_SSH_HOST')

    def _credentials(self):
        if not self.ssh_host:
            raise ClientError('configuration_missing', 'Little Dizzi SSH host is not configured')
        token_file = self.files / 'gateway.token'
        key = self.files / 'client_ed25519'
        known = Path.home() / '.ssh/known_hosts'
        for path in (token_file, key, known):
            if not path.is_file():
                raise ClientError('configuration_missing', f'Approved client file missing: {path.name}')
        token = token_file.read_text(encoding='ascii').strip()
        if not token:
            raise ClientError('configuration_invalid', 'Credential file is empty')
        return token, known, key

    def _read(self, port, token, task_id, *, absent_ok=False):
        try:
            record = _exchange(port, token, '/v0/tasks/' + task_id)
        except ClientError as exc:
            if absent_ok and exc.code == 'not_found':
                return None
            raise
        validate(record)
        if record['task_id'] != task_id:
            raise ClientError('invalid_result', 'Readback task ID mismatch', task_id)
        return record

    def read(self, task_id):
        if not TASK_ID.fullmatch(task_id):
            raise ClientError('invalid_task_id', 'Invalid task ID')
        token, known, key = self._credentials()
        with _tunnel(self.ssh_host, known, key, token) as port:
            return self._read(port, token, task_id)

    def submit(self, capability, instruction='', *, task_id=None, timeout=75):
        if capability not in CAPABILITIES:
            raise ClientError('unsupported_capability', 'Unsupported capability')
        if capability == 'local_inference':
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
                raise ClientError('invalid_instruction', 'Local inference needs an instruction of at most 4000 characters')
        elif instruction:
            raise ClientError('invalid_instruction', f'{capability} does not accept an instruction')
        task_id = task_id or 'task-' + uuid4().hex
        if not TASK_ID.fullmatch(task_id):
            raise ClientError('invalid_task_id', 'Invalid task ID')
        token, known, key = self._credentials()
        payload = {'task_id': task_id, 'capability': capability, 'instruction': instruction}
        for attempt in range(2):
            try:
                with _tunnel(self.ssh_host, known, key, token) as port:
                    try:
                        record = _exchange(port, token, '/v0/tasks', payload, timeout=timeout)
                    except (OSError, TimeoutError):
                        # Admission may have succeeded. Resolve the same ID before considering replay.
                        record = self._poll(port, token, task_id, timeout)
                        if record is None:
                            raise ClientError('outcome_uncertain', 'Submission outcome could not be verified', task_id)
                    validate(record)
                    if record['task_id'] != task_id:
                        raise ClientError('invalid_result', 'Response task ID mismatch', task_id)
                    saved = self._poll(port, token, task_id, timeout)
                    if saved != record:
                        raise ClientError('readback_mismatch', 'Durable result differs from response', task_id)
                    if record['status'] != 'completed':
                        raise ClientError('task_' + record['status'], 'Task ended with status ' + record['status'], task_id)
                    return record
            except ClientError as exc:
                if exc.code in ('connection_failed', 'connection_timeout') and attempt == 0:
                    time.sleep(.5)
                    continue
                exc.task_id = exc.task_id or task_id
                raise
        raise AssertionError('unreachable')

    def _poll(self, port, token, task_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = self._read(port, token, task_id, absent_ok=True)
            except (OSError, TimeoutError):
                record = None
            if record and record['status'] in ('completed', 'failed'):
                return record
            time.sleep(.4)
        return None


def main():
    parser = argparse.ArgumentParser(description='Submit one approved Little Dizzi task')
    parser.add_argument('capability', nargs='?', choices=CAPABILITIES)
    parser.add_argument('--instruction', default='')
    parser.add_argument('--read-task-id')
    parser.add_argument('--task-id')
    args = parser.parse_args()
    if bool(args.capability) == bool(args.read_task_id):
        parser.error('provide one capability or --read-task-id')
    client = DizziClient()
    try:
        record = client.read(args.read_task_id) if args.read_task_id else client.submit(
            args.capability, args.instruction, task_id=args.task_id)
        print(json.dumps(record, indent=2))
        return 0
    except ClientError as exc:
        print(json.dumps({'error': exc.code, 'message': str(exc), 'task_id': exc.task_id}), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': 'client_error', 'message': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
