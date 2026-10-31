"""Optional stdio-to-HTTP MCP bridge. Never opens a database or calls a model."""
import os
from mcp import Client
from typing import Any
from mcp.server import MCPServer

bridge = MCPServer("FishWorld stdio bridge")


async def forward(name, arguments):
    async with Client(os.environ.get("FISHWORLD_URL", "http://127.0.0.1:8765/mcp")) as client:
        result = await client.call_tool(name, arguments)
        if result.is_error:
            raise ValueError("FishWorld 后端拒绝请求；请在网页查看世界状态。")
        return result.structured_content


@bridge.tool()
async def world_start(player_name: str, idea: str = "", preset: str = "ocean",
                      preferences: str = "", welcome: bool = True,
                      budget_cny: float = 1.0) -> dict[str, Any]:
    """通过已经运行的 FishWorld 后端创建世界。"""
    return await forward("world_start", locals())


@bridge.tool()
async def world_action(world_id: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    """向同一后端发送行动；稳定请求 ID 防止重复推进。"""
    return await forward("world_action", locals())


@bridge.tool()
async def world_status(world_id: str) -> dict[str, Any]:
    """从后端读取世界摘要和状态。"""
    return await forward("world_status", locals())


@bridge.tool()
async def world_resume(world_id: str) -> dict[str, Any]:
    """恢复同一世界，不能代替人类批准五轮关卡。"""
    return await forward("world_resume", locals())


@bridge.tool()
async def world_end(world_id: str, archive: bool = False, reflection: str | None = None) -> dict[str, Any]:
    """暂停或归档，感想由玩家自愿填写。"""
    return await forward("world_end", locals())


@bridge.tool()
async def world_export(world_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
    """分段读取记录；沿 next_cursor 取完全部事件。"""
    return await forward("world_export", locals())


if __name__ == "__main__":
    bridge.run(transport="stdio")
