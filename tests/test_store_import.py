"""SqlStore.import_session: one conversation copied from another system, written as it was."""

import pytest

from keel.models import Message, TextPart, ToolCall
from keel.store.base import ProfileTenantMismatch


def conversation():
    return [
        (Message("user", (TextPart("How many rows?"),)), 100.0),
        (Message("assistant", (), tool_calls=(ToolCall("c1", "terminal", '{"command": "wc -l f"}'),)), 101.0),
        (Message("tool", (TextPart("42 f"),), tool_call_id="c1", tool_name="terminal"), 102.0),
        (Message("assistant", (TextPart("42 rows."),)), 103.0),
    ]


async def imported(store, session_id="console-a1", profile="p1", tenant=7, title="Row count", source="llm"):
    return await store.import_session(profile_id=profile, tenant_id=tenant, session_id=session_id, title=title,
                                      title_source=source, created_at=100.0, last_active_at=103.0,
                                      messages=conversation())


async def test_a_conversation_is_written_with_its_title_times_and_messages(store):
    result = await imported(store)
    assert result.status == "imported" and result.profile_created and len(result.message_ids) == 4
    session = await store.get_session("p1", "console-a1")
    assert (session.title, session.title_source) == ("Row count", "llm")
    assert (session.created_at, session.last_active_at, session.message_count) == (100.0, 103.0, 4)
    stored = await store.messages("console-a1")
    assert [m.id for m in stored] == list(result.message_ids)
    assert [m.message.role for m in stored] == ["user", "assistant", "tool", "assistant"]
    assert [m.created_at for m in stored] == [100.0, 101.0, 102.0, 103.0]
    assert stored[1].message.tool_calls == (ToolCall("c1", "terminal", '{"command": "wc -l f"}'),)
    assert (stored[2].message.tool_call_id, stored[2].message.tool_name, stored[2].message.text) == ("c1", "terminal", "42 f")
    assert all(m.run_id is None and m.message.reasoning == () for m in stored)


async def test_a_missing_profile_is_created_empty_for_the_tenant(store):
    await imported(store)
    profile = await store.get_profile("p1")
    assert (profile.tenant_id, profile.models, profile.skills, profile.soul) == (7, [], [], "")


async def test_an_existing_session_id_is_left_alone(store):
    await imported(store)
    again = await imported(store, title="Something else")
    assert (again.status, again.profile_created, again.message_ids) == ("exists", False, ())
    assert (await store.get_session("p1", "console-a1")).title == "Row count"
    assert len(await store.messages("console-a1")) == 4


async def test_a_session_id_owned_by_another_profile_is_also_left_alone(store):
    await imported(store, profile="p1")
    assert (await imported(store, profile="p2")).status == "exists"


async def test_a_profile_of_another_tenant_is_refused_and_nothing_is_written(store):
    await imported(store, session_id="console-a1", tenant=7)
    with pytest.raises(ProfileTenantMismatch):
        await imported(store, session_id="console-b2", tenant=8)
    assert await store.get_session("p1", "console-b2") is None


async def test_a_title_the_profile_already_uses_gets_a_number(store):
    await imported(store, session_id="console-a1")
    second = await imported(store, session_id="console-b2")
    third = await imported(store, session_id="console-c3")
    assert (second.title, third.title) == ("Row count (2)", "Row count (3)")
    assert (await store.get_session("p1", "console-b2")).title == "Row count (2)"


async def test_an_untitled_conversation_stays_untitled(store):
    result = await imported(store, title=None, source=None)
    session = await store.get_session("p1", "console-a1")
    assert (result.title, session.title, session.title_source) == (None, None, None)
