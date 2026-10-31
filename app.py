"""A single loopback backend serves Web, MCP, and the temporary API player."""
from __future__ import annotations
import argparse
import json
import os
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware
from api_player import APIPlayer
from conversation import Conversation
from export_records import export_json, export_page, iter_markdown
from llm import make_provider
from mcp_server import create_mcp
from prompts import presets
from storage import Storage, WorldError

ROOT = Path(__file__).resolve().parent


@contextmanager
def backend_lease(database):
    """One backend per DB; an OS lock is automatically released after a process crash."""
    path = Path(str(Path(database).resolve()) + ".lock")
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("该数据库已有FishWorld后端运行。请连接已有服务，或明确使用另一数据库。") from None
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class LocalSecurity(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            return JSONResponse({"detail": "仅允许本机访问。"}, status_code=421)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "拒绝跨站请求。"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-fishworld-csrf", "")
            if not secrets.compare_digest(supplied, request.app.state.csrf):
                return JSONResponse({"detail": "页面已过期，请刷新后再操作。"}, status_code=403)
        # Bound bodies independently of Content-Length, including chunked requests.
        if request.method in ("POST", "PUT", "PATCH"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_048_576:
                    return JSONResponse({"detail": "请求过大，最大1MB。"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response


class CreateWorld(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_name: str = Field(min_length=1, max_length=80)
    idea: str = Field(default="", max_length=4000)
    preset: str = "ocean"
    preferences: str = Field(default="", max_length=2000)
    welcome: bool = True
    mode: str = "mcp"
    budget_cny: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    core: str = Field(default="", max_length=32000)
    memory: str = Field(default="", max_length=16000)
    player_config: dict = Field(default_factory=dict)


class Action(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    request_id: str | None = Field(default=None, min_length=1, max_length=160)


class Recover(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)


class Budget(BaseModel):
    budget_cny: float = Field(gt=0, allow_inf_nan=False)


class Reconcile(BaseModel):
    call_id: str
    cost_cny: float = Field(ge=0, allow_inf_nan=False)


class Reflection(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


def create_app(db_path=None, provider=None, confirmation_every=None):
    load_dotenv(ROOT / ".env")
    if confirmation_every is None:
        try:
            confirmation_every = int(os.environ.get("FISHWORLD_CONFIRM_EVERY", "0"))
        except ValueError:
            raise ValueError("FISHWORLD_CONFIRM_EVERY 必须为0（关闭）或5（每五轮确认）。") from None
    database = db_path or os.environ.get("FISHWORLD_DB", str(ROOT / "data" / "fishworld.db"))
    store = Storage(database, confirmation_every=confirmation_every)
    conversation = Conversation(store, provider or make_provider())
    runner = APIPlayer(conversation)
    mcp = create_mcp(conversation)
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        with backend_lease(store.path):
            async with mcp.session_manager.run():
                try:
                    yield
                finally:
                    await runner.close()

    app = FastAPI(title="FishWorld 小鱼世界", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.store, app.state.conversation, app.state.runner = store, conversation, runner
    app.add_middleware(LocalSecurity)
    templates = Jinja2Templates(directory=ROOT / "templates")

    def view(world_id):
        result = conversation.status(world_id)
        result.update({"runner": runner.state(world_id), "latest_reply": result["last_reply"],
                       "next_confirmation": result["next_confirmation_at"]})
        return result

    @app.exception_handler(WorldError)
    async def world_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "输入格式或长度不正确，请检查表单。"}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        provider = conversation.provider
        mock = type(provider).__name__ == "MockProvider"
        return {"csrf_token": app.state.csrf, "provider": "mock" if mock else "deepseek",
                "world_model": "mock（不调用模型）" if mock else getattr(provider, "model", "unknown"),
                "mcp_url": str(request.base_url).rstrip("/") + "/mcp", "version": "0.1.0",
                "confirmation_every": store.confirmation_every,
                "confirmation_enabled": store.confirmation_every > 0}

    @app.get("/api/presets")
    async def get_presets():
        return {"presets": presets()}

    @app.get("/api/welcome")
    async def welcome():
        return {"content": (ROOT / "content" / "welcome_zh.md").read_text(encoding="utf-8")}

    @app.get("/api/worlds")
    async def worlds():
        return {"worlds": [view(item["world_id"]) for item in store.list_worlds()]}

    @app.post("/api/worlds")
    async def create(body: CreateWorld):
        if body.mode == "api" and not body.core.strip():
            raise WorldError("missing_core", "请填写或导入API临时玩家核心设定。", 400)
        allowed = {"model", "base_url", "api_key_env", "max_tokens"}
        if set(body.player_config) - allowed:
            raise WorldError("invalid_config", "模型配置只接受model、base_url、api_key_env和max_tokens。", 400)
        world = await conversation.start(player_name=body.player_name, idea=body.idea,
                                         preset=body.preset, preferences=body.preferences,
                                         welcome=body.welcome, mode=body.mode, budget_cny=body.budget_cny)
        if body.mode == "api":
            store.set_player_profile(world["world_id"], body.core, body.memory, body.player_config)
        return {**view(world["world_id"]), "welcome": world.get("welcome", "")}

    @app.get("/api/worlds/{world_id}")
    async def status(world_id: str):
        return view(world_id)

    @app.get("/api/worlds/{world_id}/events")
    async def events(world_id: str, after: int = 0, limit: int = 100):
        return export_page(store, world_id, after, limit)

    @app.get("/api/worlds/{world_id}/usage")
    async def usage(world_id: str):
        conversation.status(world_id)
        return {"calls": store.usage(world_id), "note": "服务商返回用量与本地估算；人工核对值单独标注。"}

    @app.post("/api/worlds/{world_id}/action")
    async def action(world_id: str, body: Action):
        if runner.state(world_id)["running"]:
            raise WorldError("player_running", "自动玩家正在运行，请先暂停并等待当前一轮完成。")
        return await conversation.action(world_id, body.message, body.request_id)

    @app.post("/api/worlds/{world_id}/pause")
    async def pause(world_id: str):
        return await conversation.pause(world_id)

    @app.post("/api/worlds/{world_id}/resume")
    async def resume(world_id: str):
        return await conversation.resume(world_id)

    @app.post("/api/worlds/{world_id}/approve")
    async def approve(world_id: str):
        return await conversation.approve(world_id)

    @app.post("/api/worlds/{world_id}/archive")
    async def archive(world_id: str):
        return await conversation.end(world_id, archive=True)

    @app.post("/api/worlds/{world_id}/run")
    async def run(world_id: str):
        return await runner.start(world_id)

    @app.post("/api/worlds/{world_id}/recover")
    async def recover(world_id: str, body: Recover):
        return await conversation.recover(world_id, body.request_id)

    @app.post("/api/worlds/{world_id}/recover-player")
    async def recover_player(world_id: str):
        return await runner.recover(world_id)

    @app.post("/api/worlds/{world_id}/budget")
    async def budget(world_id: str, body: Budget):
        return await conversation.update_budget(world_id, body.budget_cny)

    @app.post("/api/worlds/{world_id}/reconcile")
    async def reconcile(world_id: str, body: Reconcile):
        return await conversation.reconcile_cost(world_id, body.call_id, body.cost_cny)

    @app.post("/api/worlds/{world_id}/reflection")
    async def reflection(world_id: str, body: Reflection):
        store.get_world(world_id)
        store.update_world(world_id, {}, "reflection", body.text)
        return view(world_id)

    @app.get("/api/worlds/{world_id}/export")
    async def export(world_id: str, format: str = "json"):
        if format == "markdown":
            return StreamingResponse(iter_markdown(store, world_id), media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="fishworld-{world_id}.md"'})
        if format != "json":
            raise WorldError("invalid_format", "导出格式应为json或markdown。", 400)
        return JSONResponse(export_json(store, world_id), headers={
            "Content-Disposition": f'attachment; filename="fishworld-{world_id}.json"'})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.mount("/", mcp_app)
    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    port = int(os.getenv("FISHWORLD_PORT", "8765"))
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, access_log=False, log_level="warning")
