# astrbot_plugin_game_kb

把原来的“东方知识库”插件抽象成一个通用的 **crawl → parse → storage** 游戏/ACG 文本知识库框架，目前内置：

- **东方Project**：THBWiki
- **Blue Archive / 蔚蓝档案**：`ba-archive/blue-archive`
- **Project SEKAI / 世界计划**：`StarMoe-org/Moe-story` + `Sekai-World/sekai-master-db-cn-diff`

插件仍然把知识写入 **AstrBot 官方知识库**，检索仍使用 AstrBot 原生 KB/RAG；本项目没有另建向量数据库，也没有修改 AstrBot 的知识库 schema。

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

- 学生静态资料：`apps/blue-archive-story-editor/src/assets/students.json`
- 剧情：`apps/blue-archive-story-viewer/public/story/`
  - `main`
  - `other`
  - `event`
  - `favor`
- 默认明确排除 `public/story/ai/`，避免把 AI 生成摘要作为首选事实来源。
- 不抓 `BlueArchiveAPI` 的 `current/upcoming` Raid、Banner 等实时状态。

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
- Blue Archive 剧情站：<https://github.com/ba-archive/blue-archive>
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
