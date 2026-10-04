from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from ..core import (
    ParsedDocument,
    RawDocument,
    clean_inline_text,
    make_self_describing_chunks,
    stable_doc_name,
)
from ..http import HttpClient
from .base import BaseAdapter


class BlueArchiveAdapter(BaseAdapter):
    domain_id = "blue_archive"
    display_name = "Blue Archive"
    emoji = "🔷"
    default_kb_name = "Blue Archive知识库"
    default_kb_description = "Blue Archive / 蔚蓝档案的稳定角色资料与剧情文本知识"

    repo = "ba-archive/blue-archive"
    ref = "main"
    story_prefix = "apps/blue-archive-story-viewer/public/story/"
    students_path = "apps/blue-archive-story-editor/src/assets/students.json"

    upstream_repo = "electricgoat/ba-data"
    upstream_ref = "global"
    upstream_tables = tuple(
        (category.lower(), f"Excel/ScenarioScript{category}{index}ExcelTable.json")
        for category in ("Main", "Event", "Favor", "Group")
        for index in range(5, 0, -1)
    )

    gamekee_hosts = {"gamekee.com", "www.gamekee.com", "ba.gamekee.com"}

    def __init__(self, config) -> None:
        super().__init__(config)
        self._tree_cache: list[str] | None = None

    def bootstrap_complete(self, doc_names: set[str]) -> bool:
        return any(
            name.startswith(("story_", "upstream_", "gamekee_"))
            for name in doc_names
        )

    async def _tree(self, client: HttpClient) -> list[str]:
        if self._tree_cache is not None:
            return self._tree_cache
        url = (
            f"https://api.github.com/repos/{self.repo}/git/trees/"
            f"{self.ref}?recursive=1"
        )
        data = await client.get_json(url)
        self._tree_cache = [
            str(item.get("path") or "")
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]
        return self._tree_cache

    def _raw_url(self, path: str) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.repo}/{self.ref}/"
            f"{quote(path, safe='/')}"
        )

    def _upstream_raw_url(self, path: str) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.upstream_repo}/"
            f"{self.upstream_ref}/{quote(path, safe='/')}"
        )

    @classmethod
    def _is_gamekee_url(cls, value: str) -> bool:
        try:
            host = (urlparse(value).hostname or "").lower()
        except ValueError:
            return False
        return host in cls.gamekee_hosts

    async def _crawl_ba_archive(
        self,
        client: HttpClient,
        *,
        entry: str,
        limit: int,
        include_students: bool,
    ) -> list[RawDocument]:
        result: list[RawDocument] = []

        if include_students:
            students_url = self._raw_url(self.students_path)
            students_text = await client.get_text(students_url)
            result.append(
                RawDocument(
                    key=self.students_path,
                    title="Blue Archive 学生资料",
                    source_url=students_url,
                    content=students_text,
                    kind="ba_students",
                    extra={"path": self.students_path},
                )
            )
            if len(result) >= limit:
                return result

        paths = await self._tree(client)
        story_files = [
            p
            for p in paths
            if p.startswith(self.story_prefix)
            and p.endswith(".json")
            and "/ai/" not in p
        ]
        priority = {"main": 0, "other": 1, "event": 2, "favor": 3}
        story_files.sort(
            key=lambda p: (
                priority.get(
                    p[len(self.story_prefix) :].split("/", 1)[0],
                    9,
                ),
                p,
            )
        )

        if entry:
            probe = entry.lower().replace(" ", "")
            story_files = [
                p
                for p in story_files
                if probe in p.lower().replace(" ", "")
            ]

        remaining = max(0, limit - len(result))
        for path in story_files[:remaining]:
            url = self._raw_url(path)
            content = await client.get_text(url)
            rel = path[len(self.story_prefix) :]
            result.append(
                RawDocument(
                    key=path,
                    title=f"Blue Archive 剧情 {rel}",
                    source_url=url,
                    content=content,
                    kind="ba_story",
                    extra={"path": path, "relative_path": rel},
                )
            )
        return result

    async def _crawl_upstream(
        self,
        client: HttpClient,
        *,
        probe: str,
        limit: int,
    ) -> list[RawDocument]:
        result: list[RawDocument] = []
        numeric_probe = re.sub(r"\D+", "", probe or "")

        for category, path in self.upstream_tables:
            try:
                data = await client.get_json(self._upstream_raw_url(path))
            except Exception:
                continue

            rows = data.get("DataList") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue

            grouped: dict[str, list[dict]] = defaultdict(list)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                group_id = row.get("GroupId")
                if group_id is None:
                    continue
                key = str(group_id)
                if numeric_probe and numeric_probe not in key:
                    continue
                grouped[key].append(row)

            def group_sort_key(value: str):
                return (0, int(value)) if value.isdigit() else (1, value)

            for group_id in sorted(grouped, key=group_sort_key, reverse=True):
                source_url = self._upstream_raw_url(path)
                result.append(
                    RawDocument(
                        key=f"electricgoat:{category}:{group_id}",
                        title=f"Blue Archive 上游剧情 {category} {group_id}",
                        source_url=source_url,
                        content=json.dumps(
                            grouped[group_id],
                            ensure_ascii=False,
                        ),
                        kind="ba_upstream_story",
                        extra={
                            "category": category,
                            "group_id": group_id,
                            "table": path,
                        },
                    )
                )
                if len(result) >= limit:
                    return result

        return result

    async def _crawl_gamekee(
        self,
        client: HttpClient,
        *,
        url: str,
    ) -> list[RawDocument]:
        html = await client.get_text(url)
        return [
            RawDocument(
                key=f"gamekee:{url}",
                title="Blue Archive GameKee 页面",
                source_url=url,
                content=html,
                kind="ba_gamekee",
                extra={"url": url},
            )
        ]

    async def crawl(
        self,
        client: HttpClient,
        *,
        entry: str = "",
        limit: int = 40,
    ) -> list[RawDocument]:
        limit = max(1, min(int(limit), 500))
        entry = str(entry or "").strip()

        if self._is_gamekee_url(entry):
            return await self._crawl_gamekee(client, url=entry)

        lower = entry.lower()
        upstream_match = re.match(
            r"^(?:upstream|electricgoat|latest)(?::|\s)?(.*)$",
            lower,
        )
        if upstream_match:
            probe = upstream_match.group(1).strip()
            return await self._crawl_upstream(
                client,
                probe=probe,
                limit=limit,
            )

        result = await self._crawl_ba_archive(
            client,
            entry=entry,
            limit=limit,
            include_students=not entry,
        )
        if result:
            return result

        if entry and re.search(r"\d", entry):
            return await self._crawl_upstream(
                client,
                probe=entry,
                limit=limit,
            )
        return result

    @staticmethod
    def _strip_game_markup(text: str) -> str:
        value = clean_inline_text(text)
        value = value.replace("[USERNAME]老师", "老师")
        value = value.replace("[USERNAME]", "老师")
        value = re.sub(
            r"\[ruby=[^\]]+\](.*?)\[/ruby\]",
            r"\1",
            value,
        )
        value = re.sub(r"\[[0-9A-Fa-f]{6}\]", "", value)
        value = value.replace("[-]", "")
        return value.strip()

    @staticmethod
    def _story_payload(raw: RawDocument) -> tuple[list[dict], str | None]:
        try:
            data = json.loads(raw.content)
        except json.JSONDecodeError:
            return [], None

        group_id = None
        rows = None
        if isinstance(data, dict):
            group_id = data.get("GroupId")
            rows = data.get("content")
            if not isinstance(rows, list):
                rows = data.get("DataList")
        elif isinstance(data, list):
            rows = data

        if not isinstance(rows, list):
            return [], str(group_id) if group_id is not None else None
        clean_rows = [row for row in rows if isinstance(row, dict)]
        return clean_rows, str(group_id) if group_id is not None else None

    @staticmethod
    def _row_story_text(row: dict) -> tuple[str, str]:
        for key, language in (
            ("TextCn", "简体中文"),
            ("TextTw", "繁体中文"),
            ("TextJp", "日文"),
            ("TextEn", "英文"),
        ):
            value = row.get(key)
            if value:
                return str(value), language
        return "", ""

    def _parse_students(self, raw: RawDocument) -> list[ParsedDocument]:
        try:
            rows = json.loads(raw.content)
        except json.JSONDecodeError:
            return []
        chunks: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            family = row.get("familyName") or {}
            name = row.get("name") or {}
            cn_full = (
                f"{family.get('cn', '')}{name.get('cn', '')}".strip()
                or str(name.get("en") or row.get("id") or "未知")
            )
            jp_full = f"{family.get('jp', '')}{name.get('jp', '')}".strip()
            en_full = f"{family.get('en', '')} {name.get('en', '')}".strip()
            birthday = row.get("birthday") or {}
            lines = [
                "作品：Blue Archive / 蔚蓝档案",
                "内容类型：学生资料",
                f"学生ID：{row.get('id', '-')}",
                f"中文名：{cn_full}",
                f"日文名：{jp_full}" if jp_full else "",
                f"英文名：{en_full}" if en_full else "",
                f"所属学校：{row.get('affiliation', '-')}",
                f"社团：{row.get('club', '-')}",
                f"生日：{birthday.get('month', '-')}月{birthday.get('day', '-')}日",
                f"稀有度：{row.get('rarity', '-')}",
                f"位置类型：{row.get('type', '-')}",
                f"攻击类型：{row.get('bulletType', '-')}",
                f"防御类型：{row.get('armorType', '-')}",
                f"武器：{row.get('weapon', '-')}",
            ]
            nicknames = row.get("nickname") or []
            if nicknames:
                lines.append("别名：" + "、".join(map(str, nicknames)))
            chunks.append("\n".join(line for line in lines if line))
        if not chunks:
            return []
        return [
            ParsedDocument(
                doc_name="students_catalog.txt",
                title="Blue Archive 学生资料",
                chunks=chunks,
                source_url=raw.source_url,
            )
        ]

    def _parse_story(
        self,
        raw: RawDocument,
        max_chars: int,
        overlap: int,
    ) -> list[ParsedDocument]:
        rows, top_group_id = self._story_payload(raw)
        if not rows:
            return []

        texts: list[str] = []
        group_id = str(raw.extra.get("group_id") or "") or top_group_id or None
        language = ""
        for row in rows:
            group_id = group_id or (
                str(row.get("GroupId"))
                if row.get("GroupId") is not None
                else None
            )
            value, row_language = self._row_story_text(row)
            text = self._strip_game_markup(value)
            if text:
                texts.append(text)
                language = language or row_language

        if not texts:
            return []

        if raw.kind == "ba_upstream_story":
            category = str(raw.extra.get("category") or "story")
            source_label = (
                f"electricgoat/ba-data@{self.upstream_ref} "
                f"{raw.extra.get('table', '')}"
            ).strip()
            doc_key = f"upstream_{category}_{group_id or raw.key}"
        else:
            rel = str(raw.extra.get("relative_path") or raw.key)
            category = rel.split("/", 1)[0] if "/" in rel else "story"
            source_label = rel
            doc_key = f"story_{re.sub(r'[^0-9A-Za-z_\-]+', '_', rel)}"

        chunks = make_self_describing_chunks(
            header_lines=[
                "作品：Blue Archive / 蔚蓝档案",
                f"内容类型：剧情（{category}）",
                f"剧情GroupId：{group_id}" if group_id else "",
                f"文本语言：{language}" if language else "",
                f"源文件：{source_label}",
                f"来源：{raw.source_url}",
            ],
            body="\n".join(texts),
            limit=max_chars,
            overlap=overlap,
        )
        if not chunks:
            return []

        return [
            ParsedDocument(
                doc_name=stable_doc_name(doc_key),
                title=raw.title,
                chunks=chunks,
                source_url=raw.source_url,
            )
        ]

    def _parse_gamekee(
        self,
        raw: RawDocument,
        max_chars: int,
        overlap: int,
    ) -> list[ParsedDocument]:
        soup = BeautifulSoup(raw.content, "lxml")
        for node in soup(
            ["script", "style", "noscript", "nav", "footer", "form", "svg"]
        ):
            node.decompose()

        title_node = soup.find("h1")
        title = (
            clean_inline_text(title_node.get_text(" ", strip=True))
            if title_node
            else ""
        )
        if not title and soup.title:
            title = clean_inline_text(soup.title.get_text(" ", strip=True))
        title = title or "GameKee BA 页面"

        content_node = None
        for selector in (
            "article",
            ".article-content",
            ".wiki-content",
            ".detail-content",
            ".content",
            "#content",
        ):
            candidate = soup.select_one(selector)
            if candidate and len(candidate.get_text(" ", strip=True)) >= 120:
                content_node = candidate
                break
        content_node = content_node or soup.body or soup

        lines: list[str] = []
        last = None
        for line in content_node.get_text("\n", strip=True).splitlines():
            cleaned = clean_inline_text(line)
            if not cleaned or cleaned == last:
                continue
            if cleaned in {"登录", "注册", "编辑", "评论"}:
                continue
            lines.append(cleaned)
            last = cleaned

        chunks = make_self_describing_chunks(
            header_lines=[
                "作品：Blue Archive / 蔚蓝档案",
                "内容类型：GameKee Wiki 补充页面",
                f"页面标题：{title}",
                f"来源：{raw.source_url}",
            ],
            body="\n".join(lines),
            limit=max_chars,
            overlap=overlap,
        )
        if not chunks:
            return []

        path_key = (urlparse(raw.source_url).path or "page").strip("/") or "page"
        return [
            ParsedDocument(
                doc_name=stable_doc_name(f"gamekee_{path_key}"),
                title=title,
                chunks=chunks,
                source_url=raw.source_url,
            )
        ]

    def parse(
        self,
        raw: RawDocument,
        *,
        max_chars: int = 1200,
        overlap: int = 120,
    ) -> list[ParsedDocument]:
        if raw.kind == "ba_students":
            return self._parse_students(raw)
        if raw.kind in {"ba_story", "ba_upstream_story"}:
            return self._parse_story(raw, max_chars, overlap)
        if raw.kind == "ba_gamekee":
            return self._parse_gamekee(raw, max_chars, overlap)
        return []
