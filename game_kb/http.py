from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx


logger = logging.getLogger("astrbot")

_GITHUB_HOSTS = {
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}

_RETRYABLE_STATUS_CODES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
}

_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
)


def is_github_url(url: str) -> bool:
    """Return whether a URL belongs to a GitHub host we may accelerate."""

    try:
        hostname = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return hostname in _GITHUB_HOSTS


def wrap_github_url(url: str, proxy_url: str) -> str:
    """Wrap a GitHub URL with an AstrBot-style URL-prefix proxy.

    The whole target URL is carried inside the proxy path. Query-string
    delimiters are percent-encoded so proxies such as astrbot2github receive
    ?recursive=1 as part of the target URL rather than as the proxy service's
    own query string.
    """

    target = str(url or "").strip()
    proxy = str(proxy_url or "").strip().rstrip("/")
    if not target or not proxy:
        return target

    # Keep scheme/path separators readable while encoding ?, &, =, # and an
    # already-escaped '%' safely. A compatible prefix proxy decodes the path
    # once before fetching the target URL.
    encoded_target = quote(target, safe=":/@")
    return f"{proxy}/{encoded_target}"


@dataclass(slots=True)
class HttpSettings:
    timeout_sec: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    proxy: str = ""
    github_proxy_url: str = ""
    github_proxy_fallback_direct: bool = True

    # Internal resilience defaults. They intentionally are not exposed in the
    # plugin config to keep the user-facing settings small.
    retry_attempts: int = 3
    retry_base_delay_sec: float = 0.5


class HttpClient:
    """Shared HTTP client for all crawlers.

    A single AsyncClient is reused across requests so GitHub-heavy adapters
    can benefit from connection pooling and TLS keep-alive.
    """

    def __init__(
        self,
        settings: HttpSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        kwargs = {
            "headers": {"User-Agent": self.settings.user_agent},
            "timeout": self.settings.timeout_sec,
            "follow_redirects": True,
            "trust_env": True,
        }
        if self.settings.proxy:
            kwargs["proxy"] = self.settings.proxy
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    def _request_candidates(self, url: str) -> list[tuple[str, str]]:
        github_proxy = self.settings.github_proxy_url.strip()
        if github_proxy and is_github_url(url):
            candidates = [
                ("github_proxy", wrap_github_url(url, github_proxy)),
            ]
            if self.settings.github_proxy_fallback_direct:
                candidates.append(("direct", url))
            return candidates
        return [("direct", url)]

    async def _sleep_before_retry(self, attempt: int) -> None:
        base = max(0.0, float(self.settings.retry_base_delay_sec))
        if base <= 0:
            return
        await asyncio.sleep(base * (2 ** max(0, attempt - 1)))

    async def _get_response(
        self,
        url: str,
        *,
        accept: str | None = None,
    ) -> httpx.Response:
        attempts = max(1, int(self.settings.retry_attempts))
        candidates = self._request_candidates(url)
        last_error: Exception | None = None

        headers = {"Accept": accept} if accept else None

        for index, (mode, request_url) in enumerate(candidates):
            for attempt in range(1, attempts + 1):
                try:
                    response = await self._client.get(
                        request_url,
                        headers=headers,
                    )

                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            last_error = exc
                        if attempt < attempts:
                            await self._sleep_before_retry(attempt)
                            continue
                        break

                    response.raise_for_status()
                    return response

                except _RETRYABLE_EXCEPTIONS as exc:
                    last_error = exc
                    if attempt < attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    break

                except httpx.HTTPStatusError as exc:
                    # 4xx errors other than retryable 408/425/429 should not be
                    # hammered repeatedly. A failed prefix proxy may still
                    # fall back to direct GitHub below.
                    last_error = exc
                    break

            has_next_candidate = index + 1 < len(candidates)
            if mode == "github_proxy" and has_next_candidate:
                logger.warning(
                    "游戏知识库 GitHub 加速请求失败，回退直连: host=%s error=%s",
                    urlsplit(url).hostname or "-",
                    last_error,
                )
                continue

            if last_error is not None:
                raise last_error

        raise RuntimeError(f"HTTP 请求失败且未返回错误对象: {url}")

    async def get_text(self, url: str) -> str:
        response = await self._get_response(url)
        return response.text

    async def get_json(self, url: str):
        response = await self._get_response(
            url,
            accept="application/vnd.github+json, application/json",
        )
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
