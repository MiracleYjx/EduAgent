---

description: "EduAgent 教学评测闭环的按里程碑划分的开发任务清单"
---

# 任务清单：EduAgent 教师培训与 AI 自动阅卷闭环

**输入**：来自 `.specify/` 的设计文档

**前置条件**：`.specify/constitution.md`、`.specify/spec.md`、`.specify/plan.md`、
`.specify/research.md`、`.specify/data-model.md`、`.specify/contracts/` 和
`.specify/quickstart.md`

**组织方式**：任务按项目里程碑 M0 到 M5 分组。`[US1]` 到 `[US4]` 标签用于追踪
`spec.md` 中的用户故事；M0 和共享基础任务不使用用户故事标签。

**测试说明**：由于宪章和计划要求单元测试、契约测试、集成测试及可重复的 AI Benchmark，
本清单包含测试任务。实现行为前应先编写对应测试，并确认测试最初失败。

## 任务汇总

| 里程碑 | 重点 | 任务数 |
| --- | --- | ---: |
| M0 | 工程骨架 + Benchmark | 13 |
| M1 | 基础业务闭环 | 17 |
| M2 | RAG | 15 |
| M3 | AI 阅卷 | 19 |
| M4 | Agent + Workflow | 15 |
| M5 | 工程增强与演示交付 | 12 |
| **总计** | | **91** |

## 阶段 M0：工程骨架 + Benchmark

**目标**：建立可启动的 Python Backend App、三容器开发环境、模型 Provider 基础和首版
Benchmark，形成后续所有里程碑共用的工程基线。

**独立验证**：执行 `docker compose up -d` 后，Backend 健康检查可用；数据库迁移和
Redis 连接成功；`BaseLLMProvider`、DeepSeek 结构化输出和 Benchmark Generator 可被
单元测试调用；Benchmark 至少生成 10 条种子数据。

- [X] T001 按 `.specify/plan.md` 在 `backend/`、`tests/`、`migrations/`、`scripts/` 和 `docs/` 下创建项目骨架
- [X] T002 在 `pyproject.toml` 中初始化 Python 3.12+ 项目元数据和依赖，包括 FastAPI、Pydantic、SQLAlchemy、Alembic、LangChain、LangGraph、OpenAI-compatible SDK、Gradio、Redis 客户端和 pytest
- [X] T003 [P] 在 `.env.example` 和 `backend/app/core/config.py` 中创建类型化运行配置，依赖 T002，明确覆盖 `DATABASE_URL`、`REDIS_URL`、`LLM_PROVIDER`、`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`EMBEDDING_PROVIDER`、`RERANK_PROVIDER` 和 `CONFIDENCE_THRESHOLD`；增加配置校验和验收，确保密钥不得硬编码、不得写入日志或错误响应，并在 `.env.example` 中只提供占位示例
- [X] T004 [P] 在 `docker-compose.yml` 中创建仅包含 `postgres`、`redis` 和 `backend` 的服务，配置开发持久化卷、PostgreSQL/Redis/Backend 健康检查，以及 Backend 的显式构建上下文、Dockerfile 和启动命令（例如 `uvicorn backend.main:app --host 0.0.0.0 --port 8000`）；构建期可与 T005 并行准备，但运行期验证必须等待 T005 的应用入口和健康端点可用
- [X] T005 在 `backend/main.py` 和 `backend/app/core/app.py` 中创建 FastAPI 应用工厂、健康检查端点、Gradio 挂载点和进程入口；入口模块与 T004 声明的 Backend 启动命令保持一致，应用健康端点作为 Compose healthcheck 的验证目标
- [X] T006 在 `backend/app/core/database.py`、`alembic.ini` 和 `migrations/` 中配置 SQLAlchemy 会话、Alembic 迁移发现、PostgreSQL 就绪检查和 pgvector 扩展初始化
- [X] T007 [P] 在 `backend/app/core/redis.py` 中实现 Redis 连接和生命周期辅助函数，并为本地开发提供连接失败信息
- [X] T008 在 `backend/app/ai/llm/base.py` 和 `backend/app/ai/llm/factory.py` 中实现与 Provider 无关的 LLM 契约和 Provider 工厂，包括 `generate_structured()`
- [X] T009 在 `backend/app/core/retry_policy.py` 和 `backend/app/ai/llm/deepseek.py` 中实现统一 Retry/Backoff/Fallback 策略与 DeepSeek JSON Output 适配器，依赖 T008 的 `BaseLLMProvider` 契约和 Provider 工厂；按计划处理 Timeout、Rate Limit、Invalid JSON、Empty Response、Provider Error，限制最多 2 次重试、遵守退避/Retry-After、记录脱敏错误状态并在耗尽后返回明确业务失败状态
- [X] T010 [P] 在 `backend/app/schemas/ai.py` 中定义候选题目和评分结果共用的 Pydantic DTO，包括分数范围、置信度范围、知识点、理由和建议
- [X] T011 在 `scripts/generate_synthetic_benchmark.py` 中实现合成 Benchmark 生成器，记录数据集版本、模型版本、Prompt 版本、评分标准、参考答案、学生答案、分数、理由、知识点和验证状态，并将脱敏的种子数据/元数据写入 `benchmark/results/`
- [X] T012 在 `scripts/smoke_m0.ps1` 和 `tests/integration/test_m0_smoke.py` 中增加独立 M0 冒烟验证：执行 `docker compose up -d`，轮询 PostgreSQL、Redis 和 Backend healthcheck 直到就绪，执行 Alembic 数据库迁移检查并验证 Redis `PING`；所有检查应有超时、诊断输出和失败退出行为。该任务依赖 T003-T007，且必须在 T004 的 Compose 配置和 T005 的应用入口可用后执行，不依赖 T008-T011
- [X] T013 在 `tests/unit/evaluation/test_benchmark.py` 中增加至少 10 条可重复的 Benchmark 种子案例和 M0 单元检查，依赖 T010 的共用 DTO 和 T011 的 Benchmark Generator，覆盖重复生成一致性和必填字段校验

**M0 检查点**：先完成 T001-T011 的工程骨架、配置、基础服务和 Provider 准备，再由 T012
独立验证 `docker compose up -d`、三个容器 healthcheck、数据库迁移和 Redis 连接；最后由
T013 验证共用 Schema、Benchmark Generator 及至少 10 条可重复种子数据。T012 不依赖
T008-T011，T013 必须在 T010 -> T011 顺序完成后执行。

**M0 依赖与顺序**：

```text
T001 -> T002
T002 -> T003/T004/T005/T006/T007/T008/T010
T008 -> T009
T010 -> T011 -> T013
T003/T004/T005/T006/T007 -> T012
```

T004 与 T005 的代码准备允许并行，但 T004 的 Backend 运行期验证必须等待 T005
应用入口和健康端点存在；T012 只依赖运行环境相关任务，不依赖 LLM Provider 或
Benchmark 任务；T012 与 T013 可在各自前置条件满足后并行。上述依赖关系无反向边，
M0 不存在循环依赖。

## 阶段 M1：基础业务闭环

**目标**：完成用户、课程、知识库元数据、题库、考试、答卷和角色边界，让教师可以用
人工创建题目组织考试，学生可以参加考试并提交答案，为 M2/M3 接入 AI 链路提供业务载体。

**独立验证**：使用 Teacher 创建课程和人工题目，创建只包含已审核题目的考试；使用
Student 完成答题和提交；使用 Admin 管理用户、角色并查看运行状态；三种角色的越权操作
均被拦截。

- [X] T014 在 `backend/app/domain/enums.py` 和 `backend/app/domain/permissions.py` 中定义领域枚举、状态值、角色权限、题型和答卷状态
- [X] T015 在 `backend/app/models/` 和 `migrations/versions/` 中创建 `User`、`Role`、`Course`、`KnowledgeBase`、`Document`、`Question`、`Exam`、`Submission` 和 `Answer` 的 SQLAlchemy 模型及初始迁移
- [X] T016 在 `backend/app/services/auth_service.py` 中实现密码/会话凭证处理、JWT 创建与验证以及当前用户加载
- [X] T017 [P] 在 `tests/contract/test_auth_contract.py` 中增加认证和 RBAC 失败优先的契约测试，覆盖登录、无效令牌以及 Teacher/Student/Admin 权限
- [X] T018 在 `backend/app/api/auth.py` 和 `backend/app/core/security.py` 中实现认证中间件、角色守卫和认证端点，使其满足 T017 的测试
- [X] T019 [P] 在 `backend/app/ui/gradio_app.py` 中实现 Gradio 应用外壳、登录状态、按角色显示的导航和统一错误提示
- [X] T020 [US4] 在 `backend/app/services/admin_service.py` 中实现 Admin 用户管理、角色管理和运行状态服务
- [X] T021 [P] [US4] 在 `backend/app/api/admin.py` 中提供 Admin 管理与运行状态操作，并连接 `backend/app/ui/admin_view.py` 的 Gradio 管理员视图
- [X] T022 [US1] 在 `backend/app/services/course_service.py` 和 `backend/app/services/knowledge_base_service.py` 中实现 Course 与 KnowledgeBase 元数据管理、课程绑定、Document 元数据上传和处理状态持久化
- [X] T023 [P] [US1] 在 `backend/app/api/courses.py` 和 `backend/app/api/knowledge_bases.py` 中提供课程、知识库和 Document 元数据操作
- [X] T024 [US1] 在 `backend/app/services/question_service.py` 中实现人工 Question CRUD 以及 Draft、Pending Review、Approved、Needs Revision 状态转换
- [X] T025 [P] [US1] 在 `backend/app/api/questions.py` 中提供 Question CRUD 和审核状态操作，并在 `backend/app/ui/question_view.py` 中实现教师题目管理
- [X] T026 [US1] 在 `backend/app/services/exam_service.py` 中实现 Exam 创建、题目关联、发布检查，以及只有 Approved 题目才能加入可参加考试的规则
- [X] T027 [P] [US1] 在 `backend/app/api/exams.py` 中提供考试创建与发布操作，并在 `backend/app/ui/exam_view.py` 中实现教师组卷界面
- [X] T028 [US2] 在 `backend/app/services/submission_service.py` 中实现可参加考试查询、Submission 创建、Answer 持久化、重复提交保护和答卷状态转换
- [X] T029 [P] [US2] 在 `backend/app/api/submissions.py` 中提供学生考试和提交操作，并在 `backend/app/ui/student_exam_view.py` 中实现学生考试界面
- [X] T030 [P] [US1] 在 `tests/integration/test_teacher_setup.py` 中增加教师准备流程集成测试，覆盖课程、Document 元数据、人工题目审核和已审核题目创建考试

**M1 检查点**：非 AI 的 Teacher -> Course -> Question -> Exam 路径和 Student ->
Submission 路径可以独立运行；Admin 管理和所有角色校验均可验证。

## 阶段 M2：RAG

**目标**：将课程资料转换为可追溯知识片段，并在 PostgreSQL 内完成语义检索、关键词
检索、加权融合和可替换 Rerank，形成四组可比较的 RAG Benchmark。

**独立验证**：一份 PDF、TXT 或 Markdown 资料能够进入 Ready 状态；每个 Chunk 同时
具有 embedding、`search_vector`、课程和资料来源；Vector Only、Keyword Only、Hybrid、
Hybrid + Rerank 在同一数据集上产出带 `chunk_id` 的结果。

- [X] T031 [US1] 在 `tests/contract/test_document_parsers.py` 中增加失败优先的解析器契约测试，覆盖 PDF、TXT、Markdown、不支持格式、空文件和损坏文件
- [X] T032 [US1] 在 `backend/app/ai/ingestion/parsers.py` 中实现受支持的 Document Parser 注册表和适配器，不支持的格式不得创建可用知识
- [X] T033 [US1] 在 `backend/app/ai/ingestion/cleaning.py` 和 `backend/app/ai/ingestion/chunking.py` 中实现文本清洗和带来源元数据的确定性分块
- [X] T034 [P] 在 `backend/app/ai/embedding/base.py` 中实现 `BaseEmbeddingProvider`，提供 `embed_documents()` 和 `embed_query()`
- [X] T035 [P] 在 `backend/app/ai/embedding/factory.py` 中实现 Embedding Provider 选择和配置，兼容云端 API、本地 Hugging Face、BGE 及其他兼容 Provider
- [X] T036 在 `tests/contract/test_embedding_provider.py` 中增加 Embedding Provider 契约测试，依赖 T034 和 T035，自动验证 `embed_documents()` 与 `embed_query()` 的输入类型、空输入处理、输出向量维度、批量数量与返回数量一致，以及 Provider 切换后仍满足同一接口契约
- [X] T037 在 `backend/app/ai/ingestion/service.py` 中实现从解析、清洗、分块、Embedding 到 Ready/Failed 状态的资料摄取编排
- [X] T038 在 `backend/app/models/document_chunk.py` 和 `migrations/versions/` 中创建 `DocumentChunk` 模型及迁移，支持 pgvector embedding、`search_vector`、元数据、来源关系、HNSW 和 GIN
- [X] T039 [US1] 在 `backend/app/services/knowledge_base_service.py`、`backend/app/api/knowledge_bases.py` 和 `backend/app/ui/knowledge_base_view.py` 中将资料上传和处理状态连接到知识库服务、API 和 Gradio 界面
- [X] T040 [US1] 在 `tests/contract/test_retrieval_contract.py` 中增加失败优先的检索契约测试，覆盖来源追踪、空上下文、模式选择和结果字段
- [X] T041 在 `backend/app/ai/retrieval/vector_search.py` 中实现 pgvector 语义检索，包括 HNSW 配置和精确近邻 Benchmark 模式
- [X] T042 在 `backend/app/ai/retrieval/keyword_search.py` 中实现 PostgreSQL `tsvector + GIN` 关键词检索
- [X] T043 在 `backend/app/ai/retrieval/hybrid_search.py` 中实现候选合并、去重、Weighted Score Fusion、Top-K 选择和检索模式切换
- [X] T044 在 `backend/app/ai/retrieval/reranker.py` 中实现 Rerank 适配器契约以及 LLM Rerank 和轻量 Cross Encoder 路径，记录重排分数和 Provider 元数据
- [X] T045 [P] 在 `tests/contract/test_retrieval_contract.py` 和 `scripts/run_retrieval_benchmark.py` 中增加检索契约覆盖和四模式 Benchmark 执行器，比较 Vector Only、Keyword Only、Hybrid 和 Hybrid + Rerank

**M2 检查点**：单一 PostgreSQL 检索底座可用，来源 ID 得以保留，Rerank 保持可替换，
Embedding Provider 契约测试通过，并且 Benchmark 可以比较所有要求的检索模式。

## 阶段 M3：AI 阅卷

**目标**：完成题型路由、客观题确定性评分、主观题 RAG + LLM 结构化评分、置信度分支、
统一成绩和学生诊断。

**独立验证**：一份同时包含客观题和主观题的 Submission 能完成评分；客观题不调用 LLM，
主观题使用课程上下文并通过结构化校验；低置信度结果不会进入最终成绩，高置信度或已
复核结果能够生成诊断报告；GradingResult、DiagnosisReport、ReviewRecord、AgentRun
和 WorkflowRun 的数据库模型及 Alembic 迁移均可独立检查。

- [X] T046 [US3] 在 `tests/unit/grading/test_question_router.py` 中增加 Objective/Subjective 路由失败优先测试
- [X] T047 [US3] 在 `backend/app/services/grading/question_router.py` 中实现 Question Router，使用题目类型而非模型输出来选择 Objective 或 Subjective
- [X] T048 [US3] 在 `tests/unit/grading/test_objective_grader.py` 中增加确定性评分失败优先测试，覆盖正确、错误、缺失和重复答案
- [X] T049 [US3] 在 `backend/app/services/grading/objective_grader.py` 中实现不调用 LLM 的 Objective 规则评分
- [X] T050 [US3] 在 `backend/app/services/grading/grading_context.py` 中实现主观题答案 Query Construction 和检索上下文组装，包含题目、标准答案、评分标准、学生答案和 Final Context
- [X] T051 [US3] 在 `tests/unit/grading/test_structured_grading.py` 中增加结构化输出和分数范围失败优先测试，覆盖 JSON 解析、Pydantic 校验、分数范围、置信度范围和无效响应
- [X] T052 [US3] 在 `backend/app/services/grading/subjective_grader.py` 中使用 LLM Provider 和 RAG 上下文实现 Subjective Grading，只返回经过校验的 GradingResult DTO
- [X] T053 [US3] 在 `backend/app/services/grading/confidence_policy.py` 中实现可配置的置信度阈值策略和 Pending Review 决策
- [X] T054 [US3] 在 `backend/app/services/grading/result_aggregator.py` 中实现 Objective/Subjective 结果汇总和最终分数计算
- [X] T055 [US2] 在 `backend/app/services/diagnosis_service.py` 中实现基于已接受或人工复核结果的 Diagnosis Report，包含掌握情况、薄弱知识点、错误原因和学习建议
- [X] T056 [US3] 在 `backend/app/api/grading.py` 中提供阅卷触发、阅卷状态和单题结构化结果端点（已完成：接口合同、权限与资源归属、重复触发与错误语义保持原有契约；结果与任务状态已接通真实仓储：单题结果按 `answer_id` 就地更新并保留复核记录关联，整批产出（单题结果 + 决策快照 + 整卷结果 + 答卷进度）在同一事务内提交且提交成功后才发布完成状态，任务状态落库 `workflow_runs`（待复核写 `Paused` 并记录原因，检查点保存快照题序、计数与脱敏错误信息），`request_id` 由触发入口生成并贯穿，遗留进行中任务在启动阶段收敛为中断失败；主观题评分链路由 `subjective_pipeline` 装配 T050 检索上下文、T052 评分器与 T053 置信度策略，Provider 未就绪时保留既有业务错误码且不伪造分数）
- [X] T057 [US2] 在 `backend/app/api/results.py` 和 `backend/app/ui/results_view.py` 中提供学生成绩、错题、诊断和知识点掌握情况视图（已完成：读模型 API、学生/教师授权边界、空态与服务端统计保持原有契约；生产装配改为真实仓储读取 `exam_results`/`grading_results`，诊断改为**只读** `diagnosis_reports`（新增诊断存储与 `Stale` 过期判定，GET 不再触发生成与 LLM 调用）；最终成绩提交成功后由执行器调用 T055 生成并按真实主键与来源汇总时间落库，非最终结果不生成、旧报告不覆盖新结果、诊断失败保留已提交成绩；`results_view` 完成学生诊断、薄弱知识点与掌握度区域的动态绑定，并接入生产 `ResultsQueryService` 派生的加载器（S01：学生刷新先用授权范围内答卷把 `exam_id` 解析为 `submission_id`），未接线或加载失败时保持明确空态，待复核仍不伪装成最终成绩）
- [X] T058 [US3] 在 `tests/integration/test_grading_pipeline.py` 中增加混合答卷集成测试，证明客观题确定性评分、主观题结构化评分、统一结果和诊断不会提前生成（已完成：用真实应用装配（真实仓储 + 快照读取器 + 评分管道 + 诊断存储与生成入口，仅替换检索候选/Embedding/Provider 外依赖）在真实 PostgreSQL 隔离 schema 上验证：客观题确定性评分且不调用 LLM、主观题检索+结构化评分并落库决策快照、低置信度进入待复核且不计入最终总分（工作流 `Paused`、诊断不生成）、全部接受后形成最终成绩与 `Ready` 诊断（来源汇总时间一致）、重新汇总后旧报告 `Stale`、新 Session 重读题序与决策一致、跨答卷写入失败时整批事务回滚；该用例暴露并修复了 `lock_submission` 使用强 `FOR UPDATE` 与子表外键插入互斥阻塞的问题（改为 `FOR NO KEY UPDATE`））
- [X] T059 [US3] 在 `scripts/run_grading_benchmark.py` 中实现可重复的 Zero-shot、RAG 和 Hybrid + Rerank 阅卷 Benchmark，记录数据集、模型、Prompt、指标、结果和分析（已完成：三路使用独立实验入口并复用公开消息构造、Payload Schema、`generate_structured` 与结果校验（不绕过正式评分约束）；新增与语料对应的合成阅卷样本 `benchmark/corpus/grading_samples.json`（区分教师评分与合成参考分）；指标定义明确（MAE/RMSE 为分数分数量纲、一致率为 1 分容差内占比、有效样本集合、失败率），无教师 Ground Truth 时三项质量指标为 `null` 并给出原因；结果写入 `benchmark/results/grading_<run_id>.json` 并追加 `grading_summary.csv`，只记录数据集/模型/Prompt 版本/策略/指标/耗时，不记密钥、完整 Prompt 或学生答案原文；失败写入 `status=failed` 与脱敏错误码，不写假分数）
- [X] T060 在 `backend/app/models/grading_result.py`、`backend/app/models/exam_result.py` 和 `migrations/versions/` 中创建 `GradingResult`、`ExamResult` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；分别关联 `Answer`/`Submission` 和 `Submission`/`Exam`，持久化单题得分、理由、知识点、置信度、校验/复核状态以及整份答卷总分、结果状态和汇总时间
- [X] T061 在 `backend/app/models/diagnosis_report.py` 和 `migrations/versions/` 中创建 `DiagnosisReport` SQLAlchemy 模型及 Alembic 迁移，依赖 T060；关联 `ExamResult`/学生，持久化掌握情况、薄弱知识点、错误原因、学习建议和生成时间
- [X] T062 在 `backend/app/models/review_record.py` 和 `migrations/versions/` 中创建 `ReviewRecord` SQLAlchemy 模型及 Alembic 迁移，依赖 T060；关联 `GradingResult`/教师，持久化复核前后分数、理由、知识点、操作类型和审查时间
- [X] T063 在 `backend/app/models/agent_run.py` 和 `migrations/versions/` 中创建 `AgentRun` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；持久化 Agent 类型、Workflow ID、输入输出摘要、状态、耗时、模型、Prompt 版本、Token 统计和错误摘要
- [X] T064 在 `backend/app/models/workflow_run.py` 和 `migrations/versions/` 中创建 `WorkflowRun` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；持久化 `workflow_id`、Submission、当前节点/答案、状态、检查点、暂停原因、重试次数、恢复状态和时间戳

**M3 检查点**：核心阅卷无需 Agent 编排即可运行；结果边界结构化、具备置信度意识、
可审计；五类评分/诊断/复核/运行实体的模型和迁移就绪，并已准备好接入 LangGraph。

## 阶段 M4：Agent + Workflow

**目标**：将 M2/M3 能力编排为四类职责清晰的 Agent 和可循环、可中断、可恢复的
LangGraph 自动阅卷 Workflow，同时完成 AI 出题候选生成和教师审核。

**独立验证**：Teacher 输入课程、知识点、难度、题型和数量后得到候选题目，必须经过校验
和教师审核；一份混合答卷能够按 LangGraph 节点完成客观/主观分支，低置信度结果暂停到
Pending Review，Teacher 复核后 Workflow 恢复并生成最终诊断。

- [X] T065 在 `backend/app/ai/agents/state.py` 和 `backend/app/ai/workflows/state.py` 中定义共用 Agent 输入/输出类型和 LangGraph 状态，包括 Workflow ID、当前答案、上下文 ID、评分结果、置信度、复核状态、最终结果、诊断和错误；依赖 T060-T064 的持久化实体边界
- [X] T066 [P] [US1] 在 `backend/app/ai/agents/supervisor.py` 中实现 Supervisor Agent 的路由和工具选择决策
- [X] T067 [US1] 在 `backend/app/ai/agents/question_agent.py` 中实现 Question Agent 的检索、候选生成 Prompt 和结构化候选输出
- [X] T068 [US1] 在 `backend/app/services/question_validator.py` 中实现 Question Validator 和候选题目状态转换，保留 Candidate Generation 并阻止 Automatic Publishing
- [X] T069 [US3] 在 `backend/app/ai/agents/grading_agent.py` 中围绕上下文检索、评分校验和置信度输出实现 Grading Agent 编排
- [X] T070 [US3] 在 `backend/app/ai/agents/reviewer_agent.py` 中实现 Reviewer Agent 对分数、理由和知识点的检查，以及重新评分决策
- [X] T071 [US3] 在 `tests/unit/workflows/test_grading_workflow.py` 中增加失败优先的 LangGraph 路由测试，覆盖 Load Submission、Classify Question、Objective Rule Grade、Subjective Retrieve/Grade、Structured Validation、Confidence Check、Accept 和 Pending Review
- [X] T072 [US3] 在 `backend/app/ai/workflows/grading_workflow.py` 中实现 LangGraph 阅卷节点和条件边，包括逐题迭代、统一结果和 Generate Diagnosis
- [X] T073 [US3] 在 `backend/app/services/workflow_checkpoint.py` 中实现基于 T064 `WorkflowRun` 模型的检查点、暂停原因、重试次数和可恢复状态持久化
- [X] T074 [US3] 在 `backend/app/services/review_service.py` 中实现 Pending Review 恢复、Teacher 确认/修改、Reviewer Agent 重新评分和最终结果持久化
- [X] T075 [P] [US1] 在 `backend/app/api/question_generation.py` 和 `backend/app/ui/question_generation_view.py` 中提供 AI 出题请求和候选题目审核操作
- [X] T076 [US3] 在 `backend/app/api/workflow.py` 中提供 LangGraph 阅卷启动、状态查询和恢复操作
- [X] T077 [US3] 在 `backend/app/api/reviews.py` 和 `backend/app/ui/review_view.py` 中提供低置信度评分详情以及 Teacher 确认/修改操作
- [X] T078 [P] [US1] 在 `tests/integration/test_question_generation_workflow.py` 中增加 Question Agent 契约和教师审核集成覆盖
- [X] T079 [US3] 在 `tests/integration/test_langgraph_grading_workflow.py` 中增加完整自动阅卷 Workflow 集成覆盖，包括客观/主观路由、结构化校验失败、低置信度暂停、Teacher 恢复、重新评分和诊断

**M4 检查点**：完整 AI 路径具备状态管理和追踪能力：Question Agent 生成可审核候选题，
Grading Workflow 支持条件路由以及 Human-in-the-loop 暂停与恢复。

## 阶段 M5：工程增强与演示交付

**目标**：增加 MCP、邮件工具、审计日志、Agent Trace、评测看板、Docker Demo 和项目
文档，将 M0-M4 能力整理为可展示、可回归的交付版本。

**独立验证**：MCP 工具能够访问已授权的课程、考试和学生数据；关键业务操作具有审计和
Agent Trace；评测看板展示检索和阅卷实验；quickstart 可以从启动到完整演示闭环；全量
回归通过。

- [X] T080 在 `backend/app/mcp/server.py` 和 `backend/app/mcp/registry.py` 中创建 MCP 服务/工具注册边界，加入授权和结构化工具结果处理
- [X] T081 [P] 在 `backend/app/mcp/tools/knowledge_tools.py` 中实现 `search_questions` 和 `query_knowledge` MCP 工具
- [X] T082 [P] 在 `backend/app/mcp/tools/report_tools.py` 中实现 `get_exam_result`、`get_student_profile` 和 `create_report` MCP 工具
- [X] T083 [P] 在 `backend/app/mcp/tools/email_tool.py` 中实现 `send_email` MCP/工具适配器，明确收件人、模板和失败处理
- [X] T084 在 `backend/app/models/audit_log.py`、`migrations/versions/` 和 `backend/app/services/audit_service.py` 中实现审计事件模型、持久化、脱敏和 180 天保留策略，以及阅卷、复核、发布和角色变更的审计钩子；敏感操作详情不得包含 API Key、凭证、答案正文或隐私字段
- [X] T085 在 `backend/app/services/trace_service.py` 和 `backend/app/api/traces.py` 中基于 T063/T064 的 `AgentRun`/`WorkflowRun` 模型实现追踪采集和追踪视图序列化，覆盖 `request_id`、`user_id`、`workflow_id`、`model`、`latency`、`prompt_version`、`tokens`、`status`、`error` 字段；排除密钥、Authorization Header、完整 Prompt 和学生答案原文，并实现 Trace 默认 30 天保留
- [X] T086 在 `backend/app/ui/evaluation_dashboard.py` 和 `backend/app/services/evaluation_service.py` 中实现检索与阅卷 Benchmark 对比评测看板
- [X] T087 在 `scripts/demo_seed.py`、`scripts/run_demo.ps1` 和 `docker-compose.yml` 中实现 Docker Demo 配置、种子数据命令和一键演示入口
- [X] T088 更新 `README.md`、`docs/architecture.md`、`docs/development.md` 和 `docs/evaluation.md`，说明三容器决策、Provider 边界、Agent 职责、Workflow 图以及 MVP/非 MVP 范围
- [X] T089 按 `.specify/quickstart.md` 在 `docs/validation-report.md` 中记录端到端验证，包括启动、四种检索模式、候选审核、混合阅卷、人工复核、诊断、资源使用和 P95 响应阈值
- [X] T090 [P] 在 `docs/observability.md` 中记录 Prometheus、Grafana 和 OpenTelemetry 的可选扩展点，不将其加入 MVP 必需容器
- [X] T091 执行 `tests/` 中的全部单元、契约、集成、Benchmark、角色边界、Docker、资源上限和 P95 检查，并在 `docs/release-checklist.md` 中记录 `run_at`、数据集/模型/Prompt 版本、配置、环境资源、指标、状态、结果路径及失败时的脱敏诊断

**M5 检查点**：EduAgent 具备可重复的 Docker Demo、可审计的 AI Workflow、可用的 MCP
工具、可视化评测证据以及与宪章一致的项目文档。

## 依赖关系与执行顺序

### 里程碑依赖

- **M0**：不依赖之前的项目实现；必须先于 M1 完成，因为它提供运行环境、配置、数据库、
  Redis、Provider、共用 Schema 和 Benchmark 基础。
- **M1**：依赖 M0；必须先于 M2 和 M3 完成，因为 RAG 和阅卷需要课程、题目、考试、答卷
  以及持久化用户。
- **M2**：依赖 M1；必须先于主观题阅卷和 Question Agent 完成，因为两者需要课程知识检索。
- **M3**：依赖 M2；提供确定性评分、结构化结果、置信度策略、统一结果和诊断，供 M4
  Workflow 编排使用。
- **M4**：依赖 M3；增加 Agent 路由、候选题审核、LangGraph 状态、复核暂停、恢复和重新评分。
- **M5**：依赖所需的 M0-M4 能力；负责打包和暴露已完成系统，不应改变核心 MVP 业务规则。

### 用户故事依赖

- **US1 教师准备课程与考试（P1）**：M0 后开始，使用 M1 的课程/题目/考试任务、M2 的
  知识摄取和 M4 的 Question Agent 审核，是建议优先实现的用户侧切片。
- **US2 学生参加考试并获得反馈（P1）**：M1 的答卷任务完成后开始；诊断和结果视图依赖
  M3，最终低置信度可见性依赖 M4。
- **US3 教师复核 AI 阅卷（P1）**：依赖 M1 的考试/答卷实体和 M2 的检索；M3 实现阅卷，
  M4 增加 Agent 编排和可恢复复核。
- **US4 管理员维护基础运行秩序（P2）**：依赖 M0 的认证基础，可在 M1 与教师和学生
  用户故事并行交付。

### 关键路径

```text
M0 工程骨架 + Benchmark
  -> M1 用户/课程/题库/考试/答卷
  -> M2 课程资料与 Hybrid RAG
  -> M3 规则评分 + 主观题结构化阅卷 + 诊断
  -> M4 Multi-Agent + LangGraph + Human-in-the-loop
  -> M5 MCP/审计/Trace/看板/演示
```

### 并行机会

- **M0**：T003、T004、T005、T006、T007、T008 和 T010 可在 T002 后按各自文件边界准备；
  T009 必须等待 T008 完成；T011 必须等待 T010；T012 必须等待 T003-T007 及 T005
  的运行前置条件；T013 必须等待 T010 -> T011，不能把 T009 或 T013 标记为无前置的并行任务。
- **M1**：认证外壳完成后，T019 可与 T020-T027 并行；Admin 任务 T020-T021 可与教师
  任务 T022-T027 并行；US1、US2 和 US4 的集成测试可在各自 API 完成后并行。
- **M2**：T034 和 T035 可与解析器工作并行；T036 必须等待 T034/T035 后执行；
  T038 完成后，向量检索 T041、关键词检索 T042 和 Rerank 适配器 T044 可并行；
  T040/T045 的检索契约覆盖可和实现同步准备。
- **M3**：客观题路由/评分 T046-T049 与主观题上下文/校验 T050-T053 可并行；
  结果汇总、诊断和 API 工作分别在对应服务完成后继续；T060、T063、T064 可在 T015
  后并行，T061/T062 在 T060 后完成，所有 T060-T064 应在 T065 Workflow 状态落地前完成。
- **M4**：T065 完成后，Supervisor、Question、Grading 和 Reviewer Agent 可并行；
  出题 API/UI 和阅卷 API/UI 在各自 Agent/Workflow 契约稳定后可并行。
- **M5**：M4 检查点后，MCP 工具、审计/Trace、评测看板、资源/性能验收、文档和可选
  可观测性说明可并行；T084/T085 的脱敏与保留策略必须在 T091 发布验收前完成。

## 并行执行示例

### M0

```text
任务 T003：在 .env.example 和 backend/app/core/config.py 中实现配置
任务 T004：在 docker-compose.yml 中实现 Docker Compose
任务 T005：在 backend/main.py 和 backend/app/core/app.py 中实现应用入口与健康端点
任务 T007：在 backend/app/core/redis.py 中实现 Redis 辅助函数
任务 T012：执行 Docker、healthcheck、数据库迁移和 Redis 连接冒烟验证
```

### M1

```text
任务 T020/T021：Admin 服务和 Admin API/UI
任务 T022/T023：Course 和 KnowledgeBase 服务/API
任务 T024/T025：Question 服务/API/UI
任务 T026/T027：Exam 服务/API/UI
```

### M2

```text
任务 T034/T035：Embedding 抽象和 Provider 工厂
任务 T036：Embedding Provider 契约测试
任务 T041：pgvector 语义检索
任务 T042：PostgreSQL 关键词检索
任务 T044：Rerank 适配器
```

### M3

```text
任务 T047/T048/T049：Objective 路由和确定性评分器
任务 T050/T051/T052：Subjective 上下文、校验和 LLM 评分器
任务 T060-T064：GradingResult、ExamResult、DiagnosisReport、ReviewRecord、AgentRun 和 WorkflowRun 模型/迁移
```

### M4

```text
任务 T066/T067：Supervisor 和 Question Agent
任务 T069/T070：Grading 和 Reviewer Agent
任务 T075：出题 API/UI
任务 T077：人工复核 API/UI
```

### M5

```text
任务 T081/T082/T083：MCP 工具实现
任务 T084/T085：审计和 Agent Trace
任务 T086/T088：评测看板和项目文档
```

## 实施策略

### 优先完成 MVP

1. 完成 M0，并确认三个容器运行以及 Benchmark 种子生成。
2. 完成 M1，验证非 AI 的 Teacher -> Exam -> Student Submission 路径。
3. 完成 M2，验证可追踪来源的 Hybrid RAG 和四种检索对比。
4. 完成 M3，验证客观/主观阅卷、结构化结果、置信度策略和诊断。
5. 在宣布 AI MVP Demo 完成前完成 M4，因为最终验收路径要求 Agent 职责、LangGraph
   路由和 Human-in-the-loop 复核。
6. 在 M4 检查点停止即可完成首个完整产品演示；M5 作为工程展示和交付加固阶段。

### 增量交付

- **M0** 交付可运行工程基础和可度量的 AI 评测基础。
- **M1** 交付带角色边界的首个非 AI 业务闭环。
- **M2** 增加课程上下文检索，不改变业务实体。
- **M3** 增加自动阅卷和学生诊断，不要求 Agent 编排。
- **M4** 使用显式 Agent 和 Workflow 职责替代直接服务组合，同时保持 M3 服务契约。
- **M5** 增加集成能力、审计能力、评测可视化和演示打包。

### 每个任务的完成定义

每个完成的任务都必须使所引用的文件处于可运行或可评审状态，提供对应验证证据，遵守
宪章五条原则，并避免引入未记录的架构变化。AI 任务还必须在相关行为影响模型质量时
提供结构化 Schema 校验、失败处理、Prompt/版本追踪以及 Benchmark 或回归证据。

## Phase 6: Convergence

**目的**：在保持 Gradio、单仓库 Backend 和现有业务层边界不变的前提下，补齐 UI
与 `spec.md`、`plan.md` 及既有用户故事之间的差距。以下任务只允许修改
`backend/app/ui/` 下的视图文件；依赖的 API、Service 和数据模型由既有任务或后续
里程碑提供，不在本阶段扩展业务逻辑。

- [X] T092 重构 `backend/app/ui/gradio_app.py` 的共享工作台外壳，按 Teacher、Student、Admin 角色组织清晰的功能分区，显示当前用户与当前工作上下文，统一登录、退出、加载、空数据、成功和失败状态，并保持现有会话状态与 RBAC 边界 per plan §6、FR-004、SC-002/SC-003 (partial)
- [~] T093 [P] [US1] 在 `backend/app/ui/knowledge_base_view.py` 中实现课程、知识库、资料上传和处理状态工作区，展示 PDF/TXT/Markdown 支持范围、来源追溯、Ready/Failed 状态和可理解的失败提示；仅适配既有课程/知识库契约，不修改 Service 或 API per US1/AC1、FR-008~FR-014、T039 （部分完成：UI 已实现，业务待接入）
- [X] T094 [P] [US1] 重构 `backend/app/ui/question_view.py` 和 `backend/app/ui/exam_view.py`，将手填题目/考试 ID、原始 JSON 和 ISO 时间输入改为课程上下文、表格选中、审核状态、已审核题目筛选、草稿组卷和发布前校验的连续教师流程 per US1/AC2~AC4、FR-018、FR-020、FR-028、T025、T027 (partial)
- [~] T095 [P] [US2] 在 `backend/app/ui/results_view.py` 中实现学生成绩结果工作区，展示总分、单题得分、错题、AI 诊断、薄弱知识点、学习建议和知识点掌握情况，并明确标注 Pending Review 结果不得视为最终结论 per US2/AC3~AC4、FR-038、FR-040、T057 （部分完成：UI 已实现，业务待接入）
- [~] T096 [P] [US3] 在 `backend/app/ui/review_view.py` 中实现教师低置信度阅卷复核工作台，展示复核队列、题目、学生答案、参考答案、评分标准、AI 分数、理由、知识点和置信度，并支持确认或修改后的状态反馈 per US3/AC4~AC5、FR-036、FR-037、T077 （部分完成：UI 已实现，业务待接入）
- [X] T097 [P] [US2] 重构 `backend/app/ui/student_exam_view.py`，按题型提供可理解的答题控件和题目导航，增加考试进度、草稿保存、提交确认、提交后只读状态及重复提交提示，移除学生直接编辑题目 ID 到 JSON 的主要流程 per US2/AC1~AC2、FR-021、FR-022、SC-003、T029 (partial)
- [~] T098 [P] [US1] 在 `backend/app/ui/question_generation_view.py` 中实现 AI 出题条件填写、检索上下文不足提示、候选题列表、结构化字段预览和教师审核/退回修订操作；候选题必须显式保持 Candidate Generation 或 Pending Review 状态 per FR-024~FR-028、T075 （部分完成：UI 已实现，业务待接入）
- [~] T099 [US3] 扩展 `backend/app/ui/results_view.py` 的教师结果工作区，支持按课程、考试和学生查看成绩、AI 阅卷详情、低置信度项、知识盲点和学情分析，并与学生结果视图共享中文状态映射 per FR-039、plan §5.2、plan §6 （部分完成：UI 已实现，业务待接入）
- [~] T100 [P] 在 `backend/app/ui/evaluation_dashboard.py` 中实现检索与阅卷 Benchmark 对比看板，按实验、模型、Prompt、数据集、指标、运行状态和结果路径展示可追溯信息，并区分失败、运行中和完成状态 per plan §7、T086 （部分完成：UI 已实现，业务待接入）
- [X] T101 [P] [US4] 重构 `backend/app/ui/admin_view.py` 的管理员工作台，分离用户、角色和运行状态区域，支持从列表选择用户后编辑，补充创建/停用/删除的确认和结果反馈，并保持管理员不能访问教师/学生业务视图 per US4/AC1~AC2、FR-005、FR-006、T021 (partial)
- [X] T102 在 `backend/app/ui/` 各视图统一内部枚举、角色、审核状态和业务状态的中文显示映射，统一表格列宽/空状态/加载状态/错误提示及危险操作确认；补充视图级导入和组件构建验收，确保所有界面文字和新增注释使用中文 per FR-003、FR-004、SC-008、plan §6 (partial)

## Phase 7: Convergence

**目的**：作为 Phase 6 的 PreSkool 布局细化续篇，保留 T092-T102 原文、编号和完成状态，
从 T103 追加具体布局要求。设计基线见 `docs/ui-layout-design.md`，参考来源编号见该文档。
本节不是第二套 UI：既有任务负责能力覆盖，新增任务负责同一视图的布局增量与验收，
实现已有内容时只补差距，不重复创建页面或业务逻辑。

**边界**：本节实现仅允许修改或新增 `backend/app/ui/` 内的视图文件，继续使用 Gradio。
不得修改 Service、API、模型、权限定义、配置或依赖文件，不引入 React/Vue/Streamlit，
不复制 PreSkool 模板源码或引入其后台业务。界面文字、提示与新增注释使用中文。
缺失的业务依赖由原有里程碑任务提供；布局可先展示明确的空态/不可用态，但依赖未接通
时不得宣称业务验收完成，不得用示例成绩、随机指标或假成功状态代替真实数据。

**执行约定**：T103-T104 先提供共享组件；T105-T113 按各自依赖实施；T114-T115 消费已完成
页面的数据摘要；T116 最后连接跨页入口；T117 等待评测契约；T118 收口。所有共享外壳挂载
修改串行进行；T111/T112 共用结果视图文件，也必须串行，不标记为并行任务。

- [X] T103 [HIGH] 在 `backend/app/ui/gradio_app.py` 和新增的 `backend/app/ui/layout_view.py` 中细化 T092 的共享外壳：桌面采用 64px 顶部栏、216px 左侧导航和自适应右侧内容区；教师菜单按“工作台 / 教学准备（课程管理、知识库、题库、考试）/ AI 教学（AI 出题、AI 阅卷）/ 学情分析”分组，学生菜单为“学习概览 / 我的考试 / 成绩与诊断”，管理员菜单为“系统概览 / 用户管理 / 角色管理 / 运行状态”；选中项显示蓝色边线与文字，右侧仅显示当前页面；保留登录、当前用户、退出和已有会话/RBAC 校验，多角色只列出已授权角色，退出清空页面数据；缺失模块显示“功能暂不可用”而非“模块已准备就绪” per FR-002~FR-006、T092、plan §6、本次布局要求（参考 P1-P3）(partial)
- [X] T104 [HIGH] 在 `backend/app/ui/layout_view.py` 定义并在现有视图应用 T102 的状态显示规范：未答为灰色空心题号，已答为绿色勾选题号，标记为琥珀色旗标，当前题叠加蓝色边框；待批阅使用琥珀色时钟徽标、低置信度使用琥珀色警告横幅并显示实际置信度、已完成/已确认使用绿色勾、失败使用红色叉；徽标均带中文文字且未知值显示“状态未知”；题目待审核与评分待复核按实体区分，不将“已评分”直接映射成“最终成绩”，阈值和状态以服务结果为准；统一空态、加载、错误、危险操作的内联二次确认区 per FR-015、FR-019、FR-023、FR-028、FR-033~FR-037、T102、本次布局要求（参考 P2、P5、P6）(partial)
- [~] T105 [HIGH] [US1] 在 `backend/app/ui/knowledge_base_view.py` 细化 T093：课程管理采用顶部搜索/新建工具行、左侧约 65% 课程表格（名称、简介、知识库数、更新时间）、右侧约 35% 选中课程详情与编辑表单；知识库导航复用已选课程，顶部课程与知识库下拉框，中部资料上传行及资料表格（文件名、格式、处理阶段、更新时间），下部折叠来源详情与失败原因；以真实 Uploaded/Parsing/Chunking/Embedding/Ready/Failed 显示阶段，不以文件上传成功代表知识库就绪；依赖 T103-T104、T022-T023，文件摄取和片段追溯依赖 T038-T039，未就绪时禁用对应操作 per FR-008~FR-014、US1/AC1、T093（参考 P4）（部分完成：UI 已实现，业务待接入）
- [X] T106 [HIGH] [US1] 在 `backend/app/ui/question_view.py` 细化 T094 的题库布局：上方课程/题型/知识点/审核状态筛选与新建入口，下方约 60% 题目摘要表格和 40% 选中题详情；表格列为题干摘要、题型、分值、知识点、审核状态，详情依次为完整题干、逐项选项、参考答案、评分标准；选行自动绑定内部 ID，编辑选项使用文本行与正确答案选择控件，底部排列“保存 / 审核通过 / 退回修订”，删除收进独立确认区；依赖 T103-T105，复用 T024-T025，不要求用户编辑 JSON 或原始枚举 per FR-018~FR-020、FR-028、T094（参考 P4、P6）(partial)
- [X] T107 [HIGH] [US1] 在 `backend/app/ui/exam_view.py` 细化 T094 的考试布局：默认显示顶部课程/状态筛选和考试表格（名称、课程、开放时间、时长、题数、总分、状态）；选择草稿进入“基本信息 / 选择题目 / 发布检查”三个步骤，组卷区左侧约 60% 已审核题目表、右侧约 40% 已选题清单和题数/总分摘要，日期与时分使用可理解的控件并标注时区；发布前展示考试名称、开放时间、已审核题目数和确认区，未满足条件禁用发布；依赖 T103-T106，复用 T026-T027，保持服务对题目归属和审核状态的最终校验 per FR-020、US1/AC3~AC4、T094（参考 P5）(partial)
- [X] T108 [HIGH] [US2] 在 `backend/app/ui/student_exam_view.py` 细化 T097：入口为可参加考试表格与“开始或继续”，答题时采用精简顶部栏（考试名、保存状态、可用的截止时间、交卷）、左侧 200px 答题卡和右侧题目区；题号格不小于 44px，显示已答/未答数量与标记状态；单选和判断使用单选控件，简答使用至少 8 行且最小高 240px 的纯文本框，底部为上一题、标记、下一题；选题只切换当前题，草稿按当前答卷保存；交卷先显示内联确认区及未答题号，有未答时只能返回补答，成功后只读；依赖 T103-T104、T028-T029，遵守现有全部题目必答规则，计时只使用可靠截止信息，不新增自动交卷或个人计时规则 per FR-021~FR-023、US2/AC1~AC2、T097（参考 P3/P5 的入口；答题卡为 EduAgent 专属设计）(partial)
- [~] T109 [HIGH] [US1] 在 `backend/app/ui/question_generation_view.py` 细化 T098：上方显示当前课程，左侧约 30% 固定条件表单（课程、知识点、难度、题型、数量）和生成状态，右侧约 70% 候选题列表与选中题完整预览；检索来源使用下方折叠区域，候选题标题旁显示“待教师审核”，预览底部提供审核通过/退回修订及修订意见输入；检索不足或结构校验失败显示横幅并禁用审核通过；依赖 T103-T106、T067-T068、T075 的既有契约，不实现检索、生成或校验业务 per FR-024~FR-028、US1/AC2~AC3、T098（参考 P2 的任务分区，内容由 EduAgent 规格定义）（部分完成：UI 已实现，业务待接入）
- [~] T110 [HIGH] [US3] 在 `backend/app/ui/review_view.py` 细化 T096：顶部考试/学生/复核状态筛选，下方左侧 240px 复核队列，右侧详情再按约 45% 学生答案与 55% AI 评分并排；详情顶行显示学生、考试、题号、实际置信度和“待人工复核”横幅，答案区保留原文换行，评分区显示分数、理由、知识点、参考答案、评分标准；检索依据为可展开的编号引用列表（来源文件、页码或段落、片段正文），修改分数与理由在原位置内联编辑，底部固定确认评分/保存修改/下一条；待复核保持原服务状态，不伪造新的标记接口；依赖 T103-T104、T050、T060-T062、T074、T077，结果刷新以保存成功返回为准 per FR-031、FR-034~FR-037、US3/AC4~AC5、T096（参考 P2 待批阅队列、P6 状态；证据复核为 EduAgent 专属设计）（部分完成：UI 已实现，业务待接入）
- [~] T111 [HIGH] [US2] 在 `backend/app/ui/results_view.py` 细化 T095 的学生结果布局：顶部考试选择与结果状态横幅，其下总分、已评分题数、待复核题数三个紧凑摘要；主区分“逐题结果 / 错题与诊断”，逐题结果为摘要表格与选中题反馈，诊断区左侧薄弱知识点及真实掌握度图表、右侧错误原因和学习建议；待复核状态只显示已确认部分并说明最终成绩尚未形成，未生成诊断时显示明确空态；依赖 T103-T104、T054-T057、T060-T061，只读取当前学生授权结果，不把前端汇总作为最终成绩 per FR-033~FR-038、FR-040、US2/AC3~AC4、T095（参考 P3、P6）（部分完成：UI 已实现，业务待接入）
- [~] T112 [HIGH] [US3] 在 `backend/app/ui/results_view.py` 细化 T099 的教师结果布局：顶部课程/考试筛选，首行已提交数、最终成绩数、待复核数及仅基于最终结果的平均分摘要；中部左侧约 65% 学生成绩表（学生、总分、结果状态、待复核数）和右侧约 35% 所选学生诊断摘要，下部知识点分布图及同数据表；每条待复核结果提供携带考试/答卷/题目上下文进入 T110 的入口；依赖 T103-T104、T110-T111、T054-T057 和对应教师查询契约，权限或数据缺失显示不可用态，不绕过服务查库聚合评分 per FR-039、plan §5.2、T099（参考 P2、P6）（部分完成：UI 已实现，业务待接入）
- [X] T113 [MEDIUM] [US4] 在 `backend/app/ui/admin_view.py` 细化 T101：系统概览首行用户数、启用用户数、角色数，下一行数据库/缓存健康状态及检查时间；用户管理页为上方姓名/角色/启用状态筛选、左侧约 65% 用户表和右侧约 35% 用户编辑表单，角色管理单独显示角色说明表及选中用户角色勾选，运行状态单独显示依赖健康列表；创建为独立表单模式，停用/删除前内联显示目标用户和确认按钮；依赖 T103-T104、T020-T021，管理员菜单不提供 AI 审核/复核或学生答题入口 per FR-005~FR-006、US4/AC1~AC3、T101（参考 P1、P4）(partial)
- [~] T114 [MEDIUM] [US1] 在 `backend/app/ui/gradio_app.py` 增加教师概览的具体布局：标题行显示当前课程筛选，首行最多四个摘要（课程数、已发布考试、待审核题、待复核评分）；中部左侧约 2/3 使用紧凑课程卡片展示名称、简介、知识库数和进入课程入口，右侧约 1/3 为待办列表（类别、所属课程/考试、状态、处理入口）；下部为最近考试表与最终成绩概览；依赖 T103-T107、T109-T110、T112，统计限于已授权完整数据集，缺失数据为“暂不可用”而非零，不新增考勤、排课或预测分数 per US1、FR-039、T092、本次教师仪表板要求（参考 P2）（部分完成：UI 已实现，业务待接入）
- [~] T115 [MEDIUM] [US2] 在 `backend/app/ui/gradio_app.py` 增加学生概览：左侧沿用“学习概览 / 我的考试 / 成绩与诊断”，主区首行可参加考试数、已提交数、可查看结果数，下方左侧约 2/3 为当前可参加考试表、右侧约 1/3 为最近结果列表和已确认诊断摘要；每场考试显示名称、课程、开放时间、时长及开始/继续按钮，每项结果带中文最终/待复核状态并可进入 T111；依赖 T103-T104、T108、T111，数据只属于当前学生，不引入缴费、家长通讯或排行榜 per FR-021~FR-023、FR-040、T092、本次学生仪表板要求（参考 P3）（部分完成：UI 已实现，业务待接入）
- [X] T116 [MEDIUM] 在 `backend/app/ui/gradio_app.py` 和 `backend/app/ui/layout_view.py` 补齐顶部面包屑、搜索、消息与待办、快捷操作：顶部为品牌、当前页搜索框、消息数量和用户菜单，内容标题行显示“工作台 / 课程 / 当前页面”及最多两个当前页主操作；消息按钮展开内容区顶部的通知面板（类型、关联对象、状态、时间、查看入口），仅汇总当前用户可见的已加载待办与本次会话操作反馈，不建立消息推送/持久化已读服务；教师快捷入口为创建课程/创建考试，学生为继续作答/查看结果，管理员为创建用户/刷新状态；依赖 T103-T115 的相关页面，返回导航恢复原筛选与所选对象，未有可操作数据时不显示假计数或无效链接 per T092、SC-002/SC-003 的入口可达性、本次顶部布局要求（参考 P1-P3、P4-P6）(missing)
- [~] T117 [MEDIUM] 在 `backend/app/ui/evaluation_dashboard.py` 细化 T100：上方实验/数据集/模型/检索模式筛选，下方指标对比表和对应柱状图并排，底部选中实验的配置、模型/提示词/数据集版本、运行状态和结果位置折叠详情；图表必须带指标单位、样本量和同条件比较说明，失败项单独列出、缺失指标显示“无数据”；依赖 T103-T104、T086 的评测结果及读取授权契约，未明确读取授权时隐藏入口，不因角色为管理员就授予 AI 业务权限，不新增评测计算/文件读取越权入口 per T086、T100、plan §7、Constitution V（参考 P1 的指标层级，评测指标由项目计划定义）（部分完成：UI 已实现，业务待接入）
- [~] T118 [LOW] 对 `backend/app/ui/` 的上述布局完成显示与交互验收：在 1440x900、1024x768、390x844 下检查桌面侧栏、平板收窄、手机折叠菜单；多列详情在窄屏依次重排，答题卡移至题目上方，长题干/文件名换行且表格仅在自身区域横向滚动；确认按钮不小于 44px、题号格尺寸稳定、焦点可见，通知/确认区关闭后回到原操作；运行现有 UI 检查及视图导入/组件构建检查，抽查三角色进入、返回、退出与待复核非最终状态，记录真实截图与未就绪依赖；依赖所有本轮已实施任务，保留业务未就绪任务未勾选，不修改 UI 以外文件 per T102、FR-004、US2/AC4、plan §6、本次响应式布局要求（参考 P1-P6 的页面分区）（部分完成：待实际视口验收）

**完成边界**：本轮只追加任务和输出设计，不执行上述任务。后续需要先确认布局方向，再按
依赖选择 UI 实施任务；保留 Phase 6 与本节各任务的独立验收记录，不将布局预览视为业务闭环完成。

## Phase 8: Dev-Mode Login

**目的**：为本地演示和测试提供可控的开发模式快速登录，同时保持后端 JWT 身份鉴权、
当前用户加载和 RBAC 守卫不变。`DEV_MODE` 默认关闭；开启时只增加 UI 快速入口和
预设测试账号初始化。T120 是本阶段唯一的 Service 层例外，仅新增幂等账号初始化辅助
函数，不修改现有密码认证、JWT 签发/验证、API 端点或角色守卫。所有新增注释、界面
文字和提示使用中文，继续使用 Gradio，不引入 React/Vue/Streamlit。

- [X] T119 在 `backend/app/core/config.py` 的 `AppSettings` 中新增 `DEV_MODE: bool = False` 配置项，沿用 `.env` 加载和类型校验，并让公开配置摘要明确显示当前开关状态；配置关闭时保持现有登录行为 per FR-002、FR-004、plan §0。
- [X] T120 在 `backend/app/services/auth_service.py` 中实现 `ensure_dev_mode_accounts()`，仅在 `DEV_MODE=True` 时幂等创建或复用管理员、教师、学生各一个预设测试账号，确保账号启用且仅绑定对应角色；复用 `hash_password`、现有 User/Role 模型和 `issue_access_token`，不得覆盖同名真实账号、记录明文密码或修改 `authenticate`/JWT/RBAC 逻辑 per FR-001、FR-003、FR-004（Phase 8 唯一 Service 层例外）。
- [X] T121 在 `backend/app/ui/gradio_app.py` 的登录面板与登录事件绑定处实现开发模式登录区域：仅当 `DEV_MODE=True` 时在登录页顶部显示“开发模式快速登录”及“以管理员身份登录”“以教师身份登录”“以学生身份登录”三个按钮；按钮调用账号初始化并使用标准 JWT 建立 `LoginState`，`DEV_MODE=False` 时组件完全隐藏且正常用户名/密码登录路径不变 per FR-002、FR-003、FR-004、plan §0、§6。
- [X] T122 在 `backend/app/ui/layout_view.py` 的共享工作台顶部增加开发模式视觉提示“当前为开发模式，请勿用于生产环境”，仅在 `DEV_MODE=True` 渲染；提示不得改变导航授权或后端请求校验 per FR-004、FR-006、plan §0、§6。
- [X] T123 在 `.env.example` 中增加 `DEV_MODE=false` 及中文说明注释，说明仅用于本地演示、开启后会创建预设测试账号、上线前必须关闭并清理账号 per FR-002、FR-007。
- [X] T124 编写 `docs/dev-mode.md`，记录开关、预设账号生命周期、快速登录行为、后端安全边界、测试/清理步骤，并链接 `docs/dev-mode-removal-checklist.md` per FR-001~FR-007、plan §0。


## T092-T118 状态核对说明（2026-09-13）

本次按当前代码可运行性重新核对：共享外壳、题库、考试、答题和管理员视图（T092、T094、T097、T101-T104、T106-T108、T113、T116）保持 [X]。知识库摄取、AI 出题/复核、结果与评测数据依赖尚未完成的 M2-M5 服务，因此 T093、T095、T096、T098-T100、T105、T109-T112、T114-T115、T117 标为 [~]，表示 UI 已实现但业务待接入；T118 标为 [~]，表示布局代码存在但三种视口的实际验收证据待补。后续完成对应服务、授权契约和真实数据联调后，逐项补充验收证据并将部分任务更新为 [X]。

## Phase 9: Prefix-Cache Optimization

**目的**：评估并落地 Reasonix 三区上下文管理在 EduAgent LLM 调用链路中的可复用部分，
以稳定不可变前缀、保持仅追加日志顺序、隔离易失暂存区为约束，降低 DeepSeek 前缀缓存
未命中造成的成本。所有命中率或成本收益必须以可复现实验验证；本阶段不改变角色授权、
结构化输出和既有 Agent/Workflow 业务契约。T125 保持 `BaseLLMProvider.generate_structured`
现有必需参数兼容；若后续方案改变该抽象方法签名，必须另行标注破坏性变更并提供旧实现
的兼容适配器。

- [ ] T125 [HIGH] [P] [M3 基础] 在 `backend/app/ai/llm/base.py` 的 `BaseLLMProvider` 中新增 `supports_prefix_cache(self) -> bool` 能力方法，中文说明其表示是否支持 DeepSeek 前缀缓存优化，提供默认返回 `False` 的具体实现，不加 `@abstractmethod`，使其他 Provider 和既有测试 Stub 无需覆写即可继续实例化；仅在 `backend/app/ai/llm/deepseek.py` 的 `DeepSeekProvider` 中覆写返回 `True`，其他 Provider 保持默认 `False`。在新增的 `backend/app/ai/llm/context.py` 中定义 `ContextEnvelope` 与 `ContextManager`，保留版本化不可变前缀、仅追加日志、请求前清空且不得发送的易失暂存区、稳定消息序列化和前缀指纹，并设置 Provider 能力判断环节，激活规则按 T126 执行；保持 `generate_structured(messages, schema, model=None)` 的签名、返回类型以及 JSON Output、Pydantic 校验、重试和回退行为不变，此新增默认方法为非破坏性扩展；在 `tests/unit/ai/test_context_manager.py` 补充三区边界及默认能力继承测试，并将既有 `tests/unit/test_llm.py`、`tests/unit/test_deepseek.py` 的 M0 回归通过列为实施验收条件 per Constitution III/IV/V、plan §2/§7 (missing)
- [ ] T126 [MEDIUM] [P] [M3 基础] 在 `backend/app/core/config.py` 保留 `ENABLE_PREFIX_CACHE_OPTIMIZATION: bool = False` 的全局开关设计，并在 `.env.example`、`docs/development.md` 说明默认关闭、仅 DeepSeek 路径生效和脱敏要求；在 `backend/app/ai/llm/context.py` 中仅当 `settings.ENABLE_PREFIX_CACHE_OPTIMIZATION == True` 且 `provider.supports_prefix_cache() == True` 时激活 ContextManager 的三区架构，任一条件不满足均沿用现有消息组装及 Provider 调用行为；能力判断仅通过 Provider 方法完成，ContextManager、Agent 和业务 Service 不得通过 Provider 名称字符串或 `DeepSeekProvider` 类型判断分支；在 `tests/unit/ai/test_context_manager.py` 补充测试，覆盖非 DeepSeek Provider 在开关开启时仍不激活、DeepSeek 在开关关闭时不激活、双闸门均开启时激活，以及未激活时请求消息保持原样；配置与文档可与 T125 并行准备，能力门控联调与测试须在 T125 完成后验收 per plan 技术约束、Constitution I/III/V (missing)
- [ ] T127 [HIGH] [M3] 在 `backend/app/services/grading/` 与待实现的 Grading Agent 中固定版本化系统提示、评分 Schema 和输出约束，将题目/评分标准/学生答案/检索上下文作为请求尾部数据，按事件顺序追加检索与阅卷日志并在调用前移除易失暂存；补充 T052/T069 对应的上下文稳定性契约测试 per FR-031~FR-037、agent-workflow contract、T052/T069 (missing)
- [ ] T128 [HIGH] [M4] 在 Question Agent（`backend/app/ai/agents/question_agent.py`）中固化系统提示、工具定义和结构化输出示例，将教师条件与检索结果作为仅追加输入，查询草稿和自检过程留在易失暂存区；补充 T067/T075 的前缀字节稳定性测试，不改变候选题审核状态 per FR-024~FR-028、US1、T067/T075 (missing)
- [ ] T129 [HIGH] [M4] 在 Reviewer Agent（`backend/app/ai/agents/reviewer_agent.py`）中固定复核规则和结构化决策 Schema，将原始评分、证据与复核事件按时间追加，隔离复核决策草稿；补充 T070 的恢复/重试测试并保持 `accept/revise/regrade` 契约不变 per FR-035~FR-037、agent-workflow contract、T070 (missing)
- [ ] T130 [HIGH] [M4] 在 `backend/app/ai/workflows/` 的阅卷 LangGraph 实现中为节点路由、工具和 Schema 固定不可变前缀，以 WorkflowRun 事件和检查点仅追加记录状态，清空节点级易失计划后再发起下一次 LLM 请求；补充 T072 的暂停/恢复与前缀一致性测试，不改变 Pending/Retrieving/Grading/Needs Review/Completed/Failed/Paused 状态机 per plan §5、data-model.md、T072 (missing)
- [ ] T131 [MEDIUM] [P] [M3/M4] 在 `backend/app/ai/retrieval/reranker.py` 的 `LLMRerankAdapter` 中复用版本化 `_RERANK_SYSTEM_PROMPT` 前缀，保证候选顺序和请求日志只追加，记录前缀指纹；为 LLM 路线补充命中/未命中契约测试，并明确 Cross Encoder 路线不接入该机制 per FR-032、rag-retrieval contract、T044 (partial)
- [ ] T132 [MEDIUM] [M5] 在 `backend/app/ai/llm/`、`backend/app/models/` 或现有 AgentRun/Benchmark 记录边界中增加脱敏的前缀版本、前缀指纹、缓存命中状态、模型和 Prompt 版本指标，在 `scripts/` 增加可复现的对照实验并记录数据集/配置/结果；将命中率基线与三区架构结果写入 `docs/`，禁止以未测量估计宣称收益 per Constitution V、plan §7、T086 (missing)

**执行依赖**：T125 为上下文基础，T126 可与 T125 并行但须在相关 Agent 开关验收前完成；T127 依赖 T125/T126 以及 T052/T069，T128 依赖 T125 以及 T067/T075，T129 依赖 T125 以及 T070，T130 依赖 T125 以及 T072，T131 依赖 T125 以及 T044；T132 在 T125-T131 和 T086 的结果记录边界具备后执行。

**目标路径补充**：T126 的配置说明使用 `docs/development.md`；T127 的 Agent 实现使用 `backend/app/ai/agents/grading_agent.py`，契约测试使用 `tests/contract/test_prefix_cache_grading.py`；T128 的测试使用 `tests/contract/test_prefix_cache_question.py`；T129 的测试使用 `tests/contract/test_prefix_cache_reviewer.py`；T130 的 Workflow 测试使用 `tests/integration/test_prefix_cache_workflow.py`；T131 的测试使用 `tests/contract/test_prefix_cache_rerank.py`；T132 的对照脚本使用 `scripts/run_prefix_cache_benchmark.py`，结果说明使用 `docs/evaluation.md` 和 `docs/validation-report.md`。

## 独立修复：I01（2026-09-30）

- [X] T133 [I01] 在 `backend/app/services/question_service.py` 拒绝 Approved 题目的 `content`、`options`、`reference_answer`、`scoring_rubric`、`type`（含 `question_type` 服务别名）和 `score` 更新；允许 `difficulty`、`knowledge_points` 编辑。开放教师 `Approved -> Needs Revision -> Pending Review -> Approved` 修订审核流程，在 `backend/app/api/questions.py` 返回 HTTP 409 和 `QUESTION_APPROVED_IMMUTABLE`、中文提示、当前状态。按 TCR 更新 `tests/unit/services/test_question_service.py` 并新增 `tests/contract/test_question_update_api_contract.py`，完成聚焦回归、全量 pytest、mypy 与 ruff 检查；仅提交并推送本修复，保留已有工作区改动。对应 FR-020、FR-028；不推进 Phase 6–9 其他任务。

**T133 验证记录**

- 聚焦回归：`python -m pytest tests/unit/services/test_question_service.py tests/contract/test_question_update_api_contract.py -q`，24 passed；其中新增 20 个参数化用例，另有既有状态流转用例更新。修改测试前已形成 TCR，并在两份测试模块中保留 TCR。
- 全量检查：`python -m pytest tests/ -q`，1335 passed、111 failed、12 errors、119 skipped，耗时 1824.21 秒；全量未通过，不标记为系统验收成功。I01 两份测试文件无失败项。失败指向原有 Workflow 启动时检查点存储未就绪，以及迁移测试的 PostgreSQL 连接超时；本机 Docker 引擎未启动。
- 原代码复验：在独立进程中载入 HEAD 的 Question Service 与 Question API，`test_trigger_creates_task_and_returns_queued` 仍因 `WORKFLOW_SERVICE_NOT_READY` 失败；未改写工作区文件，也未修改这些既有失败测试。
- 静态检查：`python -m mypy backend/app/`，141 个文件通过；`python -m ruff check backend/ tests/` 通过；补丁格式检查通过。
- 本任务只完成 I01 的内容守卫、元数据例外和显式修订审核流程；数据库环境故障不纳入修复范围，不宣称已完成考试快照或历史版本冻结。其他任务状态保持不变。

## Phase 10: Convergence

**主题**：v2.0 实施前 checklist 与任务收敛（2026-10-01，Asia/Shanghai）。
**依据**：`.specify/spec.md`、`.specify/plan.md` 为意图源，宪章 `.specify/memory/constitution.md` 为约束；结合 data-model/contracts、[分析报告](../docs/v2.0-analyze-report.md) 与 [实施前清单](checklists/v2-readiness.md) 核对当前源码。
**本次产出**：仅追加本节与生成清单。T001–T133 的内容、顺序、状态及既有汇总不变；上方旧汇总不包含本节。未执行 implement，未修改设计正文、代码、迁移或测试，未运行业务验收。
**流程说明**：当前 tasks 已有 M0–M5 实施及 T133 验证记录，满足已有任务经过实施的上下文；按用户明确要求，在本次 converge 同时追加 v2.0 设计补齐和 E0–E6 任务，不另行执行 tasks/implement。已有 T125–T132 未完成的前缀缓存任务保持原样，不自动成为 v2.0 依赖；已完成/部分 UI 状态也不据此重写。
**评估口径**：12 条新 FR + 8 条既有 FR 扩展（010/018/020/024/025/028/039/040）+ 5 条 SC + 12 条 US1–US3 扩展 AC，共 37 个需求/场景条目；plan §8–14 七项、非功能四项和 Gate 10–19 十项，共 21 个计划条目；宪章 I–V 五条。只在本版触及范围核对代码，未发现确定的宪章 MUST 违规，不宣称全仓重新验收。
**发现口径**：每个新任务对应一项细分发现，稳定标识 F001–F058 与 T134–T191 按序一一对应（F序号 = T编号 − 133）。missing 27、partial 29、contradicts 2、unrequested 0；HIGH 50、MEDIUM 8、CRITICAL/LOW 0。UI 未提交工作属于已知授权范围，不作为 unrequested 清理。按用户指定 A、E0–E6 分批及依赖排序，不以全局严重度打乱执行前置。

### 当前证据与范围

| 发现范围 | 来源 | 当前源码/文档证据 | 收敛方向 |
| :--- | :--- | :--- | :--- |
| F001–F009 / G01–G09 | 清单 CHK001–009、FR/plan/contracts | data-model §3/7/8/10 缺映射；plan §9与实体引用、§11与目录树存在差异；评测重复/阈值仍待明确 | 设计补齐及用户取舍；本次不直接补文档 |
| F010–F013 / E0 | CHK010–012、兼容性目标 | tasks T133 的全量失败记录与 M5 既有证据不等价；没有 v2.0 盘点/TCR/样本计划 | 复验并建立任务和验证依赖 |
| F014–F019 / E1 | FR-044/045 | `backend/app/api/knowledge_bases.py:45` 仍用 tempfile；scripts 无 migrate_storage_paths/backup_restore；无统一文件访问/登记 | 持久存储、授权、迁移与同集恢复 |
| F020–F027 / E2 | FR-041/042/043 | models 仅有现有 Document/Question，无 PaperImport/SourcePage/ExtractedQuestion/QuestionAsset；无 OCR/导入 API | 复用知识库文件基础，新增独立导入/校正 |
| F028–F036 / E3 | FR-024/025/046/047 | `ai/retrieval/_filters.py:12` 只消费课程/知识库/资料；`ai/llm/base.py:56` 无图像能力；已有 Question Agent/字段校验，缺父题/语义报告持久映射 | 章节生产与 SQL、图片、改编、核验/审核增量 |
| F037–F046 / E4 | FR-048/049 | `models/associations.py` 仅两 ID；Exam 按 created_at/id 排序；`exam_service.py:651` 发布只校核归属/审核；`grading_task_service.py:627` 读取 Question.score；QuestionService 已有 I01 六字段守卫 | 升级原关联与全评分链、发布/历史冻结，不重做 I01 |
| F047–F051 / E5 | FR-050/051 | `api/results.py:180` 已有最终平均分/待复核汇总，`diagnosis_service.py` 已有失分诊断；缺完整分布/分母/参与统计和真实资料/练习推荐 | 扩展现有结果/诊断与师生视图 |
| F052–F058 / E6 | FR-052、SC-014 | 当前有 layout_view/design_system/题库改动及结果页；scripts/ 无 EXE 构建，缺 v2.0 七页/打包/闭环运行证据 | 复用 UI 成果，完成实际交付与验证 |

以上为当前文件的静态评估；缺少新模型/入口由路径盘点和概念检索共同核对，不把旧文档“已完成”当作新功能证据。未来任务列出的新文件是计划路径；未列完整根路径的 `models/`、`services/`、`api/`、`schemas/`、`ai/`、`ui/` 均相对 `backend/app/`，迁移/测试使用实际仓库根 `migrations/`、`tests/`。文件拆分复用 plan 蓝图，允许不改变职责的局部实现细节。

### 本节执行与完成规则

- A 组是待执行的文档任务，不表示方案已确认。G01–G05/G08 的存储、公共接口等实质取舍先提交用户决定；G06/G07 同步已确认合同。闭区间语义已在 rag 契约明确，不重复升级为未决架构问题。
- 从 E0 的 T143/T144 开始基线复验和只读盘点；A 可按受影响模块准备，G04/G07 在 E1、G03/G06 在 E2、G05 在图片核对、G01/G02/G05 在 E3、G08 在 E4 编码前完成。G09 协议先于正式样本/测量，质量阈值由 T168 基线后提请确认；不阻塞独立盘点。
- E0 建立 TCR；每一批新增/修改测试前先补齐该批 TCR，再按既有 tasks 测试说明先写目标行为用例、确认暴露缺口后实现。批末“验收”任务负责运行与证据，不表示测试留到编码后才写。本次没有测试内容修改。
- 各任务明确列直接依赖，传递依赖同样生效。跨批接口共同核对；未决事项只阻塞依赖工作。各批验证足以证明改动后停止，除新修改/失败/具体未解问题外不重复跑矩阵。
- 技术栈、原 Provider/结构化输出、四种检索和阅卷图保持；不引入完整题目版本/内容快照、自动新题图、复杂能力模型或独立基础设施。T155 的 OCR、T164 如需新增的图像 Provider 需先明确选型与成本。
- 完成依据分别记录源码、迁移/测试、真实模型、Docker、EXE、UI、闭环及恢复，不能跨级宣称。失败/未知/待核对保持真实；T190/T191 未通过不得称 v2.0 验收完成。操作真实数据的清理/恢复和后续提交/发布依各次授权，不由勾选任务自动授权。
- Checklist 文档评估为 36 项：24 通过、12 待处理；A 覆盖 CHK001–009，E0–E6 编排及下方映射覆盖 CHK010–012，追加任务不等于这些需求已实现或评审者已勾选。

### 追加任务汇总

| 分类/批次 | 内容 | 编号 | 数量 |
| :--- | :--- | :--- | ---: |
| A | G01–G09 设计补齐 | T134–T142 | 9 |
| E0 | 基线复验、文件/历史盘点、TCR、样本 | T143–T146 | 4 |
| E1 | 文件持久化、迁移、备份恢复 | T147–T152 | 6 |
| E2 | 试卷导入、OCR、校正 | T153–T160 | 8 |
| E3 | 章节范围、原题改编、图片、语义核验 | T161–T169 | 9 |
| E4 | 条件组卷、内容冻结、考试内分值与阅卷 | T170–T179 | 10 |
| E5 | 教师考情与学生知识点反馈 | T180–T184 | 5 |
| E6 | UI 收敛、EXE 与最终验证 | T185–T191 | 7 |
| 合计 | 设计 9 + 实施/验证 49 | T134–T191 | 58 |

### A. 设计补齐任务（G01–G09）

- [X] T134 [HIGH] [设计 G01] **G01 章节定位与知识点映射设计**：在 `.specify/data-model.md`、`contracts/rag-retrieval.md` 及 plan 相关引用中补齐章节稳定身份、课程/资料归属、小节序号、标签 Schema/SQL 匹配及摄取/教师核对责任；沿用契约已明确的章内闭区间和同维并集/跨维交集。给出最小字段/索引、跨章切分、旧 NULL 数据迁移方案，涉及新增实体或接口的取舍先由用户确认，不强制新增章节服务。 依赖：无新增任务前置。per FR-024/025、FR-046/047、G01、CHK001（F001） (partial)

- [X] T135 [HIGH] [设计 G02] **G02 语义核验与人工处置持久设计**：在 `.specify/data-model.md` 与 `contracts/agent-workflow.md` 明确 QuestionValidationResult 的存储、每轮输入/证据关联、分项结果/技术失败、真实执行主体、教师处置及当前内容对应/失效规则；优先评审独立报告方案，QuestionRevisionComment 保留教师文字职责。给出报告与 Needs Revision 同事务、旧数据和既有批准入口的应用边界；方案由用户确认后落文档，不引入完整题目版本。 依赖：无新增任务前置。per FR-047/028、G02、CHK002（F002） (missing)

- [X] T136 [HIGH] [设计 G03] **G03 校正字段及正式题解析设计**：在 `.specify/data-model.md` 与 `contracts/paper-import.md` 明确 question_number、analysis、knowledge_points、source_regions、暂存 assets 的字段/受校验 JSON、来源坐标、转入 Question.analysis 与资产的映射、空值和历史迁移；补齐解析的审核/冻结责任，不能用 correction_notes 承载结构化内容。字段与存储取舍经用户确认后同步。 依赖：无新增任务前置。per FR-018/028/041/042/049、G03、CHK003（F003） (partial)

- [X] T137 [HIGH] [设计 G04] **G04 文件身份、导出与备份登记设计**：在 `.specify/data-model.md`、`contracts/file-storage.md` 与 plan §11 确定稳定 file_id 到资源/相对定位/授权的唯一映射、共享字节引用、导出归属及迁移记录；优先复用已有实体身份，确需独立登记再确认。定义 BackupSet manifest、同一写入窗口、恢复核对和失败保留；不把逻辑视图预定为两张新表，关键承载/恢复取舍由用户确认。 依赖：无新增任务前置。per FR-044/045、G04、CHK004（F004） (missing)

- [X] T138 [HIGH] [设计 G05] **G05 图片理解与人工核对持久设计**：在 `.specify/data-model.md`、`contracts/vision-capability.md`、`contracts/paper-import.md` 明确暂存图/QuestionAsset 的理解条件、问题、实际调用来源、教师身份/UTC 时间/说明和失效关联；与 G02/G03 的承载复用方案共同评审，明确校正确认后如何转入/关联正式题，不以 caption 或单个布尔值替代核对证据。 依赖：T135、T136、T137。per FR-043/047、G05、CHK005（F005） (missing)

- [X] T139 [MEDIUM] [设计 G06] **G06 导入关系与状态同步**：按已确认模型/导入契约更新 `.specify/plan.md` §8/9及实体引用：Document 非空唯一关联、paper_source 的知识库条件约束、Rejected/Failed/Ready 语义及终态；同步已创建模型/契约的阶段说明，保留 v1.0 原文及知识库状态职责。 依赖：无新增任务前置。per FR-041/042、G06、CHK006（F006） (contradicts)

- [X] T140 [MEDIUM] [设计 G07] **G07 导出目录统一**：按 `contracts/file-storage.md` 同步 `.specify/plan.md` §11和目录树为独立 `storage/exports/`，使后续导出登记、迁移及备份清单只消费同一规范定位；不增加双目录隐式查找。 依赖：T137。per FR-044/045、G07、CHK007（F007） (contradicts)

- [X] T141 [HIGH] [设计 G08] **G08 组卷条件持久设计**：在 `.specify/data-model.md`、`contracts/exam-assembly.md` 明确最近一次教师组卷要求的存储、成功/失败写入时机、人工替换后的重算及旧考试合法缺省；优先评审 Exam 上受 Pydantic 校验的约束 JSON，实际分值仍以 ExamQuestion 为事实源，取舍由用户确认后同步。 依赖：无新增任务前置。per FR-048/049、G08、CHK008（F008） (missing)

- [X] T142 [MEDIUM] [设计 G09] **G09 评测协议锁定**：在 `.specify/plan.md` 非功能/门禁细节及 `docs/evaluation.md` 锁定样本与标注规则、机器/配置、重复次数、冷暖启动、计时边界、分母、失败记录及证据格式；把质量阈值明确设为真实基线后提请用户确认，不预填效果数值或把模型自信当准确率。区分业务铁律逐例验收与质量/性能实测，阈值确认由 T168 承接。 依赖：无新增任务前置。per SC-010–014、plan 非功能目标、G09、CHK009（F009） (partial)

### E0：基线复验 + 文件盘点

- [X] T143 [HIGH] [E0] **复验 v1.0 基线**：在 `docs/validation-report.md` 记录当前分支、提交、tag、工作区边界及依赖状态；按既有框架执行 pytest、mypy、ruff 和隔离 Docker M0/迁移/readiness/Redis 检查，核实 T133 的既有全量失败及 M5 证据适用范围。分开记录环境问题、基线失败与新回归，不清理用户工作区、不改旧任务状态；影响后续目标的基线问题提交具体处置建议，不自动扩成无关修复。 依赖：无新增任务前置。per plan Gate 1–9/兼容性目标、T133、CHK012（F010） (partial)

- [X] T144 [HIGH] [E0] **盘点文件与历史考试数据**：在 `docs/v2.0-storage-inventory.md` 只读盘点 Document.storage_path、临时文件、导出/来源关系及 exam_questions 的历史题序、分值/Rubric/知识点证据，区分存在、缺失、未知、待核对和受保护引用；列出可迁移对象及不可可靠回填项，不搬移/删除文件，不以当前题值代替历史值。 依赖：T143。per FR-044/049、data-model §10、CHK010/012（F011） (partial)

- [X] T145 [HIGH] [E0] **建立 v2.0 TCR 与分批验证映射**：先在 `docs/test-change-record-v2.md` 按现有 TCR 形式逐批写明必要性、覆盖行为、受影响 pytest unit/contract/integration/benchmark 模块与保留断言；将 FR/SC/Gate 映射至各批聚焦验证。每批新增/修改测试前按已确认设计补齐该批 TCR，先写能暴露目标缺口的行为用例再实现；不一次性堆建未确认设计的测试，不重写 v1.0 测试标准。 依赖：T143。per plan 兼容性目标、tasks 测试说明、Constitution V、CHK011/012（F012） (missing)

- [X] T146 [MEDIUM] [E0] **准备标注样本与评测证据入口**：按 T142 协议在 `benchmark/` 与 `docs/evaluation.md` 准备文字/扫描/图片/混合页/跨页/无答案/错序样本，语义四类错误及无问题/旧答案失效样本、图片条件样本、可满足/不可满足组卷和开发者审查的参考统计基准；采用开发者审查 + AI 辅助标注（学习项目口径），正式独立教师标注作为后续增强项；记录真实标注/来源、版本、运行环境与 JSON/CSV 结构。此任务只准备输入，准确性/性能运行分别由 T160/T168/T179/T189/T190 承接。 依赖：T142、T145。per SC-010–014、plan 可评测目标、Constitution V（F013） (missing)
  本批验收口径（用户确认，2026-10-03）：基准为 AI 辅助 + 开发者审查；标注作者AI（会话委托）+开发者，独立教师数量0。61例准备及参考核算已完成，可用于本批T168，不冒充独立教师；后续另建真实教师标注版本替换并保留历史。见 docs/evaluation.md 的 T146 口径确认节。

### E1：文件持久化

- [X] T147 [HIGH] [E1] **实现持久根目录与文件登记**：在 `backend/app/core/config.py`、新增 `backend/app/services/file_storage_service.py`、已确认映射对应模型及 `migrations/versions/` 实现持久根配置、相对定位、稳定 file_id、真实资源/导出登记和迁移状态；配置 Docker 挂载、开发与 EXE 用户目录及 `.gitignore`，可靠落盘后建立引用，失败留真实归属/诊断。按 G04 选择承载，不新增对象存储或重复路径事实源。 依赖：T137、T140、T144、T145。per FR-044/045、file-storage、CHK004/007（F014） (partial)

- [X] T148 [HIGH] [E1] **实现授权文件读取与引用生命周期**：在新增 `backend/app/api/file_storage.py` 和文件服务实现 GET /api/files/{file_id}，继承课程/考试/本人结果授权，区分 missing/history_unknown/401/403/404；实现共享引用删除保护、FILE_IN_USE 及原材料保留。E2 创建原页/题图时接入具体资源映射，E4 将发布/历史保护接入同一服务；不开放含答案源卷静态地址。 依赖：T147。per FR-043/044、file-storage 访问/删除契约（F015） (missing)

- [X] T149 [HIGH] [E1] **接入教学上传与导出文件持久化**：在 `backend/app/api/knowledge_bases.py`、`services/knowledge_base_service.py`、`ai/ingestion/service.py` 及现有导出写入边界统一消费文件服务；让解析器解析服务端持久定位，保留旧 PDF/TXT/Markdown 上传和元数据接口语义。实际导出采用 exports 并登记所属资源，不新造导出格式或伪造历史文件。 依赖：T147、T148。per FR-010/044、plan §11、file-storage（F016） (partial)

- [X] T150 [HIGH] [E1] **实现历史文件迁移工具**：在 `scripts/migrate_storage_paths.py` 基于 T144 盘点实现先复制核对、后事务更新引用/迁移状态、成功后才可清理旧副本；重复执行识别已迁移项，失败保留原定位/文件，缺失和未知单列。在隔离样本演练并记录 `docs/v2.0-storage-inventory.md`，真实文件清理/恢复等破坏性动作另按授权执行。 依赖：T144、T147、T149。per FR-044、file-storage 历史迁移、data-model §10（F017） (missing)

- [X] T151 [HIGH] [E1] **实现一致备份与恢复工具**：在 `scripts/backup_restore.py` 按 G04 manifest 与写入窗口覆盖数据库、uploads/papers/assets/exports，处理在途写入并在恢复核对通过前关闭业务写入；校验真实关联/文件、报告缺失/未知/部分失败。先在隔离目标演练，保留现有配置/凭据管理；后续 E2–E4 新资源接入登记，最终 T191 用完整业务数据再验恢复。 依赖：T137、T147、T148、T150。per FR-045、file-storage 一致备份与恢复（F018） (missing)

- [X] T152 [HIGH] [E1] **验收文件持久化和迁移兼容**：依 T145 的先行用例，在 `tests/unit/services/`、`tests/contract/`、`tests/integration/` 验证上传重启、同名不覆盖、资源越权/共享删除、复制与事务失败、幂等迁移、同集恢复及缺失/未知；保留知识库原接口回归，并在 `docs/validation-report.md` 记录 Gate 13 的阶段证据，尚无真实原卷/题图链路的部分留 T160/T191 完成。 依赖：T145、T147、T148、T149、T150、T151。per FR-044/045、SC-010/014、plan Gate 13（F019） (partial)

### E2：试卷导入 + OCR + 校正

- [X] T153 [HIGH] [E2] **实现导入实体与校正持久模型**：在 `backend/app/models/`、`schemas/`、`domain/enums.py`、`migrations/versions/` 新增 PaperImport/SourcePage/ExtractedQuestion，扩展 Document.purpose、Question.source_type/frozen_at/analysis 和 G03 校正字段；知识库默认列表/摄取限定 knowledge_base，保持知识库条件非空、Document 原路径唯一事实源、source_page_ids 唯一表达及旧未知来源。新增解析纳入 Approved 守卫，不等 E4 才保护已批准内容。 依赖：T136、T139、T145、T152。per FR-041/042、G03/G06、data-model §7.1–3/8/10（F020） (missing)

- [X] T154 [HIGH] [E2] **实现题图资产及原页关系**：新增 `backend/app/models/question_asset.py`、`backend/app/api/question_assets.py`、相关 schema/迁移和服务，在文件服务绑定原图、裁图像素坐标、同导入页来源及最多 5 图；提供教师校正原图关联，学生资产仅含允许展示区域，避免把含答案整页当题图。G05 已确认的核对结构随暂存校正可靠保存，完整理解/处置服务在 T163 接入。 依赖：T138、T147、T148、T153。per FR-043、G05、data-model §7.4、file-storage（F021） (missing)

- [X] T155 [MEDIUM] [E2] **验证并确认 OCR 推理依赖**：在 `docs/evaluation.md` 记录候选 OCR 在标注扫描/公式表格/跨页与 Windows 打包环境的能力、版本、资源和局限，优先评估 plan 候选 PaddleOCR；给出少量方案的收益/代价，请用户锁定首版适配器与关键依赖后交给 T156。无需实现所有候选，不引入训练依赖。 依赖：T146。per FR-042、plan §9/14、ocr-provider（F022） (missing)

- [X] T156 [HIGH] [E2] **实现可选 OCR Provider**：在新增 `backend/app/ai/ingestion/ocr/`、配置和 `pyproject.toml` 实现 BaseOCRProvider.extract_text/describe 及 T155 已确认适配器，Pydantic OCRResult/区域坐标/置信度校验、可选依赖延迟加载和真实错误传播；OCR_ENABLED 默认 false，扫描需要 OCR 时明确失败，空白页与坏输出分开，不静默换适配器。 依赖：T145、T155。per FR-042、ocr-provider、Constitution III/IV（F023） (missing)

- [X] T157 [HIGH] [E2] **实现试卷上传、按页解析与拆题编排**：在新增 `backend/app/services/paper_import_service.py`、`api/paper_import.py`、`ai/ingestion/paper_pipeline.py`、`paper_extraction/` 及已有 `parsers.py` 中，接入路由/依赖装配：可靠保存原卷后返回 Uploaded，逐页保存页图，文字优先提取、混合/扫描走 OCR，拆题结果经 Pydantic 后暂存；落实最多 50 页、零题失败、实际页/题进度和原错误/中间结果保留，paper_source 不进入 Chunk/Embedding。 依赖：T153、T154、T156。per FR-041/042、paper-import、plan §8/9（F024） (missing)

- [X] T158 [HIGH] [E2] **实现校正、拒绝与幂等确认入库**：在 `services/question_correction_service.py`、导入服务/API 实现列表/详情/PATCH/commit，持久修改题边界、跨页、顺序、解析与图像关联；同导入串行核对，Question(Draft)、题图/来源与 ExtractedQuestion.question_id/Corrected 同事务，非法指定批次整体拒绝，重复确认返回已有题。缺答案/Rubric 保持待补全；全部拒绝与部分确认的终态按契约处理，确认后不反写来源。 依赖：T157。per FR-041/042、paper-import 事务/状态机、US1-v2/AC2（F025） (missing)

- [X] T159 [HIGH] [E2] **实现原页与结构化题目校正界面**：新增 `backend/app/ui/paper_import_view.py` 和 `paper_correction_view.py`，接入 `gradio_app.py`/导航及真实加载器；提供独立试卷入口、原页与暂存题并排、跨页/题边界/选项/图像关联校正、显式拒绝/批次确认、待补全及失败说明。重新打开读取持久记录；图像理解/核对入口由 T163/T164 补接，不用 UI 状态代替保存。 依赖：T158。per FR-041/042、SC-010、plan Gate 10/11（F026） (missing)

- [X] T160 [HIGH] [E2] **验收导入、OCR、校正与重启**：依 T145 先行用例，在 `tests/contract/`、`tests/integration/` 和 `benchmark/` 运行文字/扫描/混合 PDF、图片、跨页/错序/无答案/损坏/超页、OCR 禁用/失败、并发校正与重复 commit；真实标注逐字段核对并分开报告自动提取和校正结果，验证原卷/页图/题图重启可读与学生源卷隔离。按 T142 记录导入/校正耗时及 Gate 10/11/13 阶段证据。 依赖：T145、T146、T152、T153、T154、T156、T157、T158、T159。per FR-041–045、SC-010、plan Gate 10/11/13（F027） (missing)
  本批验收口径变更（用户会话确认，2026-10-03）：采用用户复核的 AI 辅助参考，保留 AI 作者与会话确认，不宣称独立教师真值；详见 docs/evaluation.md 的 T160 节。T146/T168 原要求不变。
  修复重验（2026-10-03，用户批准50页≥6/8、页面≥20/24）：50页7/8、1/10页16/16、页面24/24，真实导入worker资源完整且预算通过；故障/重启与全量回归通过。按本批口径勾选，原全部导入通过口径仍未满足（1例失败保留）。证据：benchmark/results/v2/t160-retest-20261003/acceptance-summary.json。

### E3：原题改编 + 语义核验

- [X] T161 [HIGH] [E3] **实现章节定位生产与迁移**：按 G01 在 `models/document_chunk.py`、`ai/ingestion/chunking.py`、`ai/ingestion/service.py`、知识库服务/API/UI 及迁移实现真实章节/小节/知识点标注、教师核对和 (course_id,chapter_id,section_order) 索引；跨章按真实边界切分，旧片段未知保留 NULL。不能只补查询字段而没有定位生产入口。 依赖：T134、T145、T149、T153。per FR-024/025、G01、rag-retrieval Chunk定位（F028） (partial)

- [X] T162 [HIGH] [E3] **贯通章节范围与四种检索 SQL**：在 `ai/retrieval/base.py`、`_filters.py`、`vector_search.py`、`keyword_search.py`、`hybrid_search.py` 及 Query 构造/调用方贯通授权课程、资料、章节/小节和知识点；先 SQL 过滤 Ready/knowledge_base 再打分/Top-K/融合/重排。保留无范围默认，跨课程/未映射明确拒绝，合法不足不扩大范围；出题与阅卷共用同一范围消费边界。 依赖：T161。per FR-024/025/032、rag-retrieval §SQL强制过滤（F029） (partial)

- [X] T163 [HIGH] [E3] **实现核验与图片核对持久服务**：按 G02/G05 在 `models/`、`schemas/`、迁移及新增 `services/content_validation_service.py` 实现每轮报告、实际 Agent/Provider 来源、技术错误、教师处置与原图/当前内容关联；接通暂存题转正式题的核对记录及读取/处置接口，变化后旧结果失效但证据保留，不伪造教师身份或将机器报告塞进意见。 依赖：T135、T138、T145、T153、T154。per FR-043/047、G02/G05、agent-workflow、vision-capability（F030） (missing)

- [X] T164 [HIGH] [E3] **实现图片能力与结构化理解**：在 `ai/llm/base.py` 增加默认 False 的 supports_vision，保持 generate_structured 签名；经已配置且确认具备能力的适配器及新增 `ai/vision/base.py`、`ai/vision/provider.py` 处理授权图像/传输/条件 DTO，并接入 T163 的人工核对入口与导入 UI。能力按实际模型判断；若需新增关键 Provider 先提交选型供用户决定，不强迫切换旧模型。无能力/传输不可用/调用失败/不可靠分别处理，可靠人工核对可继续流程。 依赖：T148、T154、T159、T163。per FR-043、vision-capability、plan §10、Constitution III/IV（F031） (missing)

- [X] T165 [HIGH] [E3] **扩展文字生成与原题改编来源**：在 `ai/agents/question_agent.py`、`api/question_generation.py`、新增 `services/question_adaptation_service.py`、来源持久化边界及新增 QuestionSourcePaper 模型/迁移接入章节/资料/目标分值和两种生成路径；新文字题保存真实教学引用，改编候选与父题关系同事务，Course 锁下核对同课程/无环/不自引，复用原图新关联并重核验，不覆盖父题。source_type 来自真实创建路径，旧知识引用状态/快照不重定义。 依赖：T162、T163、T164。per FR-024/025/046、question-source-persistence、US1-v2/AC3（F032） (partial)

- [X] T166 [HIGH] [E3] **接入语义核验及服务端批准约束**：扩展 `services/question_validator.py`、`content_validation_service.py`、`question_service.py` 与 Agent 编排，对生成、改编、人工/导入补全候选核对答案、条件、选项歧义与 Rubric；Agent 只返结构化报告，服务计算 can_review，报告与合法 Needs Revision 转换原子保存。批准时记录真实 frozen_at，合法退回清除当前批准时间，历史未知不伪填。修改答案相关字段/图片/依据使旧核验失效，技术失败保留原错且阻止批准；现有候选 DTO、原阅卷拓扑和历史成绩读取保持。 依赖：T163、T164、T165。per FR-025/028/047、agent-workflow、SC-011（F033） (partial)

- [X] T167 [HIGH] [E3] **扩展候选审核与题库补全 UI**：在 `ui/question_generation_view.py`、`question_view.py`、相关 API/加载器展示范围、教学片段/位置、父题/原卷、解析、图像核对与分项核验，允许补全和显式修订/再提交/批准；只消费服务端真实状态和有效报告。复用当前工作区已有列表/详情/编辑与设计系统改动，实施时核实其合并状态，不覆盖用户成果。 依赖：T159、T165、T166。per FR-018/028/043/046/047、SC-011（F034） (partial)

- [X] T168 [MEDIUM] [E3] **运行质量基线并确认验收阈值**：在 `benchmark/`、`scripts/` 与 `docs/evaluation.md` 按 T142/T146 执行真实 OCR/拆题、图片条件、语义核验标注评测；记录自动/人工结果、TP/FP/TN/FN、覆盖率和失败样本、实际模型/Prompt/数据版本。依据真实基线提出质量阈值供用户确认后补齐评测协议；无可评数据不填伪造百分比，未达目标如实报告并只列当前所需修复。 依赖：T142、T146、T160、T162、T164、T166。per G09、SC-010/011、plan 可评测目标、Constitution V（F035） (missing)

- [X] T169 [HIGH] [E3] **验收范围、改编、图片与重核验**：按 T145 执行四模式 Top-K 前范围过滤/旧默认回归、跨课程/未知章节拒绝、父题无环与事务、支持/不支持图片、教师处置、持久报告与内容变更失效用例；用五类错误/无问题样本核对不能批准的场景，保留 Candidate Generation 与教师审核职责。将实际生成/改编证据写入 `docs/validation-report.md`，不得用 Schema 通过代替语义评测。 依赖：T145、T165、T166、T167、T168。per FR-024/025/043/046/047、SC-011、plan Gate 12/19（F036） (partial)

### E4：条件组卷 + 内容冻结

- [X] T170 [HIGH] [E4] **升级 ExamQuestion 与组卷条件模型**：在 `models/associations.py`、`models/exam.py`、新增 `models/exam_question.py`、schemas 和迁移升级原 exam_questions，保存 id/order_index/score/base_score/published_knowledge_points/scoring_basis 与 G08 约束；保留原 FK 语义和旧关系可读性，唯一性/金额约束一致。草稿合法缺省取题库值，旧发布数据只据真实证据处理。 依赖：T141、T144、T145、T169。per FR-048/049、G08、data-model §7.6/9/10（F037） (partial)

- [X] T171 [HIGH] [E4] **落实历史考试核对与兼容迁移**：在 `migrations/versions/`、`scripts/` 及 `docs/validation-report.md` 将 T144 盘点转为历史关联迁移/核对流程：题序依据当前确定排序或更强真实证据，分值/标准/知识点只按当时证据固定；未知显式报告，历史原始结果可读但不能未经核对重评。隔离数据库验证关联数量、历史读取及失败恢复，不以迁移执行时间伪造批准时间。 依赖：T144、T170。per FR-049、exam-scoring 历史兼容、data-model §10（F038） (missing)

- [X] T172 [HIGH] [E4] **实现条件组卷与约束持久化**：在新增 `services/exam_assembly_service.py`、`services/exam_service.py` 和 `api/exams.py` 实现 assemble：同课程已审核/补全/语义处置及图像可用候选，精确题型/数量/知识点覆盖/Decimal 总分；保存教师要求和成功组合，不自动降条件或改分。区分确有冲突与策略未找到，失败返回真实 gaps 且保持原草稿，不引入独立求解服务。 依赖：T170、T171。per FR-048、exam-assembly 输入/输出、SC-012（F039） (missing)

- [X] T173 [HIGH] [E4] **实现题序、替换、改分与完整预览**：在考试服务/API 实现 PATCH 关联题：qid 使用 Question.id，事务移位维持 1..题数，替换新关系并保留位置/显式分值；改分/替换后废弃旧评分确认，重算持久组卷条件，草稿可暂存缺口但不能发布。预览读取相同题序/本场值/授权图，保留原创建选题入口。 依赖：T172。per FR-048/049、exam-assembly 题序、替换与预览（F040） (partial)

- [X] T174 [HIGH] [E4] **实现 Rubric 换算与教师确认**：在考试评分服务/schema/API 与组卷 UI 实现本场标准准备、基准/有效满分、至少 28 位 Decimal 中间精度和 ROUND_HALF_UP；数值要点来自校验/教师核对，先乘后除仅末次量化。展示正负尾差并保存真实教师确认，定性/非加总标准核对对应语义，不正则替换数字、不自动分摊尾差；发布前缺依据/未确认明确拒绝。 依赖：T170、T173。per FR-049、exam-scoring、data-model §9.2（F041） (missing)

- [X] T175 [HIGH] [E4] **实现原子发布与全引用生命周期冻结**：在 `exam_service.py`、`question_service.py`、资产/父题服务及课程/考试/文档删除直接入口统一发布/修订/删除的事务检查与固定锁顺序；发布时固定四项本场依据，立即保护内容/解析/题图/来源，Closed/Archived 或答卷/评分/复核历史继续保护，不能通过级联删除或退回修订解冻。保留 I01 错误及元数据例外，受保护题另建派生候选，不引入完整快照。 依赖：T148、T154、T165、T170、T173、T174。per FR-018/049、exam-assembly 发布冻结、data-model §9.1（F042） (partial)

- [X] T176 [HIGH] [E4] **贯通考试评分输入与身份**：在 `services/grading/grading_task_service.py` 的 DatabaseGradingSubmissionReader/SubmissionSnapshot、`schemas/grading.py`、Agent/Workflow 状态与仓储中传递 exam_question_id、本场分值/基准/标准、发布知识点及已核对图片；替换 v2 考试上下文直接读取 Question.score 的来源。新发布依据缺失明确报错，旧历史按 T171 兼容读取，幂等身份不跨场复用。 依赖：T164、T171、T175。per FR-043/049、exam-scoring ScoringInput、US3-v2/AC1–3（F043） (partial)

- [X] T177 [HIGH] [E4] **贯通规则评分、主观评分、复核与汇总**：在 `services/grading/`、`ai/agents/grading_agent.py`、`reviewer_agent.py`、`ai/workflows/`、`services/review_service.py`、`diagnosis_service.py` 消费同一固定输入；客观题不调用 LLM，主观题使用已核对标准且不二次缩放，图像能力不足明确待处理。原始/量化分数均校验本场上限，结果 Decimal 求和，失败/缺依据/待复核不记零分，保留既有检查点与复核生命周期。 依赖：T176。per FR-029–040扩展、FR-043/049、exam-scoring、SC-012/013（F044） (partial)

- [X] T178 [HIGH] [E4] **接通组卷、答题与阅卷显示**：扩展 `ui/exam_view.py`、`student_exam_view.py`、`review_view.py` 及考试/提交接口，展示条件缺口、题序/替换、显式分值、Rubric/尾差核对与教师预览；学生答题按同一题序/本场值/题图且不泄露答案/源卷，缺必要图像明确异常。阅卷复核展示相同图像/条件/标准，旧答卷提交和参加资格规则保留。 依赖：T173、T174、T175、T176、T177。per FR-043/048/049、US2-v2/AC1、US3-v2/AC1–3（F045） (partial)

- [X] T179 [HIGH] [E4] **验收组卷、考试分值与发布冻结**：按 T145 执行条件满足/不满足无写入、移位/替换、同题两场不同满分、默认值、0.005 边界、正负尾差确认、越界失败、并发发布/修订/资产变更、Closed/Archived/历史保护及旧 API/迁移回归；核对预览到评分/复核/汇总一致。按 T142 运行≤100题组卷性能，200题既有请求边界不被改写；证据落 `docs/validation-report.md` 与 benchmark/results。 依赖：T145、T146、T171、T172、T173、T174、T175、T176、T177、T178。per FR-048/049、SC-012、plan Gate 14–16（F046） (partial)

### E5：两类分析页面

- [X] T180 [HIGH] [E5] **扩展教师考情统计服务**：在 `api/results.py` 的 ResultsQueryService、`schemas/grading.py` 与现有汇总边界增加可参加/已参加/提交、最终分布/平均、逐题得分率、发布知识点失分和关注名单；实际最终答卷为分母，失败/待复核/缺依据单列，多知识点不重复累计总分，无观测为暂无数据。复用现有最终成绩存储，不让模型生成统计。 依赖：T177、T179。per FR-050/039、exam-scoring 汇总、SC-013（F047） (partial)

- [X] T181 [HIGH] [E5] **扩展学生诊断和来源推荐**：在 `services/diagnosis_service.py`、`api/results.py`、相关 schema/检索/题库查询中基于本人最终失分与发布知识点，返回可访问同课程资料和已审核练习、真实出处/知识点/理由及原题图；缺资料/题目明确返回不足，不用未审核/待补全候选补位，保留报告与当前最终结果对应关系。 依赖：T162、T177、T179。per FR-051/040、exam-scoring 学生反馈、SC-013（F048） (partial)

- [X] T182 [HIGH] [E5] **实现教师考情分析页面**：扩展 `ui/results_view.py`、`results_loaders.py` 的教师视图，提供统计概览、分布、逐题/知识点明细、分母及关注学生详情，联动实际答卷与复核入口；空数据、未完成与失败分别展示，数值只读服务事实。 依赖：T180。per FR-050、US3-v2/AC4、SC-013（F049） (partial)

- [X] T183 [HIGH] [E5] **实现学生知识点反馈页面**：扩展 `ui/results_view.py`、`results_diagnosis.py` 及加载器，展示本人逐题答案/图片/评分解释/失分与薄弱知识点、资料/练习来源入口；待复核、失败、依据不足分别显示，授权后打开文件，不把无结果解释为零分或确定弱点。 依赖：T181。per FR-051、US2-v2/AC2–3、SC-013（F050） (partial)

- [X] T184 [HIGH] [E5] **核对两类分析与独立统计基准**：按 T145 在 `tests/contract/test_results_api_contract.py`、`tests/integration/`、`tests/unit/ui/` 对照 T146 的教师独立核算，覆盖多知识点、多场分值、复核前后、失败/缺依据/零样本和推荐越权/缺资料/未审核题；保存 UI 关键操作与真实答卷证据到 `docs/validation-report.md`，100% 样本统计一致才报告 SC-013 通过。 依赖：T145、T146、T180、T181、T182、T183。per FR-050/051、SC-013、plan Gate 19（F051） (partial)

### E6：UI 收敛 + EXE 交付

- [ ] T185 [MEDIUM] [E6] **收敛公共 UI 并验收三页样板**：在 `ui/design_system.py`、`layout_view.py`、`gradio_app.py` 与既有视图复用已存在的主题/图标/列表详情编辑结构，统一字号/间距/按钮/六类业务状态和错误提示；按 spec 的 Ant Design Pro/考试星/Canvas 参考落实实际流程。在导入校正、教师题库或组卷、学生首页或诊断三类代表页保留运行截图/操作记录到 `docs/`，尊重未提交 UI 成果。 依赖：T159、T167、T178、T184。per spec 跨功能UI约束、SC-014、plan Gate 18（F052） (partial)

- [ ] T186 [MEDIUM] [E6] **完成七类业务页面 UI 验收**：将样板规则应用到试卷导入与校正、知识库、AI 出题与审核、组卷与考试、阅卷、教师分析、学生首页与诊断七类页面，逐页验证列表/详情/显式编辑或对应查看操作、保存/取消、角色导航与真实状态，保存 `docs/v2.0-ui-acceptance.md` 及截图；仅补剩余差距，三页样板不代表全 UI 完成，不新增前端技术栈。 依赖：T185。per spec 跨功能UI约束、SC-014、plan Gate 18/19（F053） (partial)

- [ ] T187 [HIGH] [E6] **实现 Windows 单机启动器**：新增 `scripts/launch_windows.py`（或 plan 目录约定的等价入口），复用配置、迁移和 readiness：检查用户持久目录/外置模型配置、PostgreSQL/pgvector、Redis及必要网络，确认迁移成功后启动本机 FastAPI/Gradio并在就绪后打开浏览器；失败显示真实步骤，退出仅停止自己启动的应用进程，Docker 原入口保留。 依赖：T147、T151、T170、T179。per FR-052、plan §14/Gate 17、SC-014（F054） (missing)

- [ ] T188 [HIGH] [E6] **构建 onedir EXE 交付包**：在 `packaging/` 和 `scripts/build_exe.ps1` 增加 PyInstaller onedir 构建清单/脚本，收集 Gradio 静态资源、动态模块、Alembic迁移和 T155 锁定 OCR 的可选推理资源；外置配置与业务数据，不捆绑数据库安装器、不强制 onefile，记录实际依赖/构建版本而不制造跨组件版本相等门禁。依赖版本/体积以验证记录为准。 依赖：T155、T156、T186、T187。per FR-042/052、plan §9/14、Constitution I/III（F055） (missing)

- [ ] T189 [HIGH] [E6] **执行真实 Windows 交付验收**：在准备好依赖的 Windows 单机运行 T188 包，覆盖首次/后续启动、配置/数据库/Redis/迁移/模型网络失败、OCR启用/禁用、图片读取、重启及退出进程所有权；按 T142 冷暖/重复协议记录首次<30s、后续<10s目标和相关应用/数据库/Redis同时峰值≤6GB及各预算，超标如实报告。证据写 `docs/v2.0-exe-acceptance.md`，不以源码运行替代打包运行。 依赖：T142、T146、T188。per FR-052、SC-014、plan Gate 17/非功能目标（F056） (missing)

- [ ] T190 [HIGH] [E6] **完成兼容回归与 v2.0 测量汇总**：按既有框架执行全量 pytest、mypy、ruff、隔离迁移/历史读取、Docker M0/三容器 readiness及原检索/阅卷 Benchmark；汇总 T160/T168/T179/T189 的质量、耗时、资源和失败样本到 `benchmark/results/`、`docs/evaluation.md`，仅有新修改/失败时重测相关场景。保留 v1.0 用例、原证据和所有真实未通过项，不修改旧任务勾选。 依赖：T145、T152、T160、T169、T179、T184、T186、T189。per plan Gate 1–19/兼容性目标、Constitution I/V（F057） (partial)

- [ ] T191 [HIGH] [E6] **完成真实闭环、恢复及交付证据**：在实际课程完成导入校正、教学资料、知识库文字出题与原题改编两条路径、答案重核验/教师审核、带图条件组卷、本机考试、混合评分/人工复核及两类分析；覆盖缺答案/不同本场分值/失败状态。用含全部新资源和答卷评分的数据在隔离目标执行一致备份恢复，核对来源/文件/成绩，并汇总七页 UI/EXE 证据到 `docs/validation-report.md`、`docs/release-checklist.md` 以及 `docs/paper-import.md`、`docs/file-lifecycle.md`、`docs/exe-deployment.md` 使用说明；记录验收结论供用户裁决，未实测不宣称交付，发布另按授权。 依赖：T151、T160、T169、T179、T184、T186、T189、T190。per FR-041–052、SC-010–014、plan Gate 10–19（F058） (missing)

### 需求、场景与验证追溯

| 意图/验收项 | 设计与实施任务 | 聚焦/最终证据任务 |
| :--- | :--- | :--- |
| FR-010 扩展、FR-044/045 | T137/T140、T144、T147–T151 | T152、T160、T191；Gate 13 |
| FR-041/042、US1-v2/AC1–2 | T136/T139、T153–T159 | T160、T191；SC-010、Gate 10/11 |
| FR-043、US2-v2/AC1、US3-v2/AC1 | T138、T148/T154、T163/T164、T176/T178 | T160/T169/T179、T191；Gate 12 |
| FR-018/028 扩展、FR-047 | T135/T136、T153、T163/T166/T167、T175 | T169/T179、T191；SC-011、US1-v2/AC3–4 |
| FR-024/025 扩展、FR-046 | T134/T135、T161/T162/T165–T167 | T168/T169、T191；SC-011、US1-v2/AC3 |
| FR-020 扩展、FR-048、US1-v2/AC5 | T141、T170/T172/T173/T175/T178 | T179、T191；SC-012、Gate 14 |
| FR-049、US1-v2/AC4、US3-v2/AC2–3 | T136、T170/T171/T174–T178 | T179、T191；SC-012、Gate 15/16 |
| FR-039 扩展、FR-050、US3-v2/AC4 | T176/T177、T180/T182 | T184、T191；SC-013、Gate 19 |
| FR-040 扩展、FR-051、US2-v2/AC2–3 | T181/T183 | T184、T191；SC-013、Gate 19 |
| FR-052、SC-014（单机启动/恢复） | T147/T151、T187/T188 | T189/T191；Gate 13/17 |
| SC-014（七类页面）、跨功能 UI | T159/T167/T178/T182/T183/T185/T186 | T185 三页样板、T186 七类逐页、T191；Gate 18/19 |
| G09、质量/性能/资源目标 | T142/T146、T155 | T160/T168/T179/T189/T190；质量阈值基线后确认 |
| v1.0 兼容、宪章 I–V | T143/T145、各批既有入口回归 | T152/T160/T169/T179/T184/T190/T191；Gate 1–9 原要求保留 |

**依赖主线**：E0 复验/盘点与按模块设计补齐 → E1 → E2 → E3 → E4（关联/历史核对 → 组卷与 Rubric → 原子发布 → 评分/复核）→ E5 → E6。章节定位生产 T161 必须先于范围查询 T162；图像/核验承载 T163 先于能力接入/语义批准；分析不得绕过 T176/T177 的考试事实源。T155 的选型准备、A 的独立设计和 E6 启动器准备可在直接依赖满足后推进，不能用并行名义跳过所依赖合同。

**停止与下一步**：本轮以文档追加、文档检查、单独提交并推送 origin/deepcode 为终点。完成后停止，等待用户确认；下一轮建议先 T143/T144，再执行 E1 所需 T137/T140，其他设计任务按依赖展开。新任务均为未完成，不执行 implement，不将待评审清单自动勾选。
