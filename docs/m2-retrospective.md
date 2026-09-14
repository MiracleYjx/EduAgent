# M2 RAG 里程碑复盘报告

复盘日期：2026-09-14  
复盘范围：T031-T045  
复盘依据：

- `.specify/spec.md`（FR-008 至 FR-014、FR-024 至 FR-028、Edge Cases、SC-001 至 SC-009）
- `.specify/plan.md`（§1 PostgreSQL + pgvector、§2 Hybrid Retrieval、§3 Embedding/Rerank 抽象、§4.1 AI 出题链路、Validation Gates）
- `.specify/tasks.md`（M2 T031-T045）
- `.specify/data-model.md`（DocumentChunk、检索结果字段和状态规则）
- `.specify/contracts/embedding-provider.md`
- `.specify/contracts/rag-retrieval.md`
- `.specify/memory/constitution.md`
- `backend/app/ai/ingestion/`、`backend/app/ai/embedding/`、`backend/app/ai/retrieval/`
- `backend/app/models/document_chunk.py`
- `backend/app/services/knowledge_base_service.py`
- `backend/app/api/knowledge_bases.py`
- `backend/app/ui/knowledge_base_view.py`
- `migrations/versions/0004_document_chunks.py`
- `scripts/run_retrieval_benchmark.py`
- `tests/unit/ingestion/`、`tests/unit/retrieval/`、`tests/contract/test_document_parsers.py`、`tests/contract/test_embedding_provider.py`、`tests/contract/test_retrieval_contract.py`

复盘结论：⚠️ 需关注

## 一、总体评价

M2 的核心代码已经形成了从解析、清洗、确定性分块、Embedding、DocumentChunk 持久化到四种检索模式和 Rerank 适配器的完整骨架，且没有引入计划外的独立向量库或搜索引擎。解析器、分块器、Provider 抽象以及检索结果来源字段的实现与计划大体一致，当前聚焦测试为 203 passed、0 skipped，真实 PostgreSQL 检索契约也能运行。主要风险是运行环境没有可用的本地 Embedding/Rerank 依赖，真实 Benchmark 只有 Keyword Only 成功，因而尚无 Vector/Hybrid/Rerank 的质量证据。另有几个会直接影响 M3 的接口和一致性问题：LLM Rerank 在已有事件循环中会失败，Benchmark 错用 `embed_documents` 生成查询向量，文档状态可以被直接改成 Ready，以及 BGE 默认小模型与 1024 维迁移约束不一致。

## 二、维度一：计划一致性

### 2.1 范围偏移

| 类型 | 发现 | 影响 |
| :--- | :------ | :-- |
| 超出范围 | 未发现生产架构超出 M2；PostgreSQL、pgvector、tsvector/GIN、可替换 Provider/Rerank 均在 `plan.md` 范围内（`.specify/plan.md:220-247`）。测试中的 Hashing Provider 和 SQLite 精确基线只作为替身/测试方言使用（`tests/contract/test_embedding_provider.py:63-71`、`backend/app/ai/retrieval/vector_search.py:133-156`）。 | 低；不构成架构漂移。 |
| 缩小范围 | T035/T037/T039 的代码路径存在，但当前运行配置 `huggingface + BAAI/bge-large-zh-v1.5` 无 `sentence-transformers`，T045 真实运行中 Vector Only、Hybrid、Hybrid + Rerank 均以 `EMBEDDING_PROVIDER_NOT_READY` 失败（`benchmark/results/retrieval_real-t045_*.json`）。 | **高**；M2 检查点要求四种配置可比较，当前只能证明 Keyword Only 和自检替身管道。 |

### 2.2 架构决策符合度

| 决策 | 符合度 | 说明 |
| :----------------- | :----- | :-- |
| 单一 PostgreSQL 检索底座 | ✅ | Compose 只部署 PostgreSQL、Redis、Backend（`docker-compose.yml:1-78`）；DocumentChunk 的 HNSW/GIN 迁移存在（`migrations/versions/0004_document_chunks.py:83-100`），未发现 Milvus、Elasticsearch 或其他生产检索存储。 |
| Embedding 不微调 | ✅ | 代码只有本地/云端 Provider 加载和编码，没有训练、微调或权重更新路径（`backend/app/ai/embedding/providers/local.py:102-162`、`openai_compatible.py:81-111`）。 |
| Rerank 保持可替换 | ✅ | `BaseReranker`、LLM 路线、Cross Encoder 路线和按配置工厂均存在（`backend/app/ai/retrieval/reranker.py:196-224`、`318-441`），没有把业务层绑定到单一实现。 |

### 2.3 契约与数据模型符合度

| 契约/模型 | 符合度 | 说明 |
| :--------------------------- | :-- | :---- |
| `embedding-provider.md` | ⚠️ | `embed_documents`/`embed_query`、数量/维度校验和 Provider 切换都实现了（`backend/app/ai/embedding/base.py:67-175`）。但 `describe()` 只有 provider/model/dimension，没有文档版本、查询版本或调用关联标识，未完全满足契约的 Traceability 要求（`backend/app/ai/embedding/base.py:89-96`）。严重度：中。 |
| `rag-retrieval.md` | ⚠️ | 四个模式、结果字段、过滤和空结果均实现（`backend/app/ai/retrieval/base.py:110-117`、`186-233`）。但契约声明 VECTOR_ONLY 只能使用 pgvector，而实现对非 PostgreSQL 自动走 Python 精确基线（`backend/app/ai/retrieval/vector_search.py:84-99`）；同时 `BaseRetriever.search` 的类型没有声明 Hybrid 实际接受的 `RetrievalQuery`（`backend/app/ai/retrieval/base.py:337-351`、`backend/app/ai/retrieval/hybrid_search.py:99-114`）。严重度：中。 |
| DocumentChunk（`data-model.md`） | ⚠️ | `id/course_id/document_id/content/metadata/embedding/search_vector/created_at` 和关系均存在（`backend/app/models/document_chunk.py:94-141`）。但是 embedding/search_vector 可为空，Ready 的完整性只由 Service 流程约定而非模型或检索边界保证；更严重的是默认 BGE 小模型在 `backend/app/ai/embedding/providers/bge.py:21`，而迁移和模型固定 `vector(1024)`（`backend/app/models/document_chunk.py:33-34`、`migrations/versions/0004_document_chunks.py:23-24`），存在 512/1024 维不一致风险。严重度：高。 |

### 2.4 任务状态真实性

| 任务 | tasks.md 标记 | 实际可运行 | 差异说明 |
| :-------- | :---------- | :----- | :--- |
| T031 | [X] | ⚠️ | 解析器契约当前通过；提交历史显示测试提交 `1360ea3` 早于实现 `da79ce7`，但仓库没有保存当时的红灯日志。 严重度：中。 |
| T032 | [X] | ✅ | PDF/TXT/Markdown 注册表和失败码路径可运行（`backend/app/ai/ingestion/parsers.py:381-473`）。 |
| T033 | [X] | ✅ | 清洗和带偏移/来源元数据的确定性分块可运行（`cleaning.py:53-95`、`chunking.py:186-253`）。 |
| T034 | [X] | ✅ | BaseEmbeddingProvider 的输入、数量和维度边界可由测试替身验证（`backend/app/ai/embedding/base.py:67-175`）。 |
| T035 | [X] | ⚠️ | 工厂和四类实现存在，但本机缺少本地依赖且云端凭据未配置；默认工厂会明确报未就绪（`factory.py:74-99`、`local.py:88-100`）。 严重度：高。 |
| T036 | [X] | ⚠️ | 203 项聚焦测试通过，但 Provider 契约全部使用 Hashing、Fake 客户端或注入模型（`tests/contract/test_embedding_provider.py:177-227`），不能证明真实 Provider 可用；该任务本身未标注“失败优先”。 严重度：中。 |
| T037 | [X] | ⚠️ | 注入 Stub Provider 时状态编排和失败清理可运行；使用当前默认配置会在 Embedding 阶段失败（`backend/app/ai/ingestion/service.py:277-316`）。 严重度：高。 |
| T038 | [X] | ✅ | 模型、迁移、HNSW/GIN 索引和 PostgreSQL 表均已存在；数据库当前版本为 `0004_document_chunks`。 |
| T039 | [X] | ⚠️ | API/UI 和 Service 已连接真实文件摄取（`backend/app/api/knowledge_bases.py:417-453`、`backend/app/ui/knowledge_base_view.py:314-373`），契约测试通过依赖注入 Stub；默认环境仍无法得到 Ready。 严重度：高。 |
| T040 | [X] | ⚠️ | 检索基础契约和错误边界通过；历史上 `f10248b` 早于 T041/T042 实现，仍没有独立的红灯执行记录可审计。 严重度：中。 |
| T041 | [X] | ✅ | 真实 PostgreSQL 向量检索和 exact 基线可运行（`vector_search.py:101-156`）；尚未用 EXPLAIN 证明 HNSW 被规划器使用。 严重度：中。 |
| T042 | [X] | ✅ | 真实 PostgreSQL `tsvector + GIN` 路径和空结果可运行（`keyword_search.py:68-112`）。中文连续文本的分词边界有限制（`keyword_search.py:10-14`）。 严重度：中。 |
| T043 | [X] | ⚠️ | 融合、去重、权重和 Top-K 代码可运行；没有真实 Embedding 数据时无法证明线上融合质量，且 Hybrid 依赖调用方先构造 `RetrievalQuery`（`hybrid_search.py:99-180`）。 严重度：高。 |
| T044 | [X] | ⚠️ | 两条适配器路线和结构化输出可测试；当前 Cross Encoder 依赖未安装、LLM Provider 未就绪，异步调用边界还会在 M3 中失败（`reranker.py:277-303`）。 严重度：高。 |
| T045 | [X] | ⚠️ | 自检四模式记录齐全，但真实记录只有 Keyword Only 成功；因此“执行器可写记录”已完成，“四模式可比较质量”尚未完成（`scripts/run_retrieval_benchmark.py:460-482`；结果文件为 `benchmark/results/retrieval_summary.csv`）。 严重度：高。 |

## 三、维度二：代码质量

### 3.1 代码冗余

| 类别 | 数量 | 示例位置 |
| :----- | :-- | :--- |
| 重复检索逻辑 | 1 组（2 份实现） | Vector 和 Keyword 各自复制了课程/知识库/资料三段过滤逻辑：`backend/app/ai/retrieval/vector_search.py:159-175`、`keyword_search.py:124-141`。Hybrid 自身复用 `resolve_filters`，没有第三份 SQL 复制。严重度：低。 |
| 重复文本处理 | 1 处 | Parser 先统一换行（`backend/app/ai/ingestion/parsers.py:130-133`），Cleaning 又重复替换换行（`backend/app/ai/ingestion/cleaning.py:61-66`）。当前结果正确但职责边界重复。严重度：低。 |

### 3.2 死代码

| 类型 | 数量 | 示例位置 |
| :------- | :-- | :--- |
| 未引用函数/常量 | 1 个可疑公共函数 | `resolve_embedding_dimension()` 只在定义和 `__all__` 中出现，没有生产调用方（`backend/app/models/document_chunk.py:37-42`、`144-148`）。严重度：低。 |
| 注释掉的大段代码 | 0 | 在指定 M2 目录中未发现 `except: pass` 或被注释的大段实现。 |

### 3.3 过度抽象

未发现需要单独列为问题的过度抽象。Parser Registry、Embedding Factory、BaseRetriever 和 BaseReranker 均有实际消费者或替身测试；`IngestionService` 的 Provider Builder 也被 Service 和契约测试使用（`backend/app/ai/ingestion/service.py:141-160`）。

### 3.4 错误处理一致性

| 失败码类别 | 统一定义位置 | 是否有遗漏 |
| :------------ | :----- | :---- |
| DOCUMENT_* | `backend/app/ai/ingestion/parsers.py:25-44`，摄取层复用并扩展提示（`service.py:60-75`） | 基本统一；但 Service 接受任意符合正则的错误码，可能绕过类别约束（`backend/app/services/knowledge_base_service.py:275-285`）。严重度：中。 |
| EMBEDDING_* | `backend/app/ai/embedding/base.py:20-27`，Service 映射 Provider 错误（`ingestion/service.py:27-40`、`288-316`） | 统一；真实 Provider 未就绪会明确失败。 |
| RETRIEVAL_* | `backend/app/ai/retrieval/base.py:28-42` | 统一；Rerank 使用独立的 `RERANK_*` 类别是合理的适配器边界。 |
| KNOWLEDGE_BASE_EMPTY | `backend/app/ai/ingestion/service.py:60-75` | 有定义、有终态语义；但没有检索层对 Document.status 的共同门禁。严重度：高。 |

另一个高风险一致性问题是：公开状态 API 允许教师把文档从 Parsing/Embedding 直接改为 Ready，Service 只验证枚举迁移和错误字段，不验证存在非空 chunk、embedding 和 search_vector（`backend/app/services/knowledge_base_service.py:574-632`；路由暴露于 `backend/app/api/knowledge_bases.py:477-502`）。检索 SQL 也只过滤向量/全文字段，不过滤 `Document.status=Ready`（`backend/app/ai/retrieval/vector_search.py:115-123`、`keyword_search.py:92-104`）。严重度：高。

### 3.5 配置项管理

| 配置项 | 默认值 | 校验 | .env.example | 文档 |
| :--------------------- | :-- | :-- | :----------- | :-- |
| HYBRID_VECTOR_WEIGHT | 0.5 | `[0,1]`（`backend/app/core/config.py:128-130`、`hybrid_search.py:66-77`） | ✅ ` .env.example:19-20` | ✅ 计划和模块注释 |
| EMBEDDING_MODEL | Provider 决定；BGE 默认 `BAAI/bge-small-zh-v1.5`（`bge.py:21`） | 本地/云端 Provider 缺失时失败（`local.py:73-86`、`openai_compatible.py:49-74`） | ✅ `.env.example:9-16` | ⚠️ 与固定 1024 维未统一；`docs/development.md` 不存在。严重度：高。 |
| EMBEDDING_DIMENSION | `None`，模型首次加载可自报维度 | `gt=0`（`backend/app/core/config.py:121-124`） | ✅ `.env.example:12-13` | ⚠️ 迁移固定 1024，未形成单一配置事实源。严重度：高。 |
| RERANK_PROVIDER | 无运行默认，必须配置 | 支持集合校验（`backend/app/core/config.py:111-112`、`232-247`） | ✅ `.env.example:17` | ✅ 计划和 Rerank 模块注释 |
| RERANK_MAX_CANDIDATES / RERANK_TIMEOUT_SECONDS | 20 / 30 秒 | `>0`（`config.py:130-133`） | ✅ `.env.example:21-24` | ✅ 模块注释；但 public config 摘要遗漏这两个值（`config.py:250-277`）。严重度：中。 |

## 四、维度三：测试质量

### 4.1 契约测试真实性

解析器契约使用真实 `pypdf` 解析器和测试生成的 PDF 字节，同时覆盖 TXT/Markdown 的真实清洗输入（`tests/contract/test_document_parsers.py:129-289`）。Embedding 契约验证了统一抽象、输入校验、批量数量和维度，但云端使用 FakeAsyncClient，本地/BGE 使用 FakeSentenceTransformer，没有真实 API 或真实模型（`tests/contract/test_embedding_provider.py:88-190`）。检索契约既有 SQLite 精确基线，也有真实 PostgreSQL fixture；PostgreSQL 不可用时会跳过，真实路径当前包含 11 个测试，覆盖 HNSW 语义查询、GIN 关键词查询、摄取写入、Hybrid 和 Rerank（`tests/contract/test_retrieval_contract.py:391-427`、`445-843`）。测试检查了索引定义和结果，但没有 `EXPLAIN`/`EXPLAIN ANALYZE` 断言来证明 HNSW/GIN 被查询规划器实际采用。严重度：中。

Service/API/UI 测试主要使用 SQLite、TestClient、Stub Provider 或纯函数，不包含真实 Compose 上传到真实 Embedding Provider 的端到端验收（`tests/contract/test_knowledge_base_upload_contract.py:1-35`、`tests/unit/ui/test_knowledge_base_upload.py:1-18`）。严重度：中。

### 4.2 失败优先原则

| 任务 | 是否真实先红后绿 | 证据 |
| :--- | :------- | :-- |
| T031 | ⚠️ 只能确认顺序，不能确认红灯日志 | 测试文件明确写“先于 T032 实现”，提交 `1360ea3` 早于 `da79ce7`；仓库未保存测试执行输出（`tests/contract/test_document_parsers.py:1-8`）。 严重度：中。 |
| T036 | ⚠️ 未要求失败优先，且无红灯记录 | `c216c21` 在 T034/T035 实现提交之后；当前测试均依赖替身 Provider（`tests/contract/test_embedding_provider.py:177-227`）。 严重度：中。 |
| T040 | ⚠️ 只能确认顺序，不能确认红灯日志 | 文件说明先于 T041/T042，`f10248b` 早于 `13764f2`/`b42c52a`；仍没有 CI 或命令输出证明曾实际失败（`tests/contract/test_retrieval_contract.py:1-12`）。 严重度：中。 |

当前复跑结果为：`python -m pytest ...`（指定 M2 单元、Service/UI 和契约测试）**203 passed、0 skipped，13.12 秒**。这证明当前实现通过现有断言，不能反推历史上已经执行过“先红后绿”。

### 4.3 覆盖率盲区

| 模块 | 有测试 | 无测试/盲区 |
| :----------------------- | :-- | :-- |
| ingestion | ✅ | 解析、清洗、分块、编排均有单元/契约；未覆盖真实大文件、并发摄取和事件循环嵌套。 严重度：低。 |
| embedding | ✅ | 抽象、工厂和错误边界有契约；无真实本地模型、真实云端 API、模型下载或 Provider 版本兼容测试。 严重度：高。 |
| retrieval | ✅ | SQLite 单元 + 真实 PostgreSQL 契约；无 HNSW/GIN 规划器验证、中文分词质量集和真实 Embedding 质量回归。 严重度：中。 |
| knowledge_base_service | ✅ | 生命周期、权限、失败状态、chunk 写入有单元；缺少“Ready 必须有完整 chunk”的反向测试。 严重度：高。 |
| API | ⚠️ | 上传/摄取契约有 7 项测试（`tests/contract/test_knowledge_base_upload_contract.py:200-300`）；状态更新、重试和异常映射的完整 HTTP 矩阵不足。 严重度：中。 |
| UI | ⚠️ | 视图纯函数和构建测试存在（`tests/unit/ui/test_knowledge_base_upload.py:58-167`）；没有浏览器/Gradio 交互级上传、进度和失败恢复验收。 严重度：低。 |

### 4.4 Benchmark 数据质量

T045 的数据是确定性规则生成的 M0 合成阅卷样本，并非 LLM 生成，因此没有“用 LLM 生成标签再评估同一个 LLM”的直接循环。它仍然存在明显的自指偏差：每个 corpus chunk 同时拼入题干、参考答案和知识点，且题干直接作为 query；唯一正样本就是这一个包含 query 原文的 chunk（`scripts/run_retrieval_benchmark.py:147-175`）。这会让 Keyword Only 结果天然偏高，不能代表独立课程资料上的召回质量。更严重的是查询向量通过 `embed_documents([case.query])` 生成，没有调用 Provider 的 `embed_query()`（`scripts/run_retrieval_benchmark.py:557-562`）；BGE 查询前缀因此被绕过，Vector/Hybrid 指标会与真实 M3 调用路径不一致。运行记录本身诚实保留失败状态（`scripts/run_retrieval_benchmark.py:361-417`），但环境的 `memory_mb` 固定为 0（`scripts/run_retrieval_benchmark.py:444-448`），CSV 还遗漏 `precision_at_10`（`scripts/run_retrieval_benchmark.py:460-482`）。上述问题严重度分别为高、高、中、低。

实际结果如下：

| 运行 | Vector Only | Keyword Only | Hybrid | Hybrid + Rerank |
| :--- | :--- | :--- | :--- | :--- |
| `real-t045`（配置 Provider） | failed / `EMBEDDING_PROVIDER_NOT_READY` | recall@5=1.0，MRR=1.0，p95=7.556ms | failed / `EMBEDDING_PROVIDER_NOT_READY` | failed / `EMBEDDING_PROVIDER_NOT_READY` |
| `selftest-t045`（Hash Stub，仅管道自检） | recall@5=0.6，MRR=0.5811 | recall@5=1.0，MRR=1.0 | recall@5=1.0，MRR=0.95 | recall@5=1.0，MRR=0.95 |

Self-test 文件已明确标记“不得作为模型质量对比结果”（`benchmark/results/README.md:9-16`），因此当前没有足以支持 M3 模式选择的真实对比数据。

### 4.5 跳过测试清单

| 测试 | skip 原因 | 恢复条件 |
| :-- | :------ | :--- |
| `tests/contract/test_retrieval_contract.py` 中 PostgreSQL fixture 下的 11 项真实检索测试 | 数据库连接失败或 `document_chunks` 表不存在时 skip（`tests/contract/test_retrieval_contract.py:391-405`） | 启动 PostgreSQL/pgvector，并执行 `alembic upgrade head`。本次环境已满足，实际 0 skip。 |
| `tests/unit/retrieval/test_reranker.py:271-280` | sentence-transformers 已安装时，专门验证“缺依赖”行为的断言被跳过 | 要验证缺依赖分支，卸载可选依赖或保留独立的 monkeypatch 测试；本次环境未安装，因此未 skip。 |
| `tests/integration/test_m0_smoke.py:73-77`（M2 运行依赖的部署门槛） | PowerShell/Docker CLI 或 Docker daemon 不可用时 skip；本次直接探测 Docker daemon 在 10 秒内超时 | 恢复 Docker daemon，并执行 Compose 三容器启动和 `/ready` 验证。 |

## 五、维度四：M3 依赖就绪度

### 5.1 M3 前置接口

| 接口 | 就绪度 | 说明 |
| :-------------------- | :----- | :-- |
| HybridSearchRetriever | ⚠️ | 可通过 `get_retriever()` 构造，结果字段和来源保留稳定；但 M3 必须自己先调用 Embedding Provider，再构造 `RetrievalQuery`，因为 Hybrid 不接受纯文本且检索层不调用 Provider（`hybrid_search.py:99-128`）。基类类型声明没有反映这一扩展输入。 严重度：中。 |
| Reranker | ❌ | `BaseReranker`/LLM/Cross Encoder 契约存在，但 LLM 路线用 `asyncio.run()` 包住异步 Provider（`reranker.py:277-303`），在 LangGraph/Grading Agent 已有事件循环时会抛 `RuntimeError`；当前 Cross Encoder 依赖也未安装。 严重度：高。 |
| DocumentChunk | ⚠️ | 关系、embedding、search_vector 和来源字段已落库；但固定 1024 维与 BGE 默认 small 模型存在不一致，且检索没有统一的 Ready 状态过滤（`document_chunk.py:118-140`、`vector_search.py:115-123`）。 严重度：高。 |

### 5.2 Embedding Provider 缺口

当前运行配置为 `embedding_provider=huggingface`、`embedding_model=BAAI/bge-large-zh-v1.5`、`rerank_provider=cross_encoder`；`sentence_transformers` 未安装，Embedding API key 也未配置。Local Provider 会在缺少依赖时报告未就绪（`backend/app/ai/embedding/providers/local.py:28-47`、`88-100`），因此 M3 可以用 Stub/Mock 推进单元和流程开发，但不能用它完成真实 RAG 质量验收。M3 启动前必须至少选定并验证一种真实 Embedding 路线，并为 Hybrid + Rerank 选择可用的 LLM 或 Cross Encoder；否则主观题链路只能停留在替身测试。

### 5.3 检索质量证据

真实 `real-t045` 只有 Keyword Only 产出指标；Vector Only、Hybrid 和 Hybrid + Rerank 均失败。`selftest-t045` 的指标只证明 Stub Hash Embedding、融合和 Identity Rerank 的管道行为，不能比较模型质量。因而当前不能据证据宣布某个模式更优。M3 的主观题契约仍要求 `Hybrid Retrieval -> Rerank`（`.specify/spec.md:140-145`），所以目标模式应是 `HYBRID_RERANK`；在真实 Provider 和独立标注数据验证前，不应把 Keyword Only 的 1.0 指标作为降级选择依据。

### 5.4 接口稳定性风险

1. **高**：LLM Rerank 在已有事件循环中调用 `asyncio.run`，会阻断异步 Grading Agent（`backend/app/ai/retrieval/reranker.py:277-303`）。
2. **高**：Benchmark 用 `embed_documents` 代替 `embed_query`，会绕过 BGE 查询前缀并污染模式比较（`scripts/run_retrieval_benchmark.py:557-562`、`backend/app/ai/embedding/providers/bge.py:26-29`）。
3. **高**：公开状态更新可以制造 Ready 文档，检索又不校验 Ready，M3 可能收到失败文档或缺向量上下文（`backend/app/api/knowledge_bases.py:477-502`、`backend/app/ai/retrieval/vector_search.py:115-123`）。
4. **高**：BGE small 默认维度与 `vector(1024)` 固定迁移不一致；切换 Provider 时可能在写入阶段失败（`backend/app/ai/embedding/providers/bge.py:21-59`、`migrations/versions/0004_document_chunks.py:23-24`）。

## 六、修复建议（按优先级分级）

### 高优先级（M3 启动前必须修复）

| 编号 | 问题 | 文件位置 | 建议修复方式 |
| :-- | :-- | :--- | :----- |
| H01 | 异步 Grading Agent 调用 LLM Rerank 会触发嵌套 `asyncio.run` | `backend/app/ai/retrieval/reranker.py:277-303` | 将 Rerank 暴露为异步调用，或由同步适配器在线程边界执行；保持 `BaseReranker` 的结果契约和超时/错误码不变。 |
| H02 | Ready 状态和可检索数据没有共同门禁 | `backend/app/services/knowledge_base_service.py:574-632`；`backend/app/api/knowledge_bases.py:477-502`；`backend/app/ai/retrieval/vector_search.py:115-123` | 禁止外部请求直接伪造 Ready；Ready 只由成功摄取事务设置，Vector/Keyword 查询统一 join/filter `Document.status=Ready`，并增加反向契约测试。 |
| H03 | BGE 默认模型/Provider 维度与 1024 维迁移不一致 | `backend/app/ai/embedding/providers/bge.py:21-59`；`backend/app/models/document_chunk.py:33-34`；`migrations/versions/0004_document_chunks.py:23-24` | 在 M3 前冻结一个与迁移一致的模型和维度，或把维度作为受控迁移配置；Provider、写入校验和 Benchmark 必须共享同一事实源。 |
| H04 | Benchmark 查询向量走错 Provider 操作 | `scripts/run_retrieval_benchmark.py:557-562` | 改为逐条调用 `embed_query()`，保留文档向量走 `embed_documents()`；修正后重新生成四模式真实记录。 |
| H05 | 没有真实 Embedding/Rerank 运行条件 | `backend/app/ai/embedding/providers/local.py:28-47`；`backend/app/ai/retrieval/reranker.py:332-396`；`.env.example:9-24` | M3 启动前在 Docker/开发机验证一种真实 Embedding Provider 和一种 Rerank Provider，记录模型版本、依赖版本和脱敏配置；Stub 只用于单元/流程测试。 |

### 中优先级（M3 期间可修复）

| 编号 | 问题 | 文件位置 | 建议修复方式 |
| :-- | :-- | :--- | :----- |
| M01 | Vector/Keyword 重复实现同一组过滤条件 | `backend/app/ai/retrieval/vector_search.py:159-175`；`keyword_search.py:124-141` | 抽取一个只负责 SQL 条件拼接的共享辅助函数，保持两种检索的方言和排序逻辑独立。 |
| M02 | 中文 `simple` tsvector 对连续中文文本命中有限 | `backend/app/ai/retrieval/keyword_search.py:10-14`、`87-101` | 用真实课程资料建立中文术语回归集；在决定接入分词扩展前，Benchmark 明确记录该限制，不用 Keyword Only 的合成高分代表通用质量。 |
| M03 | Provider Traceability 和运行配置摘要缺少版本/检索参数 | `backend/app/ai/embedding/base.py:89-96`；`backend/app/core/config.py:250-277` | 为 Provider/文档/查询记录版本关联，并把 Hybrid 权重、Rerank 上限/超时纳入脱敏运行摘要和 Benchmark 元数据。 |
| M04 | PostgreSQL 契约未验证 HNSW/GIN 规划器实际使用 | `tests/contract/test_retrieval_contract.py:445-576`；`migrations/versions/0004_document_chunks.py:83-100` | 在真实 PostgreSQL 集成层增加只读 EXPLAIN 断言或等价索引使用证据，不把“索引存在”当作“查询使用”。 |
| M05 | API 状态更新、重试和 UI 交互矩阵覆盖不足 | `backend/app/api/knowledge_bases.py:477-522`；`tests/contract/test_knowledge_base_upload_contract.py:200-300` | 补齐失败码、非法状态迁移、重复重试和 Ready 完整性测试；保留现有测试分层，不用 UI 空态替代 Service/API 契约。 |

### 低优先级（M4/M5 可处理）

| 编号 | 问题 | 文件位置 | 建议修复方式 |
| :-- | :-- | :--- | :----- |
| L01 | `resolve_embedding_dimension()` 没有生产调用方 | `backend/app/models/document_chunk.py:37-42` | 选择一个真实配置消费者，或在后续清理中删除该公共函数，避免形成第二个维度事实源。 |
| L02 | Benchmark 环境和汇总字段不完整 | `scripts/run_retrieval_benchmark.py:444-448`、`460-482` | 记录实际内存值，并在 CSV 汇总中补齐 `precision_at_10`；不改变 JSON 规范字段。 |
| L03 | Parser 和 Cleaning 都做换行规范化 | `backend/app/ai/ingestion/parsers.py:130-133`；`backend/app/ai/ingestion/cleaning.py:61-66` | 明确规范化责任边界并保留幂等性；此项不影响当前结果，可随 M4 文本管道整理处理。 |

## 七、结论

M2 适合作为 M3 的**代码结构基线**：DocumentChunk、来源字段、Provider/Retriever/Reranker 抽象、PostgreSQL 迁移以及现有测试边界已经可以被 M3 读取和扩展。它暂时不适合作为 M3 的**运行验收基线**，因为真实 Embedding/Rerank 尚未就绪，四模式 Benchmark 没有可比的真实结果，且异步 Rerank、Ready 门禁、Benchmark 查询向量路径和维度一致性会直接影响主观题阅卷。

M3 启动前至少应完成 H01-H05：修正异步接口，确保只有完整 Ready 文档进入检索，冻结 Provider/维度组合，改正 Benchmark 的 `embed_query` 调用，并在目标运行环境跑通真实 Embedding 与 Rerank。完成这些修复并重新生成独立标注数据的四模式 Benchmark 后，才能把 M2 作为主观题 RAG 阅卷的可靠基线。
