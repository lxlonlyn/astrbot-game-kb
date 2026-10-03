from __future__ import annotations

from astrbot.api import logger
from astrbot.core.star import Context

from .core import ParsedDocument, clean_inline_text


class OptionalLLMCleaner:
    """Optional post-parser text cleaner using an AstrBot chat provider.

    Disabled by default. The parser output is already suitable for RAG; this is
    only a fallback for noisy sources/adapters added later.
    """

    def __init__(
        self,
        context: Context,
        *,
        enabled: bool = False,
        provider_id: str = "",
        max_chars_per_call: int = 5000,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.provider_id = provider_id.strip()
        self.max_chars_per_call = max(1000, int(max_chars_per_call))

    async def _provider(self):
        if self.provider_id:
            return self.context.get_provider_by_id(self.provider_id)
        return await self.context.get_using_provider_async()

    async def clean_document(self, document: ParsedDocument) -> ParsedDocument:
        if not self.enabled:
            return document
        provider = await self._provider()
        if not provider:
            logger.warning("已启用知识清洗，但当前没有可用的聊天 Provider，跳过清洗。")
            return document

        cleaned_chunks: list[str] = []
        system_prompt = (
            "你是知识库文本清洗器。只清理输入中的网页噪声、重复导航、乱码和无意义控制文本；"
            "必须保留事实、专有名词、角色名、数字ID、章节信息、原始语义和来源提示。"
            "不要总结，不要补充外部知识，不要改变事实，不要输出解释。只输出清洗后的正文。"
        )
        for chunk in document.chunks:
            if len(chunk) > self.max_chars_per_call:
                cleaned_chunks.append(chunk)
                continue
            try:
                response = await provider.text_chat(
                    prompt=f"请清洗下面这段知识库文本：\n\n{chunk}",
                    system_prompt=system_prompt,
                )
                text = clean_inline_text(getattr(response, "completion_text", ""))
                cleaned_chunks.append(text or chunk)
            except Exception as exc:
                logger.warning("知识库 LLM 清洗失败，回退原文本: %s", exc)
                cleaned_chunks.append(chunk)
        return ParsedDocument(
            doc_name=document.doc_name,
            title=document.title,
            chunks=cleaned_chunks,
            source_url=document.source_url,
        )
