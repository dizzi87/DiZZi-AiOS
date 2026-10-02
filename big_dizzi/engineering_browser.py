"""Fixed Codex-callable browser check, isolated from owner files and networking."""
import argparse
import hashlib
import fcntl
import stat
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def verify(workspace, browser, library_path, timeout=35):
    workspace = Path(workspace).resolve(strict=True)
    page = workspace / 'index.html'
    if page.is_symlink() or not page.is_file() or page.stat().st_size > 200_000:
        raise ValueError('browser_artifact_invalid')
    browser = Path(browser).resolve(strict=True)
    libraries = Path(library_path).resolve(strict=True)
    # No user-controlled command, executable, URL or host mount enters this interface.
    module = Path(__file__).with_name('browser_check.py').resolve()
    with tempfile.TemporaryDirectory(prefix='dizzi-browser-boundary-') as temp:
        root = Path(temp)
        snapshot = root / 'index.html'
        fd = os.open(page, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 200_000:
                raise ValueError('browser_artifact_invalid')
            snapshot.write_bytes(source.read(200_001))
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        script = ('import json,sys;sys.path.insert(0,"/verifier");'
                  'from browser_check import verify_static_calculator;'
                  'print(json.dumps(verify_static_calculator("/input/index.html",'
                  '{"executable":"/browser/' + browser.name + '","no_sandbox":True,'
                  '"library_path":"/libraries"})))')
        cmd = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
               '--cap-drop', 'ALL', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
               '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp',
               '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
               '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
               '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
               '--dir', '/input', '--ro-bind', str(snapshot), '/input/index.html',
               '--ro-bind', str(browser.parent), '/browser', '--ro-bind', str(libraries), '/libraries',
               '--dir', '/verifier', '--ro-bind', str(module), '/verifier/browser_check.py',
               '--chdir', '/tmp', '/usr/bin/python3', '-B', '-c', script]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()
            raise ValueError('browser_command_timeout') from None
        if process.returncode:
            raise ValueError('isolated_browser_failed')
        try:
            report = json.loads(out)
        except (ValueError, UnicodeError):
            raise ValueError('browser_report_invalid') from None
        report.update({'artifact_sha256': digest, 'boundary': 'bubblewrap: private mount/pid/network; fixed verifier; no owner home',
                       'source': 'Codex-directed engineering.verify_calculator', 'exit_code': process.returncode})
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--libraries', required=True)
    parser.add_argument('--evidence', required=True)
    config = parser.parse_args()
    tool = {'name': 'verify_calculator', 'description': 'Verify workspace index.html in an isolated browser at 390x844 with independent arithmetic/error cases. Requires form calculator, fields first/operator/second, result text Result: N or Cannot divide by zero., operators add/subtract/multiply/divide. Returns artifact checksum and evidence.',
            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    for line in sys.stdin:
        msg = None
        try:
            msg = json.loads(line)
            if 'id' not in msg: continue
            method = msg.get('method')
            if method == 'initialize':
                result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'dizzi-engineering-browser', 'version': '0.1'}}
            elif method == 'tools/list': result = {'tools': [tool]}
            elif method == 'ping': result = {}
            elif method == 'tools/call':
                params = msg.get('params', {})
                if params.get('name') != 'verify_calculator' or params.get('arguments', {}): raise ValueError('invalid_browser_arguments')
                target = Path(config.evidence)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.with_suffix('.attempts').open('a+') as counter:
                    fcntl.flock(counter, fcntl.LOCK_EX)
                    counter.seek(0); previous = counter.read().strip()
                    attempt = int(previous or '0') + 1
                    if attempt > 3: raise ValueError('browser_repair_limit')
                    counter.seek(0); counter.truncate(); counter.write(str(attempt)); counter.flush(); os.fsync(counter.fileno())
                report = verify(config.workspace, config.browser, config.libraries)
                report['attempt'] = attempt
                tmp = target.with_suffix('.tmp'); tmp.write_text(json.dumps(report)); os.replace(tmp, target)
                result = {'content': [{'type': 'text', 'text': json.dumps(report)}], 'structuredContent': report, 'isError': False}
            else: raise ValueError('unsupported_browser_method')
            response = {'jsonrpc': '2.0', 'id': msg['id'], 'result': result}
        except Exception as exc:
            # Never echo raw protocol arguments or process stderr.
            response = {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None,
                        'result': {'content': [{'type': 'text', 'text': 'Isolated browser verification failed'}], 'isError': True}}
        print(json.dumps(response), flush=True)


if __name__ == '__main__': main()
