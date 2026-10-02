"""Bounded numeric browser apps: declarative expressions, no generated host code."""
import html
import hashlib
import json
import math
from pathlib import Path
import re

MAX_ARTIFACT = 60_000
ENGINEERING_INSTRUCTION = '''Build the requested small numeric browser application as JSON only (no fences).
The supported artifact is a self-contained calculator; never claim support for timers,
servers, storage, network, deployment or arbitrary scripts. For an unsupported goal return
{"unsupported": "short explanation"}.
Schema: {"title": str, "description": str,
"inputs": [{"id": str, "label": str, "default": number, "min": number, "max": number}],
"outputs": [{"id": str, "label": str, "expression": expression}],
"checks": [{"name": str, "inputs": {input_id: number}, "expected": {output_id: number}}]}.
An expression is a number, an input ID string, or [operator, left, right].
Allowed operators: +, -, *, /, round (right is decimal places 0 through 6).
No expression refers to another output. Supply at least three meaningful calculation checks.
For a bill splitter use inputs bill, tip_percent, people and outputs total, per_person.
Round monetary outputs to two places. People must have a minimum of 1.
'''


class ArtifactError(ValueError):
    pass


def number(value):
    return type(value) in (float, int) and math.isfinite(value) and abs(value) <= 1e12


def evaluate(expression, values, depth=0):
    if depth > 12:
        raise ArtifactError('expression_too_deep')
    if number(expression):
        return expression
    if isinstance(expression, str) and expression in values:
        return values[expression]
    if not isinstance(expression, list) or len(expression) != 3 or expression[0] not in ('+', '-', '*', '/', 'round'):
        raise ArtifactError('invalid_expression')
    op, left, right = expression
    a, b = evaluate(left, values, depth + 1), evaluate(right, values, depth + 1)
    if op == '+':
        result = a + b
    elif op == '-':
        result = a - b
    elif op == '*':
        result = a * b
    elif op == '/':
        if b == 0:
            raise ArtifactError('division_by_zero')
        result = a / b
    else:
        if b != int(b) or not 0 <= b <= 6:
            raise ArtifactError('invalid_rounding')
        result = math.floor(a * 10 ** b + .5) / 10 ** b
    if not number(result):
        raise ArtifactError('numeric_limit')
    return result


def calculate(spec, values):
    fields = {f['id']: f for f in spec['inputs']}
    if set(values) != set(fields):
        raise ArtifactError('invalid_inputs')
    for key, value in values.items():
        field = fields[key]
        if not number(value) or not field['min'] <= value <= field['max']:
            raise ArtifactError('input_out_of_range')
    return {out['id']: evaluate(out['expression'], values) for out in spec['outputs']}


def verify(spec, goal):
    if not isinstance(spec, dict):
        raise ArtifactError('invalid_artifact')
    if 'unsupported' in spec:
        raise ArtifactError('unsupported_application_scope')
    if set(spec) != {'title', 'description', 'inputs', 'outputs', 'checks'}:
        raise ArtifactError('invalid_artifact_fields')
    for key, limit in (('title', 100), ('description', 800)):
        if not isinstance(spec[key], str) or not 1 <= len(spec[key]) <= limit:
            raise ArtifactError('invalid_artifact_text')
    for key, maximum in (('inputs', 8), ('outputs', 8), ('checks', 20)):
        if not isinstance(spec[key], list) or not 1 <= len(spec[key]) <= maximum:
            raise ArtifactError('invalid_artifact_size')
    for key in ('inputs', 'outputs'):
        seen = set()
        for field in spec[key]:
            expected = {'id', 'label', 'default', 'min', 'max'} if key == 'inputs' else {'id', 'label', 'expression'}
            if not isinstance(field, dict) or set(field) != expected:
                raise ArtifactError('invalid_field')
            identity = field['id']
            if not isinstance(identity, str) or not re.fullmatch('[a-z][a-z0-9_]{0,30}', identity) or identity in ('constructor', 'prototype') or identity in seen:
                raise ArtifactError('invalid_field_id')
            seen.add(identity)
            if not isinstance(field['label'], str) or not 1 <= len(field['label']) <= 100:
                raise ArtifactError('invalid_field_label')
            if key == 'inputs' and (not all(number(field[k]) for k in ('default', 'min', 'max')) or not field['min'] <= field['default'] <= field['max']):
                raise ArtifactError('invalid_field_bounds')
    defaults = {f['id']: f['default'] for f in spec['inputs']}
    calculate(spec, defaults)
    if len(spec['checks']) < 3:
        raise ArtifactError('insufficient_checks')
    checks = []
    for check in spec['checks']:
        if not isinstance(check, dict) or set(check) != {'name', 'inputs', 'expected'} or not isinstance(check['name'], str) or not 1 <= len(check['name']) <= 150:
            raise ArtifactError('invalid_check')
        checks.append((check, 'provider_authored'))
    if re.search(r'\b(bill|tip)\b', goal.lower()) and re.search(r'\b(split|splitter)\b', goal.lower()):
        # Owner-independent acceptance vectors, never supplied by the engineer model.
        for name, values, expected in (
            ('tip and four people', {'bill': 100, 'tip_percent': 15, 'people': 4}, {'total': 115, 'per_person': 28.75}),
            ('zero tip', {'bill': 42, 'tip_percent': 0, 'people': 3}, {'total': 42, 'per_person': 14}),
            ('rounding', {'bill': 100, 'tip_percent': 0, 'people': 3}, {'total': 100, 'per_person': 33.33}),
        ):
            checks.append(({'name': name, 'inputs': values, 'expected': expected}, 'independent_bill_splitter'))
    report = []
    for check, source in checks:
        if not isinstance(check['inputs'], dict) or not isinstance(check['expected'], dict):
            raise ArtifactError('invalid_check')
        actual = calculate(spec, check['inputs'])
        expected = check['expected']
        passed = set(actual) == set(expected) and all(number(v) and math.isclose(actual[k], v, abs_tol=1e-8, rel_tol=1e-9) for k, v in expected.items())
        report.append({'name': check['name'], 'source': source, 'passed': passed})
    if not all(c['passed'] for c in report):
        raise ArtifactError('calculation_check_failed')
    return {'status': 'calculation_checks_passed', 'checks': report, 'browser_verified': False,
            'limitation': 'Browser rendering and interaction have not been exercised; application acceptance remains open.'}


def render(spec):
    # The app interpreter is fixed code. Model output is JSON data, never eval or HTML.
    data = json.dumps(spec, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'">
<title>''' + html.escape(spec['title']) + '''</title><style>body{font:17px system-ui;background:#10202a;color:#eaf4f7;margin:0;padding:24px}main{max-width:560px;margin:auto}label{display:block;margin:16px 0 6px}input,button{box-sizing:border-box;width:100%;font:inherit;padding:12px;border-radius:8px;border:1px solid #abc}button{margin-top:20px;background:#8bddc3}output{display:block;padding:12px 0}#error{color:#ffb8ad}h1,p{overflow-wrap:anywhere}</style></head><body><main><h1 id="title"></h1><p id="description"></p><form id="calculator"><div id="fields"></div><button>Calculate</button></form><p id="error" role="alert"></p><section id="results" aria-live="polite"></section></main><script>
const spec = ''' + data + ''';
const el=id=>document.getElementById(id);
el('title').textContent=spec.title;el('description').textContent=spec.description;
for(const f of spec.inputs){const label=document.createElement('label');label.htmlFor=f.id;label.textContent=f.label;const input=document.createElement('input');Object.assign(input,{id:f.id,name:f.id,type:'number',step:'any',required:true,value:String(f.default),min:String(f.min),max:String(f.max)});el('fields').append(label,input)}
function evaluate(e,v,d=0){if(d>12)throw Error('Expression limit');if(typeof e==='number')return e;if(typeof e==='string'&&Object.hasOwn(v,e))return v[e];if(!Array.isArray(e)||e.length!==3)throw Error('Invalid calculation');const a=evaluate(e[1],v,d+1),b=evaluate(e[2],v,d+1);let r;switch(e[0]){case '+':r=a+b;break;case '-':r=a-b;break;case '*':r=a*b;break;case '/':if(b===0)throw Error('Cannot divide by zero');r=a/b;break;case 'round':if(!Number.isInteger(b)||b<0||b>6)throw Error('Invalid rounding');r=Math.floor(a*10**b+.5)/10**b;break;default:throw Error('Invalid operation')}if(!Number.isFinite(r)||Math.abs(r)>1e12)throw Error('Number outside supported range');return r}
function calculate(){el('error').textContent='';el('results').replaceChildren();try{const v=Object.create(null);for(const f of spec.inputs){if(!el(f.id).value.trim())throw Error('Enter all values');const n=Number(el(f.id).value);if(!Number.isFinite(n)||n<f.min||n>f.max)throw Error('Check '+f.label);v[f.id]=n}for(const o of spec.outputs){const result=document.createElement('output');result.id='result-'+o.id;result.textContent=o.label+': '+evaluate(o.expression,v);el('results').append(result)}}catch(e){el('results').replaceChildren();el('error').textContent=e.message}}
el('calculator').addEventListener('submit',e=>{e.preventDefault();calculate()});calculate();
</script></body></html>'''


def build(directory, goal_id, payload, instruction):
    if len(payload.encode()) > MAX_ARTIFACT:
        raise ArtifactError('artifact_too_large')
    try:
        spec = json.loads(payload)
    except ValueError:
        raise ArtifactError('engineer_returned_invalid_json') from None
    report = verify(spec, instruction)
    target = Path(directory) / 'artifacts' / goal_id
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name, text in (('index.html', render(spec)), ('spec.json', json.dumps(spec, indent=2)), ('verification.json', json.dumps(report, indent=2))):
        (target / name).write_text(text, encoding='utf-8')
    return {'title': spec['title'], 'url': '/api/goals/' + goal_id + '/artifact', 'verification': report,
            'sha256': hashlib.sha256((target / 'index.html').read_bytes()).hexdigest()}
