"""Behavioral acceptance of the durable service; all providers here are offline."""
import asyncio
import json
import sqlite3

import pytest

from conversation import Conversation
from llm import LLMResult, MockProvider, ProviderError
from prompts import SYSTEM
from storage import Storage, WorldError


def reply(text="珊瑚礁在眼前。", **changes):
    return {"reply": text, "scene": "珊瑚礁", "summary": "在海洋中自由探索。",
            "facts": {}, "state": {}, **changes}


def service(tmp_path, responses=None, **kwargs):
    store = Storage(tmp_path / "worlds.db")
    provider = MockProvider(responses=responses, **kwargs)
    return Conversation(store, provider), store, provider


def test_twenty_turns_shared_history_context_and_human_gates(tmp_path):
    async def run():
        responses = [reply(f"第{i}轮的外部结果。") for i in range(1, 21)]
        responses[0] = reply("你遇见了章鱼阿圆。", facts={"npc.ayuan": "阿圆是一只喜欢收藏贝壳的章鱼。"})
        responses[4] = reply("你帮助阿圆找回了贝壳。", facts={"shared.shell": "第5轮，玩家帮助阿圆找到了蓝色贝壳。"})
        conversation, store, provider = service(tmp_path, responses)
        world = await conversation.start("蓝色大肥鱼")
        wid = world["world_id"]
        assert world["turn"] == 0 and len(provider.calls) == 0
        for i in range(1, 21):
            if i > 1 and (i - 1) % 5 == 0:
                count = len(provider.calls)
                with pytest.raises(WorldError):
                    await conversation.action(wid, "试图绕过人类确认", f"blocked-{i}")
                assert len(provider.calls) == count
                assert (await conversation.resume(wid))["status"] == "paused"
                await conversation.approve(wid)
            action = "我想再次寻找阿圆。" if i == 12 else f"自由行动 {i}"
            result = await conversation.action(wid, action, f"request-{i}")
            assert result["turn"] == i
            assert result["status"] == ("paused" if i % 5 == 0 else "active")
        events = store.events(wid, limit=1000)
        messages = [event for event in events if event["kind"] in ("player", "world")]
        assert len(messages) == 40 and len(provider.calls) == 20
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert [event["turn"] for event in messages] == [i for i in range(1, 21) for _ in range(2)]
        context = json.loads(provider.calls[11]["messages"][-1]["content"])["context"]
        assert "阿圆" in context["facts"]["npc.ayuan"]
        assert "第5轮" in context["facts"]["shared.shell"]
        assert all(event["world_id"] == wid for event in events)
        assert all(event["occurred_at"] and event["observed_at"] and len(event["content_hash"]) == 64 for event in events)
        assert conversation.status(wid)["turn"] == 20
    asyncio.run(run())


def test_duplicate_concurrent_submissions_call_provider_once(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path, delay=0.02)
        wid = (await conversation.start("小鱼"))["world_id"]
        a, b = await asyncio.gather(conversation.action(wid, "看看海草", "same"),
                                    conversation.action(wid, "看看海草", "same"))
        assert a == b and len(provider.calls) == 1 and a["turn"] == 1
        with pytest.raises(WorldError, match="相同请求"):
            await conversation.action(wid, "另外一件事", "same")
        assert len(store.recent_messages(wid)) == 1
    asyncio.run(run())


def test_two_service_instances_cannot_generate_same_world_concurrently(tmp_path):
    async def run():
        a, store, provider = service(tmp_path, delay=0.05)
        b = Conversation(Storage(store.path), provider)
        wid = (await a.start("鱼"))["world_id"]
        first = asyncio.create_task(a.action(wid, "看海", "a"))
        await asyncio.sleep(0.01)
        with pytest.raises(WorldError) as exc:
            await b.action(wid, "另一个行动", "b")
        assert exc.value.code == "request_in_progress"
        assert (await first)["turn"] == 1 and len(provider.calls) == 1
    asyncio.run(run())


def test_reconnect_and_long_term_home_after_restart(tmp_path):
    async def run():
        a, store, provider = service(tmp_path, [
            reply("海边小屋已经建好。", facts={"home.location": "海边小屋"}, state={"room": "客厅"}),
            reply("蓝色书架放在窗边。", facts={"home.shelf": "窗边摆着蓝色书架"}, state={"shelf": "蓝色，窗边"})])
        wid = (await a.start("小鱼", preset="home"))["world_id"]
        await a.action(wid, "建一间海边小屋", "home-1")
        await a.action(wid, "在窗边放一只蓝色书架", "home-2")
        await a.end(wid)
        reopened = Conversation(Storage(store.path), MockProvider())
        result = await reopened.resume(wid)
        assert result["world_id"] == wid and result["turn"] == 2
        assert result["state"] == {"room": "客厅", "shelf": "蓝色，窗边"}
        assert result["facts"]["home.shelf"] == "窗边摆着蓝色书架"
        assert len(reopened.store.list_worlds()) == 1
    asyncio.run(run())


def test_no_fixed_menu_and_no_preparation_questionnaire(tmp_path):
    async def run():
        conversation, _, provider = service(tmp_path, [reply("可以去小镇，也可以看看海草。"), reply("你游到海面，阳光洒下来。")])
        wid = (await conversation.start("小鱼", idea="暂时不知道玩什么"))["world_id"]
        await conversation.action(wid, "不知道做什么，先和我聊聊。", "chat")
        await conversation.action(wid, "我不选那些活动，想去海面晒太阳。", "free")
        actual = json.loads(provider.calls[-1]["messages"][-1]["content"])["message"]
        assert actual == "我不选那些活动，想去海面晒太阳。"
    asyncio.run(run())


def test_malformed_paid_reply_preserves_usage_without_success_events(tmp_path):
    async def run():
        result = LLMResult(content="not json", usage={"prompt_tokens": 20, "completion_tokens": 5},
                           model="test", cost_cny=0.02, pricing_note="fixture estimate")
        conversation, store, _ = service(tmp_path, [result])
        wid = (await conversation.start("小鱼"))["world_id"]
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "看海", "bad")
        assert exc.value.code == "invalid_model_output"
        assert conversation.status(wid)["turn"] == 0
        assert not [event for event in store.events(wid) if event["kind"] in ("player", "world")]
        assert store.usage(wid)[0]["cost_cny"] == 0.02
        assert store.get_request(wid, "bad")["stage"] == "failed"
    asyncio.run(run())


def test_failure_unknown_usage_requires_human_reconciliation(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path, [ProviderError("network")])
        wid = (await conversation.start("小鱼"))["world_id"]
        with pytest.raises(WorldError):
            await conversation.action(wid, "看海", "network")
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "再看看", "new")
        assert exc.value.code == "unknown_cost" and len(provider.calls) == 1
        call = store.usage(wid)[0]
        await conversation.reconcile_cost(wid, call["id"], 0.005)
        await conversation.resume(wid)
        await conversation.action(wid, "继续", "after-reconcile")
        assert conversation.status(wid)["turn"] == 1
        assert store.usage(wid)[0]["status"] == "human_estimate"
    asyncio.run(run())


def test_configuration_failure_is_not_unknown_paid_debt(tmp_path):
    async def run():
        conversation, store, _ = service(tmp_path, [ProviderError("missing key", attempted=False)])
        wid = (await conversation.start("小鱼"))["world_id"]
        with pytest.raises(WorldError):
            await conversation.action(wid, "看海", "no-key")
        assert store.usage(wid)[0]["status"] == "not_sent"
        assert conversation.status(wid)["unknown_cost_calls"] == 0
        await conversation.action(wid, "继续", "configured")
        assert conversation.status(wid)["turn"] == 1
    asyncio.run(run())


def test_process_interrupt_before_commit_reuses_durable_response(tmp_path, monkeypatch):
    async def run():
        conversation, store, provider = service(tmp_path, [reply("不会丢失的回答。")])
        wid = (await conversation.start("小鱼"))["world_id"]
        original = store.finish_turn
        monkeypatch.setattr(store, "finish_turn", lambda *args: (_ for _ in ()).throw(RuntimeError("simulated crash")))
        with pytest.raises(RuntimeError):
            await conversation.action(wid, "看海", "durable")
        assert store.get_request(wid, "durable")["stage"] == "received"
        monkeypatch.setattr(store, "finish_turn", original)
        reopened = Conversation(Storage(store.path), provider)
        result = await reopened.action(wid, "看海", "durable")
        assert result["reply"] == "不会丢失的回答。" and len(provider.calls) == 1
        assert len(store.recent_messages(wid)) == 1
    asyncio.run(run())


def test_cancelled_inflight_request_never_automatically_repeats(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path, delay=1)
        wid = (await conversation.start("小鱼"))["world_id"]
        running = asyncio.create_task(conversation.action(wid, "看海", "uncertain"))
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        reopened = Conversation(Storage(store.path), provider)
        with pytest.raises(WorldError) as exc:
            await reopened.action(wid, "看海", "uncertain")
        assert exc.value.code == "request_uncertain"
        assert len(provider.calls) == 1 and reopened.status(wid)["turn"] == 0
        await reopened.recover(wid, "uncertain")
        assert store.get_request(wid, "uncertain")["stage"] == "abandoned"
    asyncio.run(run())


def test_remote_call_holds_no_sqlite_write_transaction(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path)
        original = provider.complete
        async def inspect(messages, **kwargs):
            db = sqlite3.connect(store.path, timeout=0.01, isolation_level=None)
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
            db.close()
            return await original(messages, **kwargs)
        provider.complete = inspect
        wid = (await conversation.start("小鱼"))["world_id"]
        assert (await conversation.action(wid, "看海", "txn"))["turn"] == 1
    asyncio.run(run())


def test_independent_worlds_never_share_facts_or_messages(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path, [reply(facts={"npc": "阿圆"}), reply(facts={"npc": "小鹿"})])
        a = (await conversation.start("海鱼"))["world_id"]
        b = (await conversation.start("森林鱼", preset="home"))["world_id"]
        await conversation.action(a, "认识阿圆", "same-request-id")
        await conversation.action(b, "认识小鹿", "same-request-id")
        assert conversation.status(a)["facts"]["npc"] == "阿圆"
        assert conversation.status(b)["facts"]["npc"] == "小鹿"
        assert "阿圆" not in json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        assert all(row["world_id"] == b for row in store.events(b))
    asyncio.run(run())


def test_static_prefix_history_and_bounded_context(tmp_path):
    async def run():
        conversation, _, provider = service(tmp_path)
        wid = (await conversation.start("小鱼"))["world_id"]
        for i in range(20):
            if i and i % 5 == 0:
                await conversation.approve(wid)
            await conversation.action(wid, f"探索 {i}", f"prefix-{i}")
        first, second = provider.calls[0]["messages"], provider.calls[1]["messages"]
        assert second[:len(first)] == first
        assert all(call["messages"][0] == {"role": "system", "content": SYSTEM} for call in provider.calls)
        assert max(len(call["messages"]) for call in provider.calls) <= 25
        assert max(sum(len(msg["content"]) for msg in call["messages"]) for call in provider.calls) < 85000
    asyncio.run(run())


def test_mystery_truth_is_private_and_immutable(tmp_path):
    async def run():
        conversation, store, _ = service(tmp_path, [
            reply("窗边有一道足迹。", fixed_facts={"truth": "密封答案-灯塔管理员"}),
            reply("真相被改写。", fixed_facts={"truth": "另外一个人"})])
        wid = (await conversation.start("侦探鱼"))["world_id"]
        result = await conversation.action(wid, "观察窗户", "truth")
        assert "密封答案" not in json.dumps(result, ensure_ascii=False)
        assert "密封答案" not in json.dumps(conversation.status(wid), ensure_ascii=False)
        assert "密封答案" not in json.dumps(store.events(wid), ensure_ascii=False)
        assert "密封答案" in json.dumps(store.events(wid, include_private=True), ensure_ascii=False)
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "我猜是另一个人", "change-truth")
        assert exc.value.code == "fixed_fact_conflict"
        assert conversation.status(wid)["turn"] == 1
    asyncio.run(run())


def test_reading_and_archiving_do_not_count_rounds_or_prepay_approvals(tmp_path):
    async def run():
        conversation, store, provider = service(tmp_path)
        wid = (await conversation.start("小鱼"))["world_id"]
        for _ in range(8):
            await conversation.approve(wid)
            conversation.status(wid)
            store.events(wid)
        assert store.get_world(wid)["approved_until"] == 5 and len(provider.calls) == 0
        await conversation.end(wid, archive=True, reflection="这是我自己写的感想。")
        assert conversation.status(wid)["status"] == "archived"
        assert (await conversation.resume(wid))["turn"] == 0
        assert [event["content"] for event in store.events(wid) if event["kind"] == "reflection"] == ["这是我自己写的感想。"]
    asyncio.run(run())


def test_api_player_staging_and_profile_are_private_and_durable(tmp_path):
    async def run():
        conversation, store, _ = service(tmp_path)
        wid = (await conversation.start("临时小鱼", mode="api"))["world_id"]
        store.set_player_profile(wid, "PRIVATE_CORE", "PRIVATE_MEMORY", {"model": "example"})
        journal = store.begin_player_action(wid, "api-1")
        assert journal["new"] and journal["status"] == "calling"
        assert not store.begin_player_action(wid, "api-1")["new"]
        store.save_player_action(wid, "api-1", "去看海")
        reopened = Storage(store.path)
        assert reopened.get_player_action(wid, "api-1")["message"] == "去看海"
        assert reopened.get_player_profile(wid)["core"] == "PRIVATE_CORE"
        assert "PRIVATE_CORE" not in json.dumps(conversation.status(wid))
        assert "PRIVATE_MEMORY" not in json.dumps(store.events(wid))
    asyncio.run(run())


def test_budget_checked_before_any_paid_call_and_can_be_raised(tmp_path):
    async def run():
        conversation, _, provider = service(tmp_path)
        provider.estimated_cost = lambda *args, **kwargs: 0.2
        wid = (await conversation.start("小鱼", budget_cny=0.1))["world_id"]
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "看海", "over-budget")
        assert exc.value.code == "budget_exceeded" and len(provider.calls) == 0
        assert (await conversation.resume(wid))["status"] == "paused"
        await conversation.update_budget(wid, 0.5)
        assert (await conversation.action(wid, "看海", "within-budget"))["turn"] == 1
    asyncio.run(run())


def test_pause_during_paid_call_saves_current_result_but_stops_next(tmp_path):
    async def run():
        conversation, _, provider = service(tmp_path, delay=0.03)
        wid = (await conversation.start("小鱼"))["world_id"]
        task = asyncio.create_task(conversation.action(wid, "看海", "current"))
        await asyncio.sleep(0.01)
        await conversation.pause(wid)
        result = await task
        assert result["turn"] == 1 and result["status"] == "paused"
        with pytest.raises(WorldError):
            await conversation.action(wid, "继续", "next")
        assert len(provider.calls) == 1
    asyncio.run(run())


def test_multiple_player_runners_deduplicate_same_turn_even_different_ids(tmp_path):
    async def run():
        conversation, store, _ = service(tmp_path)
        wid = (await conversation.start("临时小鱼", mode="api"))["world_id"]
        a = store.begin_player_action(wid, "api-1-first")
        b = Storage(store.path).begin_player_action(wid, "api-1-second")
        assert a["new"] and not b["new"]
        assert b["request_id"] == "api-1-first"
        with pytest.raises(WorldError):
            store.begin_player_action(wid, "api-2-too-early")
    asyncio.run(run())
