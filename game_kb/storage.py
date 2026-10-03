from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.core.knowledge_base.kb_helper import KBHelper
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider
from astrbot.core.star import Context

from .core import DomainConfig, ParsedDocument


class AstrBotKBStorage:
    """Shared storage layer backed entirely by AstrBot's native KB/RAG."""

    def __init__(
        self,
        context: Context,
        *,
        embedding_provider_id: str = "",
        rerank_provider_id: str = "",
        upload_batch_size: int = 4,
        upload_tasks_limit: int = 1,
        top_k: int = 5,
    ) -> None:
        self.context = context
        self.embedding_provider_id = embedding_provider_id.strip()
        self.rerank_provider_id = rerank_provider_id.strip()
        self.upload_batch_size = max(1, int(upload_batch_size))
        self.upload_tasks_limit = max(1, int(upload_tasks_limit))
        self.top_k = max(1, min(int(top_k), 20))
        self._write_lock = asyncio.Lock()

    async def _resolve_embedding_provider_id(self) -> str:
        if self.embedding_provider_id:
            provider = self.context.get_provider_by_id(self.embedding_provider_id)
            if not provider or not isinstance(provider, EmbeddingProvider):
                raise ValueError(
                    f"未找到可用的 Embedding Provider: {self.embedding_provider_id}"
                )
            return self.embedding_provider_id
        providers = self.context.get_all_embedding_providers()
        if not providers:
            raise ValueError("当前没有可用的 Embedding Provider，请先在 AstrBot 中配置 embedding 模型。")
        return providers[0].meta().id

    def _all_rerank_providers(self) -> list[RerankProvider]:
        provider_manager = getattr(self.context, "provider_manager", None)
        providers = getattr(provider_manager, "rerank_provider_insts", []) if provider_manager else []
        if not isinstance(providers, list):
            return []
        return [p for p in providers if isinstance(p, RerankProvider)]

    async def _resolve_rerank_provider_id(self) -> str | None:
        if self.rerank_provider_id:
            provider = self.context.get_provider_by_id(self.rerank_provider_id)
            if not provider or not isinstance(provider, RerankProvider):
                raise ValueError(
                    f"未找到可用的 Rerank Provider: {self.rerank_provider_id}"
                )
            return self.rerank_provider_id
        providers = self._all_rerank_providers()
        return providers[0].meta().id if providers else None

    async def ensure_kb(self, domain: DomainConfig) -> tuple[KBHelper, bool]:
        kb_manager = self.context.kb_manager
        kb_helper = await kb_manager.get_kb_by_name(domain.kb_name)
        if kb_helper:
            return kb_helper, False
        embedding_provider_id = await self._resolve_embedding_provider_id()
        rerank_provider_id = await self._resolve_rerank_provider_id()
        kb_helper = await kb_manager.create_kb(
            kb_name=domain.kb_name,
            description=domain.kb_description,
            emoji=domain.emoji,
            embedding_provider_id=embedding_provider_id,
            rerank_provider_id=rerank_provider_id,
            chunk_size=512,
            chunk_overlap=50,
            top_k_dense=50,
            top_k_sparse=50,
            top_m_final=self.top_k,
        )
        logger.info("游戏知识库已自动创建: domain=%s kb=%s", domain.domain_id, domain.kb_name)
        return kb_helper, True

    async def get_kb(self, domain: DomainConfig) -> KBHelper | None:
        return await self.context.kb_manager.get_kb_by_name(domain.kb_name)

    async def list_document_map(self, kb_helper: KBHelper) -> dict[str, str]:
        result: dict[str, str] = {}
        offset = 0
        limit = 500
        while True:
            docs = await kb_helper.list_documents(offset=offset, limit=limit)
            if not docs:
                break
            for doc in docs:
                result[doc.doc_name] = doc.doc_id
            if len(docs) < limit:
                break
            offset += limit
        return result

    async def upsert_documents(
        self,
        domain: DomainConfig,
        documents: list[ParsedDocument],
    ) -> tuple[int, int]:
        if not documents:
            return 0, 0
        kb_helper, _ = await self.ensure_kb(domain)
        existing = await self.list_document_map(kb_helper)
        imported = 0
        updated = 0
        async with self._write_lock:
            for doc in documents:
                old_doc_id = existing.get(doc.doc_name)
                if old_doc_id:
                    await kb_helper.delete_document(old_doc_id)
                    updated += 1
                else:
                    imported += 1
                new_doc = await kb_helper.upload_document(
                    file_name=doc.doc_name,
                    file_content=None,
                    file_type="txt",
                    batch_size=self.upload_batch_size,
                    tasks_limit=self.upload_tasks_limit,
                    max_retries=3,
                    pre_chunked_text=doc.chunks,
                )
                existing[doc.doc_name] = new_doc.doc_id
        return imported, updated

    async def retrieve(
        self,
        domain: DomainConfig,
        query: str,
        *,
        top_k: int | None = None,
    ) -> dict[str, Any] | None:
        kb_helper = await self.get_kb(domain)
        if not kb_helper:
            return None
        final_k = max(1, min(int(top_k or self.top_k), 20))
        return await self.context.kb_manager.retrieve(
            query=query,
            kb_names=[domain.kb_name],
            top_k_fusion=max(20, final_k * 4),
            top_m_final=final_k,
        )
