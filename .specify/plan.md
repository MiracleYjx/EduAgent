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

**v2.0 扩展版（本次为第一步，2026-10-01，分支 `deepcode`）**：保持 v1.0 文本 MVP、原功能与验收边界不变，新增独立试卷导入、OCR/人工校正、原题图片理解与复用、条件组卷、考试内分值、Windows EXE 单机交付和 UI 收敛，并保留基础考试、阅卷、复核与师生分析闭环。技术栈继续使用 PostgreSQL + pgvector、Provider 抽象、LangGraph 和 Gradio；Docker 仍为 v1.0 开发/验收主路径，EXE 是 v2.0 补充路径。本次只追加 Technical Context 的扩展信息、架构决策 §8–§14 和 FR-041–FR-052 追溯行；下文未更新的架构 §0–§7、Phase 0/1、Project Structure、Validation Gates 与非功能目标仍按 v1.0 基线解读，不代表 v2.0 已完成设计或验收。

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

### v2.0 Technical Context 补充（v1.0 字段保持不变）

**Language/Version（v2.0）**：继续使用 Python 3.12+；Windows EXE 的候选打包工具为 PyInstaller，作为构建依赖，不进入 v1.0 核心启动的必需依赖。

**Primary Dependencies（v2.0）**：保留 FastAPI、Pydantic、SQLAlchemy 2.x、Alembic、LangChain、LangGraph、OpenAI-compatible SDK 和 Gradio；新增 OCR 可选依赖（候选 PaddleOCR 及对应推理引擎，待真实试卷、Python 3.12/Windows 与打包验证后锁定版本）及 EXE 构建依赖 PyInstaller。本次不安装依赖、不修改依赖清单，依赖可用性不以文档声明代替验证。

**Target Platform（v2.0）**：Docker Compose 保持 v1.0 主路径；新增 Windows EXE 单机路径，本机提供 FastAPI/Gradio 服务，教师和学生在本机浏览器操作。局域网共享考试留为后续选项，不纳入本版验收。

**Constraints（v2.0）**：

- 上述 v1.0 约束继续有效；PostgreSQL + pgvector、Provider 抽象、LangGraph 与 Gradio 不替换，不新增独立检索服务。
- OCR 采用可选依赖并在需要扫描识别时加载；缺 OCR 不影响 v1.0 文本 MVP 启动，但扫描试卷处理必须明确报告缺依赖，不能静默宣告识别成功。
- EXE 不替代 Docker，不等同于模型离线；云 Provider 仍需网络，数据库、pgvector 与当前 Redis 等必需服务需独立就绪。
- 图片理解通过 Provider 能力声明和结构化 DTO，不引入多模态微调、DeepSpeed 或 RLHF；AI 新出题先限文字，原题带图的理解与复用保留。
- UI 继续使用 Gradio，按规格的 Ant Design Pro/考试星/Canvas 参考统一导航、图标、表格及列表/详情/显式编辑流程；具体页面结构与验证门槛留待第二步，不自动迁移前端。

**Scale/Scope（v2.0）**：保留 v1.0 普通开发机、单体演示规模；用户已确认初始准入上限为单份试卷最多 `N = 50` 页、单题最多 `M = 5` 张图片。试卷图片按一页计，题图数量按实际关联资产计；超限在处理/关联确认时明确拒绝，不静默截断。该上限不是实测性能或模型上下文容量，需以真实扫描、跨页及带图样本验证后锁定；v2.0 资源预算和延迟目标留待第二步，不套用 v1.0 的资源指标声称 OCR/EXE 已达标。

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
| FR-041 | §8 路径分离；§9 导入校正；§11 持久存储 | 独立 PaperImport、来源页码、导入进度与题库终态 |
| FR-042 | §9 OCR 与人工校正链路 | 文本优先/OCR、结构化 DTO、原页与题目并排校正 |
| FR-043 | §10 图片能力；§11 资产存储；§12 发布冻结 | QuestionAsset 原图、条件理解、考试/阅卷复用与人工核对 |
| FR-044 | §11 文件生命周期与持久存储 | 持久目录、历史引用迁移、丢失文件真实状态 |
| FR-045 | §11 文件生命周期与持久存储 | 同一备份集中的数据库、文件与关联清单，完整恢复核对 |
| FR-046 | §9 原题入库/候选处理；§10 图片条件；复用 §4.1 | 改编父题、知识依据与候选审核；具体模型/接口后续细化 |
| FR-047 | §9 结构化校正/核验；§10 条件核对；§13 Rubric | 答案、条件、选项、评分标准核验及修订后重核验 |
| FR-048 | §12 已审核题与发布约束；§13 分值/组卷 | 题型、数量、总分、知识点覆盖、未满足条件及预览 |
| FR-049 | §12 内容冻结；§13 考试内分值 | ExamQuestion 显式题序、有效分值、换算标准与发布稳定 |
| FR-050 | §13 结果汇总；复用 §5.2、§6 | 实际最终答卷、统计分母、知识点失分与异常状态分列 |
| FR-051 | §10 带图原题复用；复用 §5.2、§6 | 薄弱知识点关联复习资料/已审核练习；来源可追溯 |
| FR-052 | §14 Windows EXE 交付；§11 持久目录 | 单机启动、配置/依赖检查、迁移、服务就绪与浏览器 |

FR-041 至 FR-052 为 v2.0 扩展追溯行；原 FR-001 至 FR-040 追溯保持不变。模型、接口、页面结构和验收门槛尚待后续步骤细化，本表不声明扩展功能已经实现。

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

### 8. 试卷导入与知识库导入的路径分离（v2.0）

```text
知识库导入：教材/讲义 -> 教学依据 -> RAG 检索
试卷导入：试卷/扫描件 -> OCR/文本提取 -> 拆题 -> 人工校正 -> Question(Draft)
```

两条路径复用 Document 的文件元数据、解析能力、课程及来源定位基础，入口、处理链路和终态独立。DocumentChunk/Embedding 只属于知识库教学资料；试卷文本及拆题结果不自动生成教学 Chunk 或进入 RAG 索引，不把 PaperImport.Ready 当成 KnowledgeBase.Ready。

已确认关联以 [data-model.md](data-model.md) §7.1/§8.1、[paper-import.md](contracts/paper-import.md) 为准：每个 PaperImport 必须通过非空、唯一 document_id 关联同课程、purpose=paper_source 的 Document；每次新的导入尝试使用新的 Document，不能复用同一原文件记录绕过一对一约束。原文件可靠保存后才提交 Document/PaperImport；原文件定位唯一事实源为 Document.storage_path，PaperImport.original_file_path 只读投影该关系。

Document 的 v2.0 用途条件为：knowledge_base（既有默认用途）必须有非空且同课程的 knowledge_base_id，paper_source 必须 knowledge_base_id=NULL。数据库条件 CHECK 与服务端课程/用途校验共同落实，不创建虚假知识库，也不放宽 v1.0 教学资料的非空知识库条件。

知识库继续按 §2.1 的 Parser -> Cleaning -> Chunking -> Embedding 与 Document/KnowledgeBase 状态规则处理。paper_source 的 Document.status 只表达原文件 Uploaded -> Ready 或 Failed，禁止 Chunking/Embedding；OCR、拆题、校正与入库进度只由 PaperImport.status 表达。Document 原文件 Ready、PaperImport.Ready 与 Question.Approved 是三个不同事实，不能相互替代。本节是目标设计同步，业务模型和迁移仍由 E1/E2 实施。

### 9. OCR 与人工校正链路（v2.0）

```text
上传试卷
  -> 可靠保存原文件 + Document(paper_source) + PaperImport(Uploaded)
  -> 解析并保存 SourcePage；文字 PDF 优先文本提取 / 扫描页走 OCR
  -> 拆题生成 ExtractedQuestion(Extracted)
  -> 结构化校验 -> Pending Correction
  -> 人工校正界面（原页 vs 结构化题目并排）
  -> 批次确认：Corrected + Question(Draft) + 来源/题图关系
```

导入状态机独立于知识库摄取和题目审核，完整状态/错误与事务边界消费 [paper-import.md](contracts/paper-import.md) 和模型 §7.1–§7.3：

```text
PaperImport:
Uploaded -> Parsing -> Extracting -> Pending Review -> Ready
Uploaded / Parsing / Extracting / Pending Review -> Failed
Pending Review -> Rejected（教师拒绝整卷，或全部暂存题被拒绝）

ExtractedQuestion:
Extracted -> Pending Correction -> Corrected + Question(Draft)
                         \
                          -> Rejected
```

| 状态 | 业务含义 |
| :--- | :--- |
| Uploaded | 原文件与 Document/PaperImport 关联可靠保存，尚未完成解析；上传成功不等于入库完成 |
| Parsing | 确定页面、保存真实页图、逐页确定文字提取或 OCR 路径；SourcePage 无独立流程状态 |
| Extracting | 提取文字/图片关联、拆题并生成通过 Schema 校验的暂存原题；无可识别题目明确失败 |
| Pending Review | 导入等待教师校正；逐题处于 Pending Correction，核对文字、边界、选项顺序、跨页、分值和题图关联 |
| Ready | 全部暂存题为 Corrected 或 Rejected，至少一题已入库；页号覆盖 1..page_count，页图/来源可靠；不表示每题有答案或已获批准 |
| Failed | 活动处理阶段发生真实技术/持久化失败，记录错误码、步骤、原因和可用材料；不把可处理的校正/请求错误升级为失败终态 |
| Rejected | Pending Review 下教师拒绝整卷且尚无 Corrected，或全部题被拒绝；与技术 Failed 区分，保留原材料 |

Ready、Failed、Rejected 为导入终态；Corrected、Rejected 为暂存题终态。已有入库题时只能处置剩余暂存题，不能拒绝整卷并删除正式题；重新解析须创建新的任务/Document，保留旧事实。成功导入后文件丢失按文件诊断报告，不能改写原成功历史为新的解析失败。

导入 Pending Review 是待校正，不等于 Question.Pending Review 的审核状态。无答案、缺 Rubric 或必要图像条件尚未可靠核对的原题，可在题干/题型/选项/明确分值/真实来源与题图关联完成核对后入库 Draft/needs_completion，不能 Approved 或用于新发布考试；未校正的 OCR/模型结果不能直接入库，不自动补答案。

同一 PaperImport 的校正/commit 在原业务锁序内串行检查；指定批次的正式 Question、来源、QuestionAsset、合法图片核对绑定与 ExtractedQuestion.question_id/Corrected 同事务提交，任一失败回滚整批，保留此前校正及原材料。已 Corrected 重复 commit 返回原 question_id，不重复建题或覆盖正式题后来修订；校正未完成、非法状态/关联、过期核对等请求错误保持原状态并返回明确错误。终态前已启动图片调用的历史结束事实补齐按模型 §15，不改冻结校正或转入条件。

校正字段与正式解析已在模型 §13、图片理解/教师核对与转入关联已在 §15 定义；契约见 [paper-import.md](contracts/paper-import.md)、[vision-capability.md](contracts/vision-capability.md)。不以 correction_notes、caption 或 UI 内存替代结构化字段和真实核对证据。

OCR 候选为 PaddleOCR，按所需识别能力选择可选依赖及推理引擎；先验证实际扫描、公式/表格、跨页与 Windows 打包，再锁定版本和资源。官方安装说明将推理依赖与训练依赖分开，本版只评估推理路径，不引入训练依赖。[PaddleOCR 安装说明](https://www.paddleocr.ai/main/en/version3.x/installation.html)

原题改编复用既有 Question Agent 候选/审核链路，父题关系、教学依据与每轮语义核验的设计已在模型 §7.5/§8.2/§12、[question-source-persistence.md](contracts/question-source-persistence.md)、[agent-workflow.md](contracts/agent-workflow.md) 明确。内容/必要图像条件变化后旧核验失效，重新核验答案、解析和评分标准；设计已补齐不等于代码、迁移或运行验收完成。本节不改写 §4.1 的 v1.0 链路。

### 10. 图片能力与 Provider 抽象（v2.0）

图片理解遵循既有“Business Service -> AI 应用服务 -> BaseLLMProvider -> Provider 实现”边界。计划在 `BaseLLMProvider` 增加 `supports_vision() -> bool`，默认返回 `False`，不增加使现有文本 Provider 失效的抽象必实现方法；当前代码尚未提供该能力声明。本次仅定义目标接口，不修改实现。

能力声明针对实际配置的模型和适配器，不按供应商名称硬编码。图片理解调用前检查能力；DeepSeek 或其他配置模型不支持图像时返回明确的“不支持图像理解”错误及当前步骤状态，不静默改为仅文本识别，也不未经配置自动切换供应商。保留原图并进入人工核对，可继续教师校正；OCR 文字不能被伪称为完成图示理解。

支持图像的 Provider 通过既有结构化输出边界返回图示/表格/条件 DTO，经 Pydantic 校验后供业务使用。新出题先支持文字；原带图题的原图保留、条件理解和复用属于本版，新图片生成与多模态微调不纳入。

`QuestionAsset` 的原图/题目/来源页关系及图序已在 [data-model.md](data-model.md) §7.4/§13 定义；题目层 image_assessment 保存理解与教师核对，承载/失效及导入转入按 §15，访问按 [file-storage.md](contracts/file-storage.md)。这些是目标设计，具体服务与 Provider 接线仍由 E2/E3 实施，支撑组卷预览、考试、阅卷和结果展示。修改图片相关条件后同步核对题干、答案、解析和评分标准；不可靠或文件缺失时显式提示，不能用替代图或旧核验结论制造成功。

### 11. 文件生命周期与持久存储（v2.0）

```text
数据库：业务实体 + 文件元数据 + 来源关系 + 检索/考试/评分/诊断
持久目录：原试卷 + 课程文件 + 页图 + 题图 + 导出文件
```

当前知识库上传入口使用 `tempfile.gettempdir()/eduagent_uploads`，不是可靠长期存储。v2.0 改为应用管理、可配置的持久根目录；数据库保存文件关联和相对定位信息。以下是该根目录下的逻辑目录，本次不创建目录：

```text
storage/uploads/   课程原文件及导出文件（导出子目录）
storage/papers/    原试卷与页图，按导入记录组织
storage/assets/    题图及相关原始资产，按资产记录组织
```

Docker 使用持久挂载，开发环境可使用项目管理目录；EXE 使用用户可写的持久数据目录，不能以系统临时目录、程序安装只读目录或打包解压目录保存唯一原文件。文件及引用重启后保持可访问；有有效业务引用的文件不得被临时清理误删。

历史迁移按“盘点现有引用 -> 将实际存在的文件复制到持久位置并核对可读 -> 事务更新引用路径和迁移状态 -> 成功后清理旧副本”执行，避免先移走文件后更新失败导致断链。丢失文件、迁移失败及来源未知分别显式记录，不回填虚假来源；保留既有知识引用快照和历史未知语义。迁移工具和回退细节留待后续步骤，不在本次执行搬移。

备份恢复以数据库和持久目录为同一备份集，保存文件关联清单，并采用一致的备份写入窗口；恢复后核对业务记录、原页、题图及资料引用。缺文件或部分恢复不得宣称完整成功，具体命令和恢复验收在后续步骤定义，不新增独立对象存储服务。

#### 11.1 T137：文件身份与备份登记承载

G04 已按用户确认方案补齐 [data-model.md](data-model.md) §14 和 [file-storage.md](contracts/file-storage.md)：复用 Document/SourcePage/QuestionAsset 身份与规范定位，暂存资产在受校验 JSON 保存服务端文件元数据，入库前后沿用稳定 file_id。导入资产的正式定位投影原导入资产记录；其他路径保持所属资源唯一写入源，不建通用 ManagedFile 表。

仅新增 ExportFile 承载真实导出，课程/考试/答卷恰一归属、真实发起者与 teacher_only/submission_owner 授权，可靠内容及 ready 提交后才可读取。共享字节保留独立业务身份与全部引用，迁移记录/OperationReceipt 如实保存原定位、步骤、错误与 UTC 时间；数据库/文件失败不能借收据制造业务成功。

BackupSet 为磁盘 manifest.json、PostgreSQL dump 和文件清单，不建备份表。采用暂停所有业务写入、排空在途任务/事务并停止写进程的维护窗口，数据库与四类持久文件/收据来自同一窗口；共享字节只复制一次，引用全部核对。恢复到隔离数据库和根目录，完整核对并记录独立恢复报告后才切换/开放写入，失败保留原环境/材料；已有新写入时不自动退回旧环境。格式版本/摘要只服务清单及字节核对，不作为组件锁步或业务成功证明。

规范导出目录以 file-storage 契约的独立 exports/ 为准；本节原目录文字及 Project Structure 的 G07 同步由 T140 单独执行，不能据旧文字启用两套查找路径。文件服务/迁移/备份工具与运行验证由 T147–T152/T191 实施；T137 仅补设计与本任务标记，不执行真实搬移/恢复，不修改其他任务状态。

### 12. 内容冻结与版本语义（v2.0）

沿用已确认的 A：服务端冻结。I01 已实现 Approved 状态下对 `content`、`options`、`reference_answer`、`scoring_rubric`、`type`、`score` 的更新守卫，返回 `QUESTION_APPROVED_IMMUTABLE`；`difficulty`、`knowledge_points` 仍可维护。内容修订需走 `Approved -> Needs Revision -> Pending Review -> Approved`，不能靠 UI 限制代替服务端检查。

发布稳定性采用 FR-049 的“发布即稳定”要求，不仅从学生开始答题才保护：

- 草稿组卷阶段可调整题序、选题和本场分值；发布时确认题目已审核、内容/答案/评分标准/题图完整，随后冻结本场关联和评分依据。
- 已发布考试引用的原题内容在引用有效期间不能被原地修改；退回修订动作也必须检查冻结引用，不能因状态变为 Needs Revision 就绕过保护。相关拒绝规则、引用存续期及修订候选处理留待第二/三步明确；这是 I01 之外的扩展工作，未宣称已实现。
- 题库难度和知识点维护仍保留，但考试的知识点归属和统计口径按发布关联稳定保存，不能随题库维护改写历史分析；该关联信息的最小字段在后续模型设计细化，不等于引入完整题目版本系统。
- 一旦学生开始答题，考试题目、题序、题图、本场分值及对应评分标准不得改变；不能通过退回修订、重新批准或替换题图改变既有考试及结果。

“组卷时可选快照”保留为第二/三步可评估的候选机制。本版已确认规格当前不引入题目版本或完整考试内容快照，本次不把候选升级为已采用策略；如后续决定使用完整快照，需用户确认并先同步 spec 的 D2/FR-049。已有阅卷 `SubmissionSnapshot` 是运行时输入对象，不能视为已存在的发布内容持久快照。

### 13. 考试内分值与评分标准换算（v2.0）

沿用已确认的 B：同一题在不同考试可使用不同分值，不覆盖 `Question.score`。将当前仅有两个 ID 的 `exam_questions` 关联扩展为 `ExamQuestion`；计划增加显式题序及 `ExamQuestion.score`，后者覆盖题库默认分值。具体表结构、字段约束、历史关联迁移及契约留待后续步骤，本次不修改模型或迁移。

```text
草稿考试：effective_score = ExamQuestion.score（已设置）
                         或 Question.score（未设置，取默认）
发布考试：确定并固定本场 effective_score
评分/复核/总分/统计：统一读取本场 effective_score
```

本场分值沿用现有正数、最多两位小数的输入校验。发布时固定有效分值；以后不再从可变化的题库值重新推断。比例 `r = effective_score / base_question_score` 使用确定的正数基准分值计算，客观题按本场分值给分且不调用 LLM；主观题 Rubric 的要点分值按比例换算，评分输入携带对应标准及本场满分，结果不得超过该满分。

当前 `Question.scoring_rubric` 为文本，不能用字符串替换数字或正则拆分决定业务分值。换算时保留文字标准语义，显式给出基准满分、比例和本场满分；有要点分值时以经过结构化校验/教师核对的要点确定性换算，不凭空生成权重。要点含糊或依据不足时标记需核对；精度、舍入、要点汇总和 DTO 形状在后续步骤明确。计算使用 Decimal，不把浮点误差当作真实得分。

输入组装、主观题评分、客观题规则、教师复核、结果汇总和诊断均使用同一份本场有效分值与标准，不能仅改变组卷展示。当前阅卷输入读取 `Question.score`，该读取需在扩展实现时贯通到考试关联；本次没有实现该切换。失败、依据不足及待复核仍单列，评分失败不记零分。

条件组卷基于已审核题目，按题型、数量、总分和知识点覆盖筛选并显示满足/未满足条件；总分按本场有效分值求和，支持题序调整、替换和完整预览，缺题或冲突不得静默放宽。教师/学生分析沿用真实答卷和最终评分，统计分母及知识点归属可追溯，不使用模型生成数字替代统计。

### 14. Windows EXE 交付（v2.0）

EXE 单机优先：启动后在本机提供服务，教师和学生在本机浏览器使用；Docker 继续作为开发/验收主路径，局域网共享考试是后续选项。候选工具 PyInstaller 先验证 onedir 包，保留 EXE 入口，单文件打包待验证后评估；官方建议先确认 onedir 正常再尝试 onefile。[PyInstaller 打包方式说明](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-file)

初期依赖已安装的本机 PostgreSQL 及所需 pgvector 扩展；数据库一体安装包是后续独立选择，不作为首版前提。Redis 当前仍是配置必填项、应用 readiness 和管理员状态检查依赖，因此本次保留；如后续希望替换或移除，先核实用途及迁移影响并形成独立决策，不能把删除 Redis 当成单纯打包调整。模型配置继续外置，云 Provider 调用仍需网络。

```text
检查配置、持久目录与模型配置
  -> 检查 PostgreSQL/pgvector、Redis 等必需依赖
  -> 执行并确认数据库迁移
  -> 启动本机 FastAPI/Gradio 服务
  -> readiness 检查通过
  -> 打开浏览器
```

迁移失败、配置缺失或服务未就绪时显示真实失败步骤，不提前打开正常业务入口或宣告成功。EXE 路径的迁移在业务服务启动前完成，不改写 v1.0 M0 的启动/冒烟流程。打包验证需覆盖 Gradio 静态资源、动态模块、迁移文件及启用 OCR 时的原生依赖/模型资源；可选 OCR 不得成为 v1.0 主服务的导入前提。

退出仅停止启动器自己启动的应用进程，不停止用户安装的 PostgreSQL/Redis；业务文件始终保存于 §11 的持久位置。启动器入口、打包清单和验证门槛留待第二/三步，不在本次新增代码或承诺包体大小/启动耗时。

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

### v2.0 非功能目标补充（第二步，2026-10-01）

以下只约束扩展版，保留上文 v1.0 的原资源、性能及证据要求。数值是待验证的目标/预算，不是实测结果，也不以第一步文档确认或 v1.0 测试通过证明 v2.0 达标；本次不运行性能实验、不改测试。

#### 资源预算（v2.0）

| 场景 | CPU 预算参考 | 内存预算参考 | 备注 |
| :--- | :--- | :--- | :--- |
| OCR 处理（单页） | 1–2 vCPU | 500 MB–1 GB | 暂定 OCR 工作集预算，随方案、模型、页分辨率实测；该占用计入 Backend App + OCR，不重复相加 |
| 图片理解（单题） | 按本地处理实测 | 按本地处理实测 | 调用云端 Provider，不加载本地图像推理模型；图片读取/解码、编码、缓存和请求仍有本地开销，计入 Backend 预算 |
| EXE 单机运行 | 2–4 vCPU | 常态目标 2–4 GB | 包含应用及 PostgreSQL/Redis；OCR 等场景峰值按下表不超过 6 GB 验收，不能用常态目标替代峰值核对 |

预算需记录实际机器、OCR/Provider 版本、图像大小和工作负载。云模型服务端资源不计为本机内存，本机请求与图片工作集不能记为零；操作系统及浏览器占用另记，不能把应用预算解释为整机最低内存保证。

#### 性能目标（v2.0）

| 场景 | 目标 | 计时边界与条件 |
| :--- | :--- | :--- |
| 单份试卷导入（≤50 页） | < 5 分钟 | 文件成功接收至进入待人工校正状态，包含原文件/页图保存、OCR/文本提取、拆题与结构化及所用 Provider 调用；不包含教师阅读、校正和确认的等待时间 |
| 人工校正响应 | < 500 ms | 用户发起页面切换至原页和结构化题目可交互，计入图片读取与渲染；不以单个接口返回时间代替完整页面响应 |
| 条件组卷（≤100 题） | < 3 秒 | 从提交选题条件至得到可预览结果或明确未满足条件；基于已审核题库，不把临时 AI 生成混入规则选题 |
| EXE 首次启动 | < 30 秒 | 外部数据库/Redis 已就绪、配置已准备且所需模型资源可用；从启动入口至服务就绪并打开浏览器，包含配置/依赖检查及本版必要数据库迁移 |
| EXE 后续启动 | < 10 秒 | 相同配置和已准备依赖，启动至服务就绪并打开浏览器，包含必要的迁移版本检查 |

≤50 页、单题≤5 图沿用已确认准入限制；≤100 题是组卷性能验收规模，不自动成为新的业务题数上限。数据库安装、首次模型资源下载、人工输入配置及大批量历史文件迁移单独记录，不伪装为后台已完成步骤；正常启动所需迁移不能从首次启动计时中删去。

每次验收保存耗时、成功/失败状态、输入规模、软硬件/配置版本和资源峰值，超时及失败样本也保留。以上目标是否可达需真实样本验证；不能剔除慢样本、隐藏云端等待或将未测量状态声明为达标。重复次数、冷/暖启动和测量口径在第三步一致性/契约细化时确认。

#### 兼容性目标（v2.0）

- v1.0 API 契约保持兼容：保留原入口、字段语义和受支持调用；扩展字段不能改变原客户端的默认行为，考试内分值未设置时保持题库默认分值语义。
- v1.0 数据库采用新增表/字段的兼容迁移，不删除原表/字段或历史数据；扩展 ExamQuestion 时保留原考试—题目关联的可读性和既有数据关系，默认值/历史填充规则留待第三步，不用虚假来源或零分掩盖缺失。
- v1.0 现有测试和验证证据全部保留，并在扩展实现后执行兼容回归；不得通过删除测试或放宽断言制造通过。确需新增/修改测试时先形成项目要求的 TCR，本次不修改测试文件。

#### 可评测目标（v2.0）

| 能力 | 标注/样本要求 | 评测口径 |
| :--- | :--- | :--- |
| 试卷导入 | 标注原题边界、页码、题干、选项、答案/解析、分值和图片关联；包含文字 PDF、扫描件、跨页题和无答案题 | 拆题完整性按正确匹配的标注题数/标注题总数，重复/漏题另计；字段准确性分字段报告正确字段数/可评字段数，区分自动提取与人工校正后的结果 |
| 语义核验 | 标注答案错误、条件不足、选项歧义和评分标准问题，并包含无问题样本及改编后旧答案失效场景 | 分类别记录 TP/FP/TN/FN；漏报率 FN/(TP+FN)，误报率 FP/(FP+TN)。无法核验/结构化失败单独报告覆盖率，不视为“无问题” |
| 图片理解 | 标注图示/表格的关键条件与题目关联，包含清晰、不清晰及应转人工核对样本 | 报告条件识别正确率、完整性及错误补充/漏识别数量；按标注核对，不能把只提取 OCR 文字当成图示理解成功 |
| 组卷 | 标注可满足与不可满足的题型、数量、总分、知识点覆盖条件，包含带图题及考试内改分 | 可满足用例报告全部硬约束满足率；不可满足用例单独核对拒绝发布、缺口说明及调整入口，不把拒绝用例计成成功组卷 |

分母为零时显示无可评数据，不填伪造的 0% 或 100%。评测数据、配置、模型/Prompt 版本、指标、失败状态和 JSON/CSV 证据沿用既有记录规范。准确性阈值依据标注集与基线实验后确定，本次不虚构 OCR 或语义核验效果提升；授权、校正确认、教师审核和发布约束等业务铁律仍须逐例验证。

#### 资源软上限（v2.0）

| 服务 | CPU 预算参考 | 内存软上限目标 |
| :--- | :--- | :--- |
| Backend App + OCR | 2–4 vCPU | 2–4 GB |
| PostgreSQL + pgvector | 1–2 vCPU | 1–1.5 GB |
| Redis | 0.25–0.5 vCPU | 256–512 MB |
| EXE 模式合计 | 2–4 vCPU 整机验收容量 | 应用与上述依赖合计不超过 6 GB 峰值 |

各组件 CPU 区间是共享整机资源下的预算参考，不代表能在 4 vCPU 主机上同时独占各自上界；OCR 内存已包含在 Backend 行，不能重复累计。验收同时记录组件峰值和整机相关进程的同时峰值，不能靠漏记 PostgreSQL/Redis 通过总量检查；超预算如实报告，选型/并行度或目标调整须后续评审，不修改 v1.0 原软上限。

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| 宪章原则 | 设计检查 | 结果 |
|---|---|---|
| 可运行优先 | MVP 仅使用 PostgreSQL、Redis、Backend App 三个默认容器，避免独立 Milvus 和 Elasticsearch | PASS |
| AI 能力优先 | 计划重点覆盖 RAG、Embedding、Rerank、Agent、Workflow 和 Benchmark；UI 只保留 Gradio Demo | PASS |
| 模型可替换 | Embedding 使用 `BaseEmbeddingProvider`；LLM 和 Rerank 通过可替换 Provider/Adapter 隔离具体供应商 | PASS |
| 结构化输出铁律 | Grading Agent 和 Question Agent 的结果必须经过 JSON 解析与 Pydantic Validation 后才能进入业务流程 | PASS |
| 可评测原则 | Benchmark 比较 Vector Only、Keyword Only、Hybrid、Hybrid + Rerank，并记录配置、数据集、模型和 Prompt 版本 | PASS |

### v2.0 Constitution Check（第三步，2026-10-01）

以下检查只判断扩展版设计是否遵循五原则，不表示功能、性能或交付已经通过运行验收；上表与 v1.0 检查结论保持不变。

| 原则 | v2.0 设计检查 | 结果 |
| :--- | :--- | :--- |
| I. 可运行优先 | 保留 PostgreSQL、Redis、Backend App 三容器主路径；EXE 是单机补充路径，OCR 为可选依赖，不影响 v1.0 启动 | PASS |
| II. AI 能力优先 | 重心转向有依据的出题、原题改编与核验，继续保留 RAG/阅卷闭环；Gradio UI 收敛服务于 AI 能力，不扩大为独立企业后台 | PASS |
| III. 模型可替换 | 图片能力通过 BaseLLMProvider 声明和调用；OCR 采用 Provider 抽象，业务层不绑定供应商 SDK | PASS |
| IV. 结构化输出 | 试卷拆题/结构化导入、图片条件理解及语义核验结果均经 Pydantic 校验再进入 Domain DTO 与业务；OCR 原始识别文本不能直接充当已确认题目 | PASS |
| V. 可评测 | 拆题完整性、字段准确性、语义核验漏报/误报、图片条件识别及组卷约束满足均已定义评测口径，效果结论仍须真实样本和实验记录 | PASS |

本次引用层设计未发现宪章违规，不需要例外批准。详细模型/契约尚待后续同步，不能据此认为迁移、Provider 能力、发布冻结或 EXE 验收已经完成。

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

### v2.0 契约引用清单（第三步）

以下六份 v2.0 契约文件已经创建，相关模型与 T134–T138 补齐内容见 [data-model.md](data-model.md) §7–§15；本节同步真实设计产物并保留现有契约的扩展范围，不声明业务代码或接口已实施。本批次只修改计划引用，不改契约正文。

**已创建的 v2.0 契约（6 项）**：

| 契约（位于 contracts/） | 用途与架构边界 |
| :--- | :--- |
| [paper-import.md](contracts/paper-import.md) | 独立试卷导入的输入/输出、§9 状态机、原页/暂存题 DTO、校正与确认入库接口；区分上传成功、校正确认、待补全和审核批准（FR-041、FR-042） |
| [ocr-provider.md](contracts/ocr-provider.md) | 按 §9 的独立 OCR Provider 抽象定义能力、输入/输出及失败语义；若后续调整抽象形态先同步决策，不能让可选依赖阻塞 v1.0（FR-042） |
| [vision-capability.md](contracts/vision-capability.md) | §10 的 supports_vision() 声明、配置模型能力、图片输入及结构化输出边界；不支持/理解不可靠的明确状态，图片访问继承课程/考试授权（FR-043） |
| [file-storage.md](contracts/file-storage.md) | §11 的持久目录、业务文件关联与生命周期、历史引用迁移、缺失/未知状态、同一备份集的一致备份恢复（FR-044、FR-045） |
| [exam-assembly.md](contracts/exam-assembly.md) | §12–§13 的题型/数量/总分/知识点约束、规则选题及冲突/缺口处理，题序、替换和预览；明确发布冻结引用、退回修订、题图与元数据维护边界（FR-048、FR-049） |
| [exam-scoring.md](contracts/exam-scoring.md) | §13 的考试内有效分值、基准满分与 Rubric 比例换算；统一评分/复核/汇总依据、精度/舍入及历史关联语义，明确最终统计分母、失败状态单列与学生反馈来源边界（FR-049 至 FR-051） |

**现有契约的 v2.0 扩展范围（3 项）**：

| 现有契约 | 扩展点 |
| :--- | :--- |
| [agent-workflow.md](contracts/agent-workflow.md) | 图片/已核对条件输入、章节与分值出题条件、候选解析和评分要点，以及答案/条件/选项/评分标准语义核验的 Pydantic 边界；修订后重核验，未解决问题、依据不足或待补全不得批准（FR-043、FR-047 与 FR-024/025/028 扩展） |
| [rag-retrieval.md](contracts/rag-retrieval.md) | 出题章节范围的输入、课程内过滤及来源语义，依据不足明确反馈；保留现有检索模式、结果与空上下文行为，不将导入原题自动索引为教学依据（§8、FR-024/025 扩展） |
| [question-source-persistence.md](contracts/question-source-persistence.md) | 原题改编的父题关系、原试卷/页码/题图关系及新引用教学依据；区分 QuestionSourcePaper 父子题关系与既有 QuestionSourceChunk 资料快照，保留历史来源未知语义（FR-041、FR-046） |

对应验收继续引用 Validation Gates 10–19 及 [spec.md](spec.md) 的 SC-010 至 SC-014。上述清单不替代契约正文；已定义字段、约束、状态/错误 DTO、授权与事务、换算及冻结规则以模型/契约正文为准，保持 v1.0 兼容。组卷条件承载与评测协议仍待 T141/T142 补齐，后续按 tasks 的依赖实施/验证，不能将设计文件存在当作接口可用。三个新增部署/使用文档（第二步 docs/ 蓝图）也不等于这里的接口契约。

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

### v2.0 扩展模块蓝图（第二步）

以下追加在 v1.0 目录树之后，保留原结构不变。标注“新增”的模块是计划位置，按批次逐步实施，不要求一次建完，也不声明已经存在；已有文件只扩展/复用。本次不创建目录、代码、脚本或部署文档。

```text
backend/app/
├── ai/
│   ├── ingestion/
│   │   ├── parsers.py              # 已有：扩展试卷解析入口
│   │   ├── ocr/                    # 新增：可选 OCR 能力
│   │   │   ├── base.py             # OCR Provider 抽象
│   │   │   ├── factory.py          # OCR Provider 工厂
│   │   │   └── providers/          # 具体实现（PaddleOCR 等，验证后锁定）
│   │   ├── paper_extraction/       # 新增
│   │   │   ├── splitter.py         # 拆题
│   │   │   ├── extractor.py        # 结构化提取
│   │   │   └── normalizer.py       # 归一化
│   │   └── paper_pipeline.py       # 新增：试卷导入编排
│   ├── vision/                     # 新增：图片理解应用边界
│   │   ├── base.py                 # 图片理解 DTO/最小接口
│   │   └── provider.py             # 委托现有 LLM Provider 的图像能力
│   └── llm/
│       └── base.py                 # 已有：扩展 supports_vision()
├── models/
│   ├── paper_import.py             # 新增
│   ├── source_page.py              # 新增
│   ├── extracted_question.py       # 新增：暂存原题/人工校正承载
│   ├── question_asset.py           # 新增
│   └── exam_question.py            # 新增模型：题序和考试内分值
├── services/
│   ├── paper_import_service.py     # 新增：导入状态/校正确认入库
│   ├── question_correction_service.py  # 新增：人工校正
│   ├── question_adaptation_service.py  # 新增：原题改编
│   ├── content_validation_service.py   # 新增：语义核验
│   ├── exam_assembly_service.py    # 新增：条件组卷
│   └── file_storage_service.py     # 新增：文件生命周期
├── api/
│   ├── paper_import.py             # 新增：导入/校正入口
│   ├── question_assets.py          # 新增：授权后的图片访问
│   └── file_storage.py             # 新增：文件访问/管理
└── ui/
    ├── paper_import_view.py        # 新增
    ├── paper_correction_view.py    # 新增
    └── design_system.py            # 当前工作区已有未提交实现，v2.0 复用/收敛

scripts/
├── migrate_storage_paths.py        # 新增：历史文件迁移
├── backup_restore.py               # 新增：备份恢复
└── build_exe.ps1                   # 新增：EXE 打包

storage/                            # 计划：持久文件根目录，实际数据不入 Git
├── uploads/
├── papers/
└── assets/

docs/
├── paper-import.md                 # 新增计划：导入/校正说明
├── file-lifecycle.md               # 新增计划：存储/迁移/备份恢复
└── exe-deployment.md               # 新增计划：单机依赖/首次配置/启动
```

模块职责延续 §8–§14：OCR 通过可选 Provider 接入；图片应用层委托 BaseLLMProvider 能力声明，不建立第二套绑定供应商的业务 SDK 路径；试卷原题与教学依据保持独立终态。导入/资产目标模型与校正 DTO 已在 data-model.md §7/§8/§13/§15 及 paper-import/vision 契约定义；ExamQuestion 目标映射与接口引用见模型 §7.6/§9/§10 和考试契约，实际模块/迁移仍待分批实施。

`storage/` 在实际启用持久目录的实施批次中加入 `.gitignore`（计划规则 `/storage/`），原文件、页图、题图和导出数据不提交仓库；Docker 持久挂载和 EXE 用户可写数据目录遵循 §11。文件通过课程/考试授权入口访问，不因忽略 Git 就成为公开静态目录。

本次受“只修改 plan.md”约束，未编辑 `.gitignore`，也未创建 `storage/`；现有 `docs/` 版本管理规则保持。已有 `design_system.py` 及相关 UI/测试改动保持原样，不包含在本次文档提交中。

## v2.0 数据模型扩展（第三步）

本节引用 [data-model.md](data-model.md) 的既有实体与状态设计，并承接 [spec.md](spec.md) 的 v2.0 Key Entities 和架构决策 §8–§13。这些目标模型及 T134–T138 的设计补齐已写入 data-model.md §7–§15；下表只列用途与关系，具体字段/约束由该文件维护，不声明已存在物理表或已运行迁移。

| 实体 | 用途 | 关联 |
| :--- | :--- | :--- |
| `PaperImport` | 独立试卷导入任务记录，区分处理、校正和入库终态 | Course；通过非空、唯一 document_id 一对一关联同课程 paper_source Document，并关联页图与暂存原题 |
| `SourcePage` | 试卷页图及来源页上下文，供 OCR 和原页对照校正 | PaperImport；原题可跨页、同页可含多题 |
| `ExtractedQuestion` | 提取后尚待人工校正/确认的结构化原题，不能直接视为 Approved 题目 | PaperImport、SourcePage；确认后关联入库 Question |
| `QuestionAsset` | 保存原题图示/表格并建立题图关系，供组卷、考试和阅卷复用 | Question、SourcePage；改编沿真实来源追溯 |
| `QuestionSourcePaper` | 记录原题改编的父子题来源关系，不覆盖父题 | Question（改编子题）→ Question（父题）；原试卷/页码另经 PaperImport、SourcePage 追溯 |
| `ExamQuestion` | 将考试—题目关联升级为带显式题序和考试内分值的实体，承载稳定发布依据 | Exam、Question；沿用原 exam_questions 关联身份与历史数据 |

命名对应：`ExtractedQuestion` 是规格中 `ImportedQuestionDraft / CorrectionRecord` 的待校正原题承载，已在模型 §7.3/§13/§15 定义校正字段与图片核对。`QuestionSourcePaper` 按本次指定的 Question → Question 关系表示父题来源，不是 PaperImport 的别名；父子题关系与原文件/页码来源均须保留，不能把知识片段引用伪称为试卷来源。

既有实体扩展边界：

- **Document 复用**：区分知识库教学文档与试卷原文件的来源语义，复用文件元数据/持久存储基础；两条处理链路及终态独立，试卷文本不自动进入 RAG 知识索引。模型 §8.1 已定义 purpose 条件：knowledge_base 必须有同课程、非空 knowledge_base_id，paper_source 的 knowledge_base_id 必须为 NULL；PaperImport.document_id 非空且唯一，原路径只读投影 Document.storage_path。paper_source 不产生 Chunk/Embedding，其 Document 原文件状态不替代导入状态；保留 v1.0 教学文档语义。
- **Question 扩展**：增加 `source_type` 区分人工、AI 生成、试卷导入和改编，结合原试卷页码、父题关系、解析、待补全及核验概念；模型 §8.2 已定义 manual/ai_generated/paper_imported/adapted，真实证据不足的历史 source_type 保留 NULL；解析及核验修订按 §12/§13，图片核对按 §15。不能根据没有来源行推断历史题是人工题。
- **ExamQuestion 升级**：从现有 `exam_questions` 两 ID 关联表升级为实体，保持原关联与 v1.0 读取兼容；草稿本场分值可覆盖题库默认，发布时固定有效分值、换算评分依据及题序，仍按服务端冻结保护引用，不引入完整题目版本/内容快照。目标关联/分值/冻结及历史未知规则见模型 §7.6/§9/§10 与考试契约；实际兼容迁移和运行核对仍由 E4 实施。
- 本表保留六项架构引用；章/节映射见 §11、独立 QuestionValidationResult 见 §12、校正扩展见 §13、文件身份/ExportFile/磁盘 BackupSet 见 §14、题目层 ImageAssessment 见 §15。ManagedFile 是资源视图，BackupSet 不建表；未在六行表中单列不表示删除相应需求，分析/推荐继续消费既有结果和来源。

详细字段、索引、约束、基数、状态/终态、冻结生命周期、舍入与历史处理以已创建的模型/契约为准；尚待 T141/T142 的内容明确保留待补齐。T139 仅同步计划及任务标记，不修改 data-model.md 或 v1.0 原文，不执行代码、迁移、测试或业务验收。

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

### v2.0 扩展验证门禁（10–19，第二步）

新增门禁不回溯改变上文 1–9 的 v1.0 要求。以下为待实现/待验收标准，本次文档更新不勾选完成状态，也不创建测试或 tasks；模型/契约细节第三步补齐后，实施测试变更遵守 TCR 流程。

10. **试卷导入链路**：

    - 在真实课程中运行“上传试卷 -> 提取 -> 校正 -> 入库”，能够查询各阶段状态及原题来源。
    - 上传成功不等于入库完成；待校正内容不进入正式可用题库，缺答案原题保留待补全，不能批准或用于考试。
    - 处理失败保留已接收原文件和可用中间结果，明确失败阶段、诊断及需修正内容，不伪造成功。

11. **OCR 与人工校正**：

    - 使用标注的文字 PDF、真实扫描件/图片、跨页和带图样本，记录拆题与字段提取结果；文字优先提取、扫描走 OCR，校正后原题字段及关联与教师标注一致。
    - 原页和结构化题目并排界面可用，教师能够修正文字、题目边界、选项顺序、跨页内容及图片关联，并明确确认入库。
    - 原文件与页图持久保存，重启可访问；未安装 OCR 时扫描路径明确报缺依赖，v1.0 文本主路径仍可运行。

12. **图片能力与授权**：

    - 支持图像的 Provider 在样本集中分析图示/表格关键条件并返回通过校验的结构化结果；不支持时明确报错，不静默改为只读文本或未经配置切换 Provider。
    - 原图保存且题图/来源页关联正确，组卷预览、考试、阅卷和结果页复用正确图片；图片相关条件变化后重核验题干、答案和评分标准。
    - 图片访问继承课程/考试授权：教师课程归属、学生参加资格及考试/结果可见状态均被检查，不能用无授权静态地址绕过；文件缺失或理解不可靠明确提示。

13. **文件生命周期**：

    - 课程文件、原试卷、页图、题图和导出文件保存于持久目录，重启后业务引用有效，不以系统临时目录作为唯一存储。
    - 盘点并完成可迁移历史引用的搬迁/路径更新；丢失文件、迁移失败或历史来源未知显式标记，不能凭空补全历史文件。
    - 数据库与持久目录按同一备份集备份/恢复，恢复后题目、资料、原页、题图、答卷与评分关系可核对；缺文件不得宣布完整恢复。

14. **条件组卷**：

    - 按题型、数量、总分和知识点覆盖从已审核题库选题，逐项展示已满足/未满足条件；缺题或冲突明确提示，不能自动降低条件发布。
    - 题序调整、题目替换、完整预览可操作，带图原题组卷后图片关联正确。
    - 可满足用例发布前满足全部条件；不可满足或包含未审核/待补全题目的用例不能发布。

15. **考试内分值**：

    - 同题在两场考试设置不同 ExamQuestion.score，未设置时取题库默认；本场生效且不覆盖原 Question.score。
    - Rubric 要点分值按本场比例换算，标准含糊时明确需核对；客观题、主观题、复核、总分和统计均使用同一有效分值，得分不超过本场满分。
    - 覆盖差异分值、默认分值、精度/舍入及历史关联迁移场景；具体数据/接口约束按第三步设计验收，不只核对前端展示。

16. **内容冻结与发布引用保护**：

    - Approved 六类内容字段的服务端拒绝守卫及结构化错误保持（I01 已实现）；difficulty/knowledge_points 的题库维护语义保持。
    - 已发布考试的题目、题序、题图、本场分值、评分标准和知识点归属保持稳定；退回修订、再次批准或元数据修改不得绕过发布引用保护。
    - 从发布到开始答题、评分、复核及历史查看核对同一稳定依据，不能仅凭 Approved 编辑拒绝测试判定此门禁全部完成；仍采用已确认的服务端冻结策略。

17. **Windows EXE 单机交付**：

    - 在配置准备好、PostgreSQL/pgvector/Redis 已就绪的单机环境中双击启动；首次配置/模型/持久目录检查明确，所需迁移自动执行并确认结果。
    - 应用 readiness 通过后自动打开浏览器，教师与学生可在本机使用；配置、迁移或依赖失败显示真实步骤，不宣告启动成功。
    - 后续启动复用持久数据，退出只停止启动器自己启动的应用进程；Docker 原验证路径保留，云 Provider 网络依赖明确。

18. **UI 收敛**：

    - 至少 3 个代表页面完成运行效果与操作验收，覆盖导入/校正、教师题库或组卷、学生首页或诊断；整体参考 Ant Design Pro、教师流程参考考试星、学生学习结果参考 Canvas。
    - 公共图标、字号、间距、按钮、状态及错误提示一致，列表/详情/显式编辑或对应查看操作可用；保留截图和关键操作记录。
    - 三页代表样板是阶段门禁，不代替 SC-014 对七类业务页面逐页验收的最终要求，也不能据此宣称全部 UI 收敛已完成。

19. **v2.0 端到端验证**：

    - 使用一次真实课程，完成“试卷导入/校正 -> 题库与教学依据 -> 文字出题或原题改编 -> 答案核验和教师审核 -> 条件组卷 -> 本机考试 -> 阅卷/复核 -> 教师考情与学生诊断/复习推荐”的完整闭环。
    - 包含带图原题、缺答案待补全、不同本场分值和需人工复核场景；统计来自实际答卷且可追溯，评分失败、依据不足、答错和待复核分别显示，失败不直接变零分。
    - 保存课程/样本、配置/模型版本、阶段状态、截图和结果证据，保留 v1.0 原有测试与验证证据；没有完成真实运行不得以文档或单元测试结果代替系统验收。

## Complexity Tracking

无宪章违规，不需要复杂度例外记录。单体 Backend、单库检索和四类 Agent 已是满足当前
闭环的最小可解释设计；Milvus、Elasticsearch、独立 Worker 和复杂排序算法保留为演进
或实验路线，不纳入 MVP。
