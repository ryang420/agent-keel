"""Export regular submission files via registered sandbox helper executions."""

import base64
import json
import re
import uuid

from keel.sandbox.base import SandboxError

FILE_LIMIT = 1024 * 1024
TOTAL_LIMIT = 10 * FILE_LIMIT
_NAME = re.compile(r'[A-Za-z0-9_.-]+\Z')


class ExportError(ValueError):
    def __init__(self, message, *, issues=None):
        super().__init__(message)
        self.issues = issues or [{'code': 'export_failed'}]


_HELPER = r'''
import base64, json, os, stat, sys
root, mode, name = sys.argv[1:]
def fail(code):
    print(json.dumps({'error': {'code': code}}))
    sys.exit(0)
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
try:
    for component in root.strip('/').split('/'):
        try:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            if mode == 'list':
                print('{"entries":{}}')
                sys.exit(0)
            raise
        os.close(fd)
        fd = next_fd
    if mode == 'list':
        items = {}
        with os.scandir(fd) as entries:
            for item in entries:
                if len(items) >= 128:
                    fail('too_many_entries')
                info = item.stat(follow_symlinks=False)
                kind = ('file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode)
                        else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special')
                items[item.name] = {'type': kind, 'size': info.st_size}
        print(json.dumps({'entries': items}))
    else:
        src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(src, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                fail('invalid_file')
            data = source.read(1048577)
            if len(data) > 1048576:
                fail('file_too_large')
        print(json.dumps({'data': base64.b64encode(data).decode()}))
except (OSError, ValueError):
    fail('unsafe_root' if mode == 'list' else 'unsafe_file')
finally:
    os.close(fd)
'''


async def _call(coordinator, bound, mode, name=''):
    try:
        result = await coordinator.exec(bound, ['python3', '-c', _HELPER, '/work/output', mode, name],
                                        cwd='/', timeout_s=60, exec_id='artifact-' + uuid.uuid4().hex)
        if result.exit_code:
            raise ExportError('submission helper failed')
        payload = json.loads(result.output)
        if isinstance(payload, dict) and 'error' in payload:
            code = payload['error'].get('code') if isinstance(payload['error'], dict) else None
            if code not in {'too_many_entries', 'invalid_file', 'file_too_large', 'unsafe_root', 'unsafe_file'}:
                code = 'helper_failed'
            issue = {'code': code}
            if name:
                issue['name'] = _display_name(name)
            raise ExportError('submission helper refused access', issues=[issue])
        return payload
    except ExportError:
        raise
    except (SandboxError, ValueError, OSError) as exc:
        raise ExportError(str(exc)) from exc


def _display_name(name):
    return ''.join(c if c.isprintable() else '?' for c in name[:160])


async def export_submission(coordinator, bound, allowed: tuple[str, ...]) -> dict[str, bytes]:
    names = {'solution.py', *allowed}
    if any(not _NAME.fullmatch(name) or name in ('.', '..') for name in names):
        raise ExportError('invalid submission name')
    payload = await _call(coordinator, bound, 'list')
    inventory = payload.get('entries') if isinstance(payload, dict) else None
    if not isinstance(inventory, dict) or len(inventory) > 128:
        raise ExportError('invalid submission inventory')
    issues = []
    for name, entry in sorted(inventory.items()):
        if not isinstance(entry, dict) or entry.get('type') not in ('file', 'directory', 'symlink', 'special'):
            raise ExportError('invalid submission inventory')
        kind, size = entry['type'], entry.get('size')
        issue = {'name': _display_name(name), 'type': kind}
        if kind != 'file':
            issues.append({'code': 'nonregular_entry', **issue})
        elif name not in names:
            issues.append({'code': 'unexpected_entry', **issue})
        elif type(size) is not int or size < 0:
            raise ExportError('invalid submission inventory')
        elif size > FILE_LIMIT:
            issues.append({'code': 'file_too_large', **issue, 'size': size})
    if issues:
        raise ExportError('submission entries rejected', issues=issues)
    if sum(entry['size'] for entry in inventory.values()) > TOTAL_LIMIT:
        raise ExportError('submission exceeds total size limit', issues=[{'code': 'total_size_limit'}])
    files = {}
    for name, entry in sorted(inventory.items()):
        envelope = await _call(coordinator, bound, 'read', name)
        try:
            data = base64.b64decode(envelope['data'], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportError('invalid submission encoding') from exc
        if len(data) != entry['size']:
            raise ExportError('submission changed during export', issues=[{
                'code': 'changed_during_export', 'name': _display_name(name)}])
        files[name] = data
    return files
