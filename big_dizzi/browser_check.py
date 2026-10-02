"""Optional bounded browser verification of the fixed calculator artifact only."""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import tempfile


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.data = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'pre' and dict(attrs).get('id') == 'dizzi-browser-verification':
            self.inside = True

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.data += data


def verify_browser(artifact, config):
    executable = Path(config['executable'])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('browser_executable_unavailable')
    original = Path(artifact).read_text(encoding='utf-8')
    # Checks interact with actual fields and rendered outputs, not just the Python evaluator.
    harness = '''<script>
const checks=[...spec.checks];
if(spec.inputs.map(f=>f.id).sort().join(',')==='bill,people,tip_percent'){
checks.push({name:'independent tip',inputs:{bill:100,tip_percent:15,people:4},expected:{total:115,per_person:28.75}},
{name:'independent zero tip',inputs:{bill:42,tip_percent:0,people:3},expected:{total:42,per_person:14}},
{name:'independent rounding',inputs:{bill:100,tip_percent:0,people:3},expected:{total:100,per_person:33.33}});}
const reports=[];
for(const c of checks){for(const [key,value] of Object.entries(c.inputs))document.getElementById(key).value=String(value);document.getElementById('calculator').dispatchEvent(new Event('submit',{cancelable:true}));let passed=!document.getElementById('error').textContent;for(const [key,value] of Object.entries(c.expected)){const o=document.getElementById('result-'+key);const actual=o?Number(o.textContent.slice(o.textContent.lastIndexOf(':')+1)):NaN;passed=passed&&Number.isFinite(actual)&&Math.abs(actual-value)<1e-8}reports.push({name:c.name,passed});}
const overflow=document.documentElement.scrollWidth>innerWidth;
const report={browser_verified:reports.every(c=>c.passed)&&!overflow,viewport:{width:innerWidth,height:innerHeight},overflow,checks:reports};
const output=document.createElement('pre');output.id='dizzi-browser-verification';output.textContent=JSON.stringify(report);document.body.append(output);
</script>'''
    with tempfile.TemporaryDirectory(prefix='dizzi-artifact-browser-') as temporary:
        root = Path(temporary)
        page = root / 'check.html'
        page.write_text(original.replace('</body>', harness + '</body>'), encoding='utf-8')
        command = [str(executable), '--headless', '--disable-gpu', '--disable-background-networking',
                   '--no-first-run', '--disable-dev-shm-usage', '--user-data-dir=' + str(root / 'profile'),
                   '--window-size=390,844', '--dump-dom', '--virtual-time-budget=1000']
        if config.get('no_sandbox', False):
            command.append('--no-sandbox')
        command.append(page.as_uri())
        # Do not give the browser the provider API key or other application credentials.
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'LANG') if k in os.environ}
        try:
            result = subprocess.run(command, capture_output=True, timeout=25, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('browser_check_unavailable') from None
        if result.returncode:
            raise ValueError('browser_check_failed')
        parser = ReportParser()
        parser.feed(result.stdout.decode('utf-8', errors='replace'))
        try:
            report = json.loads(parser.data)
        except ValueError:
            raise ValueError('browser_report_missing') from None
        if not report.get('browser_verified'):
            raise ValueError('browser_interaction_failed')
        return report


def verify_static_calculator(artifact, config):
    """Independent browser interactions for a self-contained calculator page."""
    executable = Path(config['executable'])
    source = Path(artifact)
    if not executable.is_absolute() or not executable.is_file() or not source.is_file() or source.stat().st_size > 200_000:
        raise ValueError('static_browser_input_unavailable')
    page = source.read_text(encoding='utf-8')
    if '<form' not in page or 'id="calculator"' not in page or '<script' not in page:
        raise ValueError('unsupported_static_calculator')
    harness = '''<!doctype html><meta charset="utf-8"><style>html,body{margin:0}iframe{width:390px;height:844px;border:0}</style><iframe id="app" src="index.html" title="Calculator check"></iframe><pre id="dizzi-browser-verification">pending</pre><script>
app.addEventListener('load',()=>{let d=app.contentDocument;let cases=[['100','add','23','Result: 123'],['-7','multiply','6','Result: -42'],['81','divide','9','Result: 9'],['1','divide','0','Cannot divide by zero.']];let results=cases.map(([a,op,b,want])=>{d.querySelector('#first').value=a;d.querySelector('#operator').value=op;d.querySelector('#second').value=b;d.querySelector('#calculator button').click();return d.querySelector('#result').textContent===want});let r=document.querySelector('#dizzi-browser-verification');r.textContent=JSON.stringify({browser_verified:results.every(Boolean)&&d.documentElement.scrollWidth<=390,checks:results,overflow:d.documentElement.scrollWidth>390,viewport:[innerWidth,innerHeight]});});</script>'''
    with tempfile.TemporaryDirectory(prefix='dizzi-static-browser-') as temporary:
        root = Path(temporary)
        (root / 'index.html').write_text(page, encoding='utf-8')
        target = root / 'verify.html'
        target.write_text(harness, encoding='utf-8')
        command = [str(executable), '--headless', '--disable-gpu', '--disable-background-networking',
                   '--no-first-run', '--disable-dev-shm-usage', '--allow-file-access-from-files',
                   '--user-data-dir=' + str(root / 'profile'), '--window-size=390,844', '--dump-dom',
                   '--virtual-time-budget=2000']
        if config.get('no_sandbox'):
            command.append('--no-sandbox')
        command.append(target.as_uri())
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
        if config.get('library_path'):
            env['LD_LIBRARY_PATH'] = config['library_path']
        try:
            completed = subprocess.run(command, capture_output=True, timeout=25, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('static_browser_unavailable') from None
        if completed.returncode:
            raise ValueError('static_browser_failed')
        parser = ReportParser()
        parser.feed(completed.stdout.decode('utf-8', errors='replace'))
        try:
            report = json.loads(parser.data)
        except ValueError:
            raise ValueError('static_browser_report_missing') from None
        if not report.get('browser_verified'):
            raise ValueError('static_browser_interactions_failed')
        return report
