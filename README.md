# astrbot_plugin_game_kb

把原来的“东方知识库”插件抽象成一个通用的 **crawl → parse → storage** 游戏/ACG 文本知识库框架，目前内置：

- **东方Project**：THBWiki
- **Blue Archive / 蔚蓝档案**：`ba-archive/blue-archive` + `electricgoat/ba-data`，并支持 GameKee 单页补充
- **Project SEKAI / 世界计划**：`StarMoe-org/Moe-story` + `Sekai-World/sekai-master-db-cn-diff`

插件仍然把知识写入 **AstrBot 官方知识库**，检索仍使用 AstrBot 原生 KB/RAG；本项目没有另建向量数据库，也没有修改 AstrBot 的知识库 schema。


## 0.5.1：热重载与首次提问兜底

AstrBot 的 `on_astrbot_loaded` 只在整个 AstrBot 完成启动时触发；仅重新加载插件时不会再次触发。
因此 0.5.0 在 WebUI 热重载安装后可能出现“插件已加载，但 BA/PJSK 知识库没有创建”的情况。

0.5.1 改为使用插件 `initialize()` 生命周期启动自动创建，并保留 `on_astrbot_loaded` 作为整进程启动兜底。
此外，在真正调用 LLM 前，如果本轮问题已经能路由到某个领域而该知识库仍不存在/为空，会再次创建并进行一次小规模 bootstrap。

这个兜底**不是“每问一句都重抓数据”**：非空知识库不会因为普通提问而重写。

## 0.5.2：GitHub 加速与抓取重试

BA / PJSK 的稳定资料主要来自 GitHub。部分部署环境可以正常使用 AstrBot 本身的 GitHub 加速地址，却无法稳定直连 `raw.githubusercontent.com` 或 `api.github.com`。本版本把这项能力下沉到共用 HTTP 层：

- 新增 `github_proxy_url`：填写 AstrBot「设置 → 网络 → GitHub 加速地址」中同样的 URL 前缀即可；
- 新增 `github_proxy_fallback_direct`：加速地址连续失败后自动回退 GitHub 直连，默认开启；
- `github.com`、`raw.githubusercontent.com`、`api.github.com` 请求统一经过同一套逻辑，BA/PJSK Adapter 不各自处理代理；
- 对连接超时、读取超时、代理错误、429/5xx 等瞬时失败做有限重试与指数退避；
- 共用一个 `httpx.AsyncClient` 连接池，不再为每个文件重复建立 TLS 连接；
- GitHub API URL 的 query string 会编码进代理路径，例如 `?recursive=1` 不会被 URL 前缀代理自身吞掉。

`github_proxy_url` 与 `http_proxy` 含义不同：前者是 AstrBot 风格的 GitHub URL 前缀加速服务，后者仍然是普通 HTTP(S) 网络代理。两者都留空时保持原来的直连行为。
## 0.6.0：BA 剧情修复与多源补充

这一版修复了 BA “只有 students_catalog、没有剧情”的根因：

- `ba-archive/blue-archive` 当前剧情 JSON 顶层是对象，正文位于 `content[]`；旧 Parser 只接受顶层数组，导致剧情下载成功后被静默跳过。现在同时兼容对象和旧数组格式。
- BA 的 bootstrap 不再以“知识库非空”为完成条件。只有 `students_catalog.txt` 时仍视为引导不完整，至少出现一个剧情文档后才停止自动补库。
- 保留 `ba-archive` 作为优先简中历史剧情源。
- 增加活跃上游 `electricgoat/ba-data@global`：可按 GroupId 导入最新 ScenarioScript，优先使用 `TextTw`，没有时再回退 JP/EN。
- GameKee 不作为整站主数据源；支持直接给出 GameKee BA 页面 URL 进行单页补充，避免依赖脆弱的站内搜索/全站 HTML 爬虫。
- `HePudding/ba-storybook` 继续作为解析/分类参考，不再假设它是持续更新的数据源。
- WebUI 配置显示名已缩短，详细说明移动到 hint；底层配置 key 保持不变，避免升级后已有配置失效。

BA 手动同步示例：

```text
/游戏知识库同步 ba 40
/游戏知识库同步 ba 30 upstream
/游戏知识库同步 ba 10 upstream 59999
/游戏知识库同步 ba 1 https://www.gamekee.com/ba/xxxxxx.html
```

其中普通 `ba` 同步优先使用 ba-archive 简中内容；`upstream` / `latest` 使用 electricgoat 活跃上游；GameKee 目前只作为明确 URL 的补充源。

## 设计目标

### 1. 不再需要手动初始化

插件加载完成后，会自动：

1. 检查启用领域对应的 AstrBot 官方知识库；
2. 不存在则自动创建；
3. 如果知识库为空且 `auto_bootstrap=true`，自动后台同步一轮默认数据。

因此不再提供 `/东方知识库初始化` 一类初始化指令。

如果启动时还没有配置 Embedding Provider，插件只记录 warning，不会让 AstrBot 启动失败；配置好 Embedding Provider 后可重载插件，或使用 `/游戏知识库同步 ...`。

### 2. 组件尽量共用

代码分为三层：

```text
Source Adapter
    │
    ├─ crawl()   获取网页 / GitHub Raw / GitHub Tree
    │
    ├─ parse()   针对具体站点/数据格式转成自描述纯文本
    │
    ▼
AstrBotKBStorage
    │
    ├─ 自动创建/复用 AstrBot 官方知识库
    ├─ 按 doc_name upsert
    └─ 调用 AstrBot 原生 retrieve()
```

目录：

```text
game_kb/
├── core.py                 # RawDocument / ParsedDocument / 通用切片
├── http.py                 # 共用 HTTP 客户端
├── storage.py              # 共用 AstrBot KB 存储与检索
├── cleaning.py             # 可选 AstrBot LLM 清洗
└── adapters/
    ├── base.py             # 新游戏/动漫 Adapter 接口
    ├── touhou.py
    ├── blue_archive.py
    └── project_sekai.py
```

未来添加新作品时，通常只需要实现新的 Adapter：

```python
class NewGameAdapter(BaseAdapter):
    async def crawl(...):
        ...

    def parse(...):
        ...
```

不需要复制知识库创建、上传、检索、路由等逻辑。

## 为什么只导入“稳定知识”

本插件刻意不把“当前卡池”“当前活动剩余时间”“排行榜”“当前/即将到来的 Raid”等实时状态写进长期 RAG。

对于 BA / PJSK，主要问题不是旧知识快速失效，而是不断出现新学生、新卡、新歌、新剧情。已经发布的角色资料、歌曲信息和历史剧情基本是稳定内容，因此最适合 AstrBot 的扁平文本知识库。

### Blue Archive 默认内容

优先简中来源 `ba-archive/blue-archive`：

- 学生静态资料：`apps/blue-archive-story-editor/src/assets/students.json`
- 剧情：`apps/blue-archive-story-viewer/public/story/`
  - `main`
  - `other`
  - `event`
  - `favor`
- 明确排除 `public/story/ai/`。

活跃补充来源 `electricgoat/ba-data@global`：

- `ScenarioScriptMain1..5`
- `ScenarioScriptEvent1..5`
- `ScenarioScriptFavor1..5`
- `ScenarioScriptGroup1..5`
- 按 GroupId 拆成独立文档，优先采用国际服 `TextTw`。

GameKee：

- 不做整站默认抓取；
- 只有显式传入 BA 页面 URL 时作为 Wiki 单页补充。

仍然不把当前 Banner、Raid、排名等实时状态写入长期 RAG。

### Project SEKAI 默认内容

稳定基础资料优先：

- `Sekai-World/sekai-master-db-cn-diff`
  - `gameCharacters.json` + `characterProfiles.json` 合并成角色资料
  - `musics.json` 生成歌曲资料
- `StarMoe-org/Moe-story`
  - `worldview.txt`
  - `character_nicknames.yaml`
  - `story/event/event_map.csv`
  - `story/self/**`
  - `story/unit/**`
  - `story/event/*/detail.json`
  - `story/special/**`
  - `story/card/**`

默认不导入 `gachas.json`、实时活动状态、排名等短期信息。

## 扁平知识库与“自描述文本”

AstrBot 当前知识库适合扁平文本 RAG，因此本插件没有强行引入业务 metadata。

Parser 会把对检索有意义的信息直接写入正文，例如：

```text
作品：Project SEKAI / 世界计划
内容类型：角色资料
角色ID：19
姓名：东云绘名
组合：25时，在Nightcord。
CV：铃木实里
...
```

以及：

```text
作品：Blue Archive / 蔚蓝档案
内容类型：剧情（main）
剧情GroupId：31010
源文件：main/31010.json
...
```

这些字段会和正文一起向量化/稀疏检索，仍完全兼容 AstrBot 官方 KB。

## 自动路由

插件会在每次 LLM 请求前检查：

1. 当前人格是否绑定了某个游戏知识库；
2. 用户文本是否命中该领域关键词。

命中后直接调用 AstrBot `kb_manager.retrieve()`，并把少量相关结果作为本轮临时上下文追加给 LLM。

人格绑定优先于关键词，因此可以实现：

```text
Arona / Plana 人格 -> Blue Archive
Ena 人格          -> Project SEKAI
```

此时即使用户只问“她后来怎么了？”，只要当前人格已经绑定，对应 KB 仍会参与检索。

## 可选 LLM 清洗

专用 Parser 默认已经尽量清洗格式，因此 `enable_llm_cleaning` 默认关闭。

如果未来接入普通网页、SPA 抓取结果或格式很脏的数据源，可以开启：

```text
enable_llm_cleaning = true
cleaning_provider_id = <可选>
```

插件会调用 AstrBot 的聊天 Provider 做二次清洗，但提示模型：

- 只能去噪；
- 不总结；
- 不补充外部事实；
- 必须保留角色名、数字 ID、章节和来源语义。

## 配置

最常用的配置：

- `enable_touhou`
- `enable_blue_archive`
- `enable_pjsk`
- `auto_bootstrap`
- `touhou_bootstrap_limit`
- `ba_bootstrap_limit`
- `pjsk_bootstrap_limit`
- `default_embedding_provider_id`
- `default_rerank_provider_id`
- `session_top_k`
- `pre_chunk_max_chars`
- `pre_chunk_overlap`
- `upload_batch_size`
- `upload_tasks_limit`
- `github_proxy_url`
- `github_proxy_fallback_direct`

默认首次同步上限均为 40。注意：BA 学生目录、PJSK 角色目录和歌曲目录本身会生成多个文本块，因此“40”表示源文档数量，不等于最终向量块数量。

## 指令

### 状态

```text
/游戏知识库状态
/游戏知识库状态 ba
/游戏知识库状态 pjsk
/游戏知识库状态 touhou
```

### 手动扩充/刷新

```text
/游戏知识库同步 touhou 80 东方Project
/游戏知识库同步 ba 80
/游戏知识库同步 pjsk 80
```

对于 GitHub 数据源，第三个参数主要作为路径/数字 ID 过滤器使用，例如：

```text
/游戏知识库同步 pjsk 20 1234
```

会优先寻找路径中与 `1234` 对应的卡牌/活动/角色资料。

### 测试检索

```text
/游戏知识库搜索 ba 阿洛娜
/游戏知识库搜索 pjsk 东云绘名
/游戏知识库搜索 touhou 博丽灵梦
```

### 人格绑定

```text
/游戏知识库绑定人格 ba <persona_id>
/游戏知识库绑定人格 pjsk <persona_id>
/游戏知识库解绑人格 ba <persona_id>
/游戏知识库人格列表
```

不传 `persona_id` 时，默认使用当前生效人格。

### 东方兼容指令

保留：

```text
/东方知识库状态
/东方知识库同步 [入口] [数量]
/东方知识库搜索 <关键词>
```

不再保留初始化指令，因为初始化已自动完成。

## 数据源与边界

- 东方：<https://thbwiki.cc/>
- Blue Archive 简中剧情站：<https://github.com/ba-archive/blue-archive>
- Blue Archive 活跃数据上游：<https://github.com/electricgoat/ba-data>
- Blue Archive Wiki 补充：<https://www.gamekee.com/ba/>
- BA Storybook（解析/分类参考）：<https://github.com/HePudding/ba-storybook>
- PJSK 剧情资源：<https://github.com/StarMoe-org/Moe-story>
- PJSK master data：<https://github.com/Sekai-World/sekai-master-db-cn-diff>

本插件只提供抓取、解析和导入能力，不把这些上游内容直接打包进插件。各游戏文本、名称、剧情与素材仍归原权利人/数据源贡献者所有；公开再分发时请自行检查对应上游条款。

## 已知限制

- 当前仍是纯文本知识库，不解决图片/卡面识别。
- GitHub Adapter 通过公开 Tree/Raw 接口获取数据；大规模频繁同步可能受到 GitHub 未认证请求限额影响。
- BA 剧情 JSON 的中文文本可直接提取，但部分原始脚本中的说话人标识是韩文/脚本控制字段；第一版以干净中文正文为主，不强行猜测角色归属。
- PJSK 目前优先使用已经整理好的剧情文本与简中 master data，不导入实时卡池、排名等状态。
- 第一版没有实现“低 RAG 分数自动联网补抓”的 query-time lazy search；目前的自动行为是“启动时空库 bootstrap + 手动同步扩充”。这是刻意保持与原东方插件接近、避免过度设计的结果。

## 开源协议

沿用原仓库 MIT License。
