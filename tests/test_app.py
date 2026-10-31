"""Real HTTP and MCP clients exercise the same running service and SQLite journal."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app import create_app, backend_lease
from llm import MockProvider


@pytest.fixture
def backend(tmp_path):
    provider = MockProvider()
    application = create_app(tmp_path / "worlds.db", provider, confirmation_every=5)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                          log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}", application, provider
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive()


async def test_http_mcp_20_rounds_one_authoritative_world(backend):
    url, application, provider = backend
    async with httpx.AsyncClient(base_url=url) as web, Client(url + "/mcp") as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {
            "world_start", "world_action", "world_status", "world_resume", "world_end", "world_export"}
        created = await client.call_tool("world_start", {"player_name": "MCP验收鱼", "preset": "home"})
        assert not created.is_error
        world = created.structured_content
        assert "欢迎" in world["welcome"]
        world_id = world["world_id"]
        token = (await web.get("/api/bootstrap")).json()["csrf_token"]
        headers = {"X-FishWorld-CSRF": token}
        for turn in range(1, 21):
            result = await client.call_tool("world_action", {"world_id": world_id,
                                            "message": f"自由探索第{turn}轮", "request_id": f"mcp-{turn}"})
            assert not result.is_error
            assert result.structured_content["turn"] == turn
            # A repeated MCP delivery never advances the DB or triggers another world call.
            duplicate = await client.call_tool("world_action", {"world_id": world_id,
                                               "message": f"自由探索第{turn}轮", "request_id": f"mcp-{turn}"})
            assert duplicate.structured_content == result.structured_content
            if turn % 5 == 0:
                rejected = await client.call_tool("world_action", {"world_id": world_id,
                                                  "message": "未批准先试试", "request_id": f"blocked-{turn}"})
                assert rejected.is_error
                resumed = await client.call_tool("world_resume", {"world_id": world_id})
                assert resumed.structured_content["pause_reason"] == "confirmation_required"
                if turn < 20:
                    approved = await web.post(f"/api/worlds/{world_id}/approve", headers=headers, json={})
                    assert approved.status_code == 200
        assert len(provider.calls) == 20
        page = await web.get(f"/api/worlds/{world_id}")
        assert page.json()["turn"] == 20
        cursor, events = 0, []
        while True:
            exported = await client.call_tool("world_export", {"world_id": world_id, "after": cursor, "limit": 7})
            chunk = exported.structured_content
            events.extend(chunk["events"])
            cursor = chunk["next_cursor"]
            if not chunk["has_more"]:
                break
        assert len([e for e in events if e["kind"] in ("player", "world")]) == 40
        assert len({e["id"] for e in events}) == len(events)
        json_export = (await web.get(f"/api/worlds/{world_id}/export")).json()
        assert json_export["events"] == events
        markdown = await web.get(f"/api/worlds/{world_id}/export?format=markdown")
        assert "自由探索第20轮" in markdown.text
        assert "文字模拟" in markdown.text


async def test_real_stdio_bridge_uses_same_backend(backend):
    url, application, provider = backend
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "stdio_bridge.py")],
                                   cwd=root, env={**os.environ, "FISHWORLD_URL": url + "/mcp"})
    async with Client(params) as bridge:
        tools = await bridge.list_tools()
        assert len(tools.tools) == 6
        result = await bridge.call_tool("world_start", {"player_name": "桥接验收鱼"})
        assert not result.is_error
        world_id = result.structured_content["world_id"]
        action = await bridge.call_tool("world_action", {"world_id": world_id, "message": "游到珊瑚礁", "request_id": "stdio-one"})
        assert not action.is_error
        async with httpx.AsyncClient(base_url=url) as web:
            assert (await web.get(f"/api/worlds/{world_id}")).json()["turn"] == 1
        assert len(provider.calls) == 1


def test_browser_security_and_privacy(tmp_path):
    app = create_app(tmp_path / "browser.db", MockProvider())
    with TestClient(app) as web:
        token = web.get("/api/bootstrap").json()["csrf_token"]
        assert web.post("/api/worlds", json={"player_name": "鱼"}).status_code == 403
        assert web.get("/", headers={"Host": "evil.example"}).status_code == 421
        assert web.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
        headers = {"X-FishWorld-CSRF": token}
        created = web.post("/api/worlds", json={"player_name": "临时鱼", "mode": "api",
                            "core": "PRIVATE_SYNTHETIC_CORE", "memory": "PRIVATE_SYNTHETIC_MEMORY"}, headers=headers)
        assert created.status_code == 200
        world_id = created.json()["world_id"]
        for path in (f"/api/worlds/{world_id}", "/api/worlds", f"/api/worlds/{world_id}/export"):
            response = web.get(path)
            assert "PRIVATE_SYNTHETIC_CORE" not in response.text
            assert "PRIVATE_SYNTHETIC_MEMORY" not in response.text
        assert web.post(f"/api/worlds/{world_id}/reflection", json={"text": "玩家自愿感想"}, headers=headers).status_code == 200
        assert "玩家自愿感想" in web.get(f"/api/worlds/{world_id}/export?format=markdown").text
        assert web.get("/static/../../.env").status_code != 200
        assert web.get(f"/api/worlds/{world_id}/events?limit=0").status_code == 400
        assert web.post("/api/worlds", json={"player_name": "鱼", "budget_cny": -1}, headers=headers).status_code == 422
        assert web.get("/").headers["Cache-Control"] == "no-store"


def test_single_backend_lease_and_crash_safe_release(tmp_path):
    database = tmp_path / "one.db"
    with backend_lease(database):
        with pytest.raises(RuntimeError, match="已有"):
            with backend_lease(database):
                pytest.fail("must refuse duplicate backend")
    with backend_lease(database):
        pass
