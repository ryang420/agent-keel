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
    pass


_HELPER = r'''
import base64, json, os, stat, sys
root, mode, name = sys.argv[1:]
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
try:
    for component in root.strip('/').split('/'):
        next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = next_fd
    if mode == 'list':
        items = {}
        for item in os.listdir(fd):
            info = os.stat(item, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('nonregular submission file')
            items[item] = info.st_size
        print(json.dumps(items))
    else:
        src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(src, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                raise ValueError('invalid submission file')
            data = source.read(1048577)
            if len(data) > 1048576:
                raise ValueError('oversized submission file')
        print(json.dumps({'data': base64.b64encode(data).decode()}))
finally:
    os.close(fd)
'''


async def _call(coordinator, bound, mode, name=''):
    try:
        result = await coordinator.exec(bound, ['python3', '-c', _HELPER, '/work/output', mode, name],
                                        cwd='/', timeout_s=60, exec_id='artifact-' + uuid.uuid4().hex)
        if result.exit_code:
            raise ExportError('submission helper failed')
        return json.loads(result.output)
    except (SandboxError, ValueError, OSError) as exc:
        raise ExportError(str(exc)) from exc


async def export_submission(coordinator, bound, allowed: tuple[str, ...]) -> dict[str, bytes]:
    names = {'solution.py', *allowed}
    if any(not _NAME.fullmatch(name) or name in ('.', '..') for name in names):
        raise ExportError('invalid submission name')
    inventory = await _call(coordinator, bound, 'list')
    if not isinstance(inventory, dict) or not set(inventory) <= names:
        raise ExportError('unexpected submission files')
    if any(type(size) is not int or size < 0 or size > FILE_LIMIT for size in inventory.values()):
        raise ExportError('invalid submission file size')
    if sum(inventory.values()) > TOTAL_LIMIT:
        raise ExportError('submission exceeds total size limit')
    files = {}
    for name, size in sorted(inventory.items()):
        envelope = await _call(coordinator, bound, 'read', name)
        try:
            data = base64.b64decode(envelope['data'], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportError('invalid submission encoding') from exc
        if len(data) != size:
            raise ExportError('submission changed during export')
        files[name] = data
    return files
