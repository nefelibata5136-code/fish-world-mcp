"""Confirmation can be disabled without weakening budgets, manual stops, or recovery."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app as app_module
from api_player import APIPlayer
from conversation import Conversation
from llm import MockProvider, ProviderError
from storage import Storage, WorldError


class CostedMock(MockProvider):
    """Deterministic finite auto-play: every offline decision costs a fixture cent."""
    def estimated_cost(self, messages, role="world", config=None):
        return 0.01

    async def complete(self, messages, role="world", config=None):
        result = await super().complete(messages, role=role, config=config)
        result.cost_cny = 0.01
        result.pricing_note = "Offline test fixture, no real API request."
        return result


class StopAfterWorld(MockProvider):
    """Simulate the owner's pause arriving during a specific world's response."""
    def __init__(self, stop_after):
        super().__init__()
        self.stop_after, self.world_calls, self.on_stop = stop_after, 0, None

    async def complete(self, messages, role="world", config=None):
        result = await super().complete(messages, role=role, config=config)
        if role == "world":
            self.world_calls += 1
            if self.world_calls == self.stop_after:
                await self.on_stop()
        return result


def make_conversation(tmp_path, provider=None, confirmation_every=0):
    store = Storage(tmp_path / "policy.db", confirmation_every=confirmation_every)
    provider = provider or MockProvider()
    return Conversation(store, provider), store, provider


def test_app_default_disables_confirmation_and_reports_no_next_checkpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("FISHWORLD_CONFIRM_EVERY", raising=False)
    monkeypatch.setattr(app_module, "load_dotenv", lambda *args, **kwargs: False)
    application = app_module.create_app(tmp_path / "default.db", MockProvider())
    assert application.state.store.confirmation_every == 0
    with TestClient(application) as web:
        token = web.get("/api/bootstrap").json()["csrf_token"]
        response = web.post("/api/worlds", json={"player_name": "离线政策测试鱼"},
                            headers={"X-FishWorld-CSRF": token})
        assert response.status_code == 200
        world = response.json()
        assert world["confirmation_every"] == 0
        assert world["confirmation_enabled"] is False
        assert world["next_confirmation_at"] is None
        assert world["next_confirmation"] is None


def test_disabled_policy_allows_twenty_three_actions_without_approval(tmp_path):
    async def run():
        conversation, store, provider = make_conversation(tmp_path)
        wid = (await conversation.start("离线鱼"))["world_id"]
        for turn in range(1, 24):
            result = await conversation.action(wid, f"自由观察第{turn}轮", f"disabled-{turn}")
            assert result["turn"] == turn and result["status"] == "active"
            assert result["next_confirmation_at"] is None
        status = conversation.status(wid)
        assert status["confirmation_enabled"] is False and status["turn"] == 23
        assert len(provider.calls) == 23
        assert len([e for e in store.events(wid) if e["kind"] in ("player", "world")]) == 46
    asyncio.run(run())


def test_automatic_player_crosses_twenty_rounds_then_budget_stops_before_next_call(tmp_path):
    async def run():
        conversation, store, provider = make_conversation(tmp_path, CostedMock())
        wid = (await conversation.start("临时离线鱼", mode="api", budget_cny=0.425))["world_id"]
        store.set_player_profile(wid, "只用于无网络测试的合成核心。", "", {})
        player = APIPlayer(conversation)
        await player.start(wid)
        await asyncio.wait_for(player.tasks[wid], timeout=30)
        status = conversation.status(wid)
        assert status["turn"] == 21
        assert status["status"] == "paused" and status["pause_reason"] == "budget_exceeded"
        assert status["next_confirmation_at"] is None
        assert [call["role"] for call in provider.calls] == [role for _ in range(21) for role in ("player", "world")]
        assert status["cost_cny"] == pytest.approx(0.42)
        assert len(store.usage(wid)) == 42
        await player.close()
    asyncio.run(run())


def test_manual_pause_still_stops_automatic_player_after_sixth_round(tmp_path):
    async def run():
        provider = StopAfterWorld(6)
        conversation, store, _ = make_conversation(tmp_path, provider)
        wid = (await conversation.start("临时离线鱼", mode="api"))["world_id"]
        store.set_player_profile(wid, "这是合成测试核心。", "", {})
        provider.on_stop = lambda: conversation.pause(wid)
        player = APIPlayer(conversation)
        await player.start(wid)
        await asyncio.wait_for(player.tasks[wid], timeout=15)
        status = conversation.status(wid)
        assert status["turn"] == 6
        assert status["status"] == "paused" and status["pause_reason"] == "manual"
        assert len(provider.calls) == 12
        with pytest.raises(WorldError):
            await player.step(wid)
        assert len(provider.calls) == 12
    asyncio.run(run())


def test_unknown_cost_still_blocks_following_paid_call_when_confirmation_disabled(tmp_path):
    async def run():
        conversation, store, provider = make_conversation(tmp_path, MockProvider(responses=[ProviderError("offline failure")]))
        wid = (await conversation.start("离线鱼"))["world_id"]
        with pytest.raises(WorldError):
            await conversation.action(wid, "观察", "uncertain")
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "继续观察", "must-not-call")
        assert exc.value.code == "unknown_cost"
        assert len(provider.calls) == 1 and conversation.status(wid)["turn"] == 0
        assert store.usage(wid)[0]["cost_cny"] is None
    asyncio.run(run())


def test_unknown_pricing_still_blocks_before_any_paid_call(tmp_path):
    async def run():
        conversation, _, provider = make_conversation(tmp_path)
        provider.estimated_cost = lambda *args, **kwargs: None
        wid = (await conversation.start("离线鱼"))["world_id"]
        with pytest.raises(WorldError) as exc:
            await conversation.action(wid, "观察", "no-price")
        assert exc.value.code == "unknown_pricing" and not provider.calls
        assert conversation.status(wid)["pause_reason"] == "unknown_pricing"
    asyncio.run(run())


def test_explicit_environment_five_restores_gate_and_resume_cannot_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("FISHWORLD_CONFIRM_EVERY", "5")
    monkeypatch.setattr(app_module, "load_dotenv", lambda *args, **kwargs: False)
    provider = MockProvider()
    application = app_module.create_app(tmp_path / "enabled.db", provider)
    assert application.state.store.confirmation_every == 5
    conversation = application.state.conversation
    async def run():
        wid = (await conversation.start("启用确认的离线鱼"))["world_id"]
        for i in range(5):
            await conversation.action(wid, f"观察{i}", f"enabled-{i}")
        assert conversation.status(wid)["pause_reason"] == "confirmation_required"
        assert (await conversation.resume(wid))["status"] == "paused"
        with pytest.raises(WorldError):
            await conversation.action(wid, "绕过确认", "blocked")
        assert len(provider.calls) == 5
        await conversation.approve(wid)
        assert (await conversation.action(wid, "确认后继续", "approved"))["turn"] == 6
    asyncio.run(run())


def test_old_confirmation_pause_is_read_only_until_explicit_run_then_continues_same_world(tmp_path):
    async def run():
        original, store, _ = make_conversation(tmp_path, confirmation_every=5)
        wid = (await original.start("旧世界离线鱼", mode="api"))["world_id"]
        store.set_player_profile(wid, "旧世界的合成测试核心。", "", {})
        for i in range(5):
            await original.action(wid, f"旧行动{i}", f"old-{i}")
        before = store.get_world(wid)
        assert before["pause_reason"] == "confirmation_required"
        provider = StopAfterWorld(1)
        reopened_store = Storage(store.path, confirmation_every=0)
        reopened = Conversation(reopened_store, provider)
        for _ in range(3):
            assert reopened.status(wid)["status"] == "paused"
            assert reopened.status(wid)["next_confirmation_at"] is None
        assert reopened_store.get_world(wid) == before
        assert not provider.calls
        provider.on_stop = lambda: reopened.pause(wid)
        player = APIPlayer(reopened)
        await player.start(wid)
        await asyncio.wait_for(player.tasks[wid], timeout=15)
        assert reopened.status(wid)["turn"] == 6
        assert reopened.status(wid)["pause_reason"] == "manual"
        assert len(provider.calls) == 2 and len(reopened_store.list_worlds()) == 1
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["manual", "budget_exceeded", "unknown_cost", "unknown_pricing"])
def test_disabled_approval_does_not_clear_other_stops(tmp_path, reason):
    async def run():
        conversation, store, provider = make_conversation(tmp_path)
        wid = (await conversation.start("离线鱼", mode="api"))["world_id"]
        store.set_player_profile(wid, "用于暂停测试的合成核心。", "", {})
        store.update_world(wid, {"status": "paused", "pause_reason": reason})
        result = await conversation.approve(wid)
        assert result["status"] == "paused" and result["pause_reason"] == reason
        player = APIPlayer(conversation)
        await player.start(wid)
        await asyncio.wait_for(player.tasks[wid], timeout=5)
        assert conversation.status(wid)["pause_reason"] == reason
        assert not provider.calls
    asyncio.run(run())


def test_reenabling_gate_after_disabled_play_requires_explicit_approval(tmp_path):
    async def run():
        disabled, store, _ = make_conversation(tmp_path)
        wid = (await disabled.start("离线鱼"))["world_id"]
        for i in range(6):
            await disabled.action(wid, f"无确认行动{i}", f"off-{i}")
        provider = MockProvider()
        enabled = Conversation(Storage(store.path, confirmation_every=5), provider)
        with pytest.raises(WorldError) as exc:
            await enabled.action(wid, "重新启用后不能绕过", "enable-blocked")
        assert exc.value.code == "confirmation_required" and not provider.calls
        assert enabled.status(wid)["pause_reason"] == "confirmation_required"
        await enabled.approve(wid)
        result = await enabled.action(wid, "人类批准后继续", "enable-approved")
        assert result["turn"] == 7 and result["next_confirmation_at"] == 11
    asyncio.run(run())
