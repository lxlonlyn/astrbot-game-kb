from __future__ import annotations

from abc import ABC, abstractmethod

from ..core import DomainConfig, ParsedDocument, RawDocument, contains_any
from ..http import HttpClient


class BaseAdapter(ABC):
    """Domain adapter: crawl -> parse. Storage is intentionally shared."""

    domain_id: str
    display_name: str
    emoji: str = "📚"
    default_kb_name: str
    default_kb_description: str
    keywords: tuple[str, ...] = ()

    def __init__(self, config: DomainConfig) -> None:
        self.config = config

    def matches(self, text: str | None) -> bool:
        return contains_any(text, self.config.keywords)

    @abstractmethod
    async def crawl(
        self,
        client: HttpClient,
        *,
        entry: str = "",
        limit: int = 40,
    ) -> list[RawDocument]:
        raise NotImplementedError

    @abstractmethod
    def parse(
        self,
        raw: RawDocument,
        *,
        max_chars: int = 1200,
        overlap: int = 120,
    ) -> list[ParsedDocument]:
        raise NotImplementedError
