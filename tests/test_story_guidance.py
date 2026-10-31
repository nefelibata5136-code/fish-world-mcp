"""Operational player guidance stays outside the authoritative simulated story."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from mcp import Client

from api_player import APIPlayer
from conversation import Conversation
from export_records import collect, export_json, export_page, iter_markdown
from llm import MockProvider
from mcp_server import create_mcp
from storage import Storage, encode


def guidance(result):
    value = result["control"]["player_guidance"]
    assert isinstance(value, str) and value.strip()
    return value


def as_text(value):
    return json.dumps(value, ensure_ascii=False)


def assert_story_has_no_control(value, player_guidance):
    text = value if isinstance(value, str) else as_text(value)
    assert "player_guidance" not in text
    assert player_guidance not in text


async def test_real_mcp_guidance_is_start_resume_metadata_not_story_or_extra_calls(tmp_path):
    store = Storage(tmp_path / "mcp-story.db", confirmation_every=0)
    provider = MockProvider()
    conversation = Conversation(store, provider)
    expected_properties = {
        "world_start": {"player_name", "idea", "preset", "preferences", "welcome", "budget_cny"},
        "world_action": {"world_id", "message", "request_id"},
        "world_status": {"world_id"},
        "world_resume": {"world_id"},
        "world_end": {"world_id", "archive", "reflection"},
        "world_export": {"world_id", "after", "limit"},
    }
    async with Client(create_mcp(conversation)) as client:
        tools = await client.list_tools()
        actual_properties = {
            tool.name: set(tool.model_dump(by_alias=True)["inputSchema"]["properties"])
            for tool in tools.tools
        }
        assert actual_properties == expected_properties
        start = await client.call_tool("world_start", {
            "player_name": "合成连贯性验收鱼", "preset": "home", "welcome": False,
        })
        assert not start.is_error
        created = start.structured_content
        player_guidance = guidance(created)
        world_id = created["world_id"]
        assert provider.calls == []  # Guidance does not add a model request.
        initial_event = store.events(world_id)[0]
        assert initial_event["content"] == created["scene"]
        assert initial_event["content"] != created["setting"]

        action = "先认识阿圆并约好下次再见，然后回小屋把蓝色书架摆在东墙边。"
        advanced = await client.call_tool("world_action", {
            "world_id": world_id, "message": action, "request_id": "compound-action",
        })
        assert not advanced.is_error
        assert "control" not in advanced.structured_content
        assert len(provider.calls) == 1
        sent = json.loads(provider.calls[0]["messages"][-1]["content"])
        assert sent["message"] == action  # No menu, splitting, or one-action truncation.
        assert_story_has_no_control(provider.calls[0]["messages"], player_guidance)

        await client.call_tool("world_end", {"world_id": world_id})
        resumed = await client.call_tool("world_resume", {"world_id": world_id})
        assert not resumed.is_error
        assert guidance(resumed.structured_content) == player_guidance
        assert resumed.structured_content["world_id"] == world_id
        assert resumed.structured_content["state"]["bookshelf"] == "蓝色"
        status = (await client.call_tool("world_status", {"world_id": world_id})).structured_content
        assert "control" not in status
        assert status["turn"] == 1
        assert len(provider.calls) == 1

        page = (await client.call_tool("world_export", {"world_id": world_id})).structured_content
        assert_story_has_no_control(page, player_guidance)

    for output in (store.get_world(world_id), store.events(world_id, include_private=True),
                   conversation.status(world_id), export_json(store, world_id),
                   "".join(iter_markdown(store, world_id)), list(collect(store))):
        assert_story_has_no_control(output, player_guidance)
    with store.connect() as db:
        raw_requests = [dict(row) for row in db.execute("SELECT * FROM requests")]
    assert_story_has_no_control(raw_requests, player_guidance)
    assert len(provider.calls) == 1


async def test_api_compound_action_and_restore_keep_core_state_and_guidance_separate(tmp_path):
    core = "PRIVATE_SYNTHETIC_CORE_CONTINUITY_83：由我自己决定想做什么。"
    memory = "PRIVATE_SYNTHETIC_MEMORY_CONTINUITY_47"
    action = "我先整理蓝色书架上的书，再去和阿圆聊昨天的贝壳，之后回来休息。"
    reply = {
        "reply": "书架上的书已整理好。阿圆记得昨天的贝壳，等你回家时，窗边的灯仍亮着。",
        "scene": "森林小屋东墙边的蓝色书架前",
        "summary": "玩家整理书架，与阿圆聊过共同找到的贝壳，然后回到小屋。",
        "facts": {"home_bookshelf": "蓝色书架在东墙边。", "npc_ayuan": "阿圆记得共同找到贝壳的经历。"},
        "state": {"home_room": {"east_wall": "BLUE_SHELF_STATE_MARKER"},
                  "tags": ["用户原有标签", "未完成：再访阿圆"]},
    }
    provider = MockProvider(responses=[action, reply])
    store = Storage(tmp_path / "api-story.db", confirmation_every=0)
    conversation = Conversation(store, provider)
    created = await conversation.start("合成临时玩家", preset="home", mode="api", welcome=False)
    player_guidance = guidance(created)
    world_id = created["world_id"]
    store.set_player_profile(world_id, core, memory, {"model": "synthetic-player"})
    original_profile = store.get_player_profile(world_id)
    player = APIPlayer(conversation)
    await player.step(world_id)
    assert [call["role"] for call in provider.calls] == ["player", "world"]
    player_prompt, world_prompt = provider.calls
    assert player_guidance in player_prompt["messages"][0]["content"]
    assert core in player_prompt["messages"][0]["content"]
    assert "只输出你本人想采取的一个自然语言行动" not in player_prompt["messages"][0]["content"]
    assert "自由决定一个行动" not in player_prompt["messages"][-1]["content"]
    assert json.loads(world_prompt["messages"][-1]["content"])["message"] == action
    assert_story_has_no_control(world_prompt["messages"], player_guidance)
    assert core not in as_text(world_prompt["messages"])
    assert memory not in as_text(world_prompt["messages"])
    await conversation.end(world_id)
    await player.close()

    restored_store = Storage(store.path, confirmation_every=0)
    restored = Conversation(restored_store, provider)
    resume = await restored.resume(world_id)
    assert guidance(resume) == player_guidance
    assert resume["world_id"] == world_id and resume["turn"] == 1
    assert resume["state"] == reply["state"]
    assert resume["facts"] == reply["facts"]
    assert restored_store.get_player_profile(world_id) == original_profile
    assert len(provider.calls) == 2

    restarted_player = APIPlayer(restored)
    next_messages = restarted_player.messages(world_id, restored_store.get_player_profile(world_id))
    assert "BLUE_SHELF_STATE_MARKER" in next_messages[-1]["content"]
    assert "未完成：再访阿圆" in next_messages[-1]["content"]
    assert next_messages[0] == player_prompt["messages"][0]  # Private Core/system prefix is unchanged.
    for output in (restored_store.get_world(world_id), restored.status(world_id),
                   restored_store.events(world_id, include_private=True), export_json(restored_store, world_id),
                   "".join(iter_markdown(restored_store, world_id)), list(collect(restored_store))):
        assert_story_has_no_control(output, player_guidance)
        assert core not in as_text(output) and memory not in as_text(output)


async def test_old_opening_rules_are_projected_to_scene_without_rewriting_raw_history(tmp_path):
    store = Storage(tmp_path / "legacy-story.db", confirmation_every=0)
    conversation = Conversation(store, MockProvider())
    created = await conversation.start("旧世界合成验收鱼", welcome=False)
    world_id = created["world_id"]
    legacy_rules = "LEGACY_PRIVATE_CONTROL_MARKER：不要将这份操作说明作为世界剧情。"
    with store.connect(True) as db:
        row = dict(db.execute("SELECT * FROM events WHERE world_id=?", (world_id,)).fetchone())
        original_hash = hashlib.sha256(encode({"kind": row["kind"], "turn": row["turn"],
                                               "content": legacy_rules, "data": json.loads(row["data"])}).encode()).hexdigest()
        db.execute("UPDATE events SET content=?,content_hash=? WHERE id=?", (legacy_rules, original_hash, row["id"]))
    raw_before = store.events(world_id, include_private=True)[0]
    public = store.events(world_id)[0]
    assert public["id"] == raw_before["id"]
    assert public["content"] == created["scene"]
    assert public["source_content_hash"] == original_hash
    assert public["content_hash"] != original_hash
    for output in (conversation.status(world_id), export_page(store, world_id),
                   export_json(store, world_id), "".join(iter_markdown(store, world_id)), list(collect(store))):
        assert "LEGACY_PRIVATE_CONTROL_MARKER" not in as_text(output)
    assert store.events(world_id, include_private=True)[0] == raw_before


def test_actual_browser_renderer_ignores_ephemeral_control_metadata():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; renderer isolation is exercised when available.")
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('static/app.js','utf8').split('document.addEventListener("click"')[0];
const all=[],elements=new Map();
function element(){const el={textContent:'',hidden:false,disabled:false,value:'',href:'',style:{},dataset:{},children:[],scrollHeight:0,scrollTop:0,clientHeight:0,append(...xs){this.children.push(...xs)},after(){},replaceChildren(...xs){this.children=xs},remove(){},classList:{toggle(){}},setAttribute(){},addEventListener(){}};all.push(el);return el}
const document={querySelector(selector){if(!elements.has(selector))elements.set(selector,element());return elements.get(selector)},createElement:element,createTextNode:t=>({textContent:t})};
const context={console,document,setTimeout,clearTimeout};vm.createContext(context);vm.runInContext(source,context);
const world={world_id:'synthetic-world',name:'合成世界',player_name:'合成玩家',mode:'mcp',preset:'home',turn:1,status:'active',scene:'蓝色书架仍在东墙边。',summary:'玩家回家。',facts:{shelf:'书架已摆好。'},state:{tags:['旧标签']},cost_cny:0,budget_cny:1,confirmation_enabled:false,confirmation_every:0,control:{player_guidance:'CONTROL_MUST_NEVER_APPEAR_IN_STORY'}};
context.renderWorld(world);context.addEvent({id:'synthetic-event',seq:1,kind:'world',turn:1,content:'阿圆在门口挥手。',occurred_at:null});
assert.equal(elements.get('#world-scene').textContent,world.scene);
assert.equal(elements.get('#world-summary').textContent,world.summary);
assert(!all.some(el=>String(el.textContent).includes('CONTROL_MUST_NEVER_APPEAR_IN_STORY')));
assert(!all.some(el=>String(el.textContent).includes('player_guidance')));
console.log('PASS: actual browser renderer ignores start/resume control metadata.');
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "-e", script], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
