from __future__ import annotations

import asyncio
import re
from collections import deque
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from ..core import ParsedDocument, RawDocument, clean_inline_text, make_self_describing_chunks
from ..http import HttpClient
from .base import BaseAdapter


class TouhouAdapter(BaseAdapter):
    domain_id = "touhou"
    display_name = "东方Project"
    emoji = "☯️"
    default_kb_name = "东方Project知识库"
    default_kb_description = "从 THBWiki 抓取并清洗的东方Project设定知识"
    base_url = "https://thbwiki.cc/"
    allowed_hosts = {"thbwiki.cc", "www.thbwiki.cc"}

    stop_sections = {
        "注释", "脚注", "参考资料", "参考文献", "外部链接", "参见", "导航菜单"
    }
    remove_selectors = (
        "script", "style", "noscript", ".mw-editsection", ".toc", ".catlinks",
        ".printfooter", ".reference", ".mw-references-wrap", ".thumb", ".gallery",
        ".navbox", ".vertical-navbox", ".metadata", ".noprint", ".mw-cite-backlink",
    )
    excluded_title_keywords = (
        "lostword", "大炮弹", "弹幕神乐", "play,doujin!", "comic market", "例大祭",
        "捏他列表", "branching paths", "啤酒", "黄昏酒场", "游戏为先还是酒为先",
        "二次创作以及使用规则",
    )
    excluded_category_keywords = ("现实人物", "同人画师", "授权商业二次创作手机游戏")

    def __init__(self, config, *, crawl_delay_ms: int = 400) -> None:
        super().__init__(config)
        self.crawl_delay_ms = max(0, int(crawl_delay_ms))

    def _normalize_entry(self, entry: str) -> str:
        raw = (entry or "东方Project").strip()
        if raw.startswith(("http://", "https://")):
            url = raw
        else:
            url = urljoin(self.base_url, quote(raw.lstrip("/"), safe="/()（）-_"))
        parsed = urlparse(url)
        if parsed.netloc.lower() not in self.allowed_hosts:
            raise ValueError("东方数据源只允许 thbwiki.cc / www.thbwiki.cc。")
        return parsed._replace(query="", fragment="").geturl()

    def _supported_link(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc.lower() not in self.allowed_hosts:
            return False
        path = unquote(parsed.path.strip("/"))
        if not path:
            return False
        if path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".mp3", ".ogg")):
            return False
        prefixes = (
            "Special:", "THBWiki:", "分类:", "文件:", "模板:", "帮助:", "用户:",
            "User:", "讨论:", "MediaWiki:",
        )
        if path.startswith(prefixes) or ":" in path:
            return False
        return True

    def _discover_links(self, url: str, html: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one(".mw-parser-output") or soup.select_one("#mw-content-text")
        if not root:
            return []
        links: list[str] = []
        seen: set[str] = set()
        for node in root.select("a[href]"):
            href = str(node.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            candidate = urlparse(urljoin(url, href))._replace(query="", fragment="").geturl()
            if self._supported_link(candidate) and candidate not in seen:
                seen.add(candidate)
                links.append(candidate)
        return links

    async def crawl(self, client: HttpClient, *, entry: str = "", limit: int = 40) -> list[RawDocument]:
        limit = max(1, min(int(limit), 500))
        queue = deque([self._normalize_entry(entry)])
        queued = set(queue)
        visited: set[str] = set()
        result: list[RawDocument] = []
        while queue and len(result) < limit:
            url = queue.popleft()
            queued.discard(url)
            if url in visited:
                continue
            visited.add(url)
            html = await client.get_text(url)
            soup = BeautifulSoup(html, "lxml")
            title_node = soup.select_one("#firstHeading")
            title = clean_inline_text(title_node.get_text(" ", strip=True) if title_node else unquote(urlparse(url).path.strip("/")))
            result.append(RawDocument(key=url, title=title or url, source_url=url, content=html, kind="touhou_wiki"))
            for link in self._discover_links(url, html):
                if link not in visited and link not in queued and len(visited) + len(queue) < limit * 4:
                    queue.append(link)
                    queued.add(link)
            if self.crawl_delay_ms:
                await asyncio.sleep(self.crawl_delay_ms / 1000)
        return result

    def _extract_categories(self, soup: BeautifulSoup) -> list[str]:
        categories: list[str] = []
        for link in soup.select(".catlinks a"):
            value = clean_inline_text(link.get_text(" ", strip=True))
            if value and value != "分类" and value not in categories:
                categories.append(value)
        return categories

    def _extract_block(self, tag: Tag) -> str:
        if tag.name in {"ul", "ol"}:
            return "\n".join(
                f"- {clean_inline_text(item.get_text(' ', strip=True))}"
                for item in tag.find_all("li", recursive=False)
                if clean_inline_text(item.get_text(" ", strip=True))
            )
        if tag.name == "table":
            lines: list[str] = []
            for row in tag.select("tr"):
                cells = [clean_inline_text(c.get_text(" ", strip=True)) for c in row.find_all(["th", "td"], recursive=False)]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(cells[0] if len(cells) == 1 else f"{cells[0]}: {' / '.join(cells[1:])}")
            return "\n".join(lines)
        return clean_inline_text(tag.get_text(" ", strip=True))

    def parse(self, raw: RawDocument, *, max_chars: int = 1200, overlap: int = 120) -> list[ParsedDocument]:
        soup = BeautifulSoup(raw.content, "lxml")
        title_node = soup.select_one("#firstHeading")
        title = clean_inline_text(title_node.get_text(" ", strip=True) if title_node else raw.title)
        categories = self._extract_categories(soup)
        normalized_title = title.lower()
        if any(k.lower() in normalized_title for k in self.excluded_title_keywords):
            return []
        normalized_categories = " ".join(categories).lower()
        if any(k.lower() in normalized_categories for k in self.excluded_category_keywords):
            return []

        container = soup.select_one(".mw-parser-output") or soup.select_one("#mw-content-text")
        if not container:
            return []
        working = BeautifulSoup(str(container), "lxml")
        root = working.select_one(".mw-parser-output") or working
        for selector in self.remove_selectors:
            for node in root.select(selector):
                node.decompose()

        sections: list[tuple[str, str]] = []
        heading = "概述"
        lines: list[str] = []
        for child in root.children:
            if isinstance(child, NavigableString) or not isinstance(child, Tag):
                continue
            if child.name in {"h2", "h3", "h4"}:
                new_heading = clean_inline_text(child.get_text(" ", strip=True))
                if new_heading in self.stop_sections:
                    break
                body = clean_inline_text("\n\n".join(lines))
                if body:
                    sections.append((heading, body))
                heading = new_heading or heading
                lines = []
                continue
            block = self._extract_block(child)
            if block:
                lines.append(block)
        body = clean_inline_text("\n\n".join(lines))
        if body:
            sections.append((heading, body))
        if not sections:
            fallback = clean_inline_text(root.get_text("\n", strip=True))
            if fallback:
                sections = [("正文", fallback)]

        chunks: list[str] = []
        for section, section_body in sections:
            chunks.extend(
                make_self_describing_chunks(
                    header_lines=[
                        "作品：东方Project",
                        f"词条：{title}",
                        f"章节：{section}",
                        f"来源：{raw.source_url}",
                        f"分类：{'、'.join(categories)}" if categories else "",
                    ],
                    body=section_body,
                    limit=max_chars,
                    overlap=overlap,
                )
            )
        if not chunks:
            return []
        # Preserve the original plugin's document naming so an existing Touhou KB is reused cleanly.
        return [ParsedDocument(doc_name=title, title=title, chunks=chunks, source_url=raw.source_url)]
