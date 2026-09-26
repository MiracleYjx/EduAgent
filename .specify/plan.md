# Implementation Plan: EduAgent RAG、Multi-Agent 与阅卷 Workflow

**Branch**: `.specify` | **Date**: 2026-09-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `.specify/spec.md`, with architecture decisions extracted
from `EduAgent_项目需求与技术方案_v1.1.md` sections 11, 12, 13, 14 and 19.

## Summary

本计划为 EduAgent 第一阶段的 RAG、Embedding、Rerank、Multi-Agent 和自动阅卷 Workflow
定义可实现的架构边界。MVP 使用三个默认容器：PostgreSQL、Redis 和 Backend App。
PostgreSQL 同时承载业务数据、向量数据和全文检索数据，使用 `pgvector` 完成语义检索，
使用 `tsvector + GIN` 完成关键词检索；不在 MVP 部署独立 Milvus 和 Elasticsearch。

检索链路采用：

```text
Query
 ├── Semantic Search -> pgvector
 └── Keyword Search  -> tsvector + GIN
             ↓
        Candidate Merge
             ↓
        Weighted Score Fusion
             ↓
             Top-K
             ↓
           Rerank
             ↓
        Final Context
```

AI 业务保留 Supervisor Agent、Question Agent、Grading Agent 和 Reviewer Agent；其中 Supervisor
仅作可独立调用的离线路由组件，不进入生产图。LangGraph 以有状态图表达自动阅卷：加载答卷、
题型分流、客观题规则评分或主观题检索与评分、结构化
校验、置信度判断、低置信度人工复核、重新评分、统一结果汇总和诊断生成。

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: FastAPI、Pydantic、SQLAlchemy 2.x、Alembic、LangChain、
LangGraph、OpenAI-compatible SDK、DeepSeek API、Gradio

**Storage**: PostgreSQL 16、pgvector、PostgreSQL `tsvector` 与 GIN、Redis

**Testing**: 单元测试、契约测试、集成测试和 RAG Benchmark；测试运行器采用 pytest，
具体模型效果以可重复实验记录为准

**Target Platform**: 由 Docker Compose 启动的普通开发机环境；Backend App 同时承载
FastAPI、Gradio 和 AI Service

**Project Type**: Python Web Service + Gradio Demo UI + AI/RAG Workflow

**Performance Goals**: MVP 以普通开发机可启动、可演示和可重复 Benchmark 为目标；
不预设生产级吞吐或毫秒级搜索 SLA，检索方案的效果通过 Vector Only、Keyword Only、
Hybrid 和 Hybrid + Rerank 对比实验验证

**Constraints**: MVP 不部署独立 Milvus、Elasticsearch、Nginx 或 Worker；不微调
Embedding 模型；第一版不实现复杂 RRF 或 Learning-to-Rank；Gradio 与 Streamlit
二选一且 MVP 选 Gradio；核心 AI 输出必须经过结构化解析和 Pydantic Validation；
低置信度评分必须能够进入人工复核

**Scale/Scope**: 第一阶段为单体部署的 MVP，面向普通开发机和面试演示规模；PostgreSQL
统一检索底座适用于当前数据规模，Milvus Lite 仅保留为后续召回差异、大规模迁移或向量
数据库对比实验的可选路线

### M0 启动依赖、就绪检查与失败行为

M0 将“容器进程启动”和“应用可用”区分为两个阶段，启动依赖关系固定为：

```text
配置文件加载
    ↓
PostgreSQL 与 Redis 容器健康
    ↓
Backend App 启动并连接依赖
    ↓
Backend readiness healthcheck 通过
    ↓
允许业务请求与后续迁移/冒烟验证
```

具体约束如下：

1. `postgres` 必须先通过 `pg_isready`，`redis` 必须先通过 `redis-cli ping`；
   Backend 使用 Compose 的 `depends_on: condition: service_healthy` 等待两个依赖
   服务就绪。Backend 的 healthcheck 调用独立的 readiness 端点，该端点同时检查
   配置已加载、PostgreSQL 连接和 Redis 连接。
2. 每个 healthcheck 必须定义明确的 `interval`、`timeout`、`retries` 和
   `start_period`。就绪检查失败时，服务保持 `unhealthy`，不得被当作“已启动可用”；
   冒烟验证必须轮询并报告具体失败依赖。
3. Backend 启动阶段只允许有限次数的连接重试和等待。超过启动超时后，输出不含密钥
   的依赖诊断并以非零状态退出，或保持不可用状态供 Compose 标记为 `unhealthy`；
   不得无限重试，也不得在依赖未就绪时接受正常业务请求。
4. 数据库迁移在 Backend readiness 和 PostgreSQL healthcheck 通过后由 M0 冒烟任务
   显式执行并检查结果；Redis 连接由同一任务执行 `PING` 验证。任一检查失败都必须
   返回非零退出码并保留可定位的诊断信息。

### M0 配置外置与密钥保护

所有运行参数通过类型化配置读取，`.env.example` 只提供占位符，不提供真实密钥。MVP
配置项至少包括：

```text
DATABASE_URL
REDIS_URL
LLM_PROVIDER
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
DEEPSEEK_MODEL
EMBEDDING_PROVIDER
RERANK_PROVIDER
CONFIDENCE_THRESHOLD
```

配置加载必须在应用启动时完成类型校验；缺少必填项、URL 无效、阈值越界或 Provider
名称不支持时，应用不得进入可用状态。`DEEPSEEK_API_KEY` 等密钥只能来自环境变量或
本地未提交的 `.env` 文件，禁止硬编码到源码、测试夹具、Docker 镜像和文档示例中。
日志、Trace、Audit Log、错误响应和 Benchmark 记录只能输出配置键名或脱敏占位符，
不得写入密钥原文。T003 的验收必须检查上述配置项、占位示例、缺失配置失败和日志
脱敏四类边界。

### 错误处理策略

Provider 和 Workflow 的外部调用统一使用有限重试，避免各模块自行约定。除非场景表
另有说明，默认最多执行 3 次（1 次初始调用 + 2 次重试），退避间隔为 1 秒、2 秒，
并加入少量随机抖动；收到 Rate Limit 的 `Retry-After` 时优先遵守该值，但单次等待
不超过 30 秒。重试耗尽后必须进入明确的最终失败状态，不得无限重试。

| 场景 | 重试/退避 | Fallback | 最终业务处理 |
|---|---|---|---|
| `Timeout` | 最多 2 次重试，1s/2s 退避 | 有配置的备用 Provider 可尝试 1 次 | 仍失败则标记 `ProviderTimeout`；出题为 `GenerationFailed`，阅卷为 `Failed` 或 `Pending Review` |
| `Rate Limit` | 最多 2 次重试，遵守 `Retry-After`，上限 30s | 切换已配置的备用 Provider 1 次 | 仍受限则标记 `ProviderRateLimited`，Workflow 暂停并可恢复 |
| `Invalid JSON` | 最多 1 次结构化修复重试，间隔 1s | 不使用自由文本解析；可切换兼容 Provider 1 次 | 仍无效则标记 `StructuredOutputFailed`，不得进入正式题库或最终成绩 |
| `Empty Response` | 最多 2 次重试，1s/2s 退避 | 备用 Provider 1 次 | 仍为空则标记 `ProviderEmptyResponse`，出题失败，阅卷进入 `Failed` 或 `Pending Review` |
| `Provider Error` | 对可识别的临时错误最多 2 次重试，1s/2s 退避；永久错误不重试 | 备用 Provider 1 次 | 仍失败则标记 `ProviderFailed`，保留脱敏错误码和可恢复/不可恢复标识 |

重试必须记录 `attempt_count`、错误码和最终状态，但不得记录请求密钥或完整 Prompt
中的敏感数据。低置信度不是 Provider 错误：它必须继续走 `Pending Review`，不能被
Fallback 或重试策略静默转换为自动接受。

## 功能需求追溯表

下表将 `spec.md` 的 FR-001 至 FR-040 显式映射到本计划的设计章节和模块边界。一个
功能需求如果同时涉及领域模型、服务流程、API/UI 或 AI Workflow，则列出全部相关位置。

| 需求 | `plan.md` 设计位置 | 对应模块或边界 |
|---|---|---|
| FR-001 | §0 认证与授权设计；Phase 1 数据模型 | `User` 初始化；`auth_service.py` |
| FR-002 | §0 认证与授权设计；M0 技术上下文 | JWT 登录、配置外置；`api/auth.py`、`core/security.py` |
| FR-003 | §0 认证与授权设计；Phase 1 数据模型 | `Teacher`、`Student`、`Admin` 角色枚举 |
| FR-004 | §0 认证与授权设计；§6 UI、部署与可选工程增强 | RBAC 权限守卫；API 与 Gradio 导航边界 |
| FR-005 | §0 认证与授权设计；§6 UI、部署与可选工程增强 | Admin 用户/角色管理和运行状态视图 |
| FR-006 | §0 认证与授权设计；§4 Agent 分工 | Admin 管理域与 AI 出题/阅卷域隔离 |
| FR-007 | §0 认证与授权设计；Technical Context/Constraints | OAuth、SSO、多组织和复杂权限明确排除 |
| FR-008 | §2.1 课程、知识库与资料摄取设计；Project Structure | `Course` CRUD；`course_service.py` |
| FR-009 | §2.1 课程、知识库与资料摄取设计；§2 语义检索与 Hybrid Retrieval | Course-KnowledgeBase 绑定和检索范围 |
| FR-010 | §2.1 课程、知识库与资料摄取设计；Validation Gates | PDF、TXT、Markdown Parser 边界 |
| FR-011 | §2.1 课程、知识库与资料摄取设计；Technical Context/Constraints | DOCX 作为后续增强，不阻塞 MVP |
| FR-012 | §2.1 课程、知识库与资料摄取设计；Project Structure | Parser -> Cleaning -> Chunking -> Embedding -> Index |
| FR-013 | §1 PostgreSQL + pgvector；§2.1 课程、知识库与资料摄取设计 | `DocumentChunk` 字段、向量、全文检索和来源元数据 |
| FR-014 | §2.1 课程、知识库与资料摄取设计；§4.1 AI 出题完整链路；§5.3 主观题评分输入契约 | Course/Document/Chunk 来源追踪和课程上下文引用 |
| FR-015 | §4.2 题库、题型与审核/组卷规则；Phase 1 数据模型 | 六类题型枚举和 `Question.type` |
| FR-016 | §4.2 题库、题型与审核/组卷规则；Technical Context/Scale | 单选、判断、简答为 MVP 优先题型 |
| FR-017 | §4.2 题库、题型与审核/组卷规则；Technical Context/Constraints | 多选、填空、复杂主观题等后置边界 |
| FR-018 | §4.2 题库、题型与审核/组卷规则；Project Structure | 教师 Question CRUD 和题库管理 |
| FR-019 | §4.2 题库、题型与审核/组卷规则；Phase 1 数据模型 | Question 核心属性、评分属性、状态和创建者 |
| FR-020 | §4.2 题库、题型与审核/组卷规则；§5.1 考试提交与答卷状态 | Approved 题目组卷、Course-Question-Exam 关系 |
| FR-021 | §5.1 考试提交与答卷状态；§6 UI、部署与可选工程增强 | 学生可参加考试查询和学生视图 |
| FR-022 | §5.1 考试提交与答卷状态；Project Structure | Submission/Answer 写入和提交 API/UI |
| FR-023 | §5.1 考试提交与答卷状态；§5.2 评分结果、考试结果与诊断持久化 | 答卷、答案、题目及评分结果关系 |
| FR-024 | §4.1 AI 出题完整链路；§2.1 课程、知识库与资料摄取设计 | 课程、知识点、难度、题型和数量输入 |
| FR-025 | §4.1 AI 出题完整链路；§2 语义检索与 Hybrid Retrieval | Query Construction 到题库的完整流程 |
| FR-026 | §4.1 AI 出题完整链路；§4 Agent 分工 | Candidate Generation 与禁止自动发布 |
| FR-027 | §4.1 AI 出题完整链路；§4.2 题库、题型与审核/组卷规则 | 未审核候选不得发布、组卷或开放参加 |
| FR-028 | §4.1 AI 出题完整链路；§4.2 题库、题型与审核/组卷规则；§6 UI | 候选展示、审核状态和教师批准/退回 |
| FR-029 | §5 LangGraph 状态图与控制逻辑；§5.4 阅卷状态与分流规则 | Question Router 按题型分 Objective/Subjective |
| FR-030 | §5.4 阅卷状态与分流规则；§5.2 评分结果、考试结果与诊断持久化 | Objective 确定性评分且不调用 LLM |
| FR-031 | §5.3 主观题评分输入契约；§2.1 课程、知识库与资料摄取设计 | 题目、标准答案、评分标准、学生答案、课程上下文和检索片段 |
| FR-032 | §5.3 主观题评分输入契约；§2 语义检索与 Hybrid Retrieval；§5 LangGraph 状态图与控制逻辑 | Query Construction -> Retrieval -> Rerank -> Grading -> Validation |
| FR-033 | §5.2 评分结果、考试结果与诊断持久化；§5 LangGraph 状态图与控制逻辑 | Objective/Subjective 结果统一汇总 |
| FR-034 | §5.2 评分结果、考试结果与诊断持久化；§4 Agent 分工 | GradingResult 经过 JSON/Pydantic 后才能入业务层 |
| FR-035 | §5 LangGraph 状态图与控制逻辑；§5.2 评分结果、考试结果与诊断持久化 | Confidence Check 和自动接受/待复核分支 |
| FR-036 | §5 LangGraph 状态图与控制逻辑；§5.2 评分结果、考试结果与诊断持久化 | Pending Review、教师查看和人工确认/修改 |
| FR-037 | §5.2 评分结果、考试结果与诊断持久化；§5 LangGraph 状态图与控制逻辑 | ReviewRecord、最终评分和结果回写 |
| FR-038 | §5.2 评分结果、考试结果与诊断持久化；§6 UI、部署与可选工程增强 | DiagnosisReport 生成及学生反馈视图 |
| FR-039 | §5.2 评分结果、考试结果与诊断持久化；§6 UI、部署与可选工程增强 | 教师成绩、阅卷详情、知识盲点和学情视图 |
| FR-040 | §5.2 评分结果、考试结果与诊断持久化；§6 UI、部署与可选工程增强 | 学生成绩、错题、诊断和知识点掌握视图 |

## Architecture Decisions

### 0. 认证与授权设计

MVP 采用基础账号和 JWT 鉴权，不引入 OAuth、企业 SSO、多组织租户或复杂权限表达式。
用户初始化或注册后获得一个或多个角色，但当前业务权限按以下三个角色定义：

| 角色 | 允许的核心操作 | 明确禁止或不负责的操作 |
|---|---|---|
| Teacher | 创建课程、上传资料、管理知识库和题库、审核候选题、创建考试、查看结果、复核低置信度评分 | 不管理系统用户和角色 |
| Student | 查看有资格参加的考试、答题提交、查看自己的成绩、错题和诊断 | 不管理课程、题库、考试发布或其他学生结果 |
| Admin | 初始化/管理用户和角色、查看基础运行状态 | 不参与 AI 出题审核、阅卷复核或替代 Teacher 完成教学业务 |

认证链路为：

```text
注册/初始化用户
    -> 密码凭证校验
    -> JWT 签发
    -> 请求携带 Bearer Token
    -> JWT 验证与当前用户加载
    -> 角色权限守卫
    -> 业务 Service
```

JWT 只负责身份鉴权，角色权限由 Backend 的 RBAC 守卫执行。认证端点、当前用户加载、
角色守卫和业务 Service 必须分层，业务 Service 不得通过前端隐藏按钮代替权限判断。
Teacher、Student、Admin 的权限检查同时覆盖 API 和 Gradio 入口；越权请求统一返回
可理解的拒绝结果并记录必要的审计事件。

### 1. PostgreSQL + pgvector 替代 Milvus + Elasticsearch

原始方案包含 PostgreSQL、Redis、Milvus、Elasticsearch、Nginx 和 Worker。MVP 收敛为：

```text
PostgreSQL + pgvector
Redis
Backend App
```

PostgreSQL 的职责分为三类：

| 能力 | PostgreSQL 方案 | 说明 |
|---|---|---|
| 业务数据 | 关系表 | 用户、课程、题库、考试、答卷和评分等业务实体 |
| 语义检索 | `pgvector` | 保存知识片段 embedding；优先使用 HNSW |
| 关键词检索 | `tsvector + GIN` | 保存全文检索字段并支持术语、专有名词和编号检索 |

选择该方案的原因：

- 降低内存压力和 Docker 容器数量。
- 减少本地部署失败点，满足可运行优先。
- 让业务数据、向量数据和全文检索数据具有一致的关系追踪。
- 让普通电脑可以完成开发、Benchmark 和面试演示。
- 保留未来迁移独立向量数据库或搜索引擎的演进空间。

Milvus Lite 不是 MVP 必需基础设施。只有在需要比较 pgvector 与 Milvus 的召回差异、
研究向量数据库能力或规划大规模迁移时，才作为实验性实现。

### 2.1 课程、知识库与资料摄取设计

课程与知识库采用一对多的业务边界：一个 `Course` 可以绑定一个主知识库或多个知识库，
每个 `KnowledgeBase` 必须记录所属课程；`Document` 属于一个知识库并保留上传者、原始
文件名、格式和处理状态；`DocumentChunk` 属于一个 `Document`，同时冗余保存
`course_id` 和 `knowledge_base_id` 以便检索过滤和来源追踪。

```text
Teacher
  -> Course
  -> KnowledgeBase
  -> Document
  -> DocumentChunk
  -> Embedding + search_vector
  -> 可供出题/阅卷引用的课程上下文
```

教师侧资料入口由课程/知识库服务、API 和 Gradio 共同提供。上传时必须先校验教师对
课程和知识库的权限，再创建 `Document` 元数据并进入摄取状态机：

```text
上传文件
  -> Parsing
  -> Embedding
  -> Ready
```

任一阶段失败都进入 `Failed`，不得创建或保留可用的空知识。状态定义如下：

| 状态 | 含义 | 可执行的后续动作 |
|---|---|---|
| `Parsing` | 文件已接收，正在按格式解析、清洗和分块 | 等待处理或在失败后重新上传 |
| `Embedding` | 已形成有效文本块，正在生成 embedding 并写入检索字段 | 等待处理或在失败后重试 |
| `Ready` | 向量、全文字段和来源元数据均已完成 | 允许出题和主观题阅卷引用 |
| `Failed` | 解析、文本提取、分块或 embedding 任一环节失败 | 展示失败原因，允许修复后重新处理 |

MVP 明确支持 PDF、TXT 和 Markdown；解析器注册表按格式选择适配器。空文件、损坏文件、
无文本和不支持格式不得进入 `Ready`。DOCX 只作为后续扩展，不进入第一阶段主流程的
必需条件。每个 `DocumentChunk` 必须保留 `document_id`、`course_id`、源文件定位信息、
内容、metadata、embedding、`search_vector` 和创建时间，使 Question Agent 与 Grading
Agent 返回的上下文都能追溯到课程和原始资料。

#### 资料摄取失败码、提示与恢复路径

资料摄取失败统一将 `Document.status` 设置为 `Failed`，并保存机器可读的
`error_code`、不含敏感内容的 `error_message` 和 `retryable` 标识。教师界面显示
用户可理解的提示，同时保留“重新上传”或“重新处理”入口；失败文档不得产生可供
出题或阅卷使用的 `Ready` 知识库。

| 场景 | 状态码 | 用户提示 | 恢复路径 |
|---|---|---|---|
| 解析失败 | `DOCUMENT_PARSE_FAILED` | “资料解析失败，请检查文件内容后重新上传。” | 允许重新上传或重新处理，保留失败原因 |
| 空文件 | `DOCUMENT_EMPTY` | “上传的文件为空，请选择包含教学内容的文件。” | 更换文件后重新上传 |
| 损坏文件 | `DOCUMENT_CORRUPTED` | “文件无法读取，可能已损坏，请重新导出后上传。” | 重新导出或重新上传 |
| 不支持格式 | `DOCUMENT_UNSUPPORTED_FORMAT` | “当前仅支持 PDF、TXT 和 Markdown 文件。” | 转换为受支持格式后重新上传 |
| 无文本内容 | `DOCUMENT_NO_TEXT` | “资料中没有可提取的文本内容，请检查扫描或文件内容。” | 提供可提取文本或后续接入 OCR 后重试 |
| 空知识库 | `KNOWLEDGE_BASE_EMPTY` | “资料未形成有效知识片段，暂不能用于出题或阅卷。” | 补充有效资料并重新执行摄取 |
| Embedding 失败 | `EMBEDDING_FAILED` | “知识向量生成失败，请稍后重试或切换 Embedding Provider。” | 按 Provider 错误策略重试，必要时切换 Provider |

对于 `DOCUMENT_PARSE_FAILED`、`DOCUMENT_CORRUPTED`、`EMBEDDING_FAILED` 等临时性
错误，系统可按统一错误处理策略重试；对于空文件、不支持格式和无文本内容，不得
无意义重试，必须先修正输入。`KNOWLEDGE_BASE_EMPTY` 是摄取流程的终态失败结果，
不得被解释为可检索的空上下文。

### 2. 语义检索、关键词检索与 Hybrid Retrieval

语义检索将查询转换为 query embedding，在 `pgvector` 中获取候选知识片段。向量索引
优先选择 HNSW；数据量较小时允许使用精确近邻搜索作为 Benchmark 基线。

关键词检索使用 PostgreSQL `tsvector` 和 GIN 索引，覆盖语义检索容易遗漏的术语、
专有名词、编号和精确词形。

Hybrid Retrieval 按以下阶段执行：

1. 接收 Query。
2. 并行或顺序执行语义检索与关键词检索。
3. 合并两路候选并去重。
4. 使用 Weighted Score Fusion 统一候选分数。
5. 截取 Top-K 候选。
6. 使用 Reranker 重新排序。
7. 返回 Final Context，并保留来源知识片段标识。

第一版不引入复杂 RRF 或 Learning-to-Rank。Benchmark 必须比较 Vector Only、
Keyword Only、Hybrid 和 Hybrid + Rerank 四种配置。

#### 检索 Benchmark 输出与记录格式

四种检索模式必须在同一数据集和同一评测入口下分别运行，并将每次运行的完整结果
持久化到仓库根目录的 `benchmark/results/`。JSON 是单次运行的规范记录格式，CSV
是便于横向比较的汇总格式：

```text
benchmark/
└── results/
    ├── retrieval_<run_id>_<config>.json
    ├── retrieval_summary.csv
    └── README.md
```

每个 `retrieval_<run_id>_<config>.json` 至少包含：

```json
{
  "run_id": "string",
  "run_at": "ISO-8601 datetime",
  "config": "vector_only|keyword_only|hybrid|hybrid_rerank",
  "dataset_version": "string",
  "model_version": "string",
  "prompt_version": "string|null",
  "environment": {"cpu": "string", "memory_mb": 0},
  "metrics": {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0, "latency_p95_ms": 0.0},
  "results": [{"query_id": "string", "chunk_ids": [], "scores": [], "latency_ms": 0.0}],
  "analysis": "string"
}
```

`retrieval_summary.csv` 每行对应一次配置运行，至少包含 `run_id`、`run_at`、
`config`、`dataset_version`、`model_version`、`prompt_version`、核心指标和结果文件
路径。若运行失败，也必须写入 JSON/CSV 的失败状态、错误码和脱敏诊断，不能只保留
成功实验。T045 负责执行四种模式并写入上述位置，后续看板和文档只读取这些规范记录。

### 3. Embedding 与 Rerank 抽象

第一阶段不微调 Embedding 模型。Embedding 通过统一抽象隔离具体服务：

```python
class BaseEmbeddingProvider:
    async def embed_documents(...): ...
    async def embed_query(...): ...
```

可接入的 Provider 类型包括云端 Embedding API、本地 Hugging Face 模型、BGE 系列及
其他兼容模型。业务检索层只依赖抽象接口和配置，不直接绑定具体供应商。

Rerank 通过可替换适配器支持以下路线：

- `LLM Rerank`：优先减少本地模型部署成本，适合 MVP 验证完整链路。
- 轻量 Cross Encoder：当 Benchmark 证明独立重排模型更合适时接入。

具体采用哪一种路线必须以完整 RAG Benchmark 为依据，而不是预先宣称某种方案一定更好。

### 4. Agent 分工

MVP 追求任务分工合理，不追求 Agent 数量：

| Agent | 职责 | 允许的输出或动作 |
|---|---|---|
| Supervisor Agent | 离线、可独立调用的确定性路由组件：根据显式任务种类、题型及只读状态选择工具、Agent 或收尾建议；不接入生产 Workflow | 独立调用时的路由、暂停或完成建议信号，不直接驱动生产图 |
| Question Agent | 检索课程资料、生成候选题目 | 结构化候选题目 |
| Grading Agent | 分析学生答案、结合评分标准、调用 RAG | 结构化评分结果 |
| Reviewer Agent | 检查评分结果、评分理由和知识点，判断是否需要重新评分 | 复核决策、修订结果或重新评分请求 |

Supervisor 的 `decide` 是无模型调用、无数据库写入的独立纯函数，可用于离线路由判断或独立调用；
其 `route`、`pause`、`finish` 只描述建议，不代表生产工作流已执行这些动作。当前正式阅卷入口已
明确是评分任务，无需再识别任务类型，也不消费 Supervisor 决策；出题与阅卷由各自入口装配。
生产阅卷的题型分流、人工复核暂停/恢复与结束条件由现有 LangGraph 图、Workflow 服务及教师
决策负责。若未来新增跨任务入口，应单独设计实际消费决策的接入点并补生产路径测试，不能把
一次未被消费的 Supervisor 调用算作控制面接线。

Question Agent 的结果始终是候选生成，必须经过 Question Validator 和教师审核，不能自动
发布。Grading Agent 的结果必须经过 Structured Output 和 Pydantic Validation；低置信度
结果交由 Reviewer Agent 或教师人工复核。

### 4.1 AI 出题完整链路

AI 出题严格采用以下顺序，任何一步失败都不得直接产生正式题库内容：

```text
教师输入需求
  -> Query Construction
  -> 知识库检索
  -> Question Agent
  -> Pydantic Schema
  -> Question Validator
  -> 教师审核
  -> 题库
```

各节点职责和输入输出如下：

1. **教师输入需求**：提供课程、知识点、难度、题型和数量；系统校验课程和知识库绑定。
2. **Query Construction**：将教师条件转化为检索查询和生成约束，保留课程、知识点和
   难度过滤条件。
3. **知识库检索**：按 Vector Only、Keyword Only、Hybrid 或 Hybrid + Rerank 获取带来源
   的课程片段；无足够上下文时返回检索不足，不允许静默编造。
4. **Question Agent**：结合检索片段生成候选题目、参考答案、评分标准、难度和知识点，
   输出只能标记为 `Candidate Generation`。
5. **Pydantic Schema**：解析 JSON 并校验题型、必填字段、分值、选项和知识点格式；
   校验失败的结果进入生成失败或待修订状态。
6. **Question Validator**：检查题目是否有课程依据、答案是否完整、评分标准是否可用，
   并将候选题状态设为 `Pending Review` 或 `Needs Revision`。
7. **教师审核**：教师查看候选内容、来源片段和审核状态，执行批准或退回修订。
8. **题库**：只有审核通过后才转换为 `Approved` 正式题目；Approved 题目才允许进入
   Exam 组卷，任何 Candidate、Draft、Pending Review 或 Needs Revision 题目都不得发布。

### 4.2 题库、题型与审核/组卷规则

题库统一使用以下六类题型标识：

```text
SINGLE_CHOICE
MULTIPLE_CHOICE
TRUE_FALSE
FILL_BLANK
SHORT_ANSWER
ESSAY
```

MVP 优先实现 `SINGLE_CHOICE`、`TRUE_FALSE` 和 `SHORT_ANSWER`。`MULTIPLE_CHOICE`、
`FILL_BLANK`、复杂主观题、编程题和图文题保留为后续扩展，不得改变 MVP 的题目、考试、
答卷和阅卷主流程。Question 至少包含题型、内容、选项、参考答案、评分标准、难度、
知识点、分值、审核状态和创建者。

人工建题与 AI 出题共享 Question 领域模型，但入口和状态来源不同：

| 来源 | 初始状态 | 审核要求 |
|---|---|---|
| 教师人工创建 | `Draft` | 教师可编辑并提交审核，发布前必须达到 `Approved` |
| Question Agent 生成 | `Pending Review`，并保留 `Candidate Generation` 标识 | 必须由教师批准或退回 `Needs Revision`，不得自动发布 |

允许的审核状态转换为：

```text
Draft -> Pending Review -> Approved
                    └──-> Needs Revision -> Pending Review
```

Exam 创建和发布前必须检查所有关联题目均为 `Approved`，并且题目属于同一课程或明确
允许的课程范围。未审核题目不得加入可参加考试的题目集合，Teacher 的组卷 API、UI
和 Service 都必须执行同一条规则。

### 5. LangGraph 状态图与控制逻辑

LangGraph 用于表达有状态、可循环、可中断的业务流程。自动阅卷图的业务节点为：

```text
START
  ↓
Load Submission
  ↓
Classify Question
  ├── Objective → Rule Grade
  │                    ↓
  └── Subjective → Retrieve Context
                         ↓
                        Grade
                         ↓
                 Structured Validation
                         ↓
                  Confidence Check
                    ↙          ↘
                 High          Low
                  ↓             ↓
                Accept     Pending Review
                                ↓
                         Reviewer Agent
                                ↓
                             Re-grade
                                ↓
                              Accept
                  ↓             ↓
             Next Answer / Unified Result
                         ↓
                 Generate Diagnosis
                         ↓
                        END
```

图的关键逻辑：

- `Load Submission` 载入考试、题目、学生答案和评分所需上下文。
- `Classify Question` 只负责题型路由，不让模型决定客观题分数；它是生产图内的实际分流点，
  不依赖离线 Supervisor 的路由建议。
- `Objective -> Rule Grade` 使用学生答案与标准答案的确定性比较，不调用 LLM。
- `Subjective -> Retrieve Context` 先执行 Query Construction、Hybrid Retrieval 和
  Rerank，再把 Final Context 提供给 Grading Agent。
- `Structured Validation` 解析模型 JSON 并执行 Pydantic Validation；失败时不能进入
  最终成绩，应进入可重试或失败状态。
- `Confidence Check` 根据阈值进行条件分支。高置信度结果可以 Accept；低置信度结果
  进入 `Pending Review`。
- `Reviewer Agent` 检查评分结果、理由和知识点；完成复核后触发 `Re-grade` 或接受
  修订后的评分。
- 每道题处理完成后推进到下一道题；所有答案形成 Unified Result 后才生成 Diagnosis。
- 人工复核是可中断、可恢复的状态：教师修改评分后，Workflow 从待复核节点继续，而
  不是重新创建一条不相关的评分流程。生产暂停、恢复与终态判定使用图状态和检查点，
  不将独立 Supervisor 的 `pause`/`finish` 输出当作已执行的控制信号。

建议的 Workflow 状态至少包含：

```text
workflow_id
submission_id
current_answer_id
question_type
query
retrieved_context_ids
retrieved_context
grading_result
validation_status
confidence
review_status
final_results
diagnosis
error
```

### 5.1 考试提交与答卷状态

`Exam`、`Submission` 和 `Answer` 的关系固定为：

```text
Course 1 --- * Exam
Exam   1 --- * Submission
Submission 1 --- * Answer
Answer * --- 1 Question
```

`Submission` 表示一个学生针对一次 Exam 的答卷，`Answer` 表示该答卷中针对单道
Question 的答案。Answer 必须通过 Submission 关联到 Exam，不能接收不属于当前考试的
题目答案。Submission 状态为：

```text
Draft -> Submitted -> Graded -> Reviewed
```

- `Draft`：学生正在填写，允许保存答案但不触发最终阅卷。
- `Submitted`：提交动作完成后冻结本次答案快照，进入题型路由和评分。
- `Graded`：客观题和主观题均完成评分并形成统一结果；若有低置信度项，统一结果
  必须标记为待复核，不得伪装成最终成绩。
- `Reviewed`：低置信度项已经由教师确认或修改，最终结果和诊断报告完成持久化。

同一学生对同一 Exam 的重复提交由 Submission Service 按当前状态处理：`Draft` 允许
继续保存；`Submitted`、`Graded` 或 `Reviewed` 不允许覆盖原答卷，系统返回已有提交状态；
需要重新参加时必须创建新的、具备明确版本或重考标识的 Submission。缺少必要答案、答案
不属于当前 Exam 或重复提交都必须保持原有状态并返回可理解的错误信息。

### 5.2 评分结果、考试结果与诊断持久化

单题 `GradingResult`、整份答卷的 `ExamResult` 和学生 `DiagnosisReport` 采用逐层汇总
关系：

```text
Answer
  -> GradingResult
  -> ExamResult
  -> DiagnosisReport
```

- `GradingResult` 关联 `answer_id`、`submission_id`、题型、得分、理由、知识点、置信度、
  校验状态和复核状态。它只能由通过 Structured Output 和 Pydantic Validation 的结果
  创建或更新。
- `ExamResult` 关联 `submission_id`、`exam_id`、学生、各题 GradingResult、总分、结果状态
  和汇总时间。只有所有题目都有可接受的结果，或所有低置信度结果已经完成教师复核时，
  才能标记为最终结果。
- `DiagnosisReport` 关联 `exam_result_id` 和学生，聚合已接受的 GradingResult，生成掌握
  情况、薄弱知识点、错误原因和学习建议。低置信度或未校验结果不得进入最终诊断。

LangGraph 在 `Unified Result` 节点写入或更新 ExamResult，在 `Generate Diagnosis` 节点
读取最终 ExamResult 和按题知识点/错误原因，创建 DiagnosisReport。教师结果视图读取
ExamResult、GradingResult 和复核记录；学生结果视图只读取属于自己的 ExamResult 和
DiagnosisReport。教师复核后，Reviewer Agent 或人工修改会更新对应 GradingResult，
再重新计算 ExamResult，最后重新生成 DiagnosisReport，保证三者不产生脱节副本。

### 5.3 主观题评分输入契约

主观题评分必须接收完整输入集合，且每个字段都有明确来源：

| 输入 | 来源 | 用途 |
|---|---|---|
| 题目 | `Answer.question_id` 关联的 `Question.content` | 确定题目要求和作答目标 |
| 标准答案 | `Question.reference_answer` | 提供期望答案或核心要点 |
| 评分标准 | `Question.scoring_rubric` | 约束分值、等级和扣分依据 |
| 学生答案 | 当前 `Answer.content` | 作为被评估的作答内容 |
| 课程上下文 | `Question.course_id` 绑定的 `KnowledgeBase` | 限定评分使用的教学范围 |
| 检索片段列表 | Query Construction 后由 Hybrid Retrieval/Rerank 返回的 `DocumentChunk` 列表 | 提供可追溯的相关证据和 Final Context |

输入组装顺序为：

```text
Answer + Question
  -> 读取 reference_answer/scoring_rubric
  -> 根据 course_id 定位 KnowledgeBase
  -> Query Construction
  -> Hybrid Retrieval
  -> Rerank
  -> retrieved_context_ids + retrieved_context
  -> Grading Agent/LLM
```

检索片段列表必须带 `chunk_id`、来源 Document、课程标识和重排分数；检索为空或上下文
不足时，Grading Agent 不得把空上下文当作充分依据，必须返回失败或进入人工复核策略。

### 5.4 阅卷状态与分流规则

`Question Router` 只读取 Question 的题型标识，将答案分为 Objective 或 Subjective。
Objective 走确定性规则评分；Subjective 才读取 §5.3 的完整输入集合并调用 RAG、Rerank
和 Grading LLM。两路结果统一转换为 GradingResult，再由 Confidence Check 决定
`Accept` 或 `Pending Review`，避免模型输出反向改变题型或绕过状态机。

### 6. UI、部署与可选工程增强

MVP 的 Demo UI 选择 Gradio，Streamlit 作为备选，但两者不能同时引入。选择 Gradio 是因为
它更适合后续进行 AI Demo 交互和自定义组件扩展。

部署使用 Docker 和 Docker Compose。默认容器固定为：

```text
postgres
redis
backend
```

Backend 同时承载：

```text
FastAPI
Gradio
AI Service
```

Prometheus、Grafana 和 OpenTelemetry 作为后续可选工程增强，不属于 MVP 必须项。

### 7. 可观测性设计

MVP 先通过结构化日志、`AgentRun`/`WorkflowRun` 和 Benchmark 记录提供可观测性，
不要求额外部署 Prometheus、Grafana 或 OpenTelemetry。所有层级共享同一条追踪上下文，
字段命名和采集边界如下：

| 字段 | 类型/要求 | API 层 | Agent/Workflow 层 | LLM/Provider 层 |
|---|---|---|---|---|
| `request_id` | 字符串，必填；每个入口请求唯一 | 生成并贯穿请求 | 继承并写入 AgentRun/WorkflowRun | 继承到 Provider 调用记录 |
| `user_id` | 字符串，可为空；认证后填充 | 从 JWT 当前用户取得 | 记录执行发起者 | 仅作为关联字段，不发送给模型 |
| `workflow_id` | 字符串，可为空；Workflow 存在时必填 | 接收或创建 | 生成并贯穿节点 | 作为调用关联字段 |
| `model` | 字符串，可为空；记录实际模型名 | 可记录请求选择 | 记录 Agent 使用的模型 | Provider 必须记录实际模型 |
| `latency` | 数值，单位毫秒 | 记录端到端 API 延迟 | 记录节点和 Agent 总耗时 | 记录每次 Provider 调用耗时 |
| `prompt_version` | 字符串，可为空；只记录版本号 | 可透传 | 记录 Agent 使用版本 | 记录实际 Prompt 版本，不记录完整敏感 Prompt |
| `tokens` | 对象 `{input, output, total}`，不可得时为 null | 汇总可选 | 汇总节点调用 | 优先从 Provider 返回值采集 |
| `status` | `success`、`failure` 或 `pending_review` | 记录响应结果 | 记录节点/Workflow 状态 | 记录调用成功或失败 |
| `error` | 对象 `{code, message, retryable}`，无错误时为 null | 记录公开错误摘要 | 记录节点失败和恢复状态 | 记录脱敏 Provider 错误码与摘要 |

API 层负责生成 `request_id`、识别 `user_id`、记录入口状态和总延迟；Agent 层负责
传播上下文、生成或接收 `workflow_id`、记录节点状态和条件分支；LLM 层负责记录
实际 `model`、调用延迟、`prompt_version`、tokens、status 和错误摘要。任何一层都
不得把 API Key、Authorization Header、完整学生答案或未脱敏的敏感 Prompt 写入日志。

结构化事件至少使用以下形状，便于日志、Trace 和评测记录互相引用：

```json
{
  "request_id": "string",
  "user_id": "string|null",
  "workflow_id": "string|null",
  "layer": "api|agent|llm",
  "model": "string|null",
  "latency_ms": 0.0,
  "prompt_version": "string|null",
  "tokens": {"input": 0, "output": 0, "total": 0},
  "status": "success|failure|pending_review",
  "error": {"code": "string", "message": "string", "retryable": false}
}
```

#### 7.1 脱敏与数据保留边界

| 记录类型 | 脱敏规则 | 保留边界 |
|---|---|---|
| Trace（Agent/Workflow 执行追踪） | 学生答案不保存原文，只保留不可逆摘要、长度和必要字段；API Key、Authorization Header、完整 Prompt 和 Provider 密钥统一替换为 `[REDACTED]` | MVP 默认保留 30 天，按 `workflow_id` 查询；到期删除或归档脱敏汇总 |
| Audit Log（审计日志） | 保留操作者、资源、操作类型、时间、结果和关联 ID；敏感操作详情中的密钥、答案正文、凭证和隐私字段必须脱敏 | 追加写入、应用层不可修改；MVP 保留 180 天，过期删除或转入受控归档 |
| 评测记录 | 指标数值不做数值脱敏，但持久化内容只包含已去除身份和原始答案的统计结果；Benchmark JSON/CSV 不得包含学生答案原文 | `benchmark/results/` 中的汇总结果保留 180 天；原始评分数据若为复现实验暂存，仅限受限访问，默认保留 30 天，实验结束后删除 |

评测记录与生产 Trace/Audit Log 分离：评测记录用于比较数据集、模型、Prompt 和指标，
不承担生产操作审计；Audit Log 不记录完整 AI 输入输出；Trace 只记录 Workflow
诊断所需的最小摘要。三类记录通过 `request_id`/`workflow_id` 或 `run_id` 关联，
但不得互相复制受限原始数据。

## Non-Functional Acceptance Targets

MVP 面向普通开发机，不承诺生产级吞吐。默认验收环境预期为 4 vCPU、8 GB RAM；
4-8 vCPU、8-16 GB RAM 属于舒适运行范围。三容器的资源软上限为：

| 服务 | CPU 预期 | 内存预期 |
|---|---:|---:|
| PostgreSQL + pgvector | 1-2 vCPU | 1-1.5 GB |
| Redis | 0.25-0.5 vCPU | 256-512 MB |
| Backend App | 1-2 vCPU | 1.5-2 GB |
| 三容器合计 | 2-4 vCPU | 不超过 4 GB 峰值 |

响应阈值按调用类型区分：健康检查和普通 CRUD API 的 P95 应小于 1.5 秒；不包含
外部模型等待的同步 API P95 应小于 3 秒；AI Workflow 启动、状态查询和恢复接口
只负责提交/查询任务，P95 应小于 1 秒，模型实际完成延迟单独记录在 Agent/LLM
事件和 Benchmark 中，不将外部 Provider 的不稳定延迟伪装成固定 SLA。

统一验收证据使用结构化 JSON 和 CSV：每次 Benchmark 或性能验收必须记录日期时间
（`run_at`）、数据集版本（`dataset_version`）、模型版本（`model_version`）、
Prompt 版本（`prompt_version`）、配置（`config`）、环境资源、指标结果（`metrics`）、
运行状态和结果文件路径。规范位置为 `benchmark/results/`，JSON 保存单次完整运行，
CSV 保存跨运行汇总；失败运行也必须记录失败状态和脱敏诊断。

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| 宪章原则 | 设计检查 | 结果 |
|---|---|---|
| 可运行优先 | MVP 仅使用 PostgreSQL、Redis、Backend App 三个默认容器，避免独立 Milvus 和 Elasticsearch | PASS |
| AI 能力优先 | 计划重点覆盖 RAG、Embedding、Rerank、Agent、Workflow 和 Benchmark；UI 只保留 Gradio Demo | PASS |
| 模型可替换 | Embedding 使用 `BaseEmbeddingProvider`；LLM 和 Rerank 通过可替换 Provider/Adapter 隔离具体供应商 | PASS |
| 结构化输出铁律 | Grading Agent 和 Question Agent 的结果必须经过 JSON 解析与 Pydantic Validation 后才能进入业务流程 | PASS |
| 可评测原则 | Benchmark 比较 Vector Only、Keyword Only、Hybrid、Hybrid + Rerank，并记录配置、数据集、模型和 Prompt 版本 | PASS |

未发现需要例外批准的宪章违规。Phase 1 设计复核后仍为 PASS。

## Phase 0: Research & Decisions

研究结论已整理在 [research.md](research.md)。本阶段没有遗留的 `NEEDS CLARIFICATION`：

- 数据底座选择由需求文档明确冻结为 PostgreSQL + pgvector + `tsvector/GIN`。
- Embedding 不微调，使用 Provider 抽象。
- Rerank 保留 LLM Rerank 与轻量 Cross Encoder 两种适配路线，以 Benchmark 结果决定。
- Agent 角色和 LangGraph 节点、条件分支、人工复核状态均由需求文档明确。
- 技术栈和三容器部署边界由推荐技术栈章节明确。

## Phase 1: Design & Contracts

设计产物：

- [data-model.md](data-model.md)：知识库、检索、Agent、Workflow、考试评分和人工复核的
  数据模型、状态转换与校验规则。
- [contracts/](contracts/)：Embedding、RAG Retrieval、Agent/Workflow 的接口契约。
- [quickstart.md](quickstart.md)：从容器启动到端到端阅卷和 Benchmark 的验证路径。

## Project Structure

### Documentation (this feature)

```text
.specify/
├── plan.md                    # 本计划
├── research.md                # Phase 0 决策记录
├── data-model.md              # Phase 1 数据模型和状态
├── quickstart.md              # Phase 1 可运行验证指南
├── contracts/
│   ├── embedding-provider.md
│   ├── rag-retrieval.md
│   └── agent-workflow.md
└── tasks.md                   # 由 $speckit-tasks 生成，不在本次计划中创建
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── api/
│   ├── core/
│   ├── domain/
│   ├── repositories/
│   ├── services/
│   ├── schemas/
│   ├── models/
│   ├── ai/
│   │   ├── llm/
│   │   │   ├── base.py
│   │   │   ├── deepseek.py
│   │   │   └── factory.py
│   │   ├── embedding/
│   │   │   ├── base.py
│   │   │   └── providers/
│   │   ├── retrieval/
│   │   │   ├── vector_search.py
│   │   │   ├── keyword_search.py
│   │   │   ├── hybrid_search.py
│   │   │   └── reranker.py
│   │   ├── agents/
│   │   │   ├── supervisor.py
│   │   │   ├── question_agent.py
│   │   │   ├── grading_agent.py
│   │   │   └── reviewer_agent.py
│   │   ├── workflows/
│   │   │   └── grading_workflow.py
│   │   ├── prompts/
│   │   └── evaluation/
│   └── ui/
│       └── gradio_app.py
├── migrations/
├── tests/
│   ├── contract/
│   ├── integration/
│   └── unit/
└── main.py

docker-compose.yml
pyproject.toml
.env.example
README.md
```

**Structure Decision**: 采用单仓库 Backend App 结构。Backend 同时承载 FastAPI、Gradio、
AI Service、RAG Pipeline 和 LangGraph Workflow；PostgreSQL 与 Redis 作为外部依赖容器。
不单独创建 React 前端、Milvus、Elasticsearch、Nginx 或 Worker 服务，避免超出 MVP 边界。

## Validation Gates

实现计划必须至少通过以下门槛：

1. M0 冒烟验证必须执行 `docker compose up -d`，确认 PostgreSQL、Redis 和 Backend
   的 healthcheck 均通过，完成数据库迁移并验证 Redis `PING`；启动超时或依赖失败时
   必须以失败状态结束并输出脱敏诊断。
2. 一份受支持课程资料能够形成带课程和资料来源的知识片段。
3. Vector Only、Keyword Only、Hybrid 和 Hybrid + Rerank 能在同一 Benchmark 上运行并
   产出可比较记录。
4. Question Agent 只能产生待审核候选题目，不能绕过教师审核发布。
5. Objective 题走规则评分且不调用 LLM；Subjective 题走检索、重排、结构化校验和置信度
   分支。
6. 低置信度结果进入 Pending Review，教师复核后 Workflow 能恢复并生成最终结果。
7. 所有进入业务层的 AI 结果都能通过结构化 Schema 校验。
8. 普通开发机验收满足 4 vCPU/8 GB RAM 的预期环境，三容器峰值内存不超过 4 GB；
   健康检查和普通 CRUD API P95 小于 1.5 秒，不含外部模型等待的同步 API P95 小于
   3 秒，Workflow 启动/查询/恢复接口 P95 小于 1 秒。
9. 每次检索或阅卷 Benchmark 都在 `benchmark/results/` 生成规范 JSON/CSV 证据，
   至少包含 `run_at`、`dataset_version`、`model_version`、`prompt_version`、
   `metrics`、运行状态、环境和结果文件路径；失败运行同样必须留存脱敏诊断。

## Complexity Tracking

无宪章违规，不需要复杂度例外记录。单体 Backend、单库检索和四类 Agent 已是满足当前
闭环的最小可解释设计；Milvus、Elasticsearch、独立 Worker 和复杂排序算法保留为演进
或实验路线，不纳入 MVP。
