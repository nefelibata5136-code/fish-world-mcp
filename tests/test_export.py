import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from conversation import Conversation
from export_records import collect, export_json, export_page, iter_markdown, read_only_store
from llm import MockProvider
from storage import Storage


async def test_readonly_complete_export_and_stable_ids(tmp_path):
    path = tmp_path / "source.db"
    store = Storage(path)
    conversation = Conversation(store, MockProvider())
    world_id = (await conversation.start("导出验收鱼"))["world_id"]
    for index in range(4):
        await conversation.action(world_id, f"自由活动{index}", f"export-{index}")
    store.set_player_profile(world_id, "PRIVATE_SYNTHETIC_CORE", "PRIVATE_SYNTHETIC_MEMORY")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    source = read_only_store(path)
    cursor, events = 0, []
    while True:
        page = export_page(source, world_id, cursor, 2)
        events += page["events"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(events) == 9  # create + four complete player/world pairs
    assert export_json(source, world_id)["events"] == events
    markdown = "".join(iter_markdown(source, world_id))
    assert all(f"自由活动{index}" in markdown for index in range(4))
    rows = list(collect(source))
    repeated = list(collect(source))
    assert [r["record_id"] for r in rows] == [r["record_id"] for r in repeated]
    assert all(r["occurred_at"] and r["observed_at"] for r in rows)
    assert all(r["content"]["simulation"] for r in rows)
    assert "PRIVATE_SYNTHETIC" not in json.dumps(export_json(source, world_id))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with source.connect() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM events")
    with pytest.raises(ValueError, match="读取"):
        source.update_world(world_id, {"turn": 99})


async def test_summary_and_voluntary_reflection_remain_separate(tmp_path):
    store = Storage(tmp_path / "source.db")
    conversation = Conversation(store, MockProvider())
    world_id = (await conversation.start("感想验收鱼"))["world_id"]
    await conversation.action(world_id, "认识阿圆", "one")
    await conversation.end(world_id, reflection="这是我自愿留下的玩家感想。")
    exported = export_json(store, world_id)
    assert "自愿留下" not in exported["world"]["summary"]
    reflections = [e for e in exported["events"] if e["kind"] == "reflection"]
    assert len(reflections) == 1
    assert "自愿留下" in reflections[0]["content"]
