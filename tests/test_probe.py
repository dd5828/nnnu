"""模型端点探测：/models 解析、/v1 候选、错误映射（§7.19 探测语义）。"""

import httpx
import pytest

from nnnu.services.llm.probe import _candidate_urls, probe_models


def _transport(handler):
    """把同步 handler(request)->response 包成 httpx transport（§12.1 注入法）。"""
    return httpx.MockTransport(handler)


async def test_probe_ok_extracts_model_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

    result = await probe_models("https://api.openai.com/v1", transport=_transport(handler))
    assert result.ok is True
    assert result.models == ["gpt-4o", "gpt-4o-mini"]


async def test_probe_appends_v1_candidate():
    """base_url 没带 /v1：先试原样，404 后自动再试 /v1/models。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/models":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "qwen3"}]})

    result = await probe_models("http://localhost:11434", transport=_transport(handler))
    assert result.ok is True
    assert result.models == ["qwen3"]
    assert seen == ["http://localhost:11434/models", "http://localhost:11434/v1/models"]


async def test_probe_401_reports_bad_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    result = await probe_models(
        "https://api.deepseek.com/v1", api_key="sk-bad", transport=_transport(handler)
    )
    assert result.ok is False
    assert "401" in result.error


async def test_probe_non_200_after_candidates_reports_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    result = await probe_models("https://api.deepseek.com/v1", transport=_transport(handler))
    assert result.ok is False
    assert "500" in result.error


async def test_probe_non_json_reports_incompatible():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    result = await probe_models("https://api.deepseek.com/v1", transport=_transport(handler))
    assert result.ok is False
    assert "JSON" in result.error


async def test_probe_connect_error_reports_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = await probe_models("https://api.deepseek.com/v1", transport=_transport(handler))
    assert result.ok is False
    assert "无法连接" in result.error


async def test_probe_empty_base_url():
    result = await probe_models("  ")
    assert result.ok is False
    assert "base_url" in result.error


async def test_probe_scheme_missing_returns_friendly_error():
    """不带 http(s):// 的地址 → 友好报错而不是 500（实测踩出来的坑）。"""
    result = await probe_models("deepseek-chat")
    assert result.ok is False
    assert "格式不对" in result.error


async def test_probe_sends_bearer_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test-key-123"
        return httpx.Response(200, json={"data": []})

    result = await probe_models(
        "https://api.deepseek.com/v1", api_key="sk-test-key-123", transport=_transport(handler)
    )
    assert result.ok is True


def test_candidate_urls_dedup():
    assert _candidate_urls("https://api.deepseek.com/v1") == ["https://api.deepseek.com/v1/models"]
    assert _candidate_urls("https://api.deepseek.com/v1/") == ["https://api.deepseek.com/v1/models"]
    assert _candidate_urls("https://foo.example.com") == [
        "https://foo.example.com/models",
        "https://foo.example.com/v1/models",
    ]
