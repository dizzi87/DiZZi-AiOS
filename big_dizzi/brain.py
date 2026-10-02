"""Read-only adapter for the pinned AIS-OS 3D Brain renderer.

Only explicitly selected Core files and directories are indexed. The browser
receives display labels and note IDs, never local absolute paths.
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re


SKIP_DIRS = {'.git', 'archives', 'archive', 'data', 'scripts', 'tests', 'tmp'}
SENSITIVE = re.compile(r'(secret|credential|password|token|private.key|\.env)', re.I)
LINK = re.compile(r'\[([^\]]+)\]\(([^)#]+)(?:#[^)]*)?\)')


def _inside(path, root):
    return path == root or root in path.parents


def _selected(config):
    root = Path(config['core_root']).resolve(strict=True)
    sources = config.get('brain', {}).get('sources', [])
    if not isinstance(sources, list) or len(sources) > 12:
        raise ValueError('invalid_brain_sources')
    selected = []
    ids = set()
    for source in sources:
        if not isinstance(source, dict) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', source.get('id', '')):
            raise ValueError('invalid_brain_source')
        if source['id'] in ids or not isinstance(source.get('label'), str):
            raise ValueError('invalid_brain_source')
        ids.add(source['id'])
        paths = source.get('paths')
        if not isinstance(paths, list) or not paths or len(paths) > 20:
            raise ValueError('invalid_brain_paths')
        for raw in paths:
            if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or '..' in Path(raw).parts or SENSITIVE.search(raw):
                raise ValueError('invalid_brain_path')
            candidate = root / raw
            if candidate.is_symlink():
                raise ValueError('brain_symlink_rejected')
            target = candidate.resolve(strict=True)
            if not _inside(target, root):
                raise ValueError('brain_path_outside_core')
            selected.append((source, target, root))
    return selected


def _files(target):
    if target.is_file():
        if target.suffix.lower() == '.md' and not SENSITIVE.search(target.name):
            yield target
        return
    for path in sorted(target.rglob('*.md')):
        if any(part.startswith('.') or part.lower() in SKIP_DIRS or SENSITIVE.search(part) for part in path.relative_to(target).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        yield path


def _safe_text(value):
    # Defense in depth for accidentally pasted credentials in approved notes.
    value = re.sub(r'(?im)^.*(?:api[_ -]?key|password|secret|bearer|private[_ -]?key)\s*[:=].*$', '[redacted]', value)
    value = re.sub(r'(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b', '[redacted]', value)
    return value


def build(config):
    records = {}
    sources = {}
    for source, target, root in _selected(config):
        sid = source['id']
        sources[sid] = {'id': sid, 'label': source['label'], 'short': source.get('short', source['label']),
                        'blurb': source.get('blurb', ''), 'color': source.get('color', '#38BDF8'),
                        'staleDays': source.get('staleDays', 90)}
        for file in _files(target):
            real = file.resolve(strict=True)
            if not _inside(real, root) or real.stat().st_size > 1024 * 1024:
                continue
            relative = real.relative_to(root).as_posix()
            identity = sid + ':' + sha256(relative.encode()).hexdigest()[:12]
            body = _safe_text(real.read_text(encoding='utf-8', errors='replace'))
            title = next((m.group(1).strip() for line in body.splitlines()[:30] if (m := re.match(r'^#\s+(.+)', line))), real.stem.replace('-', ' '))
            summary = re.sub(r'\s+', ' ', re.sub(r'[`*#<>]', '', body))[:260]
            node = {'id': identity, 'source': sid, 'kind': 'note', 'title': title, 'slug': real.stem.lower(),
                    'summary': summary, 'path': relative, 'relativePath': relative, 'inRepo': True,
                    'updated': int(real.stat().st_mtime * 1000), 'created': None, 'words': len(body.split()),
                    'tags': [], 'degree': 0, 'flags': [], 'broken': [], 'linkTargets': {},
                    'ageDays': max(0, int((datetime.now(timezone.utc).timestamp() - real.stat().st_mtime) / 86400))}
            records[identity] = (node, real, body)
            if len(records) >= 3000:
                break
    by_path = {file: identity for identity, (_, file, _) in records.items()}
    links = []
    pairs = set()
    for identity, (node, file, body) in records.items():
        for match in LINK.finditer(body):
            raw = match.group(2)
            if ':' in raw or raw.startswith('/'):
                continue
            target = (file.parent / raw).resolve()
            other = by_path.get(target)
            node['linkTargets'][raw] = other
            if other and other != identity:
                pair = tuple(sorted((identity, other)))
                if pair not in pairs:
                    pairs.add(pair)
                    links.append({'source': identity, 'target': other, 'kind': 'source'})
                    node['degree'] += 1
                    records[other][0]['degree'] += 1
            elif not other:
                node['broken'].append(raw)
    nodes = [value[0] for value in records.values()]
    for node in nodes:
        if not node['degree']:
            node['flags'].append('orphan')
        if node['words'] < 60:
            node['flags'].append('stub')
        if node['broken']:
            node['flags'].append('broken-links')
    now = datetime.now(timezone.utc).isoformat()
    by_source = {}
    for sid, source in sources.items():
        subset = [n for n in nodes if n['source'] == sid]
        by_source[sid] = {**source, 'root': 'Approved Dizzi Core paths', 'count': len(subset),
                          'kinds': {'note': len(subset)}, 'words': sum(n['words'] for n in subset),
                          'newest': max((n['updated'] for n in subset), default=None),
                          'oldest': min((n['updated'] for n in subset), default=None),
                          'flagged': sum(bool(n['flags']) for n in subset)}
    graph = {'brain': {'name': 'Dizzi Brain'}, 'generatedAt': now, 'builtAt': int(datetime.now(timezone.utc).timestamp() * 1000),
             'sources': list(sources.values()), 'nodes': nodes, 'links': links, 'warnings': [],
             'inventory': {'generatedAt': now, 'totals': {'nodes': len(nodes), 'links': len(links), 'words': sum(n['words'] for n in nodes)},
                           'bySource': by_source, 'flags': {flag: [n['id'] for n in nodes if flag in n['flags']] for flag in ('stale', 'quiet', 'orphan', 'stub', 'broken-links', 'missing-folder', 'archived', 'not-ingested')},
                           'navigation': [], 'hubs': [n['id'] for n in sorted(nodes, key=lambda n: -n['degree'])[:12]],
                           'linkKinds': {'source': len(links)}}}
    return graph, records


def note(config, identity):
    _, records = build(config)
    if identity not in records:
        return None
    node, file, body = records[identity]
    if file.is_symlink() or file.resolve(strict=True) != file:
        return None
    return {'id': identity, 'markdown': body, 'path': node['path']}
