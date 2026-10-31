"""SQLite is the authoritative journal; no network operation belongs in this module."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorldError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


class Storage:
    def __init__(self, path: str | Path, confirmation_every: int = 5):
        if isinstance(confirmation_every, bool) or confirmation_every not in (0, 5):
            raise ValueError("confirmation_every 必须为0（关闭）或5（每五轮确认）。")
        self.confirmation_every = confirmation_every
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS worlds(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
                    seq INTEGER NOT NULL, kind TEXT NOT NULL, turn INTEGER NOT NULL,
                    content TEXT NOT NULL, data TEXT NOT NULL, occurred_at TEXT,
                    observed_at TEXT NOT NULL, content_hash TEXT NOT NULL,
                    UNIQUE(world_id, seq));
                CREATE TABLE IF NOT EXISTS requests(
                    world_id TEXT NOT NULL REFERENCES worlds(id), request_id TEXT NOT NULL,
                    message TEXT NOT NULL, stage TEXT NOT NULL, expected_turn INTEGER NOT NULL,
                    context TEXT NOT NULL, response TEXT, result TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(world_id, request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_generation_per_world
                    ON requests(world_id) WHERE stage IN ('calling', 'received');
                CREATE TABLE IF NOT EXISTS usage_calls(
                    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
                    request_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
                    usage TEXT NOT NULL, model TEXT NOT NULL, cost_cny REAL,
                    pricing_note TEXT NOT NULL, occurred_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS player_profiles(
                    world_id TEXT PRIMARY KEY REFERENCES worlds(id), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS player_actions(
                    world_id TEXT NOT NULL REFERENCES worlds(id), request_id TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(world_id,request_id));
            """)

    def confirmation_due(self, world: dict) -> bool:
        return self.confirmation_every > 0 and world["turn"] >= world["approved_until"]

    def confirmation_fields(self, world: dict) -> dict:
        return {"confirmation_every": self.confirmation_every,
                "confirmation_enabled": self.confirmation_every > 0,
                "next_confirmation_at": world["approved_until"] if self.confirmation_every else None}

    @contextmanager
    def connect(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _world(self, db, world_id):
        row = db.execute("SELECT data FROM worlds WHERE id=?", (world_id,)).fetchone()
        if not row:
            raise WorldError("world_not_found", "找不到这个世界。请核对世界 ID。", 404)
        return json.loads(row["data"])

    def get_world(self, world_id: str) -> dict:
        with self.connect() as db:
            return self._world(db, world_id)

    def list_worlds(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM worlds ORDER BY rowid DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def _event(self, db, world: dict, kind: str, content: str = "", data: dict | None = None):
        timestamp = now()
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE world_id=?",
                         (world["world_id"],)).fetchone()[0]
        payload = {"kind": kind, "turn": world["turn"], "content": content, "data": data or {}}
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (
            str(uuid.uuid4()), world["world_id"], seq, kind, world["turn"], content,
            encode(data or {}), timestamp, timestamp,
            hashlib.sha256(encode(payload).encode()).hexdigest()))

    def create_world(self, world: dict):
        with self.connect(True) as db:
            db.execute("INSERT INTO worlds VALUES (?,?)", (world["world_id"], encode(world)))
            self._event(db, world, "created", world["setting"], {
                "name": world["name"], "preset": world["preset"], "mode": world["mode"],
                "player_name": world["player_name"], "scene": world["scene"],
                "simulation": True})

    def update_world(self, world_id: str, changes: dict, event: str | None = None,
                     content: str = "") -> dict:
        with self.connect(True) as db:
            world = self._world(db, world_id)
            world.update(changes)
            world["updated_at"] = now()
            db.execute("UPDATE worlds SET data=? WHERE id=?", (encode(world), world_id))
            if event:
                self._event(db, world, event, content, changes)
            return world

    def events(self, world_id: str, after: int = 0, limit: int = 100,
               include_private: bool = False) -> list[dict]:
        self.get_world(world_id)
        if after < 0 or not 1 <= limit <= 1000:
            raise WorldError("invalid_page", "after 必须非负；limit 必须在 1 至 1000 之间。", 400)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE world_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (world_id, after, limit)).fetchall()
        result = []
        for row in rows:
            item = {**dict(row), "data": json.loads(row["data"])}
            if not include_private and "private_fixed_facts" in item["data"]:
                item["data"].pop("private_fixed_facts")
                item["redacted"] = True
                item["source_content_hash"] = item["content_hash"]
                item["content_hash"] = hashlib.sha256(encode({key: item[key] for key in
                    ("kind", "turn", "content", "data")}).encode()).hexdigest()
            result.append(item)
        return result

    def recent_messages(self, world_id: str, turns: int = 6) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM requests WHERE world_id=? AND stage='completed' "
                              "ORDER BY expected_turn DESC LIMIT ?", (world_id, turns)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_request(self, world_id: str, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE world_id=? AND request_id=?",
                             (world_id, request_id)).fetchone()
        return dict(row) if row else None

    def pending_request(self, world_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT request_id,stage,expected_turn,created_at,updated_at "
                             "FROM requests WHERE world_id=? AND stage IN ('calling','received')",
                             (world_id,)).fetchone()
        return dict(row) if row else None

    def claim_request(self, world_id: str, request_id: str, message: str, context: str,
                      expected_turn: int):
        with self.connect(True) as db:
            world = self._world(db, world_id)
            if world["status"] != "active":
                raise WorldError("world_paused", "世界已暂停；请先在网页检查暂停原因。")
            if world["turn"] != expected_turn:
                raise WorldError("world_changed", "世界已发生变化，请读取最新状态后重试。")
            if self.confirmation_due(world):
                raise WorldError("confirmation_required", "每五轮需要人类在网页批准继续。")
            stamp = now()
            try:
                db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    world_id, request_id, message, "calling", expected_turn, context,
                    None, None, None, stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise WorldError("request_in_progress", "另一个行动正在生成；不会并行推进这个世界。") from exc

    def record_response(self, world_id: str, request_id: str, content: str):
        with self.connect(True) as db:
            db.execute("UPDATE requests SET stage='received',response=?,updated_at=? "
                       "WHERE world_id=? AND request_id=? AND stage='calling'",
                       (content, now(), world_id, request_id))

    def fail_request(self, world_id: str, request_id: str, error: str):
        with self.connect(True) as db:
            db.execute("UPDATE requests SET stage='failed',error=?,updated_at=? "
                       "WHERE world_id=? AND request_id=? AND stage!='completed'",
                       (error, now(), world_id, request_id))

    def abandon_request(self, world_id: str, request_id: str):
        with self.connect(True) as db:
            world = self._world(db, world_id)
            row = db.execute("SELECT stage FROM requests WHERE world_id=? AND request_id=?",
                             (world_id, request_id)).fetchone()
            if not row or row["stage"] not in ("calling", "received"):
                raise WorldError("no_pending_request", "这个请求不处于待恢复状态。")
            db.execute("UPDATE requests SET stage='abandoned',error='human_recovery',updated_at=? "
                       "WHERE world_id=? AND request_id=?", (now(), world_id, request_id))
            self._event(db, world, "request_abandoned", "人类确认放弃未完成请求；可能已产生费用。",
                        {"request_id": request_id})

    def finish_turn(self, world_id: str, request_id: str, update: dict) -> dict:
        with self.connect(True) as db:
            world = self._world(db, world_id)
            request = db.execute("SELECT * FROM requests WHERE world_id=? AND request_id=?",
                                 (world_id, request_id)).fetchone()
            if not request or request["stage"] != "received":
                raise WorldError("request_changed", "请求状态已改变，拒绝重复提交。")
            if world["turn"] != request["expected_turn"]:
                raise WorldError("turn_conflict", "轮次发生冲突；已保留原始响应供人工检查。")
            facts = {**world["facts"], **update["facts"]}
            state = {**world["state"], **update["state"]}
            fixed = dict(world.get("fixed_facts", {}))
            for key, value in update.get("fixed_facts", {}).items():
                if key in fixed and fixed[key] != value:
                    raise WorldError("fixed_fact_conflict", "模型试图改写已经锁定的真相；本轮未推进。")
                fixed[key] = value
            world.update({"turn": world["turn"] + 1, "scene": update["scene"],
                          "summary": update["summary"], "facts": facts, "state": state,
                          "fixed_facts": fixed, "updated_at": now(), "last_reply": update["reply"]})
            if self.confirmation_due(world) and world["status"] != "archived":
                world.update({"status": "paused", "pause_reason": "confirmation_required"})
            self._event(db, world, "player", request["message"], {"request_id": request_id})
            self._event(db, world, "world", update["reply"], {
                "request_id": request_id, "scene": update["scene"], "facts": update["facts"],
                "state": update["state"], "summary": update["summary"],
                "private_fixed_facts": update.get("fixed_facts", {})})
            result = {"world_id": world_id, "request_id": request_id, "turn": world["turn"],
                      "reply": update["reply"], "scene": world["scene"], "status": world["status"],
                      "pause_reason": world.get("pause_reason"), "summary": world["summary"],
                      "facts": dict(list(facts.items())[-12:]), "state": state,
                      **self.confirmation_fields(world),
                      "free_action": "你可以自由表达行动，不必选择固定选项。"}
            db.execute("UPDATE worlds SET data=? WHERE id=?", (encode(world), world_id))
            db.execute("UPDATE requests SET stage='completed',result=?,updated_at=? "
                       "WHERE world_id=? AND request_id=?", (encode(result), now(), world_id, request_id))
            return result

    def begin_usage(self, world_id: str, request_id: str, role: str) -> str:
        call_id = str(uuid.uuid4())
        with self.connect(True) as db:
            db.execute("INSERT INTO usage_calls VALUES (?,?,?,?,?,?,?,?,?,?)", (
                call_id, world_id, request_id, role, "calling", "{}", "", None,
                "请求已开始；用量尚未知，不能按零费用处理。", now()))
        return call_id

    def finish_usage(self, call_id: str, result=None, status: str = "completed"):
        def value(key, default):
            if result is None:
                return default
            return result.get(key, default) if isinstance(result, dict) else getattr(result, key, default)
        not_sent = result is not None and getattr(result, "attempted", True) is False
        with self.connect(True) as db:
            db.execute("UPDATE usage_calls SET status=?,usage=?,model=?,cost_cny=?,pricing_note=? WHERE id=?", (
                "not_sent" if not_sent else status, encode(value("usage", {})), value("model", ""),
                0.0 if not_sent else value("cost_cny", None),
                value("pricing_note", "远端请求失败或中断，费用未知。"), call_id))

    def reconcile_usage(self, world_id: str, call_id: str, cost_cny: float):
        with self.connect(True) as db:
            world = self._world(db, world_id)
            row = db.execute("SELECT * FROM usage_calls WHERE world_id=? AND id=?",
                             (world_id, call_id)).fetchone()
            if not row:
                raise WorldError("usage_not_found", "找不到这次模型调用。", 404)
            if row["cost_cny"] is not None:
                raise WorldError("usage_already_known", "本次调用已有费用，不能重复核对覆盖。")
            db.execute("UPDATE usage_calls SET cost_cny=?,status='human_estimate',pricing_note=? WHERE id=?", (
                cost_cny, "人类手动核对输入的费用估算；不是程序核验的官方已入账金额。", call_id))
            self._event(db, world, "usage_reconciled", "人类手动核对未知费用。",
                        {"call_id": call_id, "cost_cny": cost_cny, "source": "human_estimate"})

    def add_usage(self, world_id: str, request_id: str, role: str, result, status: str = "completed"):
        call_id = self.begin_usage(world_id, request_id, role)
        self.finish_usage(call_id, result, status)
        return call_id

    def usage(self, world_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM usage_calls WHERE world_id=? ORDER BY occurred_at,id",
                              (world_id,)).fetchall()
        return [{**dict(row), "usage": json.loads(row["usage"])} for row in rows]

    def set_player_profile(self, world_id: str, core: str, memory: str = "", config: dict | None = None):
        self.get_world(world_id)
        with self.connect(True) as db:
            db.execute("INSERT INTO player_profiles VALUES (?,?) ON CONFLICT(world_id) "
                       "DO UPDATE SET data=excluded.data", (world_id, encode({
                           "core": core, "memory": memory, "config": config or {}})))

    def get_player_profile(self, world_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM player_profiles WHERE world_id=?", (world_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_player_action(self, world_id: str, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM player_actions WHERE world_id=? AND request_id=?",
                             (world_id, request_id)).fetchone()
        return dict(row) if row else None

    def begin_player_action(self, world_id: str, request_id: str) -> dict:
        with self.connect(True) as db:
            world = self._world(db, world_id)
            turn_match = re.match(r"^api-(\d+)-", request_id)
            if turn_match:
                turn = int(turn_match.group(1))
                if world["turn"] != turn - 1 or world["status"] != "active" or self.confirmation_due(world):
                    raise WorldError("player_turn_changed", "世界已暂停或轮次已改变，不能开始玩家模型调用。")
                existing = db.execute("SELECT * FROM player_actions WHERE world_id=? AND request_id LIKE ? "
                                      "AND status!='abandoned' ORDER BY created_at LIMIT 1",
                                      (world_id, f"api-{turn}-%")).fetchone()
                if existing:
                    return {**dict(existing), "new": False}
            row = db.execute("SELECT * FROM player_actions WHERE world_id=? AND request_id=?",
                             (world_id, request_id)).fetchone()
            if row:
                return {**dict(row), "new": False}
            stamp = now()
            db.execute("INSERT INTO player_actions VALUES (?,?,?,?,?,?)",
                       (world_id, request_id, "calling", None, stamp, stamp))
            return {"world_id": world_id, "request_id": request_id, "status": "calling", "new": True}

    def save_player_action(self, world_id: str, request_id: str, message: str):
        with self.connect(True) as db:
            stamp = now()
            db.execute("INSERT INTO player_actions VALUES (?,?,?,?,?,?) ON CONFLICT(world_id,request_id) "
                       "DO UPDATE SET status='ready',message=excluded.message,updated_at=excluded.updated_at",
                       (world_id, request_id, "ready", message, stamp, stamp))

    def fail_player_action(self, world_id: str, request_id: str):
        with self.connect(True) as db:
            db.execute("UPDATE player_actions SET status='failed',updated_at=? WHERE world_id=? AND request_id=?",
                       (now(), world_id, request_id))
