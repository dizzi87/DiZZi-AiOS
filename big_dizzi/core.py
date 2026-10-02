"""Small deterministic Core route and read-only control audit."""
from datetime import date
from pathlib import Path
import re

ROUTES = {
    'start': '00-System/START-HERE.md',
    'routing': '00-System/ROUTING.md',
    'system_state': '00-System/CURRENT-STATE.md',
    'systems_index': '20-Knowledge/Systems/INDEX.md',
    'aios_index': '50-Projects/Dizzi-AIOS/INDEX.md',
    'aios_project': '50-Projects/Dizzi-AIOS/PROJECT.md',
    'aios_state': '50-Projects/Dizzi-AIOS/CURRENT-STATE.md',
    'contract': '50-Projects/Dizzi-AIOS/TASK-RESULT-CONTRACT-v0.md',
}
MAX_CONTEXT = 48_000


def lookup(root, keys):
    root = Path(root).resolve()
    found = []
    for key in keys:
        if key not in ROUTES:
            raise ValueError('unknown_core_route')
        path = (root / ROUTES[key]).resolve()
        if not path.is_relative_to(root):
            raise ValueError('context_outside_core')
        data = path.read_text(encoding='utf-8')
        if len(data.encode()) > MAX_CONTEXT:
            raise ValueError('oversized_context:' + key)
        found.append({'route': key, 'path': ROUTES[key], 'text': data})
    return found


def route(goal):
    lower = goal.lower()
    # Explicit actions outrank incidental mentions of health, food or projects.
    if re.match(r'\s*(deploy|publish|install|delete|remove|restart|reboot)\b', lower):
        return 'approval_required', []
    if re.match(r'\s*(?:use\s+)?(?:ollama|local inference)\s*:', lower):
        return 'local_inference', []
    if re.search(r'\b(build|create|make|implement|develop)\b.{0,80}\b(app|application|website|tool|calculator)\b', lower):
        keys = ['aios_project', 'aios_state'] if re.search(r'\b(dizzi|aios)\b', lower) else []
        return 'application', keys
    if re.search(r'\b(audit)\b', lower) and re.search(r'\b(core|dizzi|aios)\b', lower):
        return 'core_audit', []
    if re.search(r'\b(stored|recorded|project|current state)\b', lower) and re.search(r'\b(dizzi|aios)\b', lower):
        return 'core_lookup', ['aios_project', 'aios_state']
    if re.search(r'\b(health|status)\b', lower) and re.search(r'\b(dizzi|aios)\b', lower):
        return 'local_health', ['start', 'routing', 'system_state', 'aios_project']
    # Ordinary writing/questions do not require a planner or project payload.
    return 'cloud_reason', []


def audit(root):
    root = Path(root)
    issues = []
    instructions = {}
    for key, relative in ROUTES.items():
        path = root / relative
        if not path.is_file():
            issues.append({'kind': 'broken_route', 'path': relative})
            continue
        if path.stat().st_size > MAX_CONTEXT:
            issues.append({'kind': 'oversized_context', 'path': relative})
        content = path.read_text(encoding='utf-8')
        if path.name == 'CURRENT-STATE.md':
            dates = re.findall(r'(?:Updated|updated:)\s*(\d{4}-\d{2}-\d{2})', content)
            if dates:
                try:
                    if (date.today() - date.fromisoformat(dates[0])).days > 30:
                        issues.append({'kind': 'possibly_stale_state', 'path': relative, 'updated': dates[0]})
                except ValueError:
                    issues.append({'kind': 'invalid_state_date', 'path': relative})
        if path.name == 'PROJECT.md':
            for marker in ('owner', 'portfolio', 'status', 'source', 'confidence'):
                if not re.search(r'^' + marker + r':\s*\S', content, re.MULTILINE):
                    issues.append({'kind': 'missing_authority_marker', 'path': relative, 'marker': marker})
            if not (path.parent / 'INDEX.md').is_file():
                issues.append({'kind': 'missing_project_index', 'path': relative})
        for line in content.splitlines():
            if re.match(r'^[-*]\s+(?:Do not|Never|Must|Always)\b', line, re.IGNORECASE):
                normalized = ' '.join(line.lower().split())
                if normalized in instructions:
                    issues.append({'kind': 'possible_duplicate_instruction', 'path': relative, 'other': instructions[normalized]})
                instructions[normalized] = relative
        if 'deployment pending' in content.lower() and path.name == 'INDEX.md':
            state = root / ROUTES['aios_state']
            if state.is_file() and 'Gateway v0 acceptance pass' in state.read_text(encoding='utf-8'):
                issues.append({'kind': 'conflicting_gateway_status', 'path': relative, 'other': ROUTES['aios_state']})
        for target in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', content):
            if '://' in target:
                continue
            linked = path.parent / target
            if not linked.exists():
                issues.append({'kind': 'stale_link', 'path': relative, 'target': target})
            elif linked.is_file() and linked.suffix == '.md':
                head = linked.read_text(encoding='utf-8')[:500]
                source_lines = [line for line in content.splitlines() if '](' + target in line]
                if re.search(r'^Archived|^status:\s*superseded', head, re.MULTILINE | re.IGNORECASE) and not any(re.search(r'archiv|reference|historical|superseded', line, re.IGNORECASE) for line in source_lines):
                    issues.append({'kind': 'superseded_active_reference', 'path': relative, 'target': target})
    for relative in ('50-Projects/Dizzi-AIOS/PROJECT.md', '50-Projects/Dizzi-AIOS/CURRENT-STATE.md'):
        if not (root / relative).is_file():
            issues.append({'kind': 'missing_project_state', 'path': relative})
    # Active control documents are explicit, so an unindexed sibling is inspectable.
    project = root / '50-Projects/Dizzi-AIOS'
    if project.is_dir():
        indexed = ' '.join((project / p).read_text(encoding='utf-8') for p in ('INDEX.md', 'PROJECT.md', 'CURRENT-STATE.md') if (project / p).is_file())
        for path in project.glob('*.md'):
            if path.name not in ('PROJECT.md', 'CURRENT-STATE.md', 'INDEX.md') and path.name not in indexed:
                issues.append({'kind': 'possible_orphan_control', 'path': str(path.relative_to(root))})
    return issues


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Read-only audit of the selected Dizzi Core routes')
    parser.add_argument('root')
    print(json.dumps(audit(parser.parse_args().root), indent=2))
