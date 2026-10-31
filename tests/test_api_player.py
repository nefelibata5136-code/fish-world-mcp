"""Run the actual two-model loop with synthetic offline player identities only."""
import asyncio
import json

import pytest

from api_player import APIPlayer
from conversation import Conversation
from export_records import export_json, iter_markdown
from llm import LLMResult, MockProvider, ProviderError
from storage import Storage, WorldError


async def setup(tmp_path, provider=None, *, budget_cny=1.0):
    store = Storage(tmp_path / "api-world.db")
    provider = provider or MockProvider()
    conversation = Conversation(store, provider)
    wid = (await conversation.start("合成临时玩家", mode="api", budget_cny=budget_cny))["world_id"]
    store.set_player_profile(wid, "SYNTHETIC_PRIVATE_CORE_4917", "SYNTHETIC_PRIVATE_MEMORY_7419", {})
    return wid, store, provider, conversation, APIPlayer(conversation)


def test_automatic_five_turns_exactly_two_calls_per_round_and_no_gate_bypass(tmp_path):
    async def run():
        wid, store, provider, conversation, player = await setup(tmp_path)
        await player.start(wid)
        await player.tasks[wid]
        assert conversation.status(wid)["turn"] == 5
        assert conversation.status(wid)["pause_reason"] == "confirmation_required"
        assert [call["role"] for call in provider.calls] == [role for _ in range(5) for role in ("player", "world")]
        assert len([event for event in store.events(wid) if event["kind"] in ("player", "world")]) == 10
        assert not player.state(wid)["running"]
        await conversation.resume(wid)
        with pytest.raises(WorldError):
            await player.step(wid)
        assert len(provider.calls) == 10
        await player.close()
    asyncio.run(run())


def test_twenty_rounds_restart_and_human_approval_keep_one_history(tmp_path):
    async def run():
        wid, store, provider, conversation, player = await setup(tmp_path)
        for expected in (5, 10, 15, 20):
            if expected > 5:
                await conversation.approve(wid)
            if expected == 15:
                await player.close()
                conversation = Conversation(Storage(store.path), provider)
                player = APIPlayer(conversation)
                assert player.store.get_player_profile(wid)["core"] == "SYNTHETIC_PRIVATE_CORE_4917"
            await player.start(wid)
            await player.tasks[wid]
            assert conversation.status(wid)["turn"] == expected
            assert conversation.status(wid)["status"] == "paused"
        messages = [event for event in store.events(wid, limit=1000) if event["kind"] in ("player", "world")]
        assert len(messages) == 40 and len(provider.calls) == 40
        assert len(store.list_worlds()) == 1
        assert [event["turn"] for event in messages] == [turn for turn in range(1, 21) for _ in range(2)]
        await player.close()
    asyncio.run(run())


def test_saved_player_decision_reused_after_restart_without_player_call(tmp_path):
    async def run():
        wid, store, provider, conversation, _ = await setup(tmp_path)
        store.begin_player_action(wid, "api-1-staged")
        store.save_player_action(wid, "api-1-staged", "我想去看一看自己的蓝色书架。")
        reopened = APIPlayer(Conversation(Storage(store.path), provider))
        result = await reopened.step(wid)
        assert result["turn"] == 1
        assert [call["role"] for call in provider.calls] == ["world"]
        player_events = [event for event in store.events(wid) if event["kind"] == "player"]
        assert player_events[0]["content"] == "我想去看一看自己的蓝色书架。"
    asyncio.run(run())


def test_core_and_private_memory_only_go_to_player_not_world_or_exports(tmp_path):
    async def run():
        wid, store, provider, conversation, player = await setup(tmp_path)
        await player.step(wid)
        player_prompt = json.dumps(provider.calls[0]["messages"])
        world_prompt = json.dumps(provider.calls[1]["messages"])
        exported = json.dumps(export_json(store, wid)) + "".join(iter_markdown(store, wid))
        public_status = json.dumps(conversation.status(wid))
        for marker in ("SYNTHETIC_PRIVATE_CORE_4917", "SYNTHETIC_PRIVATE_MEMORY_7419"):
            assert marker in player_prompt
            assert marker not in world_prompt and marker not in exported and marker not in public_status
        assert "API临时玩家" in provider.calls[0]["messages"][0]["content"]
    asyncio.run(run())


def test_missing_profile_does_not_start_paid_player_call(tmp_path):
    async def run():
        store, provider = Storage(tmp_path / "missing.db"), MockProvider()
        conversation = Conversation(store, provider)
        wid = (await conversation.start("临时玩家", mode="api"))["world_id"]
        with pytest.raises(WorldError) as exc:
            await APIPlayer(conversation).step(wid)
        assert exc.value.code == "missing_core" and len(provider.calls) == 0
    asyncio.run(run())


def test_failed_player_call_requires_recovery_and_explicit_unknown_cost_reconciliation(tmp_path):
    async def run():
        provider = MockProvider(responses=[ProviderError("synthetic timeout")])
        wid, store, _, conversation, player = await setup(tmp_path, provider)
        with pytest.raises(WorldError):
            await player.step(wid)
        with pytest.raises(WorldError) as exc:
            await APIPlayer(Conversation(Storage(store.path), provider)).step(wid)
        assert exc.value.code == "player_request_uncertain" and len(provider.calls) == 1
        await player.recover(wid)
        call = store.usage(wid)[0]
        await conversation.reconcile_cost(wid, call["id"], 0.01)
        await conversation.resume(wid)
        assert (await player.step(wid))["turn"] == 1
        assert [call["role"] for call in provider.calls] == ["player", "player", "world"]
        assert conversation.status(wid)["unknown_cost_calls"] == 0
    asyncio.run(run())


def test_cancelled_player_generation_persists_unknown_attempt_and_never_auto_retries(tmp_path):
    async def run():
        provider = MockProvider(delay=1)
        wid, store, _, conversation, player = await setup(tmp_path, provider)
        await player.start(wid)
        await asyncio.sleep(0.01)
        await player.close()
        assert conversation.status(wid)["turn"] == 0 and len(provider.calls) == 1
        reopened = APIPlayer(Conversation(Storage(store.path), provider))
        with pytest.raises(WorldError) as exc:
            await reopened.step(wid)
        assert exc.value.code == "player_request_uncertain"
        assert store.usage(wid)[0]["status"] == "interrupted"
        await reopened.recover(wid)
        assert store.usage(wid)[0]["cost_cny"] is None
        assert len(provider.calls) == 1
    asyncio.run(run())


def test_player_budget_preflight_stops_before_model_call(tmp_path):
    async def run():
        wid, _, provider, conversation, player = await setup(tmp_path, budget_cny=0.1)
        provider.estimated_cost = lambda *args, **kwargs: 0.2
        with pytest.raises(WorldError) as exc:
            await player.step(wid)
        assert exc.value.code == "budget_exceeded" and not provider.calls
        assert conversation.status(wid)["status"] == "paused"
    asyncio.run(run())


def test_budget_pause_between_player_and_world_reuses_decision_after_raising_budget(tmp_path):
    async def run():
        decision = LLMResult("我想认识阿圆。", {}, "fixture", 0.03, "fixture estimate")
        provider = MockProvider(responses=[decision])
        wid, store, _, conversation, player = await setup(tmp_path, provider, budget_cny=0.04)
        provider.estimated_cost = lambda messages, role="world", config=None: 0.03 if role == "player" else 0.02
        with pytest.raises(WorldError) as exc:
            await player.step(wid)
        assert exc.value.code == "budget_exceeded" and [call["role"] for call in provider.calls] == ["player"]
        assert player._latest(wid, 1)["status"] == "ready"
        await conversation.update_budget(wid, 0.1)
        assert (await player.step(wid))["turn"] == 1
        assert [call["role"] for call in provider.calls] == ["player", "world"]
        assert store.usage(wid)[0]["cost_cny"] == 0.03
    asyncio.run(run())


def test_mcp_world_cannot_be_autoplayed_by_temporary_player(tmp_path):
    async def run():
        store, provider = Storage(tmp_path / "mcp.db"), MockProvider()
        conversation = Conversation(store, provider)
        wid = (await conversation.start("原Agent", mode="mcp"))["world_id"]
        player = APIPlayer(conversation)
        with pytest.raises(WorldError) as exc:
            await player.start(wid)
        assert exc.value.code == "wrong_mode" and not provider.calls
    asyncio.run(run())


def test_failed_world_reply_human_recovery_keeps_original_player_decision(tmp_path):
    async def run():
        provider = MockProvider(responses=["我想帮阿圆找贝壳。", "not valid world json"])
        wid, store, _, conversation, player = await setup(tmp_path, provider)
        with pytest.raises(WorldError) as exc:
            await player.step(wid)
        assert exc.value.code == "invalid_model_output"
        staged = player._latest(wid, 1)
        assert staged["status"] == "ready"
        assert store.get_request(wid, staged["request_id"])["stage"] == "failed"
        await player.recover(wid)
        result = await player.step(wid)
        assert result["turn"] == 1
        assert [call["role"] for call in provider.calls] == ["player", "world", "world"]
        player_events = [event for event in store.events(wid) if event["kind"] == "player"]
        assert [event["content"] for event in player_events] == ["我想帮阿圆找贝壳。"]
    asyncio.run(run())
