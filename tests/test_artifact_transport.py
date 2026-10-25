import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.artifacts.transport import ExportError, export_submission
from keel.sandbox.base import SandboxError


class LocalFacade:
    def __init__(self, root):
        self.root = root
        self.calls = []

    async def exec(self, bound, argv, **kwargs):
        import asyncio
        self.calls.append(kwargs['exec_id'])
        # Trusted helper only, not a candidate program. Substitute its sandbox-owned root for this unit test.
        argv = [str(self.root) if arg == '/work/output' else arg for arg in argv]
        proc = await asyncio.create_subprocess_exec(*argv, stdout=-1, stderr=-1)
        stdout, stderr = await proc.communicate()
        return SimpleNamespace(exit_code=proc.returncode, output=stdout.decode(), error=stderr.decode())


async def test_export_preserves_empty_file(tmp_path):
    (tmp_path / 'solution.py').write_text('pass')
    (tmp_path / 'empty.csv').write_bytes(b'')
    facade = LocalFacade(tmp_path)
    result = await export_submission(facade, object(), ('empty.csv',))
    assert result == {'solution.py': b'pass', 'empty.csv': b''}
    assert len(set(facade.calls)) == 3


@pytest.mark.parametrize('kind', ['file_symlink', 'directory', 'unexpected', 'root_symlink'])
async def test_export_rejects_symlink_escape_and_nonregular_file(tmp_path, kind):
    root = tmp_path / 'out'
    root.mkdir()
    (root / 'solution.py').write_text('pass')
    if kind == 'file_symlink':
        (root / 'x.csv').symlink_to(tmp_path / 'outside')
    elif kind == 'directory':
        (root / 'x.csv').mkdir()
    elif kind == 'unexpected':
        (root / 'other.txt').write_text('extra')
    else:
        link = tmp_path / 'link'
        link.symlink_to(root)
        root = link
    with pytest.raises(ExportError):
        await export_submission(LocalFacade(root), object(), ('x.csv',))


async def test_export_enforces_individual_and_total_caps(tmp_path):
    (tmp_path / 'solution.py').write_bytes(b'x' * 1048576)
    facade = LocalFacade(tmp_path)
    assert len((await export_submission(facade, object(), ()))['solution.py']) == 1048576
    (tmp_path / 'solution.py').write_bytes(b'x' * 1048577)
    with pytest.raises(ExportError):
        await export_submission(facade, object(), ())
    (tmp_path / 'solution.py').write_bytes(b'x' * 1048576)
    names = tuple(f'{i}.csv' for i in range(10))
    for name in names:
        (tmp_path / name).write_bytes(b'x' * 1048576)
    with pytest.raises(ExportError):
        await export_submission(facade, object(), names)


async def test_export_refuses_stale_usage():
    class Stale:
        async def exec(self, *args, **kwargs):
            raise SandboxError('stale usage')
    with pytest.raises(ExportError, match='stale'):
        await export_submission(Stale(), object(), ())


@pytest.mark.parametrize('names', [('..',), ('/etc/passwd',), ('a/b.csv',)])
async def test_export_rejects_invalid_allowlist(names):
    with pytest.raises(ExportError):
        await export_submission(None, None, names)


async def test_export_rejects_malformed_envelope():
    class Broken:
        async def exec(self, *args, **kwargs):
            return SimpleNamespace(exit_code=0, output='not JSON')
    with pytest.raises(ExportError):
        await export_submission(Broken(), object(), ())


async def test_export_reports_all_offending_entries_without_reading_them(tmp_path):
    (tmp_path/'solution.py').write_text('pass')
    (tmp_path/'__pycache__').mkdir()
    (tmp_path/'results').mkdir()
    (tmp_path/'extra.txt').write_text('scratch')
    (tmp_path/'escape.csv').symlink_to('/etc/passwd')
    facade = LocalFacade(tmp_path)
    with pytest.raises(ExportError) as caught:
        await export_submission(facade, object(), ('data.csv',))
    assert caught.value.issues == [
        {'code': 'nonregular_entry', 'name': '__pycache__', 'type': 'directory'},
        {'code': 'nonregular_entry', 'name': 'escape.csv', 'type': 'symlink'},
        {'code': 'unexpected_entry', 'name': 'extra.txt', 'type': 'file'},
        {'code': 'nonregular_entry', 'name': 'results', 'type': 'directory'}]
    assert len(facade.calls) == 1  # inventory only; never read rejected content


async def test_export_size_error_identifies_offending_file(tmp_path):
    (tmp_path/'solution.py').write_bytes(b'x' * 1048577)
    with pytest.raises(ExportError) as caught:
        await export_submission(LocalFacade(tmp_path), object(), ())
    assert caught.value.issues == [{'code': 'file_too_large', 'name': 'solution.py', 'type': 'file', 'size': 1048577}]


async def test_export_bounds_inventory_before_returning_diagnostics(tmp_path):
    for index in range(129):
        (tmp_path/f'{index}.txt').write_text('')
    with pytest.raises(ExportError) as caught:
        await export_submission(LocalFacade(tmp_path), object(), ())
    assert caught.value.issues == [{'code': 'too_many_entries'}]
