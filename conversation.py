"""All entry points share this small, durable conversation service."""
from __future__ import annotations

import asyncio
import json
import math
import uuid

from prompts import ROOT, PLAYER_GUIDANCE, action_content, parse_world_reply, presets, world_messages
from storage import Storage, WorldError, now


class Conversation:
    def __init__(self, store: Storage, provider):
        self.store, self.provider = store, provider
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, world_id: str) -> asyncio.Lock:
        return self._locks.setdefault(world_id, asyncio.Lock())

    async def start(self, player_name: str, idea: str = "", preset: str = "ocean",
                    preferences: str = "", welcome: bool = True, mode: str = "mcp",
                    budget_cny: float = 1.0) -> dict:
        if not isinstance(player_name, str) or not player_name.strip() or len(player_name) > 80:
            raise WorldError("invalid_player", "请输入 1 至 80 字的玩家名称。", 400)
        if mode not in ("mcp", "api") or len(idea) > 4000 or len(preferences) > 2000:
            raise WorldError("invalid_world", "模式或世界设定长度不正确。", 400)
        if not isinstance(budget_cny, (int, float)) or not math.isfinite(budget_cny) or budget_cny <= 0:
            raise WorldError("invalid_budget", "预算必须是大于零的有限数值。", 400)
        templates = {item["id"]: item for item in presets()}
        if preset not in templates:
            raise WorldError("unknown_preset", "找不到该主题。你可以选择完全自定义活动。", 400)
        template = templates[preset]
        setting = template["setting"] + "\n玩家：" + player_name.strip()
        if idea:
            setting += "\n玩家自由活动想法：" + idea
        if preferences:
            setting += "\n偏好：" + preferences
        stamp = now()
        world = {"world_id": str(uuid.uuid4()), "name": template["name"], "player_name": player_name.strip(),
                 "preset": preset, "mode": mode, "setting": setting, "turn": 0,
                 "scene": template["scene"], "last_reply": template["scene"], "facts": {}, "state": {},
                 "fixed_facts": {}, "summary": "尚未进行行动。", "status": "active", "pause_reason": None,
                 "approved_until": self.store.confirmation_every or 5, "budget_cny": float(budget_cny),
                 "welcome_enabled": bool(welcome), "created_at": stamp, "updated_at": stamp}
        self.store.create_world(world)
        result = self.status(world["world_id"])
        welcome_path = ROOT / "content" / "welcome_zh.md"
        result["welcome"] = welcome_path.read_text(encoding="utf-8") if welcome and welcome_path.exists() else ""
        result["control"] = {"player_guidance": PLAYER_GUIDANCE, "scope": "game_only", "persist": False}
        return result

    def status(self, world_id: str) -> dict:
        world = self.store.get_world(world_id)
        calls = self.store.usage(world_id)
        known_cost = sum(item["cost_cny"] or 0 for item in calls)
        result = {key: value for key, value in world.items() if key not in ("fixed_facts",)}
        result.update({"cost_cny": known_cost,
                       "unknown_cost_calls": sum(item["cost_cny"] is None for item in calls),
                       "world_cost_cny": sum(item["cost_cny"] or 0 for item in calls if item["role"] == "world"),
                       "player_cost_cny": sum(item["cost_cny"] or 0 for item in calls if item["role"] == "player"),
                       **self.store.confirmation_fields(world),
                       "pending_request": self.store.pending_request(world_id),
                       "recent_events": self._recent_events(world_id),
                       "cost_note": "本地按返回用量估算；不是服务商已入账扣款。MCP不包含外部Agent费用。",
                       "free_action": "你可以自由表达行动，不必选择固定选项。"})
        return result

    def _recent_events(self, world_id: str) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("SELECT seq FROM events WHERE world_id=? ORDER BY seq DESC LIMIT 12", (world_id,)).fetchall()
        return self.store.events(world_id, after=max(0, rows[-1][0] - 1), limit=12) if rows else []

    def check_budget(self, world_id: str, messages: list[dict], role: str = "world", config: dict | None = None):
        status = self.status(world_id)
        if status["status"] != "active":
            raise WorldError("world_paused", "世界已暂停；请先在网页检查状态。")
        if self.store.confirmation_due(status):
            self.store.update_world(world_id, {"status": "paused", "pause_reason": "confirmation_required"})
            raise WorldError("confirmation_required", "需要人类在网页批准下一组五轮。")
        estimate = self.provider.estimated_cost(messages, role=role, config=config)
        reason = None
        if status["unknown_cost_calls"]:
            reason = "unknown_cost"
        elif estimate is None:
            reason = "unknown_pricing"
        elif status["cost_cny"] + estimate > status["budget_cny"]:
            reason = "budget_exceeded"
        if reason:
            self.store.update_world(world_id, {"status": "paused", "pause_reason": reason}, "budget_pause")
            raise WorldError(reason, "费用未知或剩余预算不足，已暂停。请在网页核对用量和预算后处理。")

    async def action(self, world_id: str, message: str, request_id: str | None = None) -> dict:
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise WorldError("invalid_message", "行动应为 1 至 4000 字的自然语言。", 400)
        request_id = request_id or str(uuid.uuid4())
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
            raise WorldError("invalid_request_id", "请求 ID 长度必须在 1 至 160 之间。", 400)
        async with self._lock(world_id):
            world = self.store.get_world(world_id)
            prior = self.store.get_request(world_id, request_id)
            if prior:
                if prior["message"] != message:
                    raise WorldError("request_id_conflict", "相同请求 ID 不能用于不同消息。")
                if prior["stage"] == "completed":
                    return {**json.loads(prior["result"]), **self.store.confirmation_fields(world)}
                if prior["stage"] == "received":
                    return self._finalize(world_id, request_id, prior["response"])
                if prior["stage"] == "calling":
                    raise WorldError("request_uncertain", "请求已发出但结果尚未确认；不会自动重复付费调用。")
                raise WorldError("request_failed", "该请求失败或已放弃；先检查费用，需要重试时使用新请求 ID。")
            if self.store.pending_request(world_id):
                raise WorldError("request_in_progress", "这个世界存在未完成请求；先等候或人工处理，不会并行调用。")
            content = action_content(world, message)
            recent = self.store.recent_messages(world_id, 6 + world["turn"] % 6)
            messages = world_messages(world, recent, content)
            self.check_budget(world_id, messages)
            self.store.claim_request(world_id, request_id, message, content, world["turn"])
            call_id = self.store.begin_usage(world_id, request_id, "world")
            try:
                result = await self.provider.complete(messages, role="world")
                self.store.finish_usage(call_id, result)
                self.store.record_response(world_id, request_id, result.content)
            except asyncio.CancelledError:
                # Leave calling journal intact: cancellation cannot prove the remote request was unpaid.
                self.store.finish_usage(call_id, status="interrupted")
                raise
            except Exception as exc:
                self.store.finish_usage(call_id, exc, status="failed")
                self.store.fail_request(world_id, request_id, "provider_failed")
                raise WorldError("provider_failed", "模型请求失败；本轮未推进。实际用量未知时会停止后续付费调用。", 502) from exc
            return self._finalize(world_id, request_id, result.content)

    def _finalize(self, world_id: str, request_id: str, content: str):
        try:
            update = parse_world_reply(content)
            return self.store.finish_turn(world_id, request_id, update)
        except WorldError as exc:
            self.store.fail_request(world_id, request_id, exc.code)
            raise

    async def pause(self, world_id: str) -> dict:
        world = self.store.get_world(world_id)
        if world["status"] == "archived":
            return self.status(world_id)
        reason = (world.get("pause_reason") if world["status"] == "paused" and world.get("pause_reason")
                  else "confirmation_required" if self.store.confirmation_due(world) else "manual")
        self.store.update_world(world_id, {"status": "paused", "pause_reason": reason}, "paused")
        return self.status(world_id)

    async def resume(self, world_id: str) -> dict:
        world = self.store.get_world(world_id)
        if self.store.confirmation_due(world):
            self.store.update_world(world_id, {"status": "paused", "pause_reason": "confirmation_required"})
        elif world.get("pause_reason") not in ("budget_exceeded", "unknown_pricing", "unknown_cost"):
            self.store.update_world(world_id, {"status": "active", "pause_reason": None}, "resumed")
        result = self.status(world_id)
        result["control"] = {"player_guidance": PLAYER_GUIDANCE, "scope": "game_only", "persist": False}
        return result

    async def approve(self, world_id: str) -> dict:
        world = self.store.get_world(world_id)
        if not self.store.confirmation_every:
            return self.status(world_id)
        if self.store.pending_request(world_id):
            raise WorldError("request_in_progress", "请等待当前请求完成，或先人工处理未完成请求。")
        if world["turn"] < world["approved_until"]:
            # Early clicks cannot preapprove an unlimited number of future rounds.
            return self.status(world_id)
        self.store.update_world(world_id, {"approved_until": world["turn"] + self.store.confirmation_every,
                                         "status": "active", "pause_reason": None}, "approved")
        return self.status(world_id)

    async def end(self, world_id: str, archive: bool = False, reflection: str | None = None) -> dict:
        if reflection is not None and (not isinstance(reflection, str) or len(reflection) > 8000):
            raise WorldError("invalid_reflection", "感想应为最多8000字的自愿文本。", 400)
        self.store.update_world(world_id, {"status": "archived" if archive else "paused",
                                         "pause_reason": "archived" if archive else "manual"},
                                "archived" if archive else "paused")
        if reflection:
            self.store.update_world(world_id, {}, "reflection", reflection)
        return self.status(world_id)

    async def update_budget(self, world_id: str, budget_cny: float) -> dict:
        if not isinstance(budget_cny, (int, float)) or not math.isfinite(budget_cny) or budget_cny <= 0:
            raise WorldError("invalid_budget", "预算必须是大于零的有限数值。", 400)
        world = self.store.get_world(world_id)
        changes = {"budget_cny": float(budget_cny)}
        if world.get("pause_reason") == "budget_exceeded" and not self.store.confirmation_due(world):
            changes.update({"status": "active", "pause_reason": None})
        self.store.update_world(world_id, changes, "budget_updated")
        return self.status(world_id)

    async def recover(self, world_id: str, request_id: str) -> dict:
        if self._lock(world_id).locked():
            raise WorldError("request_in_progress", "本进程仍在执行该请求，暂不能放弃。")
        self.store.abandon_request(world_id, request_id)
        return self.status(world_id)

    async def reconcile_cost(self, world_id: str, call_id: str, cost_cny: float) -> dict:
        if not isinstance(cost_cny, (int, float)) or not math.isfinite(cost_cny) or cost_cny < 0:
            raise WorldError("invalid_cost", "核对金额必须是非负有限数值。", 400)
        if self._lock(world_id).locked():
            raise WorldError("request_in_progress", "请求仍在进行，暂不能核对其费用。")
        self.store.reconcile_usage(world_id, call_id, float(cost_cny))
        status = self.status(world_id)
        if status["unknown_cost_calls"] == 0 and status["pause_reason"] == "unknown_cost":
            self.store.update_world(world_id, {"status": "paused", "pause_reason": "manual"})
        return self.status(world_id)
