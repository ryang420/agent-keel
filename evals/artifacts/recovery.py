"""Pure-text trace reconstruction. Terminal side effects require a separate human/code audit."""

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import posixpath

from .transport import FILE_LIMIT

TARGET = '/work/output/solution.py'


@dataclass(frozen=True)
class RecoveredScript:
    code: bytes
    operations: tuple[dict, ...]
    terminals: tuple[dict, ...]


def recover_script(messages: list[dict]) -> RecoveredScript:
    pending = deque()
    code = None
    operations, terminals = [], []
    for message in messages:
        if message.get('role') == 'assistant':
            if pending:
                raise ValueError('missing tool result before next assistant message')
            pending.extend(message.get('tool_calls', []))
        elif message.get('role') == 'tool':
            if not pending:
                raise ValueError('unpaired tool result')
            call = pending.popleft()
            name = call.get('name')
            if name not in ('write_file', 'patch', 'terminal'):
                continue
            try:
                args = json.loads(call['arguments'])
                result = json.loads(message.get('text', ''))
            except (ValueError, TypeError, KeyError):
                raise ValueError('cannot reconstruct malformed tool record') from None
            if not isinstance(args, dict) or not isinstance(result, dict):
                raise ValueError('cannot reconstruct non-object tool record')
            if name == 'terminal':
                terminals.append({'message_id': message.get('id'), 'command': args.get('command'),
                                  'exit_code': result.get('exit_code')})
                continue
            path = args.get('path')
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('ambiguous write path')
            if posixpath.normpath(path) != TARGET:
                continue
            applied = not result.get('error')
            if applied and name == 'write_file':
                value = args.get('content')
                if not isinstance(value, str) or result.get('path') != TARGET or type(result.get('bytes_written')) is not int:
                    raise ValueError('unverified write result')
                if len(value.encode()) != result['bytes_written']:
                    raise ValueError('write byte count disagrees with transcript')
                code = value
            elif applied:
                old, new = args.get('old_string'), args.get('new_string')
                if result.get('success') is not True or TARGET not in result.get('files_modified', []):
                    raise ValueError('unverified patch result')
                if code is None or not isinstance(old, str) or not old or not isinstance(new, str):
                    raise ValueError('patch without reconstructable source')
                count = code.count(old)
                if not count or (count > 1 and args.get('replace_all') is not True):
                    raise ValueError('successful patch disagrees with reconstructed source')
                code = code.replace(old, new) if args.get('replace_all') is True else code.replace(old, new, 1)
            if code is not None and len(code.encode()) > FILE_LIMIT:
                raise ValueError('reconstructed script exceeds submission limit')
            operations.append({'message_id': message.get('id'), 'tool': name, 'applied': applied,
                               'sha256': hashlib.sha256(code.encode()).hexdigest() if code is not None else None})
    if pending or code is None or not code:
        raise ValueError('incomplete transcript or missing script')
    return RecoveredScript(code.encode(), tuple(operations), tuple(terminals))
