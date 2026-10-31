import json

from api_player import APIPlayer
from conversation import Conversation
from llm import MockProvider
from prompts import action_content, world_context
from storage import Storage, encode


async def test_named_old_npc_and_home_state_survive_context_pressure_for_both_models(tmp_path):
    store = Storage(tmp_path / "pressure.db", confirmation_every=0)
    provider = MockProvider()
    service = Conversation(store, provider)
    wid = (await service.start("上下文验收鱼", mode="api"))["world_id"]
    gift = "阿圆保存着小鱼送给她的紫色贝壳，记得两人共同寻找灯塔的经历。"
    facts = {"npc.ayuan.gift": gift}
    facts.update({f"decor.{i}": f"装饰条目{i}：" + "这是一段不相关的背景陈设。" * 40 for i in range(180)})
    items = {"shell": {"description": "小鱼放在书架上的紫色贝壳", "location": "书架"}}
    items.update({f"vase.{i}": "普通花瓶的介绍。" * 120 for i in range(100)})
    state = {"home": {"items": items}, "tasks": {"return_shell": "completed"}}
    store.update_world(wid, {"facts": facts, "state": state, "scene": "小鱼回到熟悉的家，阿圆来访。",
                             "last_reply": "阿圆提到了紫色贝壳与书架。", "summary": "这段摘要很短，没有旧礼物细节。"})
    world = store.get_world(wid)
    context = json.loads(action_content(world, "我想和阿圆聊聊紫色贝壳，再看看书架。"))["context"]
    assert context["facts"]["npc.ayuan.gift"] == gift
    assert context["state"]["home"]["items"]["shell"]["location"] == "书架"
    assert context["facts_omitted"] > 0 and context["state_partial"]
    assert len(encode(context["facts"])) <= 9000 and len(encode(context["state"])) <= 8000
    assert store.get_world(wid)["state"] == state  # selecting context never changes stored truth
    store.set_player_profile(wid, "原有核心保持不变")
    tail = json.loads(APIPlayer(Conversation(Storage(store.path, confirmation_every=0), provider))
                      .messages(wid, store.get_player_profile(wid))[-1]["content"])
    assert tail["facts"]["npc.ayuan.gift"] == gift
    assert tail["state"]["home"]["items"]["shell"]["location"] == "书架"
    assert len(tail["facts"]) < 100  # relevance, rather than the old last-100 slice
    assert not provider.calls


async def test_player_context_omits_mystery_truth_and_does_not_promote_plans_to_facts(tmp_path):
    store = Storage(tmp_path / "plans.db", confirmation_every=0)
    service = Conversation(store, MockProvider())
    wid = (await service.start("上下文验收鱼"))["world_id"]
    store.update_world(wid, {"fixed_facts": {"culprit": "私有谜底"}, "facts": {"home.table": "木桌在窗边"}})
    world = store.get_world(wid)
    plan = "我打算移动桌子，如果她愿意，再邀请她吃饭。"
    assert "fixed_facts" not in world_context(world, plan)
    context = json.loads(action_content(world, plan))
    assert context["message"] == plan
    assert context["context"]["facts"]["home.table"] == "木桌在窗边"
    assert context["context"]["fixed_facts"]["culprit"] == "私有谜底"
    assert store.get_world(wid)["facts"] == {"home.table": "木桌在窗边"}
