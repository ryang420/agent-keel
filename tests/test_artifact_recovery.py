import json

import pytest


def assistant(name, args):
    return {'role': 'assistant', 'tool_calls': [{'name': name, 'arguments': json.dumps(args)}]}


def tool(identifier, result):
    return {'id': identifier, 'role': 'tool', 'text': json.dumps(result)}


def test_recovery_uses_successful_writes_and_patches_and_skips_failed_patch():
    from evals.artifacts.recovery import recover_script
    messages = [assistant('write_file', {'path': '/work/output/solution.py', 'content': 'print(1)\n'}),
                tool(1, {'path': '/work/output/solution.py', 'bytes_written': 9}),
                assistant('patch', {'path': '/work/output/solution.py', 'old_string': '1', 'new_string': '2'}),
                tool(2, {'success': True, 'files_modified': ['/work/output/solution.py']}),
                assistant('patch', {'path': '/work/output/solution.py', 'old_string': '2', 'new_string': '3'}),
                tool(3, {'error': 'patch failed'}),
                assistant('terminal', {'command': 'python -m py_compile /work/output/solution.py'}),
                tool(4, {'exit_code': 0})]
    recovered = recover_script(messages)
    assert recovered.code == b'print(2)\n'
    assert [o['applied'] for o in recovered.operations] == [True, True, False]
    assert recovered.terminals[0]['message_id'] == 4


@pytest.mark.parametrize('messages', [[], [tool(1, {})],
    [assistant('write_file', {'path': '/work/output/solution.py', 'content': 'pass'})],
    [assistant('write_file', {'path': '/work/output/solution.py', 'content': 'pass'}),
     tool(1, {'path': '/work/output/solution.py', 'bytes_written': 99})]])
def test_recovery_refuses_absent_or_ambiguous_source(messages):
    from evals.artifacts.recovery import recover_script
    with pytest.raises(ValueError):
        recover_script(messages)


def test_recovery_does_not_execute_candidate_content(tmp_path):
    from evals.artifacts.recovery import recover_script
    marker = tmp_path/'must-not-exist'
    code = f'open({str(marker)!r}, "w").write("executed")\n'
    result = recover_script([assistant('write_file', {'path': '/work/output/solution.py', 'content': code}),
                             tool(1, {'path': '/work/output/solution.py', 'bytes_written': len(code.encode())})])
    assert result.code == code.encode()
    assert not marker.exists()
