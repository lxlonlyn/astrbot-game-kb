from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RawDocument:
    """A source document returned by a crawler before domain parsing."""

    key: str
    title: str
    source_url: str
    content: str
    kind: str = "text"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocument:
    """A flat text document ready to be stored in AstrBot's native KB."""

    doc_name: str
    title: str
    chunks: list[str]
    source_url: str


@dataclass(slots=True)
class SyncTask:
    task_id: str
    domain_id: str
    entry: str
    limit: int
    status: str = "pending"
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DomainConfig:
    domain_id: str
    display_name: str
    kb_name: str
    kb_description: str
    emoji: str
    enabled: bool
    keywords: tuple[str, ...]
    bootstrap_limit: int


def normalize_for_match(text: str | None) -> str:
    return "".join(str(text or "").strip().lower().split())


def contains_any(text: str | None, keywords: tuple[str, ...] | list[str]) -> bool:
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return any(normalize_for_match(keyword) in normalized for keyword in keywords if keyword)


def clean_inline_text(text: str | None) -> str:
    value = str(text or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def hard_split_text(text: str, limit: int = 1200, overlap: int = 120) -> list[str]:
    text = clean_inline_text(text)
    if not text:
        return []
    limit = max(300, int(limit))
    overlap = max(0, min(int(overlap), limit // 3))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            split_points = [
                text.rfind("\n\n", start, end),
                text.rfind("\n", start, end),
                text.rfind("。", start, end),
                text.rfind("！", start, end),
                text.rfind("？", start, end),
                text.rfind("；", start, end),
            ]
            best = max(split_points)
            if best > start + int(limit * 0.55):
                end = best + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def make_self_describing_chunks(
    *,
    header_lines: list[str],
    body: str,
    limit: int = 1200,
    overlap: int = 120,
) -> list[str]:
    header = "\n".join(line.strip() for line in header_lines if line and line.strip())
    available = max(300, limit - len(header) - 2)
    parts = hard_split_text(body, available, overlap)
    if not parts:
        return []
    if not header:
        return parts
    return [f"{header}\n\n{part}" for part in parts]


def stable_doc_name(value: str, *, suffix: str = ".txt", max_len: int = 240) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    value = value.strip("._") or "document"
    if suffix and not value.endswith(suffix):
        value += suffix
    if len(value) > max_len:
        keep = max_len - len(suffix)
        value = value[:keep].rstrip("._") + suffix
    return value
