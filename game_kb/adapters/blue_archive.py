from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..core import ParsedDocument, RawDocument, clean_inline_text, make_self_describing_chunks, stable_doc_name
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

    def __init__(self, config) -> None:
        super().__init__(config)
        self._tree_cache: list[str] | None = None

    async def _tree(self, client: HttpClient) -> list[str]:
        if self._tree_cache is not None:
            return self._tree_cache
        url = f"https://api.github.com/repos/{self.repo}/git/trees/{self.ref}?recursive=1"
        data = await client.get_json(url)
        self._tree_cache = [
            str(item.get("path") or "")
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]
        return self._tree_cache

    def _raw_url(self, path: str) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.ref}/{quote(path, safe='/')}"

    async def crawl(self, client: HttpClient, *, entry: str = "", limit: int = 40) -> list[RawDocument]:
        limit = max(1, min(int(limit), 500))
        entry = str(entry or "").strip()
        result: list[RawDocument] = []

        # Stable student catalogue: deliberately excludes banners, raids, rankings and other live state.
        students_text = await client.get_text(self._raw_url(self.students_path))
        result.append(RawDocument(
            key=self.students_path,
            title="Blue Archive 学生资料",
            source_url=self._raw_url(self.students_path),
            content=students_text,
            kind="ba_students",
            extra={"path": self.students_path},
        ))
        if len(result) >= limit:
            return result

        paths = await self._tree(client)
        story_files = [
            p for p in paths
            if p.startswith(self.story_prefix)
            and p.endswith(".json")
            and "/ai/" not in p
        ]
        priority = {"main": 0, "other": 1, "event": 2, "favor": 3}
        story_files.sort(key=lambda p: (priority.get(p[len(self.story_prefix):].split("/", 1)[0], 9), p))

        if entry:
            probe = entry.lower().replace(" ", "")
            matched = [p for p in story_files if probe in p.lower().replace(" ", "")]
            if matched:
                story_files = matched

        for path in story_files[: max(0, limit - len(result))]:
            url = self._raw_url(path)
            content = await client.get_text(url)
            rel = path[len(self.story_prefix):]
            result.append(RawDocument(
                key=path,
                title=f"Blue Archive 剧情 {rel}",
                source_url=url,
                content=content,
                kind="ba_story",
                extra={"path": path, "relative_path": rel},
            ))
        return result

    @staticmethod
    def _strip_game_markup(text: str) -> str:
        value = clean_inline_text(text)
        value = re.sub(r"\[[0-9A-Fa-f]{6}\]", "", value)
        value = value.replace("[-]", "")
        value = value.replace("[USERNAME]老师", "老师")
        value = value.replace("[USERNAME]", "老师")
        return value.strip()

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
            cn_full = f"{family.get('cn', '')}{name.get('cn', '')}".strip() or str(name.get("en") or row.get("id") or "未知")
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
        return [ParsedDocument(
            doc_name="students_catalog.txt",
            title="Blue Archive 学生资料",
            chunks=chunks,
            source_url=raw.source_url,
        )]

    def _parse_story(self, raw: RawDocument, max_chars: int, overlap: int) -> list[ParsedDocument]:
        try:
            rows = json.loads(raw.content)
        except json.JSONDecodeError:
            return []
        if not isinstance(rows, list):
            return []
        texts: list[str] = []
        group_id = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_id = group_id or row.get("GroupId")
            text = self._strip_game_markup(row.get("TextCn") or "")
            if text:
                texts.append(text)
        if not texts:
            return []
        rel = str(raw.extra.get("relative_path") or raw.key)
        category = rel.split("/", 1)[0] if "/" in rel else "story"
        body = "\n".join(texts)
        chunks = make_self_describing_chunks(
            header_lines=[
                "作品：Blue Archive / 蔚蓝档案",
                f"内容类型：剧情（{category}）",
                f"剧情GroupId：{group_id}" if group_id else "",
                f"源文件：{rel}",
                f"来源：{raw.source_url}",
            ],
            body=body,
            limit=max_chars,
            overlap=overlap,
        )
        if not chunks:
            return []
        key = re.sub(r"[^0-9A-Za-z_\-]+", "_", rel)
        return [ParsedDocument(
            doc_name=stable_doc_name(f"story_{key}"),
            title=f"Blue Archive 剧情 {rel}",
            chunks=chunks,
            source_url=raw.source_url,
        )]

    def parse(self, raw: RawDocument, *, max_chars: int = 1200, overlap: int = 120) -> list[ParsedDocument]:
        if raw.kind == "ba_students":
            return self._parse_students(raw)
        if raw.kind == "ba_story":
            return self._parse_story(raw, max_chars, overlap)
        return []
