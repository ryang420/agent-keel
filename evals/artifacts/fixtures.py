"""Materialize frozen data; gold remains outside the tree mounted into agent sandboxes."""

import hashlib
import json
from pathlib import Path

from . import PROTOCOL
from .model import json_bytes
from .tasks import tasks


def materialized() -> dict[str, bytes]:
    result = {}
    for task in tasks():
        result[f'public/{task.name}.txt'] = task.prompt.encode()
        for scenario in (task.development, *task.verification):
            for i, step in enumerate(scenario.steps):
                for name, data in step.inputs.items():
                    result[f'inputs/{task.name}/{scenario.name}/{i}/{name}'] = data
                for name, data in step.expected.items():
                    result[f'expected/{task.name}/{scenario.name}/{i}/{name}'] = data
    return result


def _manifest(files):
    return {'protocol': PROTOCOL, 'files': {name: hashlib.sha256(data).hexdigest()
                                         for name, data in sorted(files.items())}}


def prepare(root: Path) -> dict:
    files = materialized()
    manifest = _manifest(files)
    if root.exists() and any(root.iterdir()):
        if not validate(root)['ready']:
            raise ValueError('prepared inputs changed; use a fresh evaluation root')
        return manifest
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / 'manifest.json').write_bytes(json_bytes(manifest))
    return manifest


def validate(root: Path) -> dict:
    expected = _manifest(materialized())
    try:
        recorded = json.loads((root / 'manifest.json').read_text())
        problems = [] if recorded == expected else ['manifest_changed']
        for name, digest in expected['files'].items():
            path = root / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                problems.append(name)
        for folder in ('public', 'inputs', 'expected'):
            actual = {p.relative_to(root).as_posix() for p in (root / folder).rglob('*') if p.is_file()}
            problems.extend(sorted(actual - set(expected['files'])))
        return {'ready': not problems, 'problems': problems}
    except (OSError, ValueError):
        return {'ready': False, 'problems': ['missing_or_invalid_manifest']}
