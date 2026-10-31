"""Complete, paged exports and a minimal adapter for the existing local evidence ledger."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
import sqlite3

PUBLIC_WORLD_FIELDS = ("world_id", "name", "player_name", "mode", "preset", "setting", "turn",
                       "scene", "summary", "facts", "state", "status", "pause_reason",
                       "created_at", "updated_at", "budget_cny")


def read_only_store(path):
    from storage import Storage

    class ReadOnlyStorage(Storage):
        def __init__(self, database):
            self.path = Path(database).resolve()

        @contextmanager
        def connect(self, write=False):
            if write:
                raise ValueError("导出器只能读取源数据库。")
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            try:
                yield db
            finally:
                db.close()

    return ReadOnlyStorage(path)


def public_world(store, world_id):
    world = store.get_world(world_id)
    return {key: world[key] for key in PUBLIC_WORLD_FIELDS if key in world}


def export_page(store, world_id, after=0, limit=50):
    if isinstance(limit, bool) or not 1 <= limit <= 1000 or after < 0:
        from storage import WorldError
        raise WorldError("invalid_page", "分页大小必须为1至1000，游标不能为负数。", 400)
    events = store.events(world_id, after, limit)
    cursor = events[-1]["seq"] if events else after
    return {"schema_version": 1, "simulation": True, "world": public_world(store, world_id),
            "events": events, "next_cursor": cursor,
            "has_more": bool(store.events(world_id, cursor, 1)),
            "summary_note": "摘要由世界模型生成，原始事件是核查依据；玩家感想单独保存。",
            "privacy_note": "不包含API临时玩家核心、私人记忆、密钥及尚未发现的谜案真相。"}


def iter_events(store, world_id):
    cursor = 0
    while True:
        page = export_page(store, world_id, cursor, 1000)
        yield from page["events"]
        if not page["has_more"]:
            return
        cursor = page["next_cursor"]


def export_json(store, world_id):
    return {"schema_version": 1, "simulation": True,
            "world": public_world(store, world_id), "events": list(iter_events(store, world_id)),
            "usage": store.usage(world_id),
            "privacy_note": "玩家核心、记忆摘要、密钥及隐藏谜底不在此导出。",
            "cost_note": "按返回用量计算的估算；不是服务商实际入账金额。"}


def iter_markdown(store, world_id):
    world = public_world(store, world_id)
    yield f"# {world['name']}\n\n> 本文记录文字模拟经历，不代表现实中发生。\n\n"
    yield (f"玩家：{world['player_name']}\n\n模式：{world['mode']}\n\n世界ID：{world_id}\n\n"
           f"创建时间：{world['created_at']}\n\n状态：{world['status']}；累计{world['turn']}轮\n\n"
           f"## 世界背景\n\n{world['setting']}\n\n## 原始事件\n\n")
    labels = {"player": "玩家行动", "world": "世界反馈", "reflection": "玩家自愿感想"}
    for event in iter_events(store, world_id):
        yield (f"### 事件 {event['seq']} · 第{event['turn']}轮 · {labels.get(event['kind'], event['kind'])}\n\n"
               f"时间：{event['occurred_at'] or '未知'}；ID：{event['id']}\n\n{event['content']}\n\n")
    yield f"## 客观事件摘要（模型生成，需依据原文核对）\n\n{world['summary']}\n\n"
    yield "## 已确定的重要事实\n\n" + "\n".join(f"- {k}：{v}" for k, v in world["facts"].items()) + "\n"


def collect(store):
    """Explicitly registered source adapter, compatible with local-study-export Ledger.scan."""
    observed = datetime.now(timezone.utc).isoformat()
    for world in store.list_worlds():
        for event in iter_events(store, world["world_id"]):
            yield {"schema_version": 1, "source": "fishworld.events", "record_id": event["id"],
                   "message_id": event["id"], "conversation_id": world["world_id"],
                   "role": {"player": "agent_player", "world": "world_model"}.get(event["kind"], "application"),
                   "occurred_at": event["occurred_at"], "observed_at": observed, "extracted_at": observed,
                   "content": {"simulation": True, "kind": event["kind"], "turn": event["turn"],
                               "text": event["content"], "data": event["data"],
                               "source_content_hash": event["content_hash"]},
                   "original_location": {"database": str(store.path.resolve()), "table": "events",
                                         "world_id": world["world_id"], "event_id": event["id"], "seq": event["seq"]},
                   "evidence_type": "simulated_agent_activity", "review_status": "pending"}


def export_to_ledger(store, exporter_path, output):
    """Reuse the installed unified exporter's versioning and sensitive-content quarantine.

    Deliberately writes a separate local ledger; does not rescan unrelated sources or publish.
    """
    spec = importlib.util.spec_from_file_location("fishworld_unified_exporter", exporter_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    ledger = module.Ledger(output / "checkpoint.sqlite3")
    try:
        ledger.scan("fishworld.events", collect(store))
        rows = ledger.current_rows()
        versions = [json.loads(r[0]) for r in ledger.c.execute("SELECT record_json FROM versions ORDER BY rowid")]
        files, chunk, size = [], [], 0

        def write_chunk(rows, prefix, index):
            name = f"{prefix}.{index:04d}.jsonl"
            body = "".join(module.canonical(r) + "\n" for r in rows)
            module.atomic(output / name, body)
            files.append({"path": name, "records": len(rows), "sha256": hashlib.sha256(body.encode()).hexdigest()})

        for prefix, items in (("records", rows), ("versions", versions)):
            chunk, size, index = [], 0, 0
            for row in items:
                length = len(module.canonical(row).encode()) + 1
                if chunk and size + length > 1_000_000:
                    write_chunk(chunk, prefix, index)
                    index += 1
                    chunk, size = [], 0
                chunk.append(row)
                size += length
            if chunk:
                write_chunk(chunk, prefix, index)
        manifest = {"schema_version": 1, "source": "fishworld.events", "records": len(rows),
                    "new_version_observations": ledger.added, "withheld": len(ledger.withheld),
                    "files": files, "simulation": True, "uploaded": False, "dots_read_verified": False,
                    "observed_at": datetime.now(timezone.utc).isoformat()}
        module.atomic(output / "LATEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # The review index contains evidence pointers/hashes, never withheld text.
        module.atomic(output / "review_status.jsonl", "".join(module.canonical(r) + "\n" for r in ledger.withheld))
        return manifest
    finally:
        ledger.c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读导出FishWorld记录，不调用模型或上传。")
    parser.add_argument("--db", default="data/fishworld.db")
    parser.add_argument("--world")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--unified-exporter", help="已有local-study-export/export_local.py的绝对路径")
    args = parser.parse_args()
    if not Path(args.db).is_file():
        parser.error("数据库不存在；不会创建空来源冒充真实记录。")
    store = read_only_store(args.db)
    if args.unified_exporter:
        print(json.dumps(export_to_ledger(store, args.unified_exporter, args.output), ensure_ascii=False))
    else:
        if not args.world:
            parser.error("请提供 --world 世界ID。")
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as handle:
            if args.format == "json":
                json.dump(export_json(store, args.world), handle, ensure_ascii=False, indent=2)
            else:
                handle.writelines(iter_markdown(store, args.world))
        print(f"本地已导出：{dest.resolve()}；未上传，未验证dots读取。")
