from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .game_kb.adapters import BlueArchiveAdapter, ProjectSekaiAdapter, TouhouAdapter
from .game_kb.cleaning import OptionalLLMCleaner
from .game_kb.core import DomainConfig, SyncTask
from .game_kb.http import HttpClient, HttpSettings
from .game_kb.storage import AstrBotKBStorage


PERSONA_BINDINGS_KEY = "game_kb_persona_bindings_v1"
MAX_INJECT_CHARS_PER_DOMAIN = 3500

TOUHOU_KEYWORDS = (
    "东方project", "东方", "touhou", "thbwiki", "幻想乡", "灵梦", "博丽灵梦", "魔理沙",
    "雾雨魔理沙", "咲夜", "十六夜咲夜", "妖梦", "魂魄妖梦", "幽幽子", "琪露诺",
    "蕾米莉亚", "芙兰朵露", "八云紫", "古明地觉", "古明地恋", "妹红", "铃仙",
    "早苗", "红魔馆", "永远亭", "守矢神社", "白玉楼", "香霖堂", "红魔乡", "妖妖梦",
    "永夜抄", "风神录", "地灵殿", "星莲船", "神灵庙", "辉针城", "绀珠传", "天空璋",
    "鬼形兽", "虹龙洞", "兽王园", "符卡",
)

BLUE_ARCHIVE_KEYWORDS = (
    "blue archive", "bluearchive", "蔚蓝档案", "碧蓝档案", "ブルーアーカイブ", "基沃托斯",
    "阿洛娜", "arona", "普拉娜", "plana", "白子", "砂狼白子", "星野", "小鸟游星野",
    "爱露", "日奈", "圣三一", "格黑娜", "千年科技学院", "千年", "阿比多斯", "夏莱",
    "什亭之匣", "总力战",
    # 常见学校 / 社团名也必须能触发路由；否则“补习部有哪些成员”这类自然问题
    # 在没有显式提到游戏名时不会进入 BA 知识库。
    "补习部", "補習授業部", "补习授业部", "对策委员会", "便利屋68", "风纪委员会",
    "美食研究会", "游戏开发部", "真理部", "正义实现委员会", "茶会", "修女会",
    "救护骑士团", "放学后甜点部", "百鬼夜行", "山海经", "红冬", "srt", "瓦尔基里",
    "阿里乌斯", "联邦学生会",
)

PJSK_KEYWORDS = (
    "project sekai", "projectsekai", "pjsk", "世界计划", "世界計畫", "プロセカ",
    "leo/need", "more more jump", "vivid bad squad", "wonderlands×showtime", "wonderlands x showtime",
    "25时", "nightcord", "星乃一歌", "天马咲希", "望月穗波", "日野森志步", "花里实乃理",
    "桐谷遥", "桃井爱莉", "日野森雫", "小豆泽心羽", "白石杏", "东云彰人", "青柳冬弥",
    "天马司", "凤笑梦", "草薙宁宁", "神代类", "宵崎奏", "朝比奈真冬", "东云绘名", "晓山瑞希",
)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.http = HttpClient(
            HttpSettings(
                timeout_sec=self._cfg_float("request_timeout_sec", 20.0),
                user_agent=self._cfg_str(
                    "http_user_agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                ),
                proxy=self._cfg_str("http_proxy", ""),
            )
        )
        self.domains = self._build_domains()
        self.adapters = {
            "touhou": TouhouAdapter(
                self.domains["touhou"],
                crawl_delay_ms=self._cfg_int("crawl_delay_ms", 400),
            ),
            "blue_archive": BlueArchiveAdapter(self.domains["blue_archive"]),
            "pjsk": ProjectSekaiAdapter(self.domains["pjsk"]),
        }
        self.storage = AstrBotKBStorage(
            context,
            embedding_provider_id=self._cfg_str("default_embedding_provider_id", ""),
            rerank_provider_id=self._cfg_str("default_rerank_provider_id", ""),
            upload_batch_size=self._cfg_int("upload_batch_size", 4),
            upload_tasks_limit=self._cfg_int("upload_tasks_limit", 1),
            top_k=self._cfg_int("session_top_k", 5),
        )
        self.cleaner = OptionalLLMCleaner(
            context,
            enabled=self._cfg_bool("enable_llm_cleaning", False),
            provider_id=self._cfg_str("cleaning_provider_id", ""),
        )
        self.tasks: dict[str, SyncTask] = {}
        self.running_jobs: dict[str, asyncio.Task] = {}
        self._bootstrap_job: asyncio.Task | None = None

    def _cfg_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return str(value).strip() if value is not None else default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_extra_keywords(self, key: str) -> tuple[str, ...]:
        raw = str(self.config.get(key, "") or "")
        return tuple(part.strip() for part in re.split(r"[\n,，;；]+", raw) if part.strip())

    def _build_domains(self) -> dict[str, DomainConfig]:
        return {
            "touhou": DomainConfig(
                domain_id="touhou",
                display_name="东方Project",
                kb_name=self._cfg_str("touhou_kb_name", TouhouAdapter.default_kb_name),
                kb_description=TouhouAdapter.default_kb_description,
                emoji=TouhouAdapter.emoji,
                enabled=self._cfg_bool("enable_touhou", True),
                keywords=TOUHOU_KEYWORDS + self._cfg_extra_keywords("touhou_extra_keywords"),
                bootstrap_limit=max(1, min(self._cfg_int("touhou_bootstrap_limit", 40), 500)),
            ),
            "blue_archive": DomainConfig(
                domain_id="blue_archive",
                display_name="Blue Archive",
                kb_name=self._cfg_str("ba_kb_name", BlueArchiveAdapter.default_kb_name),
                kb_description=BlueArchiveAdapter.default_kb_description,
                emoji=BlueArchiveAdapter.emoji,
                enabled=self._cfg_bool("enable_blue_archive", True),
                keywords=BLUE_ARCHIVE_KEYWORDS + self._cfg_extra_keywords("ba_extra_keywords"),
                bootstrap_limit=max(1, min(self._cfg_int("ba_bootstrap_limit", 40), 500)),
            ),
            "pjsk": DomainConfig(
                domain_id="pjsk",
                display_name="Project SEKAI",
                kb_name=self._cfg_str("pjsk_kb_name", ProjectSekaiAdapter.default_kb_name),
                kb_description=ProjectSekaiAdapter.default_kb_description,
                emoji=ProjectSekaiAdapter.emoji,
                enabled=self._cfg_bool("enable_pjsk", True),
                keywords=PJSK_KEYWORDS + self._cfg_extra_keywords("pjsk_extra_keywords"),
                bootstrap_limit=max(1, min(self._cfg_int("pjsk_bootstrap_limit", 40), 500)),
            ),
        }

    def _resolve_domain_id(self, raw: str) -> str | None:
        value = str(raw or "").strip().lower().replace("-", "_")
        aliases = {
            "touhou": "touhou", "东方": "touhou", "th": "touhou",
            "ba": "blue_archive", "blue_archive": "blue_archive", "bluearchive": "blue_archive",
            "蔚蓝档案": "blue_archive", "碧蓝档案": "blue_archive",
            "pjsk": "pjsk", "projectsekai": "pjsk", "project_sekai": "pjsk", "世界计划": "pjsk",
        }
        return aliases.get(value)

    def _sanitize_query(self, text: str | None) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"<Mnemosyne>.*?</Mnemosyne>", " ", cleaned, flags=re.S)
        cleaned = re.sub(r"<system_reminder>.*?</system_reminder>", " ", cleaned, flags=re.S)
        cleaned = re.sub(r"\[Image Attachment:[^\]]+\]", " ", cleaned)
        # Strip tags but keep their inner text, including any image caption produced upstream.
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:600]

    async def _get_persona_bindings(self) -> dict[str, list[str]]:
        raw = await self.get_kv_data(PERSONA_BINDINGS_KEY, {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, list[str]] = {}
        for persona_id, domains in raw.items():
            if not isinstance(domains, list):
                continue
            result[str(persona_id)] = [d for d in domains if d in self.domains]
        return result

    async def _set_persona_bindings(self, bindings: dict[str, list[str]]) -> None:
        await self.put_kv_data(PERSONA_BINDINGS_KEY, bindings)

    async def _get_current_conversation_persona_id(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest | None = None,
    ) -> str | None:
        if request and request.conversation:
            return request.conversation.persona_id
        cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        if not cid:
            return None
        conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
        return conversation.persona_id if conversation else None

    async def _resolve_selected_persona_id(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest | None = None,
    ) -> str | None:
        conversation_persona_id = await self._get_current_conversation_persona_id(event, request)
        provider_settings = (
            self.context.get_config(umo=event.unified_msg_origin).get("provider_settings", {}) or {}
        )
        persona_id, _, _, use_webchat_special_default = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=conversation_persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=provider_settings,
        )
        return "_chatui_default_" if use_webchat_special_default else persona_id

    async def _routed_domain_ids(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest | None,
        query: str,
    ) -> list[str]:
        routed: list[str] = []
        persona_id = await self._resolve_selected_persona_id(event, request)
        if persona_id:
            bindings = await self._get_persona_bindings()
            routed.extend(bindings.get(persona_id, []))
        for domain_id, adapter in self.adapters.items():
            if self.domains[domain_id].enabled and adapter.matches(query):
                routed.append(domain_id)
        seen: set[str] = set()
        return [d for d in routed if d in self.domains and self.domains[d].enabled and not (d in seen or seen.add(d))]

    def _remove_competing_tools(self, request: ProviderRequest) -> None:
        if not getattr(request, "func_tool", None):
            return
        for tool_name in {
            "execute_python_code", "astrbot_execute_python", "astrbot_execute_shell",
            "fetch_url", "web_search", "search_web", "browser_exec", "browser_batch_exec",
        }:
            try:
                request.func_tool.remove_tool(tool_name)
            except (AttributeError, KeyError, ValueError):
                pass

    async def _sync_domain(
        self,
        domain_id: str,
        *,
        entry: str = "",
        limit: int | None = None,
        task: SyncTask | None = None,
    ) -> SyncTask:
        domain = self.domains[domain_id]
        adapter = self.adapters[domain_id]
        limit = max(1, min(int(limit or domain.bootstrap_limit), 500))
        task = task or SyncTask(uuid.uuid4().hex[:8], domain_id, entry, limit)
        self.tasks[task.task_id] = task
        task.status = "running"
        try:
            await self.storage.ensure_kb(domain)
            raw_docs = await adapter.crawl(self.http, entry=entry, limit=limit)
            parsed_docs = []
            for raw in raw_docs:
                task.current = raw.title
                try:
                    docs = adapter.parse(
                        raw,
                        max_chars=max(300, self._cfg_int("pre_chunk_max_chars", 1200)),
                        overlap=max(0, self._cfg_int("pre_chunk_overlap", 120)),
                    )
                    if not docs:
                        task.skipped += 1
                        continue
                    for doc in docs:
                        parsed_docs.append(await self.cleaner.clean_document(doc))
                except Exception as exc:
                    task.failed += 1
                    task.errors.append(f"{raw.key}: {type(exc).__name__}: {exc}")
                    logger.warning("游戏知识库解析失败: domain=%s source=%s error=%s", domain_id, raw.key, exc)
            imported, updated = await self.storage.upsert_documents(domain, parsed_docs)
            task.imported += imported
            task.updated += updated
            task.status = "completed"
            task.current = ""
            task.message = (
                f"完成：新增 {task.imported}，更新 {task.updated}，"
                f"跳过 {task.skipped}，失败 {task.failed}。"
            )
            logger.info("游戏知识库同步完成: domain=%s %s", domain_id, task.message)
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.message = "任务已取消。"
            raise
        except Exception as exc:
            task.status = "failed"
            task.failed += 1
            task.errors.append(f"{type(exc).__name__}: {exc}")
            task.message = f"同步失败：{exc}"
            logger.error("游戏知识库同步失败: domain=%s error=%s", domain_id, exc, exc_info=True)
        return task

    def _start_sync(self, domain_id: str, *, entry: str = "", limit: int | None = None) -> SyncTask:
        running = self.running_jobs.get(domain_id)
        if running and not running.done():
            for task in reversed(list(self.tasks.values())):
                if task.domain_id == domain_id and task.status == "running":
                    return task
            raise RuntimeError(f"{self.domains[domain_id].display_name} 已有同步任务运行中。")
        domain = self.domains[domain_id]
        final_limit = max(1, min(int(limit or domain.bootstrap_limit), 500))
        task = SyncTask(uuid.uuid4().hex[:8], domain_id, entry, final_limit)
        self.tasks[task.task_id] = task
        job = asyncio.create_task(
            self._sync_domain(domain_id, entry=entry, limit=final_limit, task=task),
            name=f"game-kb-sync-{domain_id}-{task.task_id}",
        )
        self.running_jobs[domain_id] = job
        def _cleanup(_: asyncio.Task) -> None:
            current = self.running_jobs.get(domain_id)
            if current is job:
                self.running_jobs.pop(domain_id, None)
        job.add_done_callback(_cleanup)
        return task

    def _schedule_bootstrap(self) -> None:
        """Schedule creation/bootstrap exactly once for the current plugin instance."""
        if self._bootstrap_job and not self._bootstrap_job.done():
            return
        logger.info("游戏知识库：计划自动创建/复用已启用领域的 AstrBot 官方知识库。")
        self._bootstrap_job = asyncio.create_task(
            self._bootstrap(),
            name="game-kb-auto-bootstrap",
        )

    async def _bootstrap(self) -> None:
        # “免初始化”意味着知识库创建本身不受 auto_bootstrap 控制。
        # auto_bootstrap 只决定空库是否继续抓取第一批内容。
        auto_bootstrap = self._cfg_bool("auto_bootstrap", True)
        for domain_id, domain in self.domains.items():
            if not domain.enabled:
                continue
            try:
                kb, created = await self.storage.ensure_kb(domain)
                logger.info(
                    "游戏知识库已就绪: domain=%s kb=%s created=%s docs=%s chunks=%s",
                    domain_id,
                    domain.kb_name,
                    created,
                    kb.kb.doc_count,
                    kb.kb.chunk_count,
                )
                if (
                    auto_bootstrap
                    and kb.kb.doc_count == 0
                    and kb.kb.chunk_count == 0
                ):
                    logger.info(
                        "知识库为空，自动引导同步: domain=%s limit=%s",
                        domain_id,
                        domain.bootstrap_limit,
                    )
                    await self._sync_domain(
                        domain_id,
                        limit=domain.bootstrap_limit,
                    )
            except Exception as exc:
                # Most commonly no embedding provider yet. Do not make plugin startup fail.
                logger.warning(
                    "游戏知识库自动初始化暂未完成: domain=%s error=%s",
                    domain_id,
                    exc,
                )

    async def initialize(self) -> None:
        """AstrBot calls initialize() every time this plugin is loaded/reloaded."""
        self._schedule_bootstrap()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        # Full-process startup fallback.  Hot-reload is handled by initialize().
        self._schedule_bootstrap()

    async def _ensure_domain_ready_for_query(
        self,
        domain_id: str,
        query: str,
    ) -> None:
        """Create an absent KB and bootstrap an empty KB before the main LLM request.

        This is deliberately a narrow lazy behaviour: a normal question does not
        rewrite a non-empty KB.  It only repairs the “plugin was hot-loaded / KB
        not created yet / previous bootstrap failed” case.
        """
        domain = self.domains[domain_id]
        kb, _ = await self.storage.ensure_kb(domain)
        if kb.kb.doc_count or kb.kb.chunk_count:
            return
        if not self._cfg_bool("auto_bootstrap", True):
            return

        running = self.running_jobs.get(domain_id)
        if running and not running.done():
            await running
            return

        limit = max(
            1,
            min(
                self._cfg_int(
                    "query_bootstrap_limit",
                    min(domain.bootstrap_limit, 20),
                ),
                500,
            ),
        )
        task = self._start_sync(domain_id, limit=limit)
        job = self.running_jobs.get(domain_id)
        logger.info(
            "游戏知识库查询触发空库引导: domain=%s query=%s task=%s limit=%s",
            domain_id,
            query[:120],
            task.task_id,
            limit,
        )
        if job:
            await job

    @filter.on_waiting_llm_request()
    async def ensure_game_kb_before_llm(self, event: AstrMessageEvent) -> None:
        """Retry missing/empty domain KBs before AstrBot builds the main request."""
        if not self._cfg_bool("enable_routing", True):
            return
        query = self._sanitize_query(event.message_str or "")
        if not query:
            return
        try:
            domain_ids = await self._routed_domain_ids(event, None, query)
            for domain_id in domain_ids:
                await self._ensure_domain_ready_for_query(domain_id, query)
        except Exception as exc:
            # Never block an ordinary chat because a source is temporarily unavailable.
            logger.warning("游戏知识库查询前引导失败: query=%s error=%s", query[:120], exc)

    @filter.on_llm_request()
    async def inject_game_knowledge(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enable_routing", True):
            return
        query = self._sanitize_query(request.prompt or event.message_str or "")
        if not query:
            return
        domain_ids = await self._routed_domain_ids(event, request, query)
        if not domain_ids:
            return
        blocks: list[str] = []
        for domain_id in domain_ids:
            domain = self.domains[domain_id]
            try:
                kb = await self.storage.get_kb(domain)
                if not kb:
                    await self.storage.ensure_kb(domain)
                    if self._cfg_bool("auto_bootstrap", True):
                        self._start_sync(domain_id)
                    continue
                result = await self.storage.retrieve(domain, query)
                context_text = str((result or {}).get("context_text") or "").strip()
                if context_text:
                    if len(context_text) > MAX_INJECT_CHARS_PER_DOMAIN:
                        context_text = context_text[:MAX_INJECT_CHARS_PER_DOMAIN].rstrip() + "\n[内容已截断]"
                    blocks.append(f"[{domain.display_name} 知识库]\n{context_text}")
            except Exception as exc:
                logger.warning("游戏知识库检索失败: domain=%s error=%s", domain_id, exc)
        if not blocks:
            return
        request.extra_user_content_parts.append(
            TextPart(
                text=(
                    "<game_knowledge_context>\n"
                    "以下内容来自 AstrBot 官方知识库，请优先据此回答对应游戏的设定、角色、歌曲或剧情问题。\n\n"
                    + "\n\n".join(blocks)
                    + "\n</game_knowledge_context>"
                )
            ).mark_as_temp()
        )
        if self._cfg_bool("remove_competing_tools", False):
            self._remove_competing_tools(request)

    def _task_text(self, task: SyncTask) -> str:
        domain = self.domains.get(task.domain_id)
        return "\n".join([
            f"任务ID：{task.task_id}",
            f"领域：{domain.display_name if domain else task.domain_id}",
            f"状态：{task.status}",
            f"入口：{task.entry or '-'}",
            f"上限：{task.limit}",
            f"新增：{task.imported}，更新：{task.updated}，跳过：{task.skipped}，失败：{task.failed}",
            f"当前：{task.current or '-'}",
            f"说明：{task.message or '-'}",
        ])

    @filter.command("游戏知识库状态")
    async def game_kb_status(self, event: AstrMessageEvent, domain: str = ""):
        selected = self._resolve_domain_id(domain) if domain else None
        ids = [selected] if selected else list(self.domains)
        lines = ["游戏知识库状态："]
        for domain_id in ids:
            if not domain_id:
                continue
            cfg = self.domains[domain_id]
            kb = await self.storage.get_kb(cfg)
            running = self.running_jobs.get(domain_id)
            lines.append(
                f"- {cfg.display_name}: "
                + (f"{kb.kb.doc_count} 文档 / {kb.kb.chunk_count} 块" if kb else "尚未创建")
                + ("；同步中" if running and not running.done() else "")
                + ("；已禁用" if not cfg.enabled else "")
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("游戏知识库同步")
    async def game_kb_sync(self, event: AstrMessageEvent, args: GreedyStr = ""):
        parts = str(args or "").strip().split()
        if not parts:
            yield event.plain_result("用法：/游戏知识库同步 <touhou|ba|pjsk> [页数/文件数] [入口或路径关键词]")
            return
        domain_id = self._resolve_domain_id(parts[0])
        if not domain_id:
            yield event.plain_result("未知领域，可用：touhou / ba / pjsk")
            return
        limit = self.domains[domain_id].bootstrap_limit
        entry = ""
        if len(parts) >= 2:
            try:
                limit = int(parts[1])
                entry = " ".join(parts[2:])
            except ValueError:
                entry = " ".join(parts[1:])
        task = self._start_sync(domain_id, entry=entry, limit=limit)
        yield event.plain_result(
            f"已启动 {self.domains[domain_id].display_name} 同步任务：{task.task_id}\n"
            f"上限：{task.limit}\n入口：{task.entry or '默认'}"
        )

    @filter.command("游戏知识库任务")
    async def game_kb_task(self, event: AstrMessageEvent, task_id: str = ""):
        task_id = str(task_id or "").strip()
        if task_id:
            task = self.tasks.get(task_id)
        else:
            task = next(reversed(self.tasks.values()), None) if self.tasks else None
        if not task:
            yield event.plain_result("目前没有同步任务记录。")
            return
        text = self._task_text(task)
        if task.errors:
            text += "\n最近错误：\n" + "\n".join(task.errors[-5:])
        yield event.plain_result(text)

    @filter.command("游戏知识库搜索")
    async def game_kb_search(self, event: AstrMessageEvent, args: GreedyStr):
        raw = str(args or "").strip()
        if not raw or " " not in raw:
            yield event.plain_result("用法：/游戏知识库搜索 <touhou|ba|pjsk> <关键词>")
            return
        domain_raw, query = raw.split(" ", 1)
        domain_id = self._resolve_domain_id(domain_raw)
        if not domain_id:
            yield event.plain_result("未知领域，可用：touhou / ba / pjsk")
            return
        result = await self.storage.retrieve(self.domains[domain_id], query, top_k=5)
        rows = (result or {}).get("results") or []
        if not rows:
            yield event.plain_result("没有检索到相关知识块。")
            return
        lines = [f"{self.domains[domain_id].display_name} 检索：{query}"]
        for idx, item in enumerate(rows[:5], 1):
            content = re.sub(r"\s+", " ", str(item.get("content") or ""))[:180]
            lines.append(f"{idx}. [{item.get('doc_name', '-')}] score={float(item.get('score', 0)):.4f} {content}")
        yield event.plain_result("\n".join(lines))

    @filter.command("游戏知识库绑定人格")
    async def bind_game_kb_persona(
        self,
        event: AstrMessageEvent,
        domain: str,
        persona_id: str = "",
    ):
        domain_id = self._resolve_domain_id(domain)
        if not domain_id:
            yield event.plain_result("未知领域，可用：touhou / ba / pjsk")
            return
        target = str(persona_id or "").strip() or await self._resolve_selected_persona_id(event)
        if not target:
            yield event.plain_result("当前没有可绑定的人格，请显式提供 persona_id。")
            return
        bindings = await self._get_persona_bindings()
        domains = bindings.setdefault(target, [])
        if domain_id not in domains:
            domains.append(domain_id)
        await self._set_persona_bindings(bindings)
        yield event.plain_result(f"已将人格 {target} 绑定到 {self.domains[domain_id].display_name} 知识库路由。")

    @filter.command("游戏知识库解绑人格")
    async def unbind_game_kb_persona(
        self,
        event: AstrMessageEvent,
        domain: str,
        persona_id: str = "",
    ):
        domain_id = self._resolve_domain_id(domain)
        if not domain_id:
            yield event.plain_result("未知领域，可用：touhou / ba / pjsk")
            return
        target = str(persona_id or "").strip() or await self._resolve_selected_persona_id(event)
        if not target:
            yield event.plain_result("当前没有可解绑的人格，请显式提供 persona_id。")
            return
        bindings = await self._get_persona_bindings()
        bindings[target] = [d for d in bindings.get(target, []) if d != domain_id]
        if not bindings[target]:
            bindings.pop(target, None)
        await self._set_persona_bindings(bindings)
        yield event.plain_result(f"已解除人格 {target} 与 {self.domains[domain_id].display_name} 知识库路由绑定。")

    @filter.command("游戏知识库人格列表")
    async def game_kb_persona_list(self, event: AstrMessageEvent):
        current = await self._resolve_selected_persona_id(event)
        bindings = await self._get_persona_bindings()
        lines = [f"当前人格：{current or '-'}", "人格知识库路由："]
        if not bindings:
            lines.append("- 暂无")
        else:
            for persona_id, domain_ids in bindings.items():
                names = [self.domains[d].display_name for d in domain_ids if d in self.domains]
                lines.append(f"- {persona_id}: {', '.join(names) if names else '-'}")
        yield event.plain_result("\n".join(lines))

    # Touhou compatibility commands. Initialization is intentionally removed;
    # the KB is now created automatically on plugin load.
    @filter.command("东方知识库状态")
    async def touhou_status_compat(self, event: AstrMessageEvent):
        kb = await self.storage.get_kb(self.domains["touhou"])
        if not kb:
            yield event.plain_result("东方Project知识库尚未创建；插件会在可用 Embedding Provider 存在时自动创建。")
            return
        yield event.plain_result(f"东方Project知识库：{kb.kb.doc_count} 文档 / {kb.kb.chunk_count} 块")

    @filter.command("东方知识库同步")
    async def touhou_sync_compat(
        self,
        event: AstrMessageEvent,
        entry: str = "东方Project",
        limit: int = 40,
    ):
        task = self._start_sync("touhou", entry=entry, limit=limit)
        yield event.plain_result(f"已启动东方Project同步任务：{task.task_id}")

    @filter.command("东方知识库搜索")
    async def touhou_search_compat(self, event: AstrMessageEvent, query: GreedyStr):
        result = await self.storage.retrieve(self.domains["touhou"], str(query), top_k=5)
        rows = (result or {}).get("results") or []
        if not rows:
            yield event.plain_result("没有检索到相关知识块。")
            return
        lines = [f"检索词：{query}"]
        for idx, item in enumerate(rows[:5], 1):
            excerpt = re.sub(r"\s+", " ", str(item.get("content") or ""))[:160]
            lines.append(f"{idx}. [{item.get('doc_name', '-')}] score={float(item.get('score', 0)):.4f} {excerpt}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self._bootstrap_job and not self._bootstrap_job.done():
            self._bootstrap_job.cancel()
        jobs = [job for job in self.running_jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self.running_jobs.clear()
