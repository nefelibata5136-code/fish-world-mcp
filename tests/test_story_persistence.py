"""Persist actual story changes without mixing setup instructions into the narrative."""
import asyncio
import hashlib
import json

from conversation import Conversation
from llm import MockProvider
from storage import Storage, encode


def reply(text="潮水轻轻拍着窗外的礁石。", **changes):
    return {"reply": text, "scene": "海边小屋", "summary": "玩家在同一世界自由生活。",
            "facts": {}, "state": {}, **changes}


def make_conversation(tmp_path, responses=None):
    store = Storage(tmp_path / "story.db", confirmation_every=0)
    provider = MockProvider(responses=responses)
    return Conversation(store, provider), store, provider


def test_nested_room_move_keeps_items_rooms_and_npc_state_after_restart(tmp_path):
    async def run():
        conversation, store, provider = make_conversation(tmp_path, [
            reply("你把蓝色书架摆在客厅东墙，书桌仍在窗边。", facts={
                "home.bookshelf_position": "客厅东墙", "npc.ayuan": "阿圆是认识的章鱼朋友。"}, state={
                "home": {"rooms": {
                    "living": {"wall": "白色", "items": {
                        "bookshelf": {"color": "蓝色", "position": "东墙", "books": ["海洋手记"]},
                        "desk": {"position": "窗边", "material": "木头"}}},
                    "bedroom": {"curtain": "浅蓝色"}}, "garden": {"flowers": ["白色小花"]}},
                "npcs": {"ayuan": {"name": "阿圆", "relationship": "朋友"}}}),
            reply("你把同一座蓝色书架从东墙搬到西墙。", facts={"home.bookshelf_position": "客厅西墙"}, state={
                "home": {"rooms": {"living": {"items": {"bookshelf": {"position": "西墙"}}}}}})])
        wid = (await conversation.start("小鱼", preset="home"))["world_id"]
        await conversation.action(wid, "在客厅东墙放蓝色书架，在窗边放木桌。", "furnish")
        result = await conversation.action(wid, "把蓝色书架移到西墙，其他布置保持原样。", "move")
        shelf = result["state"]["home"]["rooms"]["living"]["items"]["bookshelf"]
        assert shelf == {"color": "蓝色", "position": "西墙", "books": ["海洋手记"]}
        assert "东墙" not in encode(shelf)
        await conversation.end(wid)
        reopened = Conversation(Storage(store.path, confirmation_every=0), MockProvider())
        restored = await reopened.resume(wid)
        living = restored["state"]["home"]["rooms"]["living"]
        assert living["items"]["desk"] == {"position": "窗边", "material": "木头"}
        assert living["wall"] == "白色"
        assert restored["state"]["home"]["rooms"]["bedroom"] == {"curtain": "浅蓝色"}
        assert restored["state"]["home"]["garden"] == {"flowers": ["白色小花"]}
        assert restored["state"]["npcs"]["ayuan"] == {"name": "阿圆", "relationship": "朋友"}
        assert restored["facts"]["home.bookshelf_position"] == "客厅西墙"
        assert restored["facts"]["npc.ayuan"] == "阿圆是认识的章鱼朋友。"
        assert len(provider.calls) == 2
    asyncio.run(run())


def test_explicit_lists_scalars_and_null_replace_without_implicit_deletion(tmp_path):
    async def run():
        conversation, store, _ = make_conversation(tmp_path, [
            reply(state={"home": {"guests": ["阿圆", "海龟"], "light": {"on": True},
                                  "note": "旧便条", "decor": {"vase": "白色", "rug": "蓝色"}}}),
            reply(state={"home": {"guests": [], "light": False, "note": None, "decor": {}}}),
            reply(state={"home": {"light": {"on": False, "color": "暖色"}}})])
        wid = (await conversation.start("小鱼", preset="home"))["world_id"]
        await conversation.action(wid, "布置房间并邀请朋友。", "values-1")
        await conversation.action(wid, "送别客人，关灯，清空便条。", "values-2")
        home = store.get_world(wid)["state"]["home"]
        assert home["guests"] == []
        assert home["light"] is False
        assert "note" in home and home["note"] is None
        assert home["decor"] == {"vase": "白色", "rug": "蓝色"}
        await conversation.action(wid, "换一盏暖色灯，但先不点亮。", "values-3")
        assert store.get_world(wid)["state"]["home"]["light"] == {"on": False, "color": "暖色"}
    asyncio.run(run())


def test_npc_shared_history_and_completed_promise_survive_window_and_restart(tmp_path):
    async def run():
        responses = [reply(f"第{turn}天，海边的生活平静继续。") for turn in range(1, 26)]
        responses[0] = reply("初见阿圆，她正在寻找一枚蓝色贝壳。", facts={
            "npc.ayuan": "阿圆是喜欢收集贝壳的章鱼。"}, state={
            "npcs": {"ayuan": {"name": "阿圆", "relationship": "初识", "shared_history": ["在珊瑚礁初见"]}}})
        responses[1] = reply("阿圆记下你的承诺。", facts={"promise.shell": "玩家答应帮阿圆归还蓝色贝壳，尚未完成。"}, state={
            "quests": {"shell": {"description": "归还阿圆的蓝色贝壳", "status": "pending"}},
            "pending": ["归还蓝色贝壳"]})
        responses[2] = reply("你和阿圆一起看了礁石边的日落。", facts={
            "shared.sunset": "玩家与阿圆曾一起在礁石边看日落。"}, state={
            "npcs": {"ayuan": {"relationship": "熟悉的朋友", "shared_history": ["在珊瑚礁初见", "一起看日落"]}}})
        responses[12] = reply("蓝色贝壳回到了阿圆手中，你的承诺已完成。", facts={
            "promise.shell": "玩家已经把蓝色贝壳归还阿圆，承诺已完成。"}, state={
            "quests": {"shell": {"status": "completed"}}, "pending": []})
        responses[19] = reply("家里的蓝色书架摆在西墙。", state={
            "home": {"rooms": {"living": {"bookshelf": {"position": "西墙", "color": "蓝色"}}}}})
        conversation, store, provider = make_conversation(tmp_path, responses)
        wid = (await conversation.start("小鱼", preset="home"))["world_id"]
        for turn in range(1, 26):
            await conversation.action(wid, f"第{turn}天自由生活。", f"day-{turn}")
        assert all("初见阿圆" not in row["response"] for row in store.recent_messages(wid, 6))
        await conversation.end(wid)
        returning_provider = MockProvider(responses=[reply("阿圆记得日落和那枚已经归还的贝壳。")])
        reopened = Conversation(Storage(store.path, confirmation_every=0), returning_provider)
        await reopened.resume(wid)
        await reopened.action(wid, "我回家后想去看看阿圆，她还记得我们的日落和贝壳吗？", "return")
        context = json.loads(returning_provider.calls[0]["messages"][-1]["content"])["context"]
        assert context["facts"]["shared.sunset"] == "玩家与阿圆曾一起在礁石边看日落。"
        assert "已经" in context["facts"]["promise.shell"] and "已完成" in context["facts"]["promise.shell"]
        assert context["state"]["quests"]["shell"] == {"description": "归还阿圆的蓝色贝壳", "status": "completed"}
        assert context["state"]["pending"] == []
        assert context["state"]["npcs"]["ayuan"] == {
            "name": "阿圆", "relationship": "熟悉的朋友", "shared_history": ["在珊瑚礁初见", "一起看日落"]}
        assert context["state"]["home"]["rooms"]["living"]["bookshelf"] == {"position": "西墙", "color": "蓝色"}
        events = store.events(wid, limit=1000)
        assert len([event for event in events if event["kind"] in ("player", "world")]) == 52
        assert any("初见阿圆" in event["content"] for event in events)
        assert len(provider.calls) == 25 and len(returning_provider.calls) == 1
    asyncio.run(run())


def test_nested_updates_never_merge_across_worlds(tmp_path):
    async def run():
        conversation, store, _ = make_conversation(tmp_path, [
            reply(state={"home": {"room": {"shelf": {"color": "蓝色", "position": "东墙"}}},
                         "npcs": {"friend": {"name": "阿圆"}}}),
            reply(state={"home": {"room": {"shelf": {"color": "红色", "position": "南墙"}}},
                         "npcs": {"friend": {"name": "小鹿"}}}),
            reply(state={"home": {"room": {"shelf": {"position": "西墙"}}}})])
        a = (await conversation.start("海里的鱼", preset="home"))["world_id"]
        b = (await conversation.start("森林里的鱼", preset="home"))["world_id"]
        await conversation.action(a, "布置蓝色书架，认识阿圆。", "a-1")
        await conversation.action(b, "布置红色书架，认识小鹿。", "b-1")
        await conversation.action(a, "把书架移到西墙。", "a-2")
        first, second = store.get_world(a)["state"], store.get_world(b)["state"]
        assert first["home"]["room"]["shelf"] == {"color": "蓝色", "position": "西墙"}
        assert second["home"]["room"]["shelf"] == {"color": "红色", "position": "南墙"}
        assert first["npcs"]["friend"]["name"] == "阿圆"
        assert second["npcs"]["friend"]["name"] == "小鹿"
    asyncio.run(run())


def event_hash(event):
    return hashlib.sha256(encode({key: event[key] for key in ("kind", "turn", "content", "data")}).encode()).hexdigest()


def test_new_created_event_records_scene_instead_of_world_instructions(tmp_path):
    async def run():
        conversation, store, provider = make_conversation(tmp_path)
        world = await conversation.start("小鱼", preset="home", preferences="SETUP_ONLY_RULE_4917")
        created = store.events(world["world_id"])[0]
        assert created["kind"] == "created" and created["content"] == world["scene"]
        assert "SETUP_ONLY_RULE_4917" in world["setting"]
        assert "SETUP_ONLY_RULE_4917" not in created["content"]
        assert "source_content_hash" not in created and "redacted" not in created
        assert created["content_hash"] == event_hash(created)
        assert store.events(world["world_id"], include_private=True)[0] == created
        assert not provider.calls
    asyncio.run(run())


def test_legacy_created_event_projects_scene_without_rewriting_source_or_hash(tmp_path):
    async def run():
        conversation, store, _ = make_conversation(tmp_path)
        world = await conversation.start("旧世界小鱼", preset="home")
        wid = world["world_id"]
        original = store.events(wid, include_private=True)[0]
        legacy = {**original, "content": "LEGACY_CONTROL_RULE_7419：世界设定与权限说明。"}
        legacy_hash = event_hash(legacy)
        with store.connect(True) as db:
            db.execute("UPDATE events SET content=?,content_hash=? WHERE id=?",
                       (legacy["content"], legacy_hash, original["id"]))
        before_world = store.get_world(wid)
        for _ in range(2):
            public = store.events(wid)[0]
            raw = store.events(wid, include_private=True)[0]
            assert public["id"] == raw["id"] == original["id"]
            assert public["seq"] == raw["seq"] == original["seq"]
            assert public["content"] == original["data"]["scene"]
            assert "LEGACY_CONTROL_RULE_7419" not in public["content"]
            assert public["source_content_hash"] == legacy_hash and public["redacted"] is True
            assert public["content_hash"] == event_hash(public)
            assert raw["content"] == legacy["content"] and raw["content_hash"] == legacy_hash
            assert "redacted" not in raw and "source_content_hash" not in raw
        with store.connect() as db:
            row = db.execute("SELECT content,content_hash FROM events WHERE id=?", (original["id"],)).fetchone()
        assert row["content"] == legacy["content"] and row["content_hash"] == legacy_hash
        assert store.get_world(wid) == before_world
    asyncio.run(run())
