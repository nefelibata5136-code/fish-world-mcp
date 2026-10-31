"""One HTTP request per decision, explicit usage, and an offline demonstration provider."""
from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
PRICING = json.loads((ROOT / "content" / "pricing.json").read_text(encoding="utf-8"))


@dataclass
class LLMResult:
    content: str
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = "unknown"
    cost_cny: float | None = None
    pricing_note: str = "用量或价格未知；不能视为免费。"


class ProviderError(RuntimeError):
    """Safe public error: upstream bodies, request URLs, and API keys never escape."""

    def __init__(self, message: str, *, attempted: bool = True):
        super().__init__(message)
        self.attempted = attempted
        self.usage = normalize_usage(None)
        self.cost_cny = None
        self.pricing_note = "请求未获得可确认的用量；可能已产生费用，请先核对服务商账单。" if attempted else "调用前配置检查失败，未发送模型请求。"


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    prompt = _count(raw.get("prompt_tokens"))
    output = _count(raw.get("completion_tokens"))
    hit = _count(raw.get("prompt_cache_hit_tokens"))
    details = raw.get("prompt_tokens_details") or {}
    if hit is None and isinstance(details, dict):
        hit = _count(details.get("cached_tokens"))
    miss = _count(raw.get("prompt_cache_miss_tokens"))
    if prompt is None and hit is not None and miss is not None:
        prompt = hit + miss
    if prompt is not None and hit is not None and hit <= prompt and miss is None:
        miss = prompt - hit
    if prompt is not None and miss is not None and miss <= prompt and hit is None:
        hit = prompt - miss
    if prompt is not None and (hit is not None and hit > prompt or miss is not None and miss > prompt):
        hit = miss = None
    if prompt is not None and hit is not None and miss is not None and hit + miss != prompt:
        hit = miss = None
    return {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": output,
        "total_tokens": prompt + output if prompt is not None and output is not None else None,
        "cache_hit_ratio": hit / prompt if hit is not None and prompt else (0.0 if prompt == 0 and hit == 0 else None),
        "usage_known": prompt is not None and output is not None,
    }


def pricing_period(at: datetime, pricing: dict = PRICING) -> tuple[str, str]:
    """DeepSeek weekends stay off-peak, including weekend make-up work days."""
    if at.tzinfo is None:
        raise ValueError("计费时间必须包含时区。")
    local = at.astimezone(BEIJING)
    date = local.date().isoformat()
    in_peak_window = local.weekday() in pricing["peak_weekdays"] and any(start <= local.hour < end for start, end in pricing["peak_hours"])
    if not in_peak_window:
        return "off_peak", "空闲时段"
    if local.year not in pricing["holiday_coverage_years"]:
        return "peak", "节假日日历未覆盖该年份，按高峰价格保守估算"
    if any(start <= date <= end for start, end in pricing["holiday_ranges"]):
        return "off_peak", "中国节假日空闲时段"
    return "peak", "高峰时段"


def calculate_cost(model: str, usage: dict, at: datetime, *, end_at: datetime | None = None,
                   pricing: dict = PRICING) -> tuple[float | None, str]:
    table = pricing["models"].get(model)
    if not table:
        return None, "模型未登记价格，用量仍保留，费用未知。"
    if usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
        return None, "服务商未完整返回输入/输出用量；费用未知。"
    period, note = pricing_period(at, pricing)
    if end_at is not None:
        final_period, _ = pricing_period(end_at, pricing)
        if final_period != period:
            period, note = "peak", "请求跨计费时段，按高峰价格保守估算"
    rates = table[period]
    hit, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
    if hit is None or miss is None:
        hit, miss = 0, usage["prompt_tokens"]
        note += "；缓存分项未知，按全部未命中估算"
    cost = (hit * rates["input_hit"] + miss * rates["input_miss"] + usage["completion_tokens"] * rates["output"]) / 1_000_000
    return cost, f"{note}；人民币估算（价格核对 {pricing['verified_at']}），非平台已入账金额。"


def _custom_rates(base_url: str, model: str) -> dict | None:
    filename = os.getenv("FISHWORLD_CUSTOM_PRICING_FILE")
    if not filename:
        return None
    try:
        path = Path(filename)
        catalog = json.loads((path if path.is_absolute() else ROOT / path).read_text(encoding="utf-8-sig"))
        rows = catalog["providers"]
        if not isinstance(rows, list):
            raise ValueError()
        matches = []
        for row in rows:
            rates = {key: row[key] for key in ("input_hit", "input_miss", "output")}
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 for value in rates.values()):
                raise ValueError()
            if row["base_url"].rstrip("/") == base_url and row["model"] == model:
                matches.append(rates)
        if len(matches) > 1:
            raise ValueError()
        return matches[0] if matches else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("自定义价格文件格式不正确；请检查 providers、地址、模型及非负有限单价。", attempted=False) from None


def _custom_cost(usage: dict, rates: dict) -> tuple[float | None, str]:
    if not usage.get("usage_known"):
        return None, "服务商未完整返回用量；费用未知。"
    hit, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
    note = "本地所有者配置的人民币价格估算，未经服务商官方核验；非平台已入账金额。"
    if hit is None or miss is None:
        hit, miss = 0, usage["prompt_tokens"]
        note += "缓存分项未知，按全部未命中估算。"
    return (hit * rates["input_hit"] + miss * rates["input_miss"] + usage["completion_tokens"] * rates["output"]) / 1_000_000, note


class Provider:
    def __init__(self, *, base_url: str | None = None, model: str | None = None,
                 api_key_env: str = "DEEPSEEK_API_KEY", transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], datetime] | None = None):
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("FISHWORLD_WORLD_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.api_key_env = api_key_env
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    def settings(self, role: str, config: dict | None) -> dict:
        cfg = config or {}
        base_url = str(cfg.get("base_url") or self.base_url).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("模型地址格式不正确，请使用不含凭据及查询参数的 HTTP(S) 地址。", attempted=False)
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProviderError("远程模型服务必须使用 HTTPS。", attempted=False)
        model = str(cfg.get("model") or (os.getenv("FISHWORLD_PLAYER_MODEL") if role == "player" else None) or self.model)
        key_env = str(cfg.get("api_key_env") or self.api_key_env)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_env):
            raise ProviderError("API Key 环境变量名称不正确。", attempted=False)
        max_tokens = cfg.get("max_tokens", 1200 if role == "player" else 4096)
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 64 <= max_tokens <= 16384:
            raise ProviderError("max_tokens 必须是 64 到 16384 的整数。", attempted=False)
        return {"base_url": base_url, "model": model, "api_key_env": key_env, "max_tokens": max_tokens}

    def estimated_cost(self, messages: list[dict], role: str = "world", config: dict | None = None) -> float | None:
        cfg = self.settings(role, config)
        rates = (PRICING["models"].get(cfg["model"], {}).get("peak")
                 if urlsplit(cfg["base_url"]).hostname == "api.deepseek.com"
                 else _custom_rates(cfg["base_url"], cfg["model"]))
        if rates is None:
            return None
        # UTF-8 bytes + envelope allowance, all cache misses, peak rate, maximum output.
        input_cap = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + len(messages) * 16 + 256
        return (input_cap * max(rates["input_hit"], rates["input_miss"]) + cfg["max_tokens"] * rates["output"]) / 1_000_000

    async def complete(self, messages: list[dict], role: str = "world", config: dict | None = None) -> LLMResult:
        cfg = self.settings(role, config)
        key = os.getenv(cfg["api_key_env"])
        if not key:
            raise ProviderError(f"未设置 {cfg['api_key_env']}。请先在本地 .env 配置，或使用 Mock 演示。", attempted=False)
        payload = {"model": cfg["model"], "messages": messages, "stream": False, "max_tokens": cfg["max_tokens"]}
        official = urlsplit(cfg["base_url"]).hostname == "api.deepseek.com"
        custom_rates = None if official else _custom_rates(cfg["base_url"], cfg["model"])
        if official:
            payload["thinking"] = {"type": "disabled"}
        if role == "world":
            payload["response_format"] = {"type": "json_object"}
        started = self.now()
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(90.0, connect=15.0), follow_redirects=False) as client:
                response = await client.post(cfg["base_url"] + "/chat/completions", headers={"Authorization": "Bearer " + key}, json=payload)
        except httpx.TimeoutException:
            raise ProviderError("模型请求超时，未自动重试；请核对请求状态与账单。") from None
        except httpx.HTTPError:
            raise ProviderError("模型网络请求失败，未自动重试；请核对网络、请求状态与账单。") from None
        ended = self.now()
        if response.status_code != 200:
            raise ProviderError(f"模型服务返回 HTTP {response.status_code}，未自动重试。请检查模型配置、额度或稍后重试。")
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            raise ProviderError("模型服务返回了无法解析的响应；用量未知，未自动重试。") from None
        if not isinstance(body, dict):
            raise ProviderError("模型服务响应格式错误；用量未知，未自动重试。")
        usage = normalize_usage(body.get("usage"))
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                content = ""
            # An incomplete completion must never become a successful world event.
            if choice.get("finish_reason") not in (None, "stop"):
                content = ""
        except (KeyError, IndexError, TypeError):
            content = ""
        actual_model = str(body.get("model") or cfg["model"])
        if official:
            cost, note = calculate_cost(cfg["model"], usage, started, end_at=ended)
        elif custom_rates is not None:
            cost, note = _custom_cost(usage, custom_rates)
        else:
            cost, note = None, "自定义服务商的价格未登记；费用未知，不能视为免费。"
        return LLMResult(content, usage, actual_model, cost, note)


DeepSeekProvider = Provider


class MockProvider:
    """An honest offline demo: deterministic fixtures, not evidence of model reasoning."""
    def __init__(self, responses: list[Any] | None = None, *, delay: float = 0):
        self.calls: list[dict] = []
        self.responses = list(responses or [])
        self.delay = delay

    def estimated_cost(self, messages: list[dict], role: str = "world", config: dict | None = None) -> float:
        return 0.0

    async def complete(self, messages: list[dict], role: str = "world", config: dict | None = None) -> LLMResult:
        import asyncio
        self.calls.append({"messages": copy.deepcopy(messages), "role": role, "config": {k: v for k, v in (config or {}).items() if "key" not in k.lower()}})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.responses:
            supplied = self.responses.pop(0)
            if isinstance(supplied, BaseException):
                raise supplied
            if isinstance(supplied, LLMResult):
                return supplied
            content = supplied if isinstance(supplied, str) else json.dumps(supplied, ensure_ascii=False)
        elif role == "player":
            content = "我想沿着海草森林慢慢游，观察周围，并和阿圆聊聊今天的发现。"
        else:
            last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            try:
                action = json.loads(last)
                context = action.get("context", {})
                message = action.get("message", last)
            except (ValueError, AttributeError):
                context, message = {}, last
            if not isinstance(context, dict):
                context = {}
            facts = dict(context.get("facts") or {})
            state = dict(context.get("state") or {})
            scene = context.get("scene") or "阳光下的珊瑚礁"
            if "阿圆" in message or "章鱼" in message:
                facts["npc_ayuan"] = "阿圆是一只喜欢收藏贝壳的章鱼，是玩家在珊瑚礁认识的朋友。"
            if "贝壳" in message and any(word in message for word in ["找", "帮助", "找到", "送"]):
                facts["ayuan_shell"] = "玩家帮助阿圆找到贝壳，阿圆记得这次共同经历。"
            if "小屋" in message or "家" in message:
                facts["home"] = facts.get("home", "玩家拥有一间可随时返回的小屋。")
                scene = "熟悉的小屋"
            if "书架" in message:
                facts["home_bookshelf"] = "小屋里摆放了玩家选择的书架。"
                state["bookshelf"] = "已放置"
            if "蓝色" in message and "书架" in message:
                facts["home_bookshelf"] = "小屋里摆放了一座蓝色书架。"
                state["bookshelf"] = "蓝色"
            if "阿圆" in message:
                reply = "阿圆从珊瑚礁边游来，向你挥动腕足。" + ("她还记得你帮她找到贝壳的经历。" if "ayuan_shell" in facts else "她喜欢收藏贝壳，也愿意听听你的打算。")
            elif any(word in message for word in ["没想法", "不知道", "不知"]):
                reply = "可以先随便聊聊。你可以去海里观察生物、经营一家书店、布置一个家，或安静地休息；也欢迎提出任何自己的想法。"
            else:
                reply = f"你表达了自己的打算：{message}。海水轻轻流动，周围的世界为你的自由选择留出了空间。"
            if "书架" in message:
                reply = "小屋里的书架已经按你的想法摆好，下次回来仍会保留。"
            summary = "；".join(facts.values()) or "玩家正在自由探索文字世界，可以随时改变活动。"
            content = json.dumps({"reply": reply, "scene": scene, "summary": summary, "facts": facts, "state": state}, ensure_ascii=False)
        usage = {"prompt_tokens": 0, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
                 "completion_tokens": 0, "total_tokens": 0, "cache_hit_ratio": None, "usage_known": True}
        return LLMResult(content, usage, "mock-offline", 0.0, "Mock 离线演示，未发送模型请求，费用为 0。")


def make_provider() -> Provider | MockProvider:
    name = os.getenv("FISHWORLD_PROVIDER", "mock").lower()
    if name == "mock":
        return MockProvider()
    if name in {"deepseek", "openai-compatible"}:
        return Provider()
    raise ValueError("FISHWORLD_PROVIDER 只支持 mock、deepseek 或 openai-compatible。")
