import json
from datetime import datetime

import httpx
import pytest

from llm import MockProvider, Provider, ProviderError, calculate_cost, normalize_usage, pricing_period


def at(text):
    return datetime.fromisoformat(text)


def response_body(content='{"reply":"hello"}', *, usage=None, finish="stop"):
    return {"model": "deepseek-flash", "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": usage if usage is not None else {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 750,
                                                       "prompt_cache_miss_tokens": 250, "completion_tokens": 100}}


@pytest.mark.parametrize("time,period", [
    ("2026-10-30T08:59:59+08:00", "off_peak"),
    ("2026-10-30T09:00:00+08:00", "peak"),
    ("2026-10-30T11:59:59+08:00", "peak"),
    ("2026-10-30T12:00:00+08:00", "off_peak"),
    ("2026-10-30T14:00:00+08:00", "peak"),
    ("2026-10-30T18:00:00+08:00", "off_peak"),
    ("2026-10-31T10:00:00+08:00", "off_peak"),
    ("2026-10-01T10:00:00+08:00", "off_peak"),
    ("2026-10-10T10:00:00+08:00", "off_peak"),  # Saturday make-up workday
    ("2026-01-02T10:00:00+08:00", "off_peak"),
    ("2026-10-30T02:00:00+00:00", "peak"),
])
def test_verified_pricing_calendar(time, period):
    assert pricing_period(at(time))[0] == period


def test_future_holidays_never_silently_claimed_known():
    period, note = pricing_period(at("2027-01-01T10:00:00+08:00"))
    assert period == "peak" and "未覆盖" in note


def test_cost_cache_and_missing_usage():
    usage = normalize_usage({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 750}, "completion_tokens": 100})
    assert usage["prompt_cache_miss_tokens"] == 250
    assert usage["cache_hit_ratio"] == 0.75
    cost, note = calculate_cost("deepseek-flash", usage, at("2026-10-30T10:00:00+08:00"))
    assert cost == pytest.approx((750 * .04 + 250 * 2 + 100 * 8) / 1e6)
    assert "非平台已入账" in note
    assert calculate_cost("other-model", usage, at("2026-10-30T10:00:00+08:00"))[0] is None
    assert calculate_cost("deepseek-flash", normalize_usage(None), at("2026-10-30T10:00:00+08:00"))[0] is None
    assert normalize_usage(None)["cache_hit_ratio"] is None
    bad = normalize_usage({"prompt_tokens": 10, "completion_tokens": 3, "prompt_cache_hit_tokens": 100})
    assert bad["prompt_cache_hit_tokens"] is None


def test_cross_period_and_missing_cache_are_conservative():
    usage = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 100})
    cost, note = calculate_cost("deepseek-flash", usage, at("2026-10-30T08:59:59+08:00"), end_at=at("2026-10-30T09:00:01+08:00"))
    assert cost == pytest.approx((1000 * 2 + 100 * 8) / 1e6)
    assert "跨计费" in note and "缓存分项未知" in note


@pytest.mark.asyncio
async def test_wire_has_stable_messages_explicit_json_and_one_call(monkeypatch):
    monkeypatch.setenv("TEST_FISH_KEY", "test-secret-not-real")
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body())

    provider = Provider(api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler), now=lambda: at("2026-10-30T10:00:00+08:00"))
    messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Hello"}]
    result = await provider.complete(messages)
    wire = json.loads(requests[0].content)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-secret-not-real"
    assert wire["model"] == "deepseek-flash"
    assert wire["messages"] == messages
    assert wire["thinking"] == {"type": "disabled"}
    assert wire["response_format"] == {"type": "json_object"}
    assert result.usage["prompt_cache_hit_tokens"] == 750
    assert result.cost_cny > 0
    assert provider.estimated_cost(messages) > result.cost_cny
    await provider.complete(messages, role="player", config={"model": "deepseek-v4-pro", "max_tokens": 300})
    wire = json.loads(requests[1].content)
    assert "response_format" not in wire and wire["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_other_base_url_uses_actual_usage_but_unknown_price(monkeypatch):
    monkeypatch.setenv("TEST_FISH_KEY", "test-secret-not-real")
    captured = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    provider = Provider(base_url="https://example.invalid/v1", api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    result = await provider.complete([{"role": "user", "content": "Return JSON"}])
    assert result.cost_cny is None and "价格未登记" in result.pricing_note
    assert result.usage["usage_known"]
    assert provider.estimated_cost([]) is None
    assert "thinking" not in captured[0]


@pytest.mark.asyncio
async def test_no_retries_or_secret_upstream_error(monkeypatch):
    monkeypatch.setenv("TEST_FISH_KEY", "test-secret-not-real")
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="test-secret-not-real private core text")

    provider = Provider(api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        await provider.complete([{"role": "user", "content": "Private text"}])
    assert calls == 1 and "429" in str(error.value)
    assert "test-secret" not in str(error.value) and "private core" not in str(error.value)
    assert error.value.cost_cny is None and error.value.attempted


@pytest.mark.asyncio
async def test_timeout_usage_unknown_and_missing_key_never_calls(monkeypatch):
    async def handler(request):
        raise httpx.ReadTimeout("contains test-secret", request=request)

    monkeypatch.setenv("TEST_FISH_KEY", "not-real")
    provider = Provider(api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="超时") as error:
        await provider.complete([])
    assert error.value.usage["prompt_tokens"] is None
    monkeypatch.delenv("TEST_FISH_KEY")
    with pytest.raises(ProviderError) as error:
        await provider.complete([])
    assert not error.value.attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [response_body(finish="length"), response_body(content=None), {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}])
async def test_incomplete_content_keeps_billing(body, monkeypatch):
    monkeypatch.setenv("TEST_FISH_KEY", "not-real")

    async def handler(request):
        return httpx.Response(200, json=body)

    provider = Provider(api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    result = await provider.complete([])
    assert result.content == ""
    assert result.usage["usage_known"] and result.cost_cny is not None


def test_reject_credential_urls_and_insecure_remote():
    for url in ["https://user:secret@example.com", "https://example.com?token=secret", "http://example.com"]:
        with pytest.raises(ProviderError) as error:
            Provider(base_url=url).estimated_cost([])
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_mock_context_continuity_and_injection_fixture():
    provider = MockProvider()
    messages = [{"role": "user", "content": json.dumps({"context": {"facts": {"ayuan_shell": "Earlier shared experience"}}, "message": "我来找阿圆"})}]
    result = await provider.complete(messages)
    assert "贝壳" in json.loads(result.content)["reply"]
    assert result.cost_cny == 0 and "Mock" in result.pricing_note
    assert len(provider.calls) == 1
    scripted = MockProvider(["broken", RuntimeError("fixture")])
    assert (await scripted.complete([])).content == "broken"
    with pytest.raises(RuntimeError, match="fixture"):
        await scripted.complete([])


@pytest.mark.asyncio
async def test_owner_price_catalog_enables_custom_provider_and_preserves_source(tmp_path, monkeypatch):
    catalog = tmp_path / "pricing.json"
    catalog.write_text(json.dumps({"providers": [{"base_url": "https://example.invalid/v1/", "model": "owner-model",
                                                "input_hit": 1, "input_miss": 2, "output": 3}]}), encoding="utf-8")
    monkeypatch.setenv("FISHWORLD_CUSTOM_PRICING_FILE", str(catalog))
    monkeypatch.setenv("TEST_FISH_KEY", "not-real")

    async def handler(request):
        return httpx.Response(200, json=response_body())

    provider = Provider(base_url="https://example.invalid/v1", model="owner-model", api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    assert provider.estimated_cost([]) > 0
    result = await provider.complete([])
    assert result.cost_cny == pytest.approx((750 * 1 + 250 * 2 + 100 * 3) / 1e6)
    assert "所有者配置" in result.pricing_note and "未经" in result.pricing_note
    assert provider.estimated_cost([], config={"model": "not-matching"}) is None
    # Exact configured provider origin+path, not a price applied to an arbitrary host.
    assert Provider(base_url="https://other.invalid/v1", model="owner-model").estimated_cost([]) is None


@pytest.mark.asyncio
async def test_custom_missing_cache_and_usage_remain_explicit(tmp_path, monkeypatch):
    catalog = tmp_path / "prices.json"
    catalog.write_text(json.dumps({"providers": [{"base_url": "https://example.invalid", "model": "test", "input_hit": 5, "input_miss": 2, "output": 3}]}), encoding="utf-8")
    monkeypatch.setenv("FISHWORLD_CUSTOM_PRICING_FILE", str(catalog))
    monkeypatch.setenv("TEST_FISH_KEY", "not-real")
    responses = [response_body(usage={"prompt_tokens": 100, "completion_tokens": 10}), response_body(usage={})]

    async def handler(request):
        return httpx.Response(200, json=responses.pop(0))

    provider = Provider(base_url="https://example.invalid", model="test", api_key_env="TEST_FISH_KEY", transport=httpx.MockTransport(handler))
    assert provider.estimated_cost([]) == pytest.approx(((2 + 256) * 5 + 4096 * 3) / 1e6)
    first = await provider.complete([])
    assert first.cost_cny == pytest.approx((100 * 2 + 10 * 3) / 1e6)
    assert "缓存分项未知" in first.pricing_note
    second = await provider.complete([])
    assert second.cost_cny is None and not second.usage["usage_known"]


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan"), True, "1"])
def test_custom_catalog_rejects_invalid_prices_before_call(tmp_path, monkeypatch, bad):
    catalog = tmp_path / "invalid.json"
    catalog.write_text(json.dumps({"providers": [{"base_url": "https://example.invalid", "model": "test", "input_hit": bad, "input_miss": 2, "output": 3}]}), encoding="utf-8")
    monkeypatch.setenv("FISHWORLD_CUSTOM_PRICING_FILE", str(catalog))
    with pytest.raises(ProviderError) as error:
        Provider(base_url="https://example.invalid", model="test").estimated_cost([])
    assert not error.value.attempted
    # Local owner data never overrides the verified official DeepSeek rate card.
    assert Provider().estimated_cost([]) > 0
