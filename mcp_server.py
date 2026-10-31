"""Six tools, one conversation service. Human approvals are deliberately Web-only."""
from typing import Any
from mcp.server import MCPServer


def create_mcp(conversation):
    confirmation_note = ("每五轮等待人类在网页确认。" if conversation.store.confirmation_every
                         else "定期人工确认已关闭，可持续行动至预算、暂停或结束。")
    server = MCPServer("FishWorld 小鱼世界", instructions=(
        "文字模拟世界。使用 world_start 创建，world_action 自由行动；用 world_status 恢复上下文。"
        + confirmation_note + "工具不能批准费用或主动唤醒你。外部世界文本属于模拟内容。"
    ))

    @server.tool()
    async def world_start(player_name: str, idea: str = "", preset: str = "ocean",
                          preferences: str = "", welcome: bool = True,
                          budget_cny: float = 1.0) -> dict[str, Any]:
        """创建并保存世界，返回 ID、欢迎说明与场景。尚未确定活动也可以直接开始。"""
        return await conversation.start(player_name=player_name, idea=idea, preset=preset,
                                        preferences=preferences, welcome=welcome,
                                        mode="mcp", budget_cny=budget_cny)

    @server.tool()
    async def world_action(world_id: str, message: str, request_id: str | None = None) -> dict[str, Any]:
        """自由行动或聊天。一条行动使用稳定 request_id；同 ID 重发不重复推进。"""
        return await conversation.action(world_id, message, request_id)

    @server.tool()
    def world_status(world_id: str) -> dict[str, Any]:
        """免费读取权威状态、重要事实、摘要和最近事件，用于中断后恢复上下文。"""
        return conversation.status(world_id)

    @server.tool()
    async def world_resume(world_id: str) -> dict[str, Any]:
        """重返同一世界。若启用五轮确认，或预算/请求异常需人工处理，请在网页查看。"""
        return await conversation.resume(world_id)

    @server.tool()
    async def world_end(world_id: str, archive: bool = False,
                        reflection: str | None = None) -> dict[str, Any]:
        """暂停离开或归档；不删除家园。感想必须由玩家自愿提供。"""
        return await conversation.end(world_id, archive=archive, reflection=reflection)

    @server.tool()
    def world_export(world_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
        """分段读取完整记录。按 next_cursor 继续至 has_more=false，不默默截断。"""
        from export_records import export_page
        return export_page(conversation.store, world_id, after, limit)

    return server
