"""MessageCoalescer behaviour tests."""

import asyncio

import pytest

from app.services.message_queue import MessageCoalescer


@pytest.mark.asyncio
async def test_burst_messages_collapse_into_one_flush():
    calls: list[tuple] = []

    async def flush(sid, chat_id, bot_id, combined):
        calls.append((sid, chat_id, bot_id, combined))

    c = MessageCoalescer(flush=flush, window_seconds=0.1)
    await c.enqueue("sid1", "chatA", 1, "hola")
    await c.enqueue("sid1", "chatA", 1, "espera")
    await c.enqueue("sid1", "chatA", 2, "y también")
    await asyncio.sleep(0.3)

    assert len(calls) == 1
    sid, chat_id, bot_id, combined = calls[0]
    assert sid == "sid1"
    assert chat_id == "chatA"
    # Latest bot_id wins
    assert bot_id == 2
    assert combined == "hola\n\nespera\n\ny también"


@pytest.mark.asyncio
async def test_different_chat_ids_get_separate_buffers():
    calls: list[tuple] = []

    async def flush(sid, chat_id, bot_id, combined):
        calls.append((sid, chat_id, bot_id, combined))

    c = MessageCoalescer(flush=flush, window_seconds=0.1)
    await c.enqueue("sid1", "chatA", 1, "one")
    await c.enqueue("sid1", "chatB", 1, "two")
    await asyncio.sleep(0.3)

    assert {(sid, chat_id, combined) for sid, chat_id, _, combined in calls} == {
        ("sid1", "chatA", "one"),
        ("sid1", "chatB", "two"),
    }


@pytest.mark.asyncio
async def test_forget_cancels_pending_flush():
    calls: list[tuple] = []

    async def flush(*args):
        calls.append(args)

    c = MessageCoalescer(flush=flush, window_seconds=0.1)
    await c.enqueue("sid1", "chat", 1, "ghost")
    c.forget("sid1")
    await asyncio.sleep(0.3)

    assert calls == []


@pytest.mark.asyncio
async def test_messages_after_flush_start_a_new_buffer():
    calls: list[tuple] = []

    async def flush(sid, chat_id, bot_id, combined):
        calls.append((sid, chat_id, bot_id, combined))

    c = MessageCoalescer(flush=flush, window_seconds=0.1)
    await c.enqueue("sid1", "chat", 1, "first")
    await asyncio.sleep(0.3)
    await c.enqueue("sid1", "chat", 1, "second")
    await asyncio.sleep(0.3)

    assert [combined for _, _, _, combined in calls] == ["first", "second"]
