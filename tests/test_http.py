import asyncio
import httpx
import pytest

from game_kb.http import HttpClient, HttpSettings, is_github_url, wrap_github_url


def test_is_github_url():
    assert is_github_url("https://github.com/owner/repo")
    assert is_github_url("https://raw.githubusercontent.com/owner/repo/main/a.txt")
    assert is_github_url("https://api.github.com/repos/owner/repo")
    assert not is_github_url("https://thbwiki.cc/")


def test_wrap_github_url_matches_astrbot_prefix_style_and_keeps_query():
    target = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    wrapped = wrap_github_url(target, "https://proxy.example/")
    assert wrapped == (
        "https://proxy.example/"
        "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    )
    assert "%3F" not in wrapped
    assert "/https://api.github.com/" in wrapped


def test_httpx_does_not_collapse_embedded_https_prefix():
    target = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    wrapped = wrap_github_url(target, "https://proxy.example")
    request = httpx.Request("GET", wrapped)
    assert str(request.url) == wrapped
    assert request.url.raw_path.startswith(b"/https://api.github.com/")


def test_github_proxy_retries_then_falls_back_to_direct():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "proxy.example":
            raise httpx.ConnectError("proxy down", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def run():
        client = HttpClient(
            HttpSettings(
                github_proxy_url="https://proxy.example",
                github_proxy_fallback_direct=True,
                retry_attempts=2,
                retry_base_delay_sec=0,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.get_json(
                "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
            )
            assert result == {"ok": True}
        finally:
            await client.aclose()

    asyncio.run(run())
    assert len(calls) == 3
    assert calls[0].startswith("https://proxy.example/")
    assert calls[1].startswith("https://proxy.example/")
    assert calls[2].startswith("https://api.github.com/")


def test_github_proxy_can_disable_direct_fallback():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("proxy down", request=request)

    async def run():
        client = HttpClient(
            HttpSettings(
                github_proxy_url="https://proxy.example",
                github_proxy_fallback_direct=False,
                retry_attempts=2,
                retry_base_delay_sec=0,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(httpx.ConnectError):
                await client.get_text(
                    "https://raw.githubusercontent.com/owner/repo/main/a.txt"
                )
        finally:
            await client.aclose()

    asyncio.run(run())
    assert len(calls) == 2
    assert all(call.startswith("https://proxy.example/") for call in calls)


def test_non_github_request_does_not_use_github_proxy():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="ok", request=request)

    async def run():
        client = HttpClient(
            HttpSettings(
                github_proxy_url="https://proxy.example",
                retry_attempts=2,
                retry_base_delay_sec=0,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert await client.get_text("https://thbwiki.cc/") == "ok"
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls == ["https://thbwiki.cc/"]


def test_direct_transient_error_is_retried():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, text="done", request=request)

    async def run():
        client = HttpClient(
            HttpSettings(
                retry_attempts=2,
                retry_base_delay_sec=0,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert await client.get_text("https://thbwiki.cc/test") == "done"
        finally:
            await client.aclose()

    asyncio.run(run())
    assert call_count == 2
