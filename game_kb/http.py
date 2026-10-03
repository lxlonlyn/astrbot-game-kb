from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(slots=True)
class HttpSettings:
    timeout_sec: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    proxy: str = ""


class HttpClient:
    def __init__(self, settings: HttpSettings) -> None:
        self.settings = settings

    async def get_text(self, url: str) -> str:
        kwargs = {
            "headers": {"User-Agent": self.settings.user_agent},
            "timeout": self.settings.timeout_sec,
            "follow_redirects": True,
            "trust_env": True,
        }
        if self.settings.proxy:
            kwargs["proxy"] = self.settings.proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_json(self, url: str):
        kwargs = {
            "headers": {
                "User-Agent": self.settings.user_agent,
                "Accept": "application/vnd.github+json, application/json",
            },
            "timeout": self.settings.timeout_sec,
            "follow_redirects": True,
            "trust_env": True,
        }
        if self.settings.proxy:
            kwargs["proxy"] = self.settings.proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
