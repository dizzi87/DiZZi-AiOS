"""Durable goal orchestration using portable records and bounded adapters."""
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import hashlib
import zipfile
import os
import re
import time
from pathlib import Path
import sqlite3
from threading import BoundedSemaphore, Lock, Thread
from uuid import uuid4

from contract_validation import validate
from client.little_dizzi import ClientError, DizziClient
from big_dizzi.artifacts import ArtifactError, ENGINEERING_INSTRUCTION, build
from big_dizzi.browser_check import verify_browser, verify_static_calculator
from big_dizzi.core import audit, lookup, route
from big_dizzi.providers import ProviderError, USAGE_KEYS, create_provider
from big_dizzi.codex_runtime import CodexRuntime


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def usage():
    return dict.fromkeys(USAGE_KEYS)


def health_answer(health, status):
    try:
        gateway = json.loads(health['result'])
        guest = json.loads(status['result'])
        if gateway.get('gateway') != 'ok':
            return 'Gateway returned an unrecognised health result. Inspect the task results below.'
        lines = ['Little Dizzi gateway is responding.', 'Observed: ' + status['timestamps']['ended_at']]
        uptime = guest.get('uptime_seconds')
        if isinstance(uptime, (int, float)):
            lines.append(f'AIOS guest uptime: {uptime / 86400:.1f} days.')
        for title, section, available, total in (
            ('Memory', 'memory_kib', 'MemAvailable', 'MemTotal'),
            ('Root disk', 'root_disk_bytes', 'available', 'total'),
        ):
            values = guest.get(section, {})
            if isinstance(values.get(total), (int, float)) and values[total] > 0 and isinstance(values.get(available), (int, float)):
                lines.append(f'{title}: {values[available] / values[total] * 100:.1f}% available.')
        lines.append('Scope: the gateway and AIOS guest. Proxmox, NAS and other homelab services were not checked.')
        return '\n'.join(lines)
    except (ValueError, TypeError, AttributeError):
        return 'Local tasks completed. Inspect their recorded results below; no wider system health claim is made.'


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'big-dizzi.sqlite3'
        self.lock = Lock()
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS goals
              (id TEXT PRIMARY KEY, instruction TEXT NOT NULL, route TEXT, status TEXT NOT NULL,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT, error TEXT);
              CREATE TABLE IF NOT EXISTS tasks
              (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), capability TEXT NOT NULL,
               record TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events
              (id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT);
              CREATE TABLE IF NOT EXISTS links
              (task_id TEXT PRIMARY KEY REFERENCES tasks(id), parent_id TEXT NOT NULL, attempt INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS approvals
              (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), action TEXT NOT NULL,
               decision TEXT NOT NULL, at TEXT NOT NULL, actor TEXT);
              CREATE TABLE IF NOT EXISTS runtime_requests
              (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
               rpc_id TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
               status TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS artifacts
              (goal_id TEXT PRIMARY KEY REFERENCES goals(id), manifest TEXT NOT NULL);''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_goal(self, instruction):
        goal_id = 'goal-' + uuid4().hex
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO goals VALUES (?,?,?,?,?,?,?,?)', (goal_id, instruction, None, 'pending', now(), now(), None, None))
            db.execute('INSERT INTO events(goal_id,at,event,detail) VALUES (?,?,?,?)', (goal_id, now(), 'submitted', None))
        return goal_id

    def update_goal(self, goal_id, **fields):
        if set(fields) - {'route', 'status', 'result', 'error'}:
            raise ValueError('invalid_goal_fields')
        fields['updated_at'] = now()
        with self.lock, self.connect() as db:
            db.execute('UPDATE goals SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), goal_id))

    def event(self, goal_id, event, detail=None):
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO events(goal_id,at,event,detail) VALUES (?,?,?,?)', (goal_id, now(), event, detail))

    def task(self, goal_id, capability, record, *, parent_id=None, attempt=1):
        validate(record)
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                       (record['task_id'], goal_id, capability, json.dumps(record)))
            db.execute('INSERT OR IGNORE INTO links VALUES (?,?,?)', (record['task_id'], parent_id or goal_id, attempt))

    def artifact(self, goal_id, manifest):
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO artifacts VALUES (?,?)', (goal_id, json.dumps(manifest)))

    def request_approval(self, goal_id, action):
        identity = 'approval-' + uuid4().hex
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?)', (identity, goal_id, action, 'requested', now(), None))
        self.event(goal_id, 'approval_requested', action)
        return identity

    def decide_approval(self, goal_id, identity, decision, *, actor='local-owner'):
        if decision not in ('approved', 'denied'):
            raise ValueError('invalid_approval_decision')
        with self.lock, self.connect() as db:
            changed = db.execute("UPDATE approvals SET decision=?,at=?,actor=? WHERE id=? AND goal_id=? AND decision='requested'",
                                 (decision, now(), actor, identity, goal_id)).rowcount
            if not changed:
                raise ValueError('approval_not_pending')
        self.event(goal_id, 'approval_' + decision, identity)
        # Approval records intent; it never creates an unsupported execution capability.
        status = 'blocked' if decision == 'approved' else 'cancelled'
        self.update_goal(goal_id, status=status, result='Approval recorded. A reviewed deployment package and supported execution path are still required.' if decision == 'approved' else 'Action denied; nothing dispatched.')
        self.export(goal_id)

    def get(self, goal_id):
        with self.connect() as db:
            goal = db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
            if goal is None:
                return None
            tasks = db.execute('SELECT t.capability,t.record,l.parent_id,l.attempt FROM tasks t LEFT JOIN links l ON l.task_id=t.id WHERE t.goal_id=? ORDER BY t.rowid', (goal_id,)).fetchall()
            events = db.execute('SELECT at,event,detail FROM events WHERE goal_id=? ORDER BY id', (goal_id,)).fetchall()
            approvals = db.execute('SELECT * FROM approvals WHERE goal_id=? ORDER BY rowid', (goal_id,)).fetchall()
            requests = db.execute('SELECT * FROM runtime_requests WHERE goal_id=? ORDER BY rowid', (goal_id,)).fetchall()
            artifact = db.execute('SELECT manifest FROM artifacts WHERE goal_id=?', (goal_id,)).fetchone()
        return {**dict(goal), 'tasks': [{'capability': t['capability'], 'record': json.loads(t['record']),
                                       'parent_id': t['parent_id'] or goal_id, 'attempt': t['attempt'],
                                       'adapter_retries': None} for t in tasks],
                'runtime_requests': [{**dict(r), 'payload': json.loads(r['payload']), 'response': json.loads(r['response']) if r['response'] else None} for r in requests],
                'events': [dict(e) for e in events], 'approvals': [dict(a) for a in approvals],
                'artifact': json.loads(artifact['manifest']) if artifact else None}

    def add_runtime_request(self, goal_id, task_id, rpc_id, method, payload):
        identity = 'request-' + uuid4().hex
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO runtime_requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identity, goal_id, task_id, str(rpc_id), method, json.dumps(payload), 'pending', None, now(), now()))
        self.update_goal(goal_id, status='awaiting_input' if method.endswith('requestUserInput') else 'awaiting_approval')
        self.event(goal_id, 'runtime_request_pending', identity)
        return identity

    def respond_runtime_request(self, goal_id, identity, response, *, actor='local-owner'):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM runtime_requests WHERE id=? AND goal_id=? AND status='pending'", (identity, goal_id)).fetchone()
            if row is None: raise ValueError('request_not_pending')
            payload = json.loads(row['payload'])
            if row['method'].endswith('requestUserInput'):
                answers = response.get('answers')
                expected = {q['id'] for q in payload['questions']}
                if not isinstance(answers, dict) or set(answers) != expected or any(not isinstance(v, str) or not 1 <= len(v) <= 2000 for v in answers.values()):
                    raise ValueError('invalid_input')
                result = {'answers': {k: {'answers': [v]} for k, v in answers.items()}}
            else:
                decision = response.get('decision')
                if decision not in ('approved', 'denied'): raise ValueError('invalid_decision')
                if decision == 'approved' and not payload.get('can_approve'): raise ValueError('outside_execution_boundary')
                if row['method'] == 'mcpServer/elicitation/request':
                    result = {'action': 'accept' if decision == 'approved' else 'decline', 'content': {} if decision == 'approved' else None}
                elif row['method'] == 'item/permissions/requestApproval':
                    result = {'permissions': {}, 'scope': 'turn'}
                else:
                    result = {'decision': 'accept' if decision == 'approved' else 'decline'}
            changed = db.execute("UPDATE runtime_requests SET response=?,status='answered',updated_at=? WHERE id=? AND status='pending'", (json.dumps(result), now(), identity)).rowcount
            if changed != 1: raise ValueError('duplicate_response')
        self.event(goal_id, 'runtime_request_answered', identity + ' by ' + actor)

    def recent(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,instruction,route,status,created_at,updated_at FROM goals ORDER BY rowid DESC LIMIT 20')]

    def export(self, goal_id):
        goal = self.get(goal_id)
        directory = self.directory / 'outcomes'
        directory.mkdir(mode=0o700, exist_ok=True)
        summary = (f"# {goal_id}\n\nUpdated: {goal['updated_at']}\n\nGoal: {goal['instruction']}\n\n"
                   f"Route: {goal['route']}\nStatus: {goal['status']}\n\nResult: {goal['result'] or 'None yet'}\n\n"
                   f"Error: {goal['error'] or 'None'}\n\nEvidence tasks: " + ', '.join(t['record']['task_id'] for t in goal['tasks']) + '\n')
        for extension, text in (('md', summary), ('json', json.dumps(goal, indent=2))):
            target = directory / f'{goal_id}.{extension}'
            temporary = target.with_suffix('.' + extension + '.tmp')
            temporary.write_text(text, encoding='utf-8')
            os.replace(temporary, target)

    def recover_interrupted(self):
        with self.connect() as db:
            ids = [r['id'] for r in db.execute("SELECT id FROM goals WHERE status IN ('pending','running','awaiting_input','awaiting_approval') AND (status != 'awaiting_approval' OR EXISTS (SELECT 1 FROM runtime_requests WHERE runtime_requests.goal_id=goals.id))")]
        for goal_id in ids:
            with self.connect() as db:
                db.execute("UPDATE runtime_requests SET status='interrupted',updated_at=? WHERE goal_id=? AND status IN ('pending','answered','dispatching')", (now(), goal_id))
            for task in self.get(goal_id)['tasks']:
                record = task['record']
                if record['status'] in ('pending', 'running'):
                    record['status'] = 'failed'
                    record['timestamps']['ended_at'] = now()
                    record['error'] = {'code': 'orchestrator_interrupted', 'message': 'Execution observation interrupted; remote outcome may be unknown. No replay was made.'}
                    self.task(goal_id, task['capability'], record)
            self.update_goal(goal_id, status='interrupted', error='Service restarted during this goal. Check existing task IDs before retrying; no work was replayed.')
            self.event(goal_id, 'interrupted', 'Retained task IDs; remote outcomes may be unknown')
            self.export(goal_id)


def new_task(instruction, capability, config, *, runtime='configured-provider'):
    record = {'contract_version': '0', 'task_id': 'task-' + uuid4().hex,
              'request': {'instruction': instruction, 'worker_id': capability, 'runtime': runtime,
                          'model': {'provider': config['provider']['name'], 'id': config['provider']['models'][capability]},
                          'reasoning_profile': 'configured', 'allowed_tools': [], 'approval_scope': {'mode': 'deny', 'actions': []},
                          'limits': {'max_duration_seconds': 90, 'max_tool_calls': 0, 'max_input_tokens': None,
                                     'max_output_tokens': config['provider'].get('max_output_tokens', {}).get(capability, 1200)}},
              'actual': None, 'timestamps': {'created_at': now(), 'started_at': None, 'ended_at': None},
              'status': 'pending', 'result': None, 'error': None, 'tool_events': [], 'approval_events': [],
              'usage': usage(), 'runtime_metadata': {}}
    validate(record)
    return record


def local_record(capability, instruction=''):
    config = {'provider': {'name': 'local', 'models': {capability: capability}}}
    record = new_task(instruction or capability, capability, config, runtime='little-dizzi-gateway-v0')
    record['request']['worker_id'] = 'little-dizzi'
    record['request']['reasoning_profile'] = 'none'
    record['request']['limits']['max_duration_seconds'] = 75
    record['request']['limits']['max_output_tokens'] = None
    # The actual Ollama model is learned from the returned record, not guessed here.
    return record


class Orchestrator:
    def __init__(self, config, *, provider=None, client=None):
        self.config = config
        self.store = Store(config['state_dir'])
        self.provider = provider or (None if config['provider']['name'] == 'codex' else create_provider(config['provider']))
        self.client = client or DizziClient()
        self.slots = BoundedSemaphore(config.get('max_active_goals', 2))
        self.active = {}
        self.active_lock = Lock()

    def reconcile_interrupted(self, goal_id=None, *, force=False):
        with self.store.connect() as db:
            ids = [goal_id] if goal_id else [row['id'] for row in db.execute("SELECT id FROM goals WHERE status='interrupted' ORDER BY rowid DESC LIMIT 20")]
        for goal_id in ids:
            goal = self.store.get(goal_id)
            for task in goal['tasks']:
                record = task['record']
                metadata = record.get('runtime_metadata') or {}
                thread_id, turn_id = metadata.get('thread_id'), metadata.get('turn_id')
                if not thread_id or not turn_id or (metadata.get('recovery_checked_at') and not force):
                    continue
                config = dict(self.config['provider'])
                config['enable_little_dizzi'] = task['capability'] == 'health'
                try:
                    with CodexRuntime(config) as runtime:
                        history = runtime.request('thread/read', {'threadId': thread_id, 'includeTurns': True}, 20)
                    turns = (history.get('thread') or {}).get('turns') or []
                    found = next((turn for turn in turns if turn.get('id') == turn_id), None)
                    metadata['recovery_turn_status'] = found.get('status') if found else 'not_found'
                    metadata['recovery_checked_at'] = now()
                    record['runtime_metadata'] = metadata
                    self.store.task(goal_id, task['capability'], record)
                    self.store.event(goal_id, 'runtime_history_reconciled', metadata['recovery_turn_status'])
                except Exception as exc:
                    self.store.event(goal_id, 'runtime_history_unavailable', exc.code if isinstance(exc, ProviderError) else 'runtime_unavailable')
            parent_ids = {task['record']['task_id'] for task in goal['tasks'] if task['capability'] == 'health' and task['record']['request']['runtime'] == 'codex-app-server'}
            audit_path = Path.home() / '.local/share/little-dizzi-connector/audit.jsonl'
            if parent_ids and audit_path.is_file():
                known = {task['record']['task_id'] for task in self.store.get(goal_id)['tasks']}
                with audit_path.open('rb') as handle:
                    handle.seek(max(0, audit_path.stat().st_size - 2_000_000))
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        task_id = event.get('task_id')
                        if event.get('status') != 'dispatch_started' or event.get('parent_task_id') not in parent_ids or task_id in known:
                            continue
                        try:
                            child = self.client.read(task_id)
                            validate(child)
                            if child['task_id'] != task_id or child['request']['model']['id'] not in ('health', 'system_status'):
                                raise ValueError('readback_mismatch')
                            self.store.task(goal_id, child['request']['model']['id'], child, parent_id=event['parent_task_id'])
                            self.store.event(goal_id, 'little_dizzi_readback_recovered', task_id)
                            known.add(task_id)
                        except Exception:
                            self.store.event(goal_id, 'little_dizzi_readback_uncertain', task_id)
            self.store.export(goal_id)

    def runtime_request(self, goal_id, record, rpc_id, method, params, runtime):
        workspace = (self.store.directory / 'workspaces' / goal_id).resolve()
        if method.endswith('requestUserInput'):
            questions = params.get('questions')
            if (params.get('isBlocking') is not True or not isinstance(params.get('itemId'), str)
                    or not params['itemId'] or not isinstance(questions, list)
                    or not 1 <= len(questions) <= 3 or any(q.get('isSecret') for q in questions)):
                raise ProviderError('runtime_input_unsupported', 'Secret or unsupported input request refused')
            payload = {'thread_id': runtime.thread_id, 'turn_id': runtime.turn_id,
                       'item_id': params['itemId'], 'is_blocking': True,
                       'questions': [{k: q[k] for k in ('id', 'header', 'question', 'options') if k in q} for q in questions]}
        else:
            paths = params.get('_change_paths', [])
            confined = bool(paths) and all(Path(p).resolve().is_relative_to(workspace) for p in paths)
            # Native command approvals may bypass the sandbox. Only bounded file edits
            # with observed paths can be accepted; no session/policy amendments.
            allowed = method == 'item/fileChange/requestApproval' and confined and not params.get('grantRoot')
            if method == 'mcpServer/elicitation/request':
                schema = params.get('requestedSchema', {})
                allowed = (params.get('serverName') in ('engineering', 'dizzi') and params.get('mode') == 'form'
                           and schema == {'type': 'object', 'properties': {}})
            command = params.get('command') or ''
            if re.search(r'(?i)token|secret|password|credential|authorization|api[_-]?key', command): command = '[sensitive command withheld]'
            payload = {'action': ('Approve scoped ' + params.get('serverName', '') + ' tool' if method == 'mcpServer/elicitation/request' else method.split('/')[1]), 'command': command[:500], 'paths': paths,
                       'reason': str(params.get('reason') or params.get('message') or '')[:500], 'can_approve': allowed,
                       'boundary': 'Only the configured bounded operation; no broader permissions' if allowed else 'Requested authority cannot be safely granted by this v0 boundary; deny or cancel.'}
        identity = self.store.add_runtime_request(goal_id, record['task_id'], rpc_id, method, payload)
        record['runtime_metadata']['pending_request'] = identity
        self.store.task(goal_id, record['request']['worker_id'], record)
        self.store.export(goal_id)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if runtime.cancel_requested: raise ProviderError('codex_cancelled', 'Cancelled while waiting for owner')
            wait_deadline = getattr(runtime, 'wait_deadline', None)
            if isinstance(wait_deadline, (int, float)) and time.monotonic() >= wait_deadline:
                self.store.event(goal_id, 'command_timeout', 'Concurrent command exceeded its deadline while approval was pending')
                runtime.cancel()
                raise ProviderError('codex_command_timeout', 'Concurrent command deadline exceeded; no automatic retry')
            if runtime.process and runtime.process.poll() is not None:
                raise ProviderError('codex_runtime_exit', 'Runtime disconnected while waiting; reply was not replayed')
            with self.store.connect() as db:
                row = db.execute('SELECT status,response FROM runtime_requests WHERE id=?', (identity,)).fetchone()
                if row['status'] == 'answered':
                    db.execute("UPDATE runtime_requests SET status='dispatching',updated_at=? WHERE id=?", (now(), identity))
                    result = json.loads(row['response'])
                else:
                    result = None
            if result is not None:
                self.store.update_goal(goal_id, status='running')
                return result
            time.sleep(.1)
        raise ProviderError('codex_input_timeout', 'Owner input deadline reached; no action replayed')

    def continue_goal(self, goal_id, instruction):
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000:
            raise ValueError('continuation_instruction_required')
        if not self.slots.acquire(blocking=False): raise ValueError('capacity_busy')
        claimed = False
        try:
            if (self.store.get(goal_id) or {}).get('status') != 'interrupted':
                raise ValueError('goal_not_interrupted')
            self.reconcile_interrupted(goal_id, force=True)
            checked = [t for t in self.store.get(goal_id)['tasks']
                       if t['record']['request']['runtime'] == 'codex-app-server'
                       and t['record']['runtime_metadata'].get('recovery_checked_at')]
            if not checked:
                raise ValueError('reconciliation_unavailable')
            with self.store.lock, self.store.connect() as db:
                changed = db.execute("UPDATE goals SET status='running',updated_at=? WHERE id=? AND status='interrupted'", (now(), goal_id)).rowcount
                if changed != 1: raise ValueError('goal_not_interrupted')
                claimed = True
            goal = self.store.get(goal_id)
            parents = [t for t in goal['tasks'] if t['record']['request']['runtime'] == 'codex-app-server']
            if not parents: raise ValueError('no_runtime_history')
            task = parents[-1]
            if not task['record']['runtime_metadata'].get('thread_id'): raise ValueError('no_runtime_history')
            # v0 continuation is inspection/finalisation only, with all mutating tools
            # disabled. This avoids replaying any uncertain engineering or local action.
            self.store.event(goal_id, 'continuation_authorized', 'Read-only inspection; no replay of earlier actions')
            Thread(target=self._continue, args=(goal_id, task, instruction), daemon=True).start()
        except Exception:
            if claimed:
                self.store.update_goal(goal_id, status='interrupted')
            self.slots.release()
            raise

    def _continue(self, goal_id, task, instruction):
        try:
            record = self.codex_task(goal_id, task['capability'],
                'Recovery inspection only. Do not repeat any previous command or tool action. '
                'Inspect completed evidence only as needed, explain remaining uncertainty, and answer: ' + instruction,
                existing=task['record'])
            self.store.update_goal(goal_id, status='completed', result=record['result'], error=None)
        except Exception as exc:
            self.store.update_goal(goal_id, status='interrupted', error=str(exc) if isinstance(exc, ProviderError) else 'Continuation failed; no replay')
        finally:
            self.store.export(goal_id); self.slots.release()

    def cancel(self, goal_id):
        with self.active_lock:
            runtime = self.active.get(goal_id)
        if runtime is None:
            raise ValueError('goal_not_running')
        self.store.event(goal_id, 'cancel_requested', 'Owner requested Codex turn interruption')
        runtime.cancel()

    def submit(self, instruction, *, asynchronous=True):
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000:
            raise ValueError('instruction must contain 1–4000 characters')
        if not self.slots.acquire(blocking=False):
            raise ValueError('capacity_busy: wait for an active goal to finish')
        try:
            goal_id = self.store.create_goal(instruction.strip())
            if asynchronous:
                Thread(target=self.run, args=(goal_id,), daemon=True).start()
        except Exception:
            self.slots.release()
            raise
        if not asynchronous:
            self.run(goal_id)
        return goal_id

    def cloud(self, goal_id, capability, instruction, *, parent_id=None, attempt=1):
        if self.config['provider']['name'] == 'codex' and self.provider is None:
            return self.codex_task(goal_id, capability, instruction, parent_id=parent_id, attempt=attempt)
        runtime = getattr(self.provider, 'runtime', 'configured-provider')
        record = new_task(instruction, capability, self.config, runtime=runtime)
        self.store.task(goal_id, capability, record, parent_id=parent_id, attempt=attempt)
        self.store.event(goal_id, 'task_started', capability)
        record['status'] = 'running'
        record['timestamps']['started_at'] = now()
        self.store.task(goal_id, capability, record)

        def observed(counts, actual_model, provider_id):
            record['usage'] = counts
            record['actual'] = {'worker_id': capability, 'runtime': runtime,
                                'model': {'provider': self.config['provider']['name'], 'id': actual_model} if actual_model else None,
                                'reasoning_profile': None}
            record['runtime_metadata'] = {'provider_response_id': provider_id} if provider_id else {}
        try:
            result, counts, actual_model, provider_id = self.provider.call(record)
            observed(counts, actual_model, provider_id)
            record['result'] = result
            record['status'] = 'completed'
        except Exception as exc:
            record['status'] = 'failed'
            if isinstance(exc, ProviderError):
                record['error'] = {'code': exc.code, 'message': str(exc)}
                if exc.observation:
                    observed(*exc.observation)
            else:
                record['error'] = {'code': 'provider_failure', 'message': 'Cloud request failed; no automatic replay'}
            raise
        finally:
            record['timestamps']['ended_at'] = now()
            self.store.task(goal_id, capability, record)
            self.store.event(goal_id, 'task_' + record['status'], capability)
        return record

    def codex_task(self, goal_id, capability, instruction, *, parent_id=None, attempt=1, existing=None):
        record = existing or new_task(instruction, capability, self.config, runtime='codex-app-server')
        if existing:
            record['runtime_metadata'].setdefault('initial_instruction', record['request']['instruction'])
            record['runtime_metadata'].setdefault('prior_outcomes', []).append({'result': record['result'], 'error': record['error'], 'usage': record['usage'], 'timestamps': dict(record['timestamps'])})
            record['runtime_metadata'].setdefault('prior_turns', []).append({k: record['runtime_metadata'].get(k) for k in ('turn_id', 'turn_status')})
            record['request']['instruction'] = instruction
            record['error'] = None
            record['timestamps']['ended_at'] = None
        engineering = capability == 'engineer' and existing is None
        record['request']['allowed_tools'] = ['commandExecution', 'fileChange', 'engineering.verify_calculator'] if engineering else (['read_only_commands', 'submit_dizzi_task', 'read_dizzi_task'] if capability == 'health' and existing is None else ['read_only_commands'])
        record['request']['limits']['max_duration_seconds'] = 300 if engineering else 150
        record['request']['limits']['max_tool_calls'] = 50 if engineering else (8 if capability == 'health' else 20)
        record['runtime_metadata']['limits_enforcement'] = {'duration': 'Big Dizzi deadline', 'tool_calls': 'Big Dizzi event count', 'output_tokens': 'requested; runtime enforcement unverified', 'command_timeout': '30 seconds per command; 40 browser / 90 Dizzi tool; owned process termination'}
        workspace = self.store.directory / 'workspaces' / goal_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.store.task(goal_id, capability, record, parent_id=parent_id, attempt=attempt)
        self.store.event(goal_id, 'task_started', capability)
        record['status'] = 'running'
        record['timestamps']['started_at'] = now()
        self.store.task(goal_id, capability, record)
        text_parts = []
        tool_count = 0
        def on_event(kind, data):
            nonlocal tool_count
            if kind == 'isolation':
                record['runtime_metadata']['isolation'] = data
                self.store.event(goal_id, 'isolation_verified', json.dumps(data))
            elif kind == 'thread':
                record['runtime_metadata'].update(data)
                record['actual'] = {'worker_id': capability, 'runtime': 'codex-app-server', 'model': None, 'reasoning_profile': None}
            elif kind == 'turn':
                record['runtime_metadata'].update(data)
            elif kind == 'text':
                text_parts.append(data['delta'])
                record['result'] = ''.join(text_parts)
            elif kind == 'usage' and isinstance(data.get('last'), dict):
                u = data['last']
                record['usage'] = {'input_tokens': u.get('inputTokens'), 'output_tokens': u.get('outputTokens'),
                    'cache_read_tokens': u.get('cachedInputTokens'), 'cache_write_tokens': u.get('cacheWriteInputTokens'),
                    'reasoning_tokens': u.get('reasoningOutputTokens'), 'context_tokens': None}
                record['runtime_metadata']['conversation_usage'] = data.get('total')
            elif kind == 'item':
                if data['phase'] == 'item/started':
                    tool_count += 1
                    if tool_count > record['request']['limits']['max_tool_calls']:
                        raise ProviderError('codex_tool_limit', 'Codex exceeded the local tool-call limit; outcome may be uncertain')
                item = data['item']
                command = item.get('command') if engineering and isinstance(item.get('command'), str) else None
                redacted = '[redacted command]' if command and re.search(r'(?i)token|secret|password|credential|authorization|api[_-]?key', command) else command[:500] if command else None
                safe = {'item_id': item.get('id'), 'type': data['type'], 'phase': data['phase'], 'status': item.get('status'),
                        'command': redacted, 'command_sha256': hashlib.sha256(command.encode()).hexdigest() if command else None,
                        'exit_code': item.get('exitCode'), 'tool': item.get('tool'), 'server': item.get('server')}
                record['runtime_metadata'].setdefault('execution', []).append(safe)
                if data['type'] == 'mcpToolCall' and data['phase'] == 'item/completed' and item.get('server') == 'dizzi':
                    payload = (item.get('result') or {}).get('structuredContent') or {}
                    child = payload.get('record')
                    if isinstance(child, dict) and item.get('tool') in ('submit_dizzi_task', 'read_dizzi_task'):
                        validate(child)
                        capability_name = child['request']['worker_id'] if child['request']['worker_id'] in ('health', 'system_status', 'local_inference') else child['request']['model']['id']
                        self.store.task(goal_id, capability_name, child, parent_id=record['task_id'])
                        safe['little_task_id'] = child['task_id']
                        safe['capability'] = capability_name
                self.store.event(goal_id, 'runtime_' + data['type'], str(safe)[:300])
            elif kind in ('request_dispatch', 'request_delivered'):
                identity = record['runtime_metadata'].get('pending_request')
                if identity and kind == 'request_delivered':
                    with self.store.connect() as db:
                        db.execute("UPDATE runtime_requests SET status='delivered',updated_at=? WHERE id=?", (now(), identity))
                    record['runtime_metadata'].pop('pending_request', None)
                self.store.event(goal_id, kind, identity)
            elif kind == 'command_timeout':
                record['runtime_metadata']['command_timeout'] = data
                self.store.event(goal_id, 'command_timeout', json.dumps(data))
            elif kind == 'unsupported_request':
                self.store.event(goal_id, 'runtime_input_required', data['method'])
            elif kind == 'completed':
                record['runtime_metadata']['turn_status'] = data['status']
            self.store.task(goal_id, capability, record)
        try:
            runtime_config = dict(self.config['provider'])
            runtime_config['enable_little_dizzi'] = capability == 'health' and existing is None
            runtime_config['workspace'] = str(workspace)
            runtime_config['engineering'] = engineering
            runtime_config['browser'] = self.config.get('artifact_browser', {})
            runtime_config['browser_evidence'] = str(self.store.directory / 'verification' / (record['task_id'] + '-browser.json'))
            runtime_config['on_request'] = lambda ident, method, params, child: self.runtime_request(goal_id, record, ident, method, params, child)
            if existing:
                runtime_config['resume_thread'] = record['runtime_metadata']['thread_id']
            runtime_config['parent_task_id'] = record['task_id']
            with CodexRuntime(runtime_config) as runtime:
                with self.active_lock:
                    self.active[goal_id] = runtime
                try:
                    result, _, _, _ = runtime.run(record, on_event, cwd=workspace, allow_tools=engineering)
                finally:
                    with self.active_lock:
                        self.active.pop(goal_id, None)
            record['result'] = result
            if capability == 'health' and existing is None:
                children = [t for t in self.store.get(goal_id)['tasks'] if t['parent_id'] == record['task_id']]
                if {t['capability'] for t in children} != {'health', 'system_status'} or any(t['record']['status'] != 'completed' for t in children):
                    raise ProviderError('little_dizzi_unverified', 'Approved health and system-status tool results were not both validated')
            record['status'] = 'completed'
        except ProviderError as exc:
            record['status'] = 'failed'
            record['error'] = {'code': exc.code, 'message': str(exc)}
            raise
        finally:
            with self.store.connect() as db:
                db.execute("UPDATE runtime_requests SET status='interrupted',updated_at=? WHERE task_id=? AND status IN ('pending','answered','dispatching')", (now(), record['task_id']))
            record['timestamps']['ended_at'] = now()
            self.store.task(goal_id, capability, record)
            self.store.event(goal_id, 'task_' + record['status'], capability)
        return record

    def preserve_engineering_artifact(self, goal_id):
        workspace = self.store.directory / 'workspaces' / goal_id
        files = []
        for path in sorted(workspace.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix in ('.py', '.html', '.js', '.css') and path.stat().st_size <= 200_000:
                files.append(path)
        if not files:
            return None
        target = self.store.directory / 'artifacts' / goal_id
        target.mkdir(parents=True, exist_ok=True)
        bundle = target / 'engineering.zip'
        with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.name)
        manifest = {'title': 'Codex engineering files', 'url': '/api/goals/' + goal_id + '/artifact-bundle',
                    'previewable': False, 'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(), 'files': [{'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
                    'verification': {'browser_verified': False, 'limitation': 'Browser acceptance is pending.'}}
        page = workspace / 'index.html'
        browser = self.config.get('artifact_browser')
        if page in files:
            for task in self.store.get(goal_id)['tasks']:
                report_path = self.store.directory / 'verification' / (task['record']['task_id'] + '-browser.json')
                if not report_path.is_file(): continue
                report = json.loads(report_path.read_text())
                calls = task['record']['runtime_metadata'].get('execution', [])
                directed = any(e.get('server') == 'engineering' and e.get('tool') == 'verify_calculator' and e.get('phase') == 'item/completed' and e.get('status') == 'completed' for e in calls)
                if directed and report.get('browser_verified') and report.get('artifact_sha256') == hashlib.sha256(page.read_bytes()).hexdigest():
                    preview = target / 'index.html'; preview.write_bytes(page.read_bytes())
                    manifest.update({'previewable': True, 'preview_url': '/api/goals/' + goal_id + '/artifact',
                                     'preview_sha256': report['artifact_sha256']})
                    manifest['verification'] = {'browser_verified': True, 'codex_directed': True, 'browser': report,
                        'source': report['source'], 'limitation': 'Bounded local calculator only; no deployed service claimed.'}
        self.store.artifact(goal_id, manifest)
        return manifest

    def local(self, goal_id, capability, instruction=''):
        record = local_record(capability, instruction)
        task_id = record['task_id']
        self.store.task(goal_id, capability, record)
        record['status'] = 'running'
        record['timestamps']['started_at'] = now()
        self.store.task(goal_id, capability, record)
        self.store.event(goal_id, 'task_started', capability)
        try:
            returned = self.client.submit(capability, instruction, task_id=task_id)
            validate(returned)
            if returned['task_id'] != task_id:
                raise ValueError('local_task_id_mismatch')
            record = returned
            if record['status'] != 'completed':
                raise ClientError('local_task_failed', 'Little Dizzi task did not complete', task_id)
        except Exception as exc:
            if isinstance(exc, ClientError) and exc.task_id == task_id:
                try:
                    saved = self.client.read(task_id)
                    validate(saved)
                    if saved['task_id'] == task_id:
                        record = saved
                        if saved['status'] == 'completed':
                            self.store.event(goal_id, 'readback_recovered', task_id)
                            return record
                except Exception:
                    pass
            record['status'] = 'failed'
            record['timestamps']['ended_at'] = now()
            record['error'] = {'code': 'local_call_unverified', 'message': 'Local call did not return a verified result. Retain this task ID for readback; do not blindly replay.'}
            raise
        finally:
            self.store.task(goal_id, capability, record)
            self.store.event(goal_id, 'task_' + record['status'], capability)
        return record

    def application(self, goal_id, goal, context):
        context_text = '\n'.join(x['text'][:5000] for x in context)
        plan = self.cloud(goal_id, 'plan_goal', 'Plan a small self-contained numeric browser calculator. If the goal needs other capabilities, explain that limit. Goal: ' + goal['instruction'] + '\nSelected context:\n' + context_text)
        instruction = ENGINEERING_INSTRUCTION + '\nGoal: ' + goal['instruction'] + '\nPlan:\n' + plan['result']
        for attempt in (1, 2):
            proposal = self.cloud(goal_id, 'engineer', instruction, parent_id=plan['task_id'], attempt=attempt)
            check = local_record('verify_artifact')
            check['request']['runtime'] = 'big-dizzi-calculator-builder'
            check['request']['worker_id'] = 'artifact-verifier'
            check['request']['allowed_tools'] = ['build_calculator_artifact', 'verify_calculator_browser']
            check['request']['limits']['max_tool_calls'] = 2
            check['request']['approval_scope'] = {'mode': 'preapproved', 'actions': ['write_goal_artifact']}
            check['approval_events'] = [{'action': 'write_goal_artifact', 'decision': 'approved',
                                         'actor_id': 'local-owner-goal', 'at': now()}]
            check['status'] = 'running'
            check['timestamps']['started_at'] = now()
            check['actual'] = {'worker_id': 'artifact-verifier', 'runtime': 'big-dizzi-calculator-builder', 'model': None, 'reasoning_profile': None}
            self.store.task(goal_id, 'verify_artifact', check, parent_id=proposal['task_id'], attempt=attempt)
            failure = None
            try:
                manifest = build(self.config['state_dir'], goal_id, proposal['result'], goal['instruction'])
                browser = self.config.get('artifact_browser')
                if browser:
                    artifact_path = Path(self.config['state_dir']) / 'artifacts' / goal_id / 'index.html'
                    try:
                        browser_report = verify_browser(artifact_path, browser)
                        manifest['verification']['browser_verified'] = True
                        manifest['verification']['browser'] = browser_report
                        manifest['verification']['limitation'] = 'Verified bounded calculator interactions at a mobile viewport; no deployment or other application classes claimed.'
                    except ValueError as exc:
                        manifest['verification']['browser_error'] = str(exc)
                    (artifact_path.parent / 'verification.json').write_text(json.dumps(manifest['verification'], indent=2), encoding='utf-8')
                self.store.artifact(goal_id, manifest)
                check['tool_events'].append({'tool': 'build_calculator_artifact', 'started_at': check['timestamps']['started_at'],
                                             'ended_at': now(), 'duration_ms': None,
                                             'result': 'Built bounded artifact; SHA-256 ' + manifest['sha256'], 'error': None})
                if browser:
                    check['tool_events'].append({'tool': 'verify_calculator_browser', 'started_at': check['timestamps']['started_at'],
                                                 'ended_at': now(), 'duration_ms': None,
                                                 'result': 'Browser checks passed' if manifest['verification']['browser_verified'] else None,
                                                 'error': manifest['verification'].get('browser_error')})
                check['result'] = json.dumps(manifest['verification'])
                check['status'] = 'completed'
            except (ArtifactError, ValueError, TypeError, KeyError, OverflowError) as exc:
                failure = str(exc) if isinstance(exc, ArtifactError) else 'invalid_artifact_shape'
                check['status'] = 'failed'
                check['error'] = {'code': 'artifact_verification_failed', 'message': failure}
            finally:
                check['timestamps']['ended_at'] = now()
                self.store.task(goal_id, 'verify_artifact', check)
            if failure is None:
                self.store.event(goal_id, 'artifact_built', manifest['title'])
                verified = manifest['verification']['browser_verified']
                self.store.update_goal(goal_id, status='completed' if verified else 'needs_verification', result='Built ' + manifest['title'] + ('. Calculation and browser interaction checks passed at 390×844. Open the artifact below.' if verified else '. Calculation checks passed. Open the artifact below. Browser interaction and mobile verification are still required before application acceptance.'))
                return
            if failure == 'unsupported_application_scope' or attempt == 2:
                raise ArtifactError(failure)
            self.store.event(goal_id, 'engineering_retry', 'Repair attempt 2 after ' + failure)
            instruction += '\nThe previous artifact failed validation: ' + failure + '\nPrevious JSON:\n' + proposal['result'][:60_000] + '\nReturn corrected JSON.'

    def run(self, goal_id):
        try:
            goal = self.store.get(goal_id)
            selected, keys = route(goal['instruction'])
            self.store.update_goal(goal_id, route=selected, status='running')
            self.store.event(goal_id, 'routed', selected)
            context = lookup(self.config['core_root'], keys)
            self.store.event(goal_id, 'context_loaded', ', '.join(x['route'] for x in context) or 'No project context required')
            if selected == 'approval_required':
                self.store.request_approval(goal_id, goal['instruction'])
                self.store.update_goal(goal_id, status='awaiting_approval', result='This action requires a reviewed execution plan and owner approval. v0 has no deployment or destructive-action executor. Recording approval alone will not execute it.')
                return
            if selected == 'local_health':
                if self.config['provider']['name'] == 'codex' and self.provider is None:
                    outcome = self.codex_task(goal_id, 'health', 'Check Dizzi-AIOS health using the approved Dizzi MCP tools. Call both health and system_status. Report only the AIOS guest scope. Original goal: ' + goal['instruction'])['result']
                else:
                    health = self.local(goal_id, 'health')
                    status = self.local(goal_id, 'system_status')
                    outcome = health_answer(health, status)
            elif selected == 'local_inference':
                outcome = self.local(goal_id, 'local_inference', goal['instruction'].split(':', 1)[1].strip())['result']
            elif selected == 'core_lookup':
                outcome = '\n\n'.join(x['path'] + '\n' + x['text'] for x in context)
            elif selected == 'core_audit':
                findings = audit(self.config['core_root'])
                outcome = json.dumps(findings, indent=2) if findings else 'No findings in the selected Core routes. This is a scoped audit, not a full-vault certification.'
            elif selected == 'cloud_reason':
                outcome = self.cloud(goal_id, 'reason', goal['instruction'])['result']
            else:
                if self.config['provider']['name'] == 'codex' and self.provider is None:
                    engineering_goal = (goal['instruction'] + '\nImplement a self-contained static calculator in index.html without backend/network. '
                        'Create and run automated tests. Use form id calculator, fields first/operator/second, result id result. '
                        'Operators add/subtract/multiply/divide. Output exactly Result: N or Cannot divide by zero. '
                        'Call engineering.verify_calculator to perform independent browser checks. At most two repair cycles. '
                        'Do not attempt direct Chromium or change sandbox/trust configuration.')
                    outcome = self.codex_task(goal_id, 'engineer', engineering_goal)['result']
                    manifest = self.preserve_engineering_artifact(goal_id)
                    verified = bool(manifest and manifest['verification'].get('codex_directed'))
                    self.store.update_goal(goal_id, status='completed' if verified else 'needs_verification', result=outcome)
                    self.store.event(goal_id, 'engineering_verified' if verified else 'browser_verification_pending')
                    return
                else:
                    self.application(goal_id, goal, context)
                    return
            self.store.update_goal(goal_id, status='completed', result=outcome)
            self.store.event(goal_id, 'goal_completed')
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ProviderError, ArtifactError)) else 'Goal failed; inspect its task records. No automatic replay was made.'
            cancelled = isinstance(exc, ProviderError) and exc.code == 'codex_cancelled'
            uncertain = isinstance(exc, ProviderError) and exc.code in ('codex_runtime_exit', 'codex_timeout', 'codex_turn_interrupted', 'codex_command_timeout')
            self.store.update_goal(goal_id, status='cancelled' if cancelled else ('interrupted' if uncertain else 'failed'), error=message)
            self.store.event(goal_id, 'goal_cancelled' if cancelled else 'goal_failed', message)
        finally:
            try:
                self.store.export(goal_id)
            finally:
                self.slots.release()
