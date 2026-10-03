from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import quote

from ..core import ParsedDocument, RawDocument, clean_inline_text, make_self_describing_chunks, stable_doc_name
from ..http import HttpClient
from .base import BaseAdapter


class ProjectSekaiAdapter(BaseAdapter):
    domain_id = "pjsk"
    display_name = "Project SEKAI"
    emoji = "🎵"
    default_kb_name = "Project SEKAI知识库"
    default_kb_description = "Project SEKAI / 世界计划的稳定角色、歌曲与剧情文本知识"

    story_repo = "StarMoe-org/Moe-story"
    story_ref = "main"
    master_repo = "Sekai-World/sekai-master-db-cn-diff"
    master_ref = "main"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._tree_cache: list[str] | None = None

    def _raw_url(self, repo: str, ref: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(path, safe='/')}"

    async def _story_tree(self, client: HttpClient) -> list[str]:
        if self._tree_cache is not None:
            return self._tree_cache
        url = f"https://api.github.com/repos/{self.story_repo}/git/trees/{self.story_ref}?recursive=1"
        data = await client.get_json(url)
        self._tree_cache = [
            str(item.get("path") or "")
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]
        return self._tree_cache

    async def _fetch_master_characters(self, client: HttpClient) -> RawDocument:
        chars_url = self._raw_url(self.master_repo, self.master_ref, "gameCharacters.json")
        profiles_url = self._raw_url(self.master_repo, self.master_ref, "characterProfiles.json")
        chars, profiles = await client.get_text(chars_url), await client.get_text(profiles_url)
        content = json.dumps({"characters": json.loads(chars), "profiles": json.loads(profiles)}, ensure_ascii=False)
        return RawDocument(
            key="master:characters",
            title="Project SEKAI 角色资料",
            source_url=profiles_url,
            content=content,
            kind="pjsk_master_characters",
            extra={"characters_url": chars_url, "profiles_url": profiles_url},
        )

    async def _fetch_master_musics(self, client: HttpClient) -> RawDocument:
        url = self._raw_url(self.master_repo, self.master_ref, "musics.json")
        return RawDocument(
            key="master:musics",
            title="Project SEKAI 歌曲资料",
            source_url=url,
            content=await client.get_text(url),
            kind="pjsk_master_musics",
        )

    async def crawl(self, client: HttpClient, *, entry: str = "", limit: int = 40) -> list[RawDocument]:
        limit = max(1, min(int(limit), 500))
        entry = str(entry or "").strip()
        result: list[RawDocument] = []

        # Compact stable master data first. We deliberately do not ingest current gacha/event state.
        result.append(await self._fetch_master_characters(client))
        if len(result) < limit:
            result.append(await self._fetch_master_musics(client))

        fixed = ["worldview.txt", "character_nicknames.yaml", "story/event/event_map.csv"]
        for path in fixed:
            if len(result) >= limit:
                return result
            url = self._raw_url(self.story_repo, self.story_ref, path)
            try:
                content = await client.get_text(url)
            except Exception:
                continue
            kind = "pjsk_event_map" if path.endswith(".csv") else "pjsk_text"
            result.append(RawDocument(key=path, title=f"Project SEKAI {path}", source_url=url, content=content, kind=kind, extra={"path": path}))

        if len(result) >= limit:
            return result

        paths = await self._story_tree(client)
        candidates = [
            p for p in paths
            if (
                p.startswith("story/self/") and p.endswith(".txt")
                or p.startswith("story/unit/") and p.endswith(".txt")
                or p.startswith("story/event/") and p.endswith("/detail.json")
                or p.startswith("story/special/") and p.endswith(".txt")
                or p.startswith("story/card/") and p.endswith(".txt")
            )
        ]
        def priority(path: str):
            if path.startswith("story/self/"):
                return (0, path)
            if path.startswith("story/unit/"):
                return (1, path)
            if path.startswith("story/event/"):
                return (2, path)
            if path.startswith("story/special/"):
                return (3, path)
            return (4, path)
        candidates.sort(key=priority)

        if entry:
            # Path/id based expansion is deterministic and does not require GitHub code-search authentication.
            probe = entry.lower().strip().replace("\\", "/")
            matched = [p for p in candidates if probe in p.lower()]
            if probe.isdigit():
                exactish = [p for p in candidates if f"/{probe}.txt" in p or f"/{probe}/" in p]
                matched = exactish or matched
            if matched:
                candidates = matched

        for path in candidates[: max(0, limit - len(result))]:
            url = self._raw_url(self.story_repo, self.story_ref, path)
            content = await client.get_text(url)
            kind = "pjsk_event_detail" if path.endswith("detail.json") else "pjsk_text"
            result.append(RawDocument(key=path, title=f"Project SEKAI {path}", source_url=url, content=content, kind=kind, extra={"path": path}))
        return result

    def _parse_characters(self, raw: RawDocument) -> list[ParsedDocument]:
        try:
            data = json.loads(raw.content)
            chars = data.get("characters") or []
            profiles = data.get("profiles") or []
        except (json.JSONDecodeError, AttributeError):
            return []
        char_map = {row.get("id"): row for row in chars if isinstance(row, dict)}
        profile_map = {row.get("characterId"): row for row in profiles if isinstance(row, dict)}
        ids = sorted(set(char_map) | set(profile_map), key=lambda v: (v is None, v))
        chunks: list[str] = []
        unit_names = {
            "light_sound": "Leo/need", "idol": "MORE MORE JUMP!", "street": "Vivid BAD SQUAD",
            "theme_park": "Wonderlands×Showtime", "school_refusal": "25时，在Nightcord。",
            "piapro": "VIRTUAL SINGER",
        }
        for cid in ids:
            c = char_map.get(cid, {})
            p = profile_map.get(cid, {})
            if not c and not p:
                continue
            full_name = f"{c.get('firstName', '')}{c.get('givenName', '')}".strip() or f"角色{cid}"
            lines = [
                "作品：Project SEKAI / 世界计划",
                "内容类型：角色资料",
                f"角色ID：{cid}",
                f"姓名：{full_name}",
                f"英文名：{c.get('firstNameEnglish', '')} {c.get('givenNameEnglish', '')}".strip(),
                f"组合：{unit_names.get(c.get('unit'), c.get('unit', '-'))}",
                f"CV：{p.get('characterVoice', '-')}",
                f"生日：{p.get('birthday', '-')}",
                f"身高：{p.get('height', c.get('height', '-'))}",
                f"学校：{p.get('school', '-')}",
                f"年级：{p.get('schoolYear', '-')}",
                f"爱好：{p.get('hobby', '-')}",
                f"特长：{p.get('specialSkill', '-')}",
                f"喜欢的食物：{p.get('favoriteFood', '-')}",
                f"不喜欢的食物：{p.get('hatedFood', '-')}",
                f"不擅长：{p.get('weak', '-')}",
                f"角色介绍：{clean_inline_text(p.get('introduction', ''))}",
            ]
            chunks.append("\n".join(line for line in lines if line and not line.endswith("：")))
        if not chunks:
            return []
        return [ParsedDocument(doc_name="characters_catalog.txt", title="Project SEKAI 角色资料", chunks=chunks, source_url=raw.source_url)]

    def _parse_musics(self, raw: RawDocument) -> list[ParsedDocument]:
        try:
            rows = json.loads(raw.content)
        except json.JSONDecodeError:
            return []
        chunks: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            infos = row.get("infos") or []
            info = infos[0] if infos and isinstance(infos[0], dict) else {}
            lines = [
                "作品：Project SEKAI / 世界计划",
                "内容类型：歌曲资料",
                f"歌曲ID：{row.get('id', '-')}",
                f"标题：{row.get('title', '-')}",
                f"读音：{row.get('pronunciation', '-')}",
                f"作者/创作者：{info.get('creator', '-')}",
                f"作词：{row.get('lyricist') or info.get('lyricist') or '-'}",
                f"作曲：{row.get('composer') or info.get('composer') or '-'}",
                f"编曲：{row.get('arranger') or info.get('arranger') or '-'}",
                f"是否原创曲：{'是' if row.get('isNewlyWrittenMusic') else '否'}",
            ]
            chunks.append("\n".join(lines))
        if not chunks:
            return []
        return [ParsedDocument(doc_name="musics_catalog.txt", title="Project SEKAI 歌曲资料", chunks=chunks, source_url=raw.source_url)]

    def _parse_event_detail(self, raw: RawDocument) -> list[ParsedDocument]:
        try:
            data = json.loads(raw.content)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        lines = [
            "作品：Project SEKAI / 世界计划",
            "内容类型：活动剧情摘要",
            f"活动ID：{data.get('event_id', '-')}",
            f"活动标题（日文）：{data.get('title_jp', '-')}",
            f"活动标题（中文）：{data.get('title_cn', '-')}",
            f"活动大纲：{clean_inline_text(data.get('outline_cn') or data.get('outline_jp') or '')}",
            f"活动总结：{clean_inline_text(data.get('summary_cn') or '')}",
        ]
        for chapter in data.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            lines.append(
                f"第{chapter.get('chapter_no', '-')}话 {chapter.get('title_cn') or chapter.get('title_jp') or ''}："
                f"{clean_inline_text(chapter.get('summary_cn') or '')}"
            )
        body = "\n".join(lines)
        path = str(raw.extra.get("path") or raw.key)
        return [ParsedDocument(
            doc_name=stable_doc_name(path.replace("/", "_")),
            title=data.get("title_cn") or data.get("title_jp") or raw.title,
            chunks=[body],
            source_url=raw.source_url,
        )]

    def _parse_event_map(self, raw: RawDocument) -> list[ParsedDocument]:
        reader = csv.DictReader(io.StringIO(raw.content))
        chunks: list[str] = []
        for row in reader:
            event_id = row.get("id") or row.get("eventId") or "-"
            name = row.get("name") or row.get("title") or "-"
            label = row.get("boxLabel") or ""
            chunks.append(
                "\n".join([
                    "作品：Project SEKAI / 世界计划",
                    "内容类型：活动索引",
                    f"活动ID：{event_id}",
                    f"活动名：{name}",
                    f"活动别名：{label}" if label else "",
                ]).strip()
            )
        if not chunks:
            return []
        return [ParsedDocument(doc_name="event_map.txt", title="Project SEKAI 活动索引", chunks=chunks, source_url=raw.source_url)]

    def _parse_text(self, raw: RawDocument, max_chars: int, overlap: int) -> list[ParsedDocument]:
        body = clean_inline_text(raw.content)
        if not body:
            return []
        path = str(raw.extra.get("path") or raw.key)
        first_line = body.splitlines()[0][:160] if body.splitlines() else raw.title
        chunks = make_self_describing_chunks(
            header_lines=[
                "作品：Project SEKAI / 世界计划",
                f"资料：{first_line}",
                f"源文件：{path}",
                f"来源：{raw.source_url}",
            ],
            body=body,
            limit=max_chars,
            overlap=overlap,
        )
        if not chunks:
            return []
        return [ParsedDocument(
            doc_name=stable_doc_name(path.replace("/", "_")),
            title=first_line,
            chunks=chunks,
            source_url=raw.source_url,
        )]

    def parse(self, raw: RawDocument, *, max_chars: int = 1200, overlap: int = 120) -> list[ParsedDocument]:
        if raw.kind == "pjsk_master_characters":
            return self._parse_characters(raw)
        if raw.kind == "pjsk_master_musics":
            return self._parse_musics(raw)
        if raw.kind == "pjsk_event_detail":
            return self._parse_event_detail(raw)
        if raw.kind == "pjsk_event_map":
            return self._parse_event_map(raw)
        if raw.kind == "pjsk_text":
            return self._parse_text(raw, max_chars, overlap)
        return []
