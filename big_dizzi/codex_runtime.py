"""Pinned Codex app-server transport with separate local and SIWC auth modes."""
import json
import os
import signal
from pathlib import Path
import select
import subprocess
import time
import tomllib
from collections import deque
from threading import Lock

from big_dizzi.providers import ProviderError
from big_dizzi.siwc import Credentials

PINNED_VERSION = 'codex-cli 0.155.0-alpha.9.2'


class CodexRuntime:
    runtime = 'codex-app-server'

    def __init__(self, config):
        self.config = config
        self.executable = config['executable']
        self.process = None
        self.sequence = 0
        self.write_lock = Lock()
        self.thread_id = None
        self.turn_id = None
        self.cancel_requested = False
        self.cancel_at = None
        self.pending = deque()
        self.buffer = b''
        self.on_request = None
        self.items = {}
        self.wait_deadline = None

    def __enter__(self):
        try:
            version = subprocess.run([self.executable, '--version'], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError('codex_unavailable', 'Pinned Codex runtime unavailable; no API fallback') from None
        if version != PINNED_VERSION:
            raise ProviderError('codex_version_mismatch', 'Pinned Codex executable/version is unavailable')
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'CODEX_HOME', 'CODEX_SQLITE_HOME', 'XDG_RUNTIME_DIR') if key in os.environ}
        siwc = self.config.get('auth_mode') == 'siwc'
        if siwc:
            try:
                env['ACCESS_TOKEN'] = Credentials(self.config['siwc_credentials_dir']).access_token()
            except (KeyError, ValueError, OSError) as exc:
                raise ProviderError('siwc_plan_usage_unavailable', 'Big Dizzi SIWC plan use unavailable; no API fallback') from exc
            codex_home = Path(self.config['siwc_codex_home']).expanduser()
            codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            env['CODEX_HOME'] = str(codex_home)
        # The child never inherits ambient provider keys or unrelated application secrets.
        repo = Path(__file__).resolve().parents[1]
        args = [self.executable, 'app-server', '--listen', 'stdio://', '--disable', 'apps', '--disable', 'plugins',
                '-c', 'approval_policy="never"']
        if siwc:
            args += ['-c', 'model_provider="openai_chatgpt_plan"',
                     '-c', 'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
                     '-c', 'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
                     '-c', 'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                     '-c', 'model_providers.openai_chatgpt_plan.wire_api="responses"',
                     '-c', 'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
                     '-c', 'model_providers.openai_chatgpt_plan.supports_websockets=false']
        else:
            args += ['-c', 'forced_login_method="chatgpt"', '-c', 'model_provider="openai"']
        if self.config.get('collaboration_mode') == 'plan':
            args[2:2] = ['--enable', 'default_mode_request_user_input']
        # Override complete TOML tables: quoted components in dotted CLI keys are literal
        # in this pinned parser, so projects."/path" did not scope trust correctly.
        home = Path(env.get('CODEX_HOME', str(Path.home() / '.codex')))
        if siwc:
            owner_config = {}
        else:
            try:
                owner_config = tomllib.loads((home / 'config.toml').read_text())
            except FileNotFoundError:
                owner_config = {}
        for name in owner_config.get('mcp_servers', {}):
            if not name.replace('_', '').replace('-', '').isalnum():
                raise ProviderError('codex_config_boundary', 'Unsupported inherited MCP name')
            args += ['-c', 'mcp_servers.' + name + '.enabled=false']
        workspace = str(Path(self.config.get('workspace', repo)).resolve())
        profile = ('permissions={dizzi={filesystem={":root"="deny",":minimal"="read",'
                   '":workspace_roots"={"."="' + ('write' if self.config.get('engineering') else 'read') + '",".codex"="read",".git"="read"},'
                   + json.dumps(str(Path(self.executable).resolve())) + '="read"},network={enabled=false}}}')
        args += ['-c', 'default_permissions="dizzi"', '-c', profile,
                 '-c', 'projects={' + json.dumps(workspace) + '={trust_level="trusted"}}',
                 '-c', 'shell_environment_policy.inherit="none"',
                 '-c', 'shell_environment_policy.set={PATH="/usr/bin:/bin",LANG="C.UTF-8"}',
                 '--disable', 'shell_snapshot', '--disable', 'unified_exec']
        if self.config.get('enable_little_dizzi'):
            args += ['-c', 'mcp_servers.dizzi.command="/usr/bin/python3"',
                     '-c', 'mcp_servers.dizzi.args=' + json.dumps([str(repo / 'connector/work_mcp.py')]),
                     '-c', 'mcp_servers.dizzi.env.DIZZI_ALLOWED_CAPABILITIES="health,system_status"',
                     '-c', 'mcp_servers.dizzi.tool_timeout_sec=85']
            if self.config.get('parent_task_id'):
                args += ['-c', 'mcp_servers.dizzi.env.DIZZI_PARENT_TASK_ID=' + json.dumps(self.config['parent_task_id'])]
        if self.config.get('engineering'):
            browser = self.config['browser']
            args += ['-c', 'mcp_servers.engineering.command="/usr/bin/python3"',
                     '-c', 'mcp_servers.engineering.args=' + json.dumps([
                         str(repo / 'big_dizzi/engineering_browser.py'), '--workspace', workspace,
                         '--browser', browser['executable'], '--libraries', browser['library_path'],
                         '--evidence', self.config['browser_evidence']]),
                     '-c', 'mcp_servers.engineering.tool_timeout_sec=40']
        args = ['/usr/bin/python3', str(repo / 'big_dizzi/owned_child.py'), str(os.getpid()), *args]
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, bufsize=0, start_new_session=True, cwd=workspace)
        try:
            self.request('initialize', {'clientInfo': {'name': 'big_dizzi', 'title': 'Big Dizzi', 'version': '0.2.0'}, 'capabilities': {'experimentalApi': True}}, 15)
            self.send({'method': 'initialized', 'params': {}})
            if siwc:
                self.plan = 'SIWC granted; inference pending'
                self.rate_limits = {'ordinary_usage_allowed': None}
            else:
                account = self.request('account/read', {}, 15).get('account') or {}
                if account.get('type') != 'chatgpt':
                    raise ProviderError('chatgpt_auth_unavailable', 'Codex ChatGPT-plan authentication is unavailable; sign in with Codex')
                self.plan = account.get('planType')
                snapshot = self.request('account/rateLimits/read', {}, 15)
                if snapshot.get('ordinaryUsageAllowed') is False:
                    raise ProviderError('codex_usage_limited', 'ChatGPT-plan Codex usage is unavailable; no retry or API fallback')
                limits = snapshot.get('rateLimits') or {}
                self.rate_limits = {'ordinary_usage_allowed': snapshot.get('ordinaryUsageAllowed'),
                                    'primary': limits.get('primary'), 'secondary': limits.get('secondary'),
                                    'rate_limit_reached_type': limits.get('rateLimitReachedType')}
            servers = self.request('mcpServerStatus/list', {}, 15).get('data', [])
            exposed = {entry.get('name'): set((entry.get('tools') or {}).keys()) for entry in servers if entry.get('tools')}
            expected = {'dizzi': {'submit_dizzi_task', 'read_dizzi_task'}} if self.config.get('enable_little_dizzi') else {}
            if self.config.get('engineering'):
                expected['engineering'] = {'verify_calculator'}
            if exposed != expected:
                raise ProviderError('codex_tool_boundary_failed', 'Codex exposes tools outside the approved task scope')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)

    def send(self, obj):
        with self.write_lock:
            self.process.stdin.write((json.dumps(obj, separators=(',', ':')) + '\n').encode())
            self.process.stdin.flush()

    def receive(self, deadline):
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    return json.loads(line)
                except ValueError:
                    raise ProviderError('codex_protocol_error', 'Invalid runtime protocol') from None
            if self.process.poll() is not None:
                raise ProviderError('codex_runtime_exit', 'Codex app-server exited; outcome may be uncertain')
            if select.select([self.process.stdout], [], [], min(.2, max(0, deadline-time.monotonic())))[0]:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ProviderError('codex_runtime_exit', 'Codex app-server closed; outcome may be uncertain')
                self.buffer += chunk
                if len(self.buffer) > 8_000_000:
                    raise ProviderError('codex_protocol_error', 'Runtime message exceeded boundary')
        raise ProviderError('codex_timeout', 'Codex exceeded the task deadline; outcome may be uncertain')

    def request(self, method, params, timeout=15):
        self.sequence += 1
        ident = self.sequence
        self.send({'id': ident, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            msg = self.receive(deadline)
            if msg.get('id') == ident and 'method' not in msg:
                if 'error' in msg:
                    raise ProviderError('codex_request_failed', 'Codex rejected ' + method)
                return msg['result']
            # Do not lose start/completion events interleaved with RPC responses.
            self.pending.append(msg)

    def handle_request(self, msg, on_event):
        supported = ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                     'item/tool/requestUserInput', 'mcpServer/elicitation/request', 'item/permissions/requestApproval')
        params = msg.get('params') or {}
        if msg['method'] not in supported or params.get('threadId') != self.thread_id or params.get('turnId') != self.turn_id:
            on_event('unsupported_request', {'method': msg['method'], 'thread_matches': params.get('threadId') == self.thread_id, 'turn_matches': params.get('turnId') == self.turn_id})
            self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Unsupported runtime request'}})
            raise ProviderError('codex_request_unsupported', 'Runtime requested an unsupported action')
        if not self.on_request:
            raise ProviderError('codex_input_unavailable', 'Dashboard request handler unavailable')
        params = dict(params)
        params['_change_paths'] = [x.get('path', '') for x in self.items.get(params.get('itemId'), {}).get('changes', [])]
        result = self.on_request(msg['id'], msg['method'], params, self)
        on_event('request_dispatch', {'request_id': str(msg['id'])})
        self.send({'id': msg['id'], 'result': result})
        on_event('request_delivered', {'request_id': str(msg['id'])})

    def cancel(self):
        self.cancel_requested = True
        self.cancel_at = time.monotonic()
        if self.thread_id and self.turn_id and self.process and self.process.poll() is None:
            self.sequence += 1
            self.send({'id': self.sequence, 'method': 'turn/interrupt',
                       'params': {'threadId': self.thread_id, 'turnId': self.turn_id}})
            return True
        return False

    def models(self):
        return self.request('model/list', {}).get('data', [])

    def check_isolation(self, cwd):
        home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        protected = [str(home / 'auth.json'), str(home / 'config.toml'),
                     str(Path.home() / '.ssh'),
                     '/proc/' + str(self.process.pid) + '/environ']
        # Attempt an actual open and report booleans only. Never read credential bytes.
        # The parent authentication was checked independently with account/read.
        code = ('import os,json\n'
                'paths=' + repr(protected) + '\n'
                'def denied(p):\n try:\n  fd=os.open(p,os.O_RDONLY);os.close(fd);return False\n except (OSError,PermissionError):return True\n'
                'print(json.dumps({"protected_unreadable":all(denied(p) for p in paths),'
                '"secret_env_absent":not any(k in os.environ for k in ("OPENAI_API_KEY","CODEX_API_KEY","CODEX_HOME","DIZZI_PARENT_CANARY"))}))')
        result = self.request('command/exec', {'command': ['/usr/bin/python3', '-c', code],
            'cwd': str(cwd), 'permissionProfile': 'dizzi', 'timeoutMs': 5000}, 10)
        try:
            report = json.loads(result.get('stdout', ''))
        except ValueError:
            report = {}
        if result.get('exitCode') != 0 or report != {'protected_unreadable': True, 'secret_env_absent': True}:
            raise ProviderError('codex_isolation_failed', 'Engineering read isolation could not be verified')
        return report

    def run(self, record, on_event, *, cwd, allow_tools=False):
        model = record['request']['model']['id']
        if model not in {entry.get('model') for entry in self.models()}:
            raise ProviderError('codex_model_unavailable', 'Requested model is absent from Codex model discovery')
        instruction = record['request']['instruction']
        if allow_tools:
            on_event('isolation', self.check_isolation(cwd))
        self.on_request = self.config.get('on_request')
        resume = self.config.get('resume_thread')
        params = {'model': model, 'modelProvider': 'openai_chatgpt_plan' if self.config.get('auth_mode') == 'siwc' else 'openai', 'cwd': str(cwd),
                  'permissions': 'dizzi', 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user'}
        if resume:
            params['threadId'] = resume
        else:
            params['developerInstructions'] = ('Stay in the supplied workspace and use the fixed engineering browser tool. '
                'Every command has a 30 second host-enforced deadline. Never broaden permissions. '
                'At most two repair cycles. Do not launch background processes. Do not read credentials.' if allow_tools else
                'Answer directly using only task-scoped tools when needed. Do not run commands or read unrelated files.')
        thread = self.request('thread/resume' if resume else 'thread/start', params, 30)['thread']
        thread_id = thread['id']
        self.thread_id = thread_id
        on_event('thread', {'thread_id': thread_id, 'auth_mode': 'chatgpt', 'plan': self.plan,
                            'runtime_version': PINNED_VERSION, 'rate_limits': self.rate_limits})
        turn_params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': instruction}]}
        if self.config.get('collaboration_mode') == 'plan':
            turn_params['collaborationMode'] = {
                'mode': 'plan', 'settings': {'model': model, 'developer_instructions': None}}
        response = self.request('turn/start', turn_params, 30)
        turn_id = response['turn']['id']
        self.turn_id = turn_id
        if self.cancel_requested:
            self.cancel()
        on_event('turn', {'turn_id': turn_id})
        deadline = time.monotonic() + record['request']['limits']['max_duration_seconds']
        parts = []
        active_items = {}
        reported_usage = None
        final_status = None
        while final_status is None:
            if self.cancel_at and time.monotonic() - self.cancel_at > 10:
                raise ProviderError('codex_cancelled', 'Cancellation requested; Codex terminal event unavailable. Outcome may be uncertain.')
            try:
                tool_deadline = min((v[0] + v[1] for v in active_items.values()), default=deadline)
                if time.monotonic() >= min(deadline, tool_deadline):
                    raise ProviderError('codex_timeout', 'Runtime deadline reached')
                msg = self.pending.popleft() if self.pending else self.receive(min(deadline, tool_deadline))
            except ProviderError as exc:
                timed_out = next((k for k, v in active_items.items() if time.monotonic() >= v[0] + v[1]), None)
                if timed_out:
                    on_event('command_timeout', {'item_id': timed_out, 'termination': 'interrupt turn and terminate owned runtime process group'})
                try:
                    self.send({'id': 999999, 'method': 'turn/interrupt', 'params': {'threadId': thread_id, 'turnId': turn_id}})
                except Exception:
                    pass
                if timed_out:
                    raise ProviderError('codex_command_timeout', 'Command/tool deadline exceeded; no automatic retry') from None
                raise
            if 'id' in msg and 'method' in msg:
                started_wait = time.monotonic()
                command_deadlines = [start + limit for item_id, (start, limit) in active_items.items()
                                     if self.items.get(item_id, {}).get('type') == 'commandExecution']
                self.wait_deadline = min(command_deadlines, default=None)
                try:
                    self.handle_request(msg, on_event)
                finally:
                    self.wait_deadline = None
                waited = time.monotonic() - started_wait
                deadline += waited
                active_items = {k: (v[0] if self.items.get(k, {}).get('type') == 'commandExecution' else v[0] + waited, v[1])
                                for k, v in active_items.items()}
                continue
            method = msg.get('method', '')
            params = msg.get('params') or {}
            if method == 'item/agentMessage/delta' and params.get('turnId') == turn_id:
                delta = params.get('delta', '')
                if isinstance(delta, str):
                    parts.append(delta)
                    on_event('text', {'delta': delta})
            elif method == 'thread/tokenUsage/updated' and params.get('turnId') == turn_id:
                reported_usage = (params.get('tokenUsage') or {}).get('last')
                on_event('usage', {'last': reported_usage, 'total': (params.get('tokenUsage') or {}).get('total')})
            elif method in ('item/started', 'item/completed') and params.get('turnId') == turn_id:
                item = params.get('item') or {}
                kind = item.get('type')
                self.items[item.get('id')] = item
                if kind in ('commandExecution', 'fileChange', 'mcpToolCall'):
                    if method == 'item/started':
                        limit = (90 if item.get('server') == 'dizzi' else 40) if kind == 'mcpToolCall' else min(30, self.config.get('command_timeout', 30))
                        active_items[item['id']] = (time.monotonic(), limit)
                    else:
                        active_items.pop(item['id'], None)
                    on_event('item', {'phase': method, 'type': kind, 'item': item})
            elif method == 'turn/completed' and params.get('turn', {}).get('id') == turn_id:
                final_status = params['turn'].get('status')
                on_event('completed', {'status': final_status, 'error': params['turn'].get('error')})
        if final_status != 'completed':
            code = 'codex_cancelled' if self.cancel_requested and final_status == 'interrupted' else 'codex_turn_' + str(final_status)
            raise ProviderError(code, 'Codex turn ended without completion')
        return ''.join(parts), reported_usage, thread_id, turn_id
