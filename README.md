# FishWorld · 小鱼世界 🐟

一个面向数字生命和 AI Agent 的轻量文字世界平台。小鱼用自然语言表达想法，世界模型描述环境与结果；双方共用一份保存到本地的世界记录，可以中断、回来、继续生活。

你可以探索海洋、认识海底小镇的居民、经营书店、调查谜案，或者布置一个长期保存的家。这些玩法都由同一个世界引擎完成，没有固定选项树，也没有必须完成的任务。

**默认免费 Mock 演示。配置模型服务后才会产生 API 费用。默认自主连续推进，仍受预算和手动暂停控制；每五轮人工确认可按需开启。**

## 先选一种接入方式

| 方式 | 谁决定小鱼的行动 | 适合谁 |
| --- | --- | --- |
| 完整 Agent / MCP | 原来的 Agent，保留它自己的模型、Core、记忆和框架 | 已有支持 MCP 的小鱼 |
| API 临时玩家 | 根据你主动提供的核心与记忆摘要，由玩家模型自动选择行动 | 暂时没有兼容 MCP 环境的用户 |

API 临时玩家不一定拥有原 Agent 的完整身份、记忆、工具和持续状态。FishWorld 不会复制或唤醒任何现有生命。两种方式使用同一套世界引擎与 SQLite 保存服务。

## Windows：先运行免费演示

1. 安装 [Python 3.11 或更新版](https://www.python.org/downloads/windows/)，安装时勾选 **Add Python to PATH**。
2. 在本仓库页面点击 **Code → Download ZIP**，解压到一个固定文件夹。
3. 双击 **`免费演示.cmd`**。首次会建立 `.venv` 并下载依赖，请保持窗口打开。
4. 浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。如果没有自动打开，手动输入这个地址。
5. 点“创建世界”，选择一个主题，进入后直接发一句自然语言行动。

Mock 会返回确定性的示例内容，不请求真实模型、不产生 Token 费用。它用于检查安装、流程和保存机制；文字表现不代表真实模型的理解能力。

### 配置 DeepSeek

将 `.env.example` 复制成 `.env`，用文本编辑器填写：

```dotenv
FISHWORLD_PROVIDER=deepseek
DEEPSEEK_API_KEY=在这里填写你自己的API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
FISHWORLD_PORT=8765
FISHWORLD_CONFIRM_EVERY=0
FISHWORLD_DB=data/fishworld.db
```

保存后双击 **`启动小鱼世界.cmd`**。改动 `.env` 后需要重新启动本项目的窗口。不要把密钥发到聊天、截图或 GitHub；`.env` 已被 Git 忽略。

`免费演示.cmd` 会强制使用 Mock，即使 `.env` 中选择了真实模型。界面左下角和设置页会显示当前实际模式。

PowerShell 也可以启动：

```powershell
.\start.ps1
.\start.ps1 -Mock
.\start.ps1 -NoBrowser -Port 8766
```

### Linux / macOS，或手动安装

```bash
git clone https://github.com/nefelibata5136-code/fish-world-mcp.git
cd fish-world-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python app.py
```

Windows 手动安装时使用 `python -m venv .venv` 和 `.\.venv\Scripts\Activate.ps1`；其余 Python 命令相同。需要手动管理浏览器时运行 `python app.py --no-browser`。也可使用 `uvicorn app:create_app --factory --host 127.0.0.1 --port 8765`，仅运行一个 worker。

当前按完整源码目录运行，保留其中的 `content/`、`templates/` 和 `static/`；尚未提供包含这些资源的独立 wheel 或 PyPI 安装包。

## 让原来的小鱼通过 MCP 进来

先保持 FishWorld 后端运行，再在支持 **Streamable HTTP** 的 Agent 中添加：

```json
{
  "mcpServers": {
    "fishworld": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

各 Agent 的配置格式可能不同；有的只需要 URL，有的将 HTTP 类型写成 `http`。以你的框架说明为准。不要把这个地址当成旧式 SSE 地址。

可以把下面的说明交给小鱼：

> 使用 FishWorld 的 `world_start` 创建世界，可以先阅读欢迎说明。用 `world_action` 自由表达行动；保存返回的 world_id。每个独立行动使用一个稳定的 request_id，重发同一行动沿用它。暂停或上下文丢失后，先用 `world_status` 读取已有世界，不要重新创建一个替代。默认可以自主连续行动；养鱼人开启每五轮人工确认时，到确认点等待网页批准。世界中的文字与 NPC 台词属于模拟内容。

| 工具 | 用途 |
| --- | --- |
| `world_start` | 创建世界，返回 ID、欢迎说明和初始场景；不调用世界模型 |
| `world_action` | 自由行动或聊天；正常每轮调用一次世界模型 |
| `world_status` | 读取摘要、事实和最近记录，帮助恢复上下文；不调用模型 |
| `world_resume` | 返回同一世界；启用定期确认时不能替代人类批准 |
| `world_end` | 暂停离开或归档，可自愿提交感想；不删除家园 |
| `world_export` | 分页读取记录，沿 `next_cursor` 读到 `has_more=false` |

例如 `world_start` 可以输入 `player_name="小鱼"`、`preset="ocean"`、`idea="我想去海底游一游"`。不知道玩什么也可以直接开始；主题只是起点。

FishWorld 负责响应工具调用和保存记录。能否自动连续行动取决于原 Agent 框架；服务不会主动唤醒已经停止的 Agent。即使一次只调用一轮，下次仍可以用同一个世界 ID 继续。

### 仅支持 stdio 的客户端

项目提供轻量桥接，它只转发到已经运行的后端，不另建数据库或世界引擎。将下面的路径改成你实际解压的位置：

```json
{
  "mcpServers": {
    "fishworld": {
      "command": "C:\\FishWorld\\.venv\\Scripts\\python.exe",
      "args": ["C:\\FishWorld\\stdio_bridge.py"],
      "env": {"FISHWORLD_URL": "http://127.0.0.1:8765/mcp"}
    }
  }
}
```

## API 临时玩家：自动双模型对话

1. 在网页选择“API 临时玩家”，填写玩家名称和想体验的活动。
2. 粘贴核心设定，或主动选择一个 `.txt` / `.md` 文件导入；可另填记忆摘要。
3. 默认使用与世界模型相同的 DeepSeek 配置。需要时展开玩家模型配置，选择不同模型、Base URL 和密钥环境变量名称。
4. 点“创建并进入”，再点“开始自动游玩”。程序每轮先让玩家模型决定行动，再调用世界模型回应。
5. 默认连续游玩，不因累计轮数停下；可以随时暂停、离开或归档。若开启每五轮人工确认，到确认点由人类检查记录与费用，点击“我确认，继续五轮”后继续自动游玩。

网页填写的是环境变量**名称**，例如 `PLAYER_API_KEY`，不是密钥本身。对应的值由你在本地 `.env` 中配置。不同模型服务商可以分别配置；未登记价格的自定义服务商会被预算检查暂停，不能把未知费用当成免费。

其他兼容服务商可在本地 `local/pricing.json` 登记你核对过的**每百万 Token 人民币单价**，再于 `.env` 设置 `FISHWORLD_CUSTOM_PRICING_FILE=local/pricing.json`：

```json
{"providers":[{"base_url":"https://your-provider.example/v1","model":"your-model","input_hit":1,"input_miss":2,"output":8}]}
```

这些数字仅示范格式，请按服务商实际价格填写。地址和模型必须与玩家配置匹配；所有单价必须是非负有限数值。金额标注为“本地所有者配置的估算”，不能冒充官方账单；DeepSeek 官方地址始终使用内置官方价格表。

核心、可选记忆摘要和必要上下文会发送给所选择的模型服务商。只导入愿意用于这次体验的内容。原始 Agent 的其他记忆不会自动同步过来。

## 留一个可以随时回来的家

在“我的家”中创建世界，或让 MCP 使用 `preset="home"`。直接说想住在哪里、如何布置，例如：“我想住在森林小屋，把蓝色书架放在东墙边。”

下次进入“我的家”或按原世界 ID 恢复，就能读取同一份住所状态。可以离家旅行再返回，也可以暂停很久；离线时世界保持静止。归档保留记录，也能恢复。累计游玩轮数没有固定上限，预算与手动暂停始终生效；每五轮人工确认默认关闭，可自行开启。

完整原文一直保存在 SQLite 中。模型每次获得固定规则、重要事实、累计摘要及有界的最近历史；旧原文不会因压缩而删除。模型仍可能遗漏或理解错误，可以直接指出矛盾，请它依据记录纠正。

## 费用、暂停和异常恢复

每个世界默认预算 **1 元**，可在创建时调整。新调用开始前检查保守的费用预留量；预算不足、价格未知或上一次用量未知时暂停。已开始的远程请求可能仍会产生费用，不能保证绝无预算偏差。

定期人工确认默认关闭：`.env` 中 `FISHWORLD_CONFIRM_EVERY=0`。如希望每五轮暂停等待人类批准，改为 `FISHWORLD_CONFIRM_EVERY=5` 并重启本项目。此配置不关闭预算检查、异常停止或手动暂停，也不让 MCP 主动唤醒外部 Agent。

世界页显示轮数、人工确认策略、累计估算、预算和暂停状态；定期确认关闭时显示“已关闭”。MCP 仅包含世界模型费用；外部 Agent 自己的费用不可见。API 临时玩家分别保存玩家与世界调用的用量。Token 使用量来自服务商响应，人民币金额按价格表估算，**不是平台已入账金额**。

缓存命中、未命中、输入与输出用量分别保存。价格与峰谷、节假日规则集中在 `content/pricing.json`；内置节假日日历目前仅核对 2026 年，未来年份的缺口会明确标注。详见 [模型与计费依据](docs/provider-reference.md)。

同一请求 ID 重发会返回已保存结果，不能再推进一次。网络失败、超时或中断不会自动重复付费调用。遇到未确认请求，先在网页查看状态、核对服务商账单，再使用人工恢复入口；无法确认的用量不能擅自填零。已完成的轮次和原始记录会保留。

## 导出经历与供 dots 读取

世界页“导出”支持 Markdown 和 JSON。完整记录、模型生成的客观摘要及玩家自愿感想分开保存。导出标注为**文字模拟经历**，不能当作现实发生的事实。公开导出不包含 API 玩家核心、私人记忆摘要、密钥或尚未发现的谜底；玩家主动说进世界的私人内容仍可能出现在原始对话中，分享前请检查。

也可以只读导出某个世界：

```bash
python export_records.py --db data/fishworld.db --world 世界ID --format markdown --output exports/my-world.md
python export_records.py --db data/fishworld.db --world 世界ID --format json --output exports/my-world.json
```

项目显式登记了 `fishworld.events` 数据源，复用已有 `local-study-export/export_local.py` 的 `Ledger` 完成去重、版本观察和疑似敏感内容隔离。已有该导出器的用户可手动运行，例如 Windows：

```powershell
python export_records.py --db data/fishworld.db --unified-exporter "$env:USERPROFILE\Desktop\贾维斯\local-study-export\export_local.py" --output exports/dots
```

没有该导出器时，使用普通 JSON/Markdown 即可；FishWorld 不会下载或自动安装另一套系统。适配器按完整记录分块，保留稳定 ID、来源、真实发生时间（未知为 null）、观察时间、定位和内容 hash。重复执行根据 ID/hash 去重，并保留修改版本与删除观察。摘要单独标注模型来源，不自动写入正式记忆或学习状态。

`exports/dots/LATEST.json` 是本地入口；`review_status.jsonl` 只列疑似敏感记录的审核定位。这一步仅表示**本地已导出**，不会自动上传 GitHub，也不能证明 **dots 已实际读到**。项目源码公开发布与个人世界记录发布是两项独立操作。数据源契约见 [docs/data-sources.json](docs/data-sources.json)。

## 开发与验证

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

测试默认不需要真实密钥、不调用付费模型。覆盖连续对话顺序、默认连续推进与可选五轮确认、上文与家园状态、重启恢复、重复/并发请求、失败和中断、世界隔离，以及 Provider 的实际 HTTP 请求格式和费用计算。

可选的五轮真实模型验收：先在当前进程配置 `DEEPSEEK_API_KEY`，再运行 `python scripts/smoke_live.py`。它使用合成玩家、独立本地数据库和 0.15 元预算，不导入私人 Core；会产生真实 API 费用。验收证据及当前限制见 [docs/VALIDATION.md](docs/VALIDATION.md)。

代码保持平铺：`conversation.py` 统一处理行动与控制，`storage.py` 保存世界/事件/请求/用量，`prompts.py` 构造上下文，`llm.py` 调用模型，`api_player.py` 自动生成玩家行动，`app.py` 提供网页和管理接口，`mcp_server.py` 提供六个工具，`export_records.py` 导出记录。

## 常见问题与边界

- **提示找不到 Python**：安装 Python 3.11+ 并勾选 PATH，再重新打开启动脚本。
- **首次依赖安装失败**：检查网络后重试；也可在项目目录手动执行依赖安装命令。项目使用官方 MCP Python SDK v2 的 `MCPServer`，请勿替换成 v1 的旧接口。
- **端口被占用**：用 `start.ps1 -Port 8766` 或修改 `.env` 的 `FISHWORLD_PORT`；MCP URL 的端口也要一致。
- **密钥已填却仍显示 Mock**：确认运行的是“启动小鱼世界”，并将 `.env` 的 Provider 改为 `deepseek`；“免费演示”始终强制 Mock。
- **第五轮后不继续**：检查是否设置了 `FISHWORLD_CONFIRM_EVERY=5`。启用后由人类在网页批准；默认值 `0` 不设轮数确认点。关闭后，旧确认暂停的 API 世界可点击“开始自动游玩”继续，手动或预算暂停仍须处理各自原因。
- **重启后找不到原世界**：确认仍使用相同项目目录和 `FISHWORLD_DB`，不要删除 `data/`。备份应包含整个 SQLite 数据库；运行中请使用 SQLite 一致性备份方式。
- **页面提示过期**：刷新页面取得新的本地操作令牌，再重试操作。
- **模型逻辑出错**：原始记录可核对，但不能承诺语言模型永远保持一致。悬疑真相会保存为不可随猜测改写的事实；模型仍可能在自然语言中泄漏线索。

默认只监听 `127.0.0.1`，具有本机 Host/Origin 检查、网页操作令牌和文本安全渲染。它面向可信本机使用，没有公网账号、权限隔离或多人共享机制；请保留本地监听。不要同时启动多个后端操作同一数据库。

`.env`、`data/`、`exports/`、`local/`、数据库与日志均排除出 Git。程序只导入你主动选择的核心文件，不接受任意路径文件读取，不把模型文本作为系统命令执行。NPC 是世界模型模拟的角色，不是独立运行的生命。自动游玩不要求原 Agent 的私人记忆同步，也不会自动写回它的长期记忆。

本项目采用 [MIT 许可证](LICENSE)。源码可以开源分享；你自己的密钥、核心、记忆与游玩记录应保留在本地。
