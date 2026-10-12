import json
from pathlib import Path

import pytest

from keel.tools import run_tool
from keel.tools.base import ToolContext, preview_for, redact, truncate_middle


@pytest.fixture
def ctx(local_sandbox):
    backend, sandbox, work = local_sandbox
    work.mkdir(parents=True, exist_ok=True)
    return ToolContext(backend, sandbox, cwd=str(work))


async def call(ctx, name, **args):
    outcome = await run_tool(name, json.dumps(args), ctx)
    return json.loads(outcome.content), outcome.error


async def test_read_file_numbers_lines_and_pages(ctx):
    (ctx_path := Path(ctx.cwd) / "a.txt").write_text("one\ntwo\nthree\n")
    body, error = await call(ctx, "read_file", path="a.txt")
    assert not error and body == {"content": "1|one\n2|two\n3|three", "total_lines": 3, "file_size": 14,
                                  "truncated": False}
    body, _ = await call(ctx, "read_file", path=str(ctx_path), offset=2, limit=1)
    assert body["content"] == "2|two" and body["truncated"] and body["next_offset"] == 3


async def test_read_file_refuses_missing_binary_and_directories(ctx):
    Path(ctx.cwd, "bin").write_bytes(b"\x00\x01")
    Path(ctx.cwd, "dir").mkdir()
    for path, text in (("nope", "File not found"), ("bin", "binary"), ("dir", "directory")):
        body, error = await call(ctx, "read_file", path=path)
        assert error and text in body["error"]


async def test_write_file_creates_parents(ctx):
    body, error = await call(ctx, "write_file", path="deep/new.txt", content="héllo")
    assert not error and body["bytes_written"] == len("héllo".encode())
    assert Path(ctx.cwd, "deep/new.txt").read_text() == "héllo"


async def test_patch_replaces_a_unique_string_and_returns_a_diff(ctx):
    Path(ctx.cwd, "f.py").write_text("a = 1\nb = 2\n")
    body, error = await call(ctx, "patch", path="f.py", old_string="b = 2", new_string="b = 3")
    assert not error and body["success"] and "-b = 2" in body["diff"] and "+b = 3" in body["diff"]
    assert Path(ctx.cwd, "f.py").read_text() == "a = 1\nb = 3\n"


@pytest.mark.parametrize("old, new, replace_all, message", [
    ("zzz", "y", False, "not found"),
    ("a", "b", False, "appears 2 times"),
    ("a", "a", False, "must differ"),
])
async def test_patch_refusals(ctx, old, new, replace_all, message):
    Path(ctx.cwd, "f.txt").write_text("a a\n")
    body, error = await call(ctx, "patch", path="f.txt", old_string=old, new_string=new, replace_all=replace_all)
    assert error and message in body["error"]


async def test_patch_replace_all(ctx):
    Path(ctx.cwd, "f.txt").write_text("a a\n")
    body, error = await call(ctx, "patch", path="f.txt", old_string="a", new_string="b", replace_all=True)
    assert not error and Path(ctx.cwd, "f.txt").read_text() == "b b\n"


async def test_search_content_files_and_counts(ctx):
    Path(ctx.cwd, "x.py").write_text("alpha\nbeta alpha\n")
    Path(ctx.cwd, "y.txt").write_text("alpha\n")
    body, _ = await call(ctx, "search_files", pattern="alph[a]", file_glob="*.py")
    assert body["total_count"] == 2 and {(m["line"], m["content"]) for m in body["matches"]} == {
        (1, "alpha"), (2, "beta alpha")}
    body, _ = await call(ctx, "search_files", pattern="alpha", output_mode="count")
    assert body["total_count"] == 3 and sorted(body["counts"].values()) == [1, 2]
    body, _ = await call(ctx, "search_files", pattern="alpha", output_mode="files_only")
    assert body["total_count"] == 2
    body, _ = await call(ctx, "search_files", pattern="*.txt", target="files")
    assert body["total_count"] == 1 and body["files"][0].endswith("y.txt")
    body, _ = await call(ctx, "search_files", pattern="nothing-here")
    assert body == {"matches": [], "total_count": 0}


async def test_search_with_context_returns_text(ctx):
    Path(ctx.cwd, "x.txt").write_text("before\nhit\nafter\n")
    body, _ = await call(ctx, "search_files", pattern="hit", context=1)
    assert body["total_count"] == 1 and "before" in body["output"] and "after" in body["output"]


async def test_bad_calls_become_error_results(ctx):
    outcome = await run_tool("no_such_tool", "{}", ctx)
    assert outcome.error and "Unknown tool" in json.loads(outcome.content)["error"]
    outcome = await run_tool("read_file", "{not json", ctx)
    assert outcome.error and "Invalid JSON" in json.loads(outcome.content)["error"]
    outcome = await run_tool("read_file", "{}", ctx)
    assert outcome.error and "path" in json.loads(outcome.content)["error"]
    outcome = await run_tool("read_file", json.dumps({"path": "a", "offset": "x"}), ctx)
    assert outcome.error and "offset" in json.loads(outcome.content)["error"]


def test_truncate_middle_keeps_head_and_tail():
    text = "h" * 600 + "t" * 400
    cut = truncate_middle(text, 100)
    assert cut.startswith("h" * 40) and cut.endswith("t" * 60)
    assert "[OUTPUT TRUNCATED - 900 chars omitted out of 1,000 total]" in cut
    assert truncate_middle("short", 100) == "short"


# Fake credentials are assembled from pieces so no secret-looking literal sits in the source.
PW = "hunter" + "2"
PRIVATE_KEY = "-----BEGIN " + "PRIVATE KEY-----"
RSA_PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"


def test_redaction_and_previews():
    assert redact("key " + "AKIA" + "ABCDEFGHIJKLMNOP" + " end") == "key [REDACTED] end"
    assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"
    assert redact(f"export DB_PASSWORD={PW} x") == "export DB_PASSWORD=[REDACTED] x"
    assert preview_for("terminal", json.dumps({"command": "echo TOKEN=abc"})) == "echo TOKEN=[REDACTED]"
    assert preview_for("read_file", json.dumps({"path": "/a/b"})) == "/a/b"
    assert preview_for("execute_code", json.dumps({"code": "\n\nimport os\nprint(1)"})) == "import os"
    assert preview_for("search_files", json.dumps({"pattern": "x", "path": "/d"})) == "x in /d"
    assert len(preview_for("terminal", json.dumps({"command": "x" * 900}))) == 500
    assert preview_for("terminal", "{broken") == ""


# --- fix round 1 ---

from keel.tools import base as tools_base  # noqa: E402


async def test_read_file_budget_counts_json_escaping(ctx):
    Path(ctx.cwd, "j.txt").write_text("".join('a"b\\' * 10 + "\n" for _ in range(2500)))
    outcome = await run_tool("read_file", json.dumps({"path": "j.txt"}), ctx)
    body = json.loads(outcome.content)
    assert len(outcome.content) <= tools_base.MAX_TOOL_RESULT_CHARS
    assert body["truncated"] and body["next_offset"] > 1 and body["total_lines"] == 2500


async def test_read_file_cuts_an_oversized_single_line(ctx):
    Path(ctx.cwd, "min.js").write_text("x" * 300_000)
    body, error = await call(ctx, "read_file", path="min.js")
    assert not error and body["total_lines"] == 1 and not body["truncated"]
    assert "line truncated, 300000 chars" in body["content"] and len(body["content"]) < 10_000
    assert body["file_size"] == 300_000


async def test_read_file_size_is_raw_bytes(ctx):
    Path(ctx.cwd, "b.txt").write_bytes(b"ab\xff\xfe\n")
    body, _ = await call(ctx, "read_file", path="b.txt")
    assert body["file_size"] == 5


async def test_run_tool_keeps_oversized_json_valid(ctx, monkeypatch):
    async def big(args, context):
        return tools_base.ToolOutcome(tools_base.tool_json({"ok": 1, "output": "z" * 300_000, "n": 5}))
    monkeypatch.setitem(tools_base.HANDLERS, "big", big)
    outcome = await run_tool("big", "{}", ctx)
    body = json.loads(outcome.content)
    assert len(outcome.content) <= tools_base.MAX_TOOL_RESULT_CHARS
    assert body["ok"] == 1 and body["n"] == 5 and "OUTPUT TRUNCATED" in body["output"]


async def test_run_tool_oversized_non_json_falls_back(ctx, monkeypatch):
    async def big(args, context):
        return tools_base.ToolOutcome("q" * 300_000)
    monkeypatch.setitem(tools_base.HANDLERS, "big", big)
    outcome = await run_tool("big", "{}", ctx)
    assert len(outcome.content) < 101_000 and "OUTPUT TRUNCATED" in outcome.content


@pytest.mark.parametrize("raw, expected", [
    (f'export DB_PASSWORD="{PW}" x', "export DB_PASSWORD=[REDACTED] x"),
    ("TOKEN='abc def'", "TOKEN=[REDACTED]"),
    (f"psql --password {PW} db", "psql --password [REDACTED] db"),
    ("run --api-key=abc123 now", "run --api-key=[REDACTED] now"),
    ("postgres://user:" + "pw" + "@host/db", "postgres://user:[REDACTED]@host/db"),
    ('{"db_password": "x y", "a": 1}', '{"db_password": [REDACTED], "a": 1}'),
    (f"password: {PW}", "password: [REDACTED]"),
    ("gh" + "p_" + "a" * 36, "[REDACTED]"),
    ("github_" + "pat_" + "B" * 30, "[REDACTED]"),
    ("sk-" + "c" * 30, "[REDACTED]"),
    ("xox" + "b-1234567890-abcdef", "[REDACTED]"),
    ("the password policy requires 12 characters", "the password policy requires 12 characters"),
])
def test_redaction_shapes(raw, expected):
    assert redact(raw) == expected


async def test_exec_clears_exec_id_on_error_but_keeps_it_on_cancel(ctx):
    import asyncio
    from keel.sandbox.base import SandboxError
    seen = []

    async def on_exec(exec_id):
        seen.append(exec_id)

    ctx.on_exec = on_exec

    class Boom:
        def __init__(self, exc):
            self.exc = exc

        async def exec(self, *a, **k):
            raise self.exc

    real = ctx.backend
    ctx.backend = Boom(SandboxError("gone"))
    with pytest.raises(SandboxError):
        await ctx.exec(["true"], timeout_s=1)
    assert seen[0] and seen[-1] is None and len(seen) == 2
    seen.clear()
    ctx.backend = Boom(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ctx.exec(["true"], timeout_s=1)
    assert len(seen) == 1 and seen[0]
    ctx.backend = real


async def test_search_unreadable_file_warns_but_returns_matches(ctx):
    import os
    Path(ctx.cwd, "ok.txt").write_text("needle\n")
    locked = Path(ctx.cwd, "locked.txt")
    locked.write_text("needle\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        body, error = await call(ctx, "search_files", pattern="needle")
        assert not error and body["total_count"] == 1 and "locked.txt" in body["warning"]
        body, error = await call(ctx, "search_files", pattern="absent")
        assert error and "Permission denied" in body["error"]
    finally:
        locked.chmod(0o600)


async def test_search_caps_long_match_lines(ctx):
    Path(ctx.cwd, "m.txt").write_text("needle" + "y" * 5000 + "\n")
    body, _ = await call(ctx, "search_files", pattern="needle")
    assert len(body["matches"][0]["content"]) < 600 and "truncated" in body["matches"][0]["content"]


async def test_search_count_and_context_honour_offset_and_limit(ctx):
    for name in "abc":
        Path(ctx.cwd, f"{name}.txt").write_text("hit\nfiller\nfiller\nfiller\nhit\n")
    body, _ = await call(ctx, "search_files", pattern="hit", output_mode="count", limit=2, offset=1)
    assert len(body["counts"]) == 2 and body["total_count"] == 6
    body, _ = await call(ctx, "search_files", pattern="hit", context=1, limit=2, offset=1)
    assert body["total_count"] == 6 and body["output"].count("hit") == 2


# --- fix round 2 ---

def test_redaction_is_linear_on_hostile_input():
    import time
    start = time.perf_counter()
    for text in ("a" * 100_000, "_" * 100_000, "-".join(["ab"] * 50_000), "TOKEN" * 20_000,
                 "\n" * 100_000, "x://a:" * 20_000, "--token=\"" * 10_000,
                 RSA_PRIVATE_KEY + "a" * 100_000, PRIVATE_KEY * 5_000):
        redact(text)
    assert time.perf_counter() - start < 1.0


def test_preview_bounds_its_work():
    assert len(preview_for("terminal", json.dumps({"command": "a" * 1_000_000}))) == 500


@pytest.mark.parametrize("text", [
    "def f(password: str, secret: int)", "if token: return x", "The token: expired", "  return {'token': 1}",
])
def test_redaction_leaves_code_and_prose_alone(text):
    assert redact(text) == text


def test_redaction_still_catches_unterminated_private_keys():
    assert redact(PRIVATE_KEY + "\nabcd") == "[REDACTED]"


async def test_cap_keeps_json_valid_for_many_medium_strings_and_lists(ctx, monkeypatch):
    async def many(args, context):
        return tools_base.ToolOutcome(tools_base.tool_json({"items": ["m" * 400 for _ in range(1000)], "k": 1}))

    async def listing(args, context):
        return tools_base.ToolOutcome(tools_base.tool_json(["m" * 400 for _ in range(1000)]))
    monkeypatch.setitem(tools_base.HANDLERS, "many", many)
    monkeypatch.setitem(tools_base.HANDLERS, "listing", listing)
    outcome = await run_tool("many", "{}", ctx)
    body = json.loads(outcome.content)
    assert len(outcome.content) <= tools_base.MAX_TOOL_RESULT_CHARS and body["k"] == 1 and "_truncated" in body
    outcome = await run_tool("listing", "{}", ctx)
    body = json.loads(outcome.content)
    assert len(outcome.content) <= tools_base.MAX_TOOL_RESULT_CHARS and "dropped" in body[-1]


def test_cap_falls_back_to_a_valid_error_object():
    raw = json.dumps({"a": "x" * 50, "b": 1})
    out = json.loads(tools_base.cap_json_result(raw, 20))
    assert out["error"] == "result too large"
