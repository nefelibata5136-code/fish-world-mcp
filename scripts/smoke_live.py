"""Optional real-API acceptance: five synthetic rounds, isolated DB, <= 0.15 CNY budget.

Run only after configuring DEEPSEEK_API_KEY and accepting the provider's charges.
No original life, Core, private memory, or external Agent is involved.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conversation import Conversation
from llm import Provider
from storage import Storage, WorldError


async def main():
    output = ROOT / "local"
    output.mkdir(exist_ok=True)
    report = {"kind": "real_api_acceptance", "started_at": datetime.now(timezone.utc).isoformat(),
              "synthetic_only": True, "real_life_awakened": False,
              "provider": "DeepSeek Chat Completions", "model": "deepseek-flash",
              "max_world_calls": 5, "budget_cny": .15, "paid_api_player_calls": 0}
    if not os.getenv("DEEPSEEK_API_KEY"):
        report.update({"ok": False, "error_code": "missing_key", "calls": 0})
    else:
        store = Storage(output / ("live-smoke-" + uuid.uuid4().hex + ".sqlite3"))
        provider = Provider(model="deepseek-flash")
        service = Conversation(store, provider)
        world = await service.start("验收小鱼", preset="ocean", welcome=False, budget_cny=.15,
                                    idea="这是隔离的合成验收世界。玩家自由认识名叫阿圆的章鱼，建立住所并回访。")
        world_id = world["world_id"]
        actions = [
            "我在浅海珊瑚礁认识一只名叫阿圆、喜欢收集贝壳的章鱼。我向她打招呼，请她介绍自己。",
            "我和阿圆一起在珊瑚礁找到了她丢失的紫色贝壳，并把贝壳交给她。请记住这次我们一起完成的事。",
            "我在附近建一间可长期返回的海边小屋，把一座蓝色书架放在房间东墙边。我现在站在屋内看看。",
            "我暂时离开小屋，到珊瑚礁附近的小镇散步；房间的布置保持原样。",
            "现在我想再次寻找阿圆：她是谁，我们之前共同经历了什么？随后我回到原来的小屋，看看书架是什么颜色、放在哪边。请依据此前记录回答。",
        ]
        responses = []
        try:
            for index, message in enumerate(actions, 1):
                result = await service.action(world_id, message, request_id=f"live-acceptance-{index}")
                responses.append(result.get("reply") or result.get("last_reply") or "")
            state = service.status(world_id)
            last = responses[-1]
            persistent = json.dumps({"facts": state["facts"], "state": state["state"], "summary": state["summary"]}, ensure_ascii=False)
            report["checks"] = {
                "five_successful_rounds": state["turn"] == 5,
                "paused_for_human": state["status"] == "paused" and state["pause_reason"] == "confirmation_required",
                "ayuan_retained": "阿圆" in persistent and "阿圆" in last,
                "shell_experience_retained": "贝壳" in persistent and "贝壳" in last,
                "blue_bookshelf_retained": "蓝色" in persistent and "书架" in persistent and "蓝色" in last and "书架" in last,
                "east_wall_retained": "东" in persistent and "东" in last,
            }
            report["ok"] = all(report["checks"].values())
        except Exception as error:
            report.update({"ok": False, "error_code": error.code if isinstance(error, WorldError) else type(error).__name__})
        usage = store.usage(world_id)
        report.update({"world_id": world_id, "completed_rounds": service.status(world_id)["turn"],
                       "calls": len(usage), "cost_cny_estimate": sum(item["cost_cny"] or 0 for item in usage),
                       "unknown_cost_calls": sum(item["cost_cny"] is None for item in usage),
                       "response_sha256": [hashlib.sha256(text.encode()).hexdigest() for text in responses],
                       "usage": [{key: value for key, value in item.items() if key in {"role", "status", "usage", "model", "cost_cny", "pricing_note"}} for item in usage]})
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (output / "live-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
