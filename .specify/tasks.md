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

- [ ] T031 [US1] 在 `tests/contract/test_document_parsers.py` 中增加失败优先的解析器契约测试，覆盖 PDF、TXT、Markdown、不支持格式、空文件和损坏文件
- [ ] T032 [US1] 在 `backend/app/ai/ingestion/parsers.py` 中实现受支持的 Document Parser 注册表和适配器，不支持的格式不得创建可用知识
- [ ] T033 [US1] 在 `backend/app/ai/ingestion/cleaning.py` 和 `backend/app/ai/ingestion/chunking.py` 中实现文本清洗和带来源元数据的确定性分块
- [ ] T034 [P] 在 `backend/app/ai/embedding/base.py` 中实现 `BaseEmbeddingProvider`，提供 `embed_documents()` 和 `embed_query()`
- [ ] T035 [P] 在 `backend/app/ai/embedding/factory.py` 中实现 Embedding Provider 选择和配置，兼容云端 API、本地 Hugging Face、BGE 及其他兼容 Provider
- [ ] T036 在 `tests/contract/test_embedding_provider.py` 中增加 Embedding Provider 契约测试，依赖 T034 和 T035，自动验证 `embed_documents()` 与 `embed_query()` 的输入类型、空输入处理、输出向量维度、批量数量与返回数量一致，以及 Provider 切换后仍满足同一接口契约
- [ ] T037 在 `backend/app/ai/ingestion/service.py` 中实现从解析、清洗、分块、Embedding 到 Ready/Failed 状态的资料摄取编排
- [ ] T038 在 `backend/app/models/document_chunk.py` 和 `migrations/versions/` 中创建 `DocumentChunk` 模型及迁移，支持 pgvector embedding、`search_vector`、元数据、来源关系、HNSW 和 GIN
- [ ] T039 [US1] 在 `backend/app/services/knowledge_base_service.py`、`backend/app/api/knowledge_bases.py` 和 `backend/app/ui/knowledge_base_view.py` 中将资料上传和处理状态连接到知识库服务、API 和 Gradio 界面
- [ ] T040 [US1] 在 `tests/contract/test_retrieval_contract.py` 中增加失败优先的检索契约测试，覆盖来源追踪、空上下文、模式选择和结果字段
- [ ] T041 在 `backend/app/ai/retrieval/vector_search.py` 中实现 pgvector 语义检索，包括 HNSW 配置和精确近邻 Benchmark 模式
- [ ] T042 在 `backend/app/ai/retrieval/keyword_search.py` 中实现 PostgreSQL `tsvector + GIN` 关键词检索
- [ ] T043 在 `backend/app/ai/retrieval/hybrid_search.py` 中实现候选合并、去重、Weighted Score Fusion、Top-K 选择和检索模式切换
- [ ] T044 在 `backend/app/ai/retrieval/reranker.py` 中实现 Rerank 适配器契约以及 LLM Rerank 和轻量 Cross Encoder 路径，记录重排分数和 Provider 元数据
- [ ] T045 [P] 在 `tests/contract/test_retrieval_contract.py` 和 `scripts/run_retrieval_benchmark.py` 中增加检索契约覆盖和四模式 Benchmark 执行器，比较 Vector Only、Keyword Only、Hybrid 和 Hybrid + Rerank

**M2 检查点**：单一 PostgreSQL 检索底座可用，来源 ID 得以保留，Rerank 保持可替换，
Embedding Provider 契约测试通过，并且 Benchmark 可以比较所有要求的检索模式。

## 阶段 M3：AI 阅卷

**目标**：完成题型路由、客观题确定性评分、主观题 RAG + LLM 结构化评分、置信度分支、
统一成绩和学生诊断。

**独立验证**：一份同时包含客观题和主观题的 Submission 能完成评分；客观题不调用 LLM，
主观题使用课程上下文并通过结构化校验；低置信度结果不会进入最终成绩，高置信度或已
复核结果能够生成诊断报告；GradingResult、DiagnosisReport、ReviewRecord、AgentRun
和 WorkflowRun 的数据库模型及 Alembic 迁移均可独立检查。

- [ ] T046 [US3] 在 `tests/unit/grading/test_question_router.py` 中增加 Objective/Subjective 路由失败优先测试
- [ ] T047 [US3] 在 `backend/app/services/grading/question_router.py` 中实现 Question Router，使用题目类型而非模型输出来选择 Objective 或 Subjective
- [ ] T048 [US3] 在 `tests/unit/grading/test_objective_grader.py` 中增加确定性评分失败优先测试，覆盖正确、错误、缺失和重复答案
- [ ] T049 [US3] 在 `backend/app/services/grading/objective_grader.py` 中实现不调用 LLM 的 Objective 规则评分
- [ ] T050 [US3] 在 `backend/app/services/grading/grading_context.py` 中实现主观题答案 Query Construction 和检索上下文组装，包含题目、标准答案、评分标准、学生答案和 Final Context
- [ ] T051 [US3] 在 `tests/unit/grading/test_structured_grading.py` 中增加结构化输出和分数范围失败优先测试，覆盖 JSON 解析、Pydantic 校验、分数范围、置信度范围和无效响应
- [ ] T052 [US3] 在 `backend/app/services/grading/subjective_grader.py` 中使用 LLM Provider 和 RAG 上下文实现 Subjective Grading，只返回经过校验的 GradingResult DTO
- [ ] T053 [US3] 在 `backend/app/services/grading/confidence_policy.py` 中实现可配置的置信度阈值策略和 Pending Review 决策
- [ ] T054 [US3] 在 `backend/app/services/grading/result_aggregator.py` 中实现 Objective/Subjective 结果汇总和最终分数计算
- [ ] T055 [US2] 在 `backend/app/services/diagnosis_service.py` 中实现基于已接受或人工复核结果的 Diagnosis Report，包含掌握情况、薄弱知识点、错误原因和学习建议
- [ ] T056 [US3] 在 `backend/app/api/grading.py` 中提供阅卷触发、阅卷状态和单题结构化结果端点
- [ ] T057 [US2] 在 `backend/app/api/results.py` 和 `backend/app/ui/results_view.py` 中提供学生成绩、错题、诊断和知识点掌握情况视图
- [ ] T058 [US3] 在 `tests/integration/test_grading_pipeline.py` 中增加混合答卷集成测试，证明客观题确定性评分、主观题结构化评分、统一结果和诊断不会提前生成
- [ ] T059 [US3] 在 `scripts/run_grading_benchmark.py` 中实现可重复的 Zero-shot、RAG 和 Hybrid + Rerank 阅卷 Benchmark，记录数据集、模型、Prompt、指标、结果和分析
- [ ] T060 在 `backend/app/models/grading_result.py`、`backend/app/models/exam_result.py` 和 `migrations/versions/` 中创建 `GradingResult`、`ExamResult` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；分别关联 `Answer`/`Submission` 和 `Submission`/`Exam`，持久化单题得分、理由、知识点、置信度、校验/复核状态以及整份答卷总分、结果状态和汇总时间
- [ ] T061 在 `backend/app/models/diagnosis_report.py` 和 `migrations/versions/` 中创建 `DiagnosisReport` SQLAlchemy 模型及 Alembic 迁移，依赖 T060；关联 `ExamResult`/学生，持久化掌握情况、薄弱知识点、错误原因、学习建议和生成时间
- [ ] T062 在 `backend/app/models/review_record.py` 和 `migrations/versions/` 中创建 `ReviewRecord` SQLAlchemy 模型及 Alembic 迁移，依赖 T060；关联 `GradingResult`/教师，持久化复核前后分数、理由、知识点、操作类型和审查时间
- [ ] T063 在 `backend/app/models/agent_run.py` 和 `migrations/versions/` 中创建 `AgentRun` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；持久化 Agent 类型、Workflow ID、输入输出摘要、状态、耗时、模型、Prompt 版本、Token 统计和错误摘要
- [ ] T064 在 `backend/app/models/workflow_run.py` 和 `migrations/versions/` 中创建 `WorkflowRun` SQLAlchemy 模型及 Alembic 迁移，依赖 T015；持久化 `workflow_id`、Submission、当前节点/答案、状态、检查点、暂停原因、重试次数、恢复状态和时间戳

**M3 检查点**：核心阅卷无需 Agent 编排即可运行；结果边界结构化、具备置信度意识、
可审计；五类评分/诊断/复核/运行实体的模型和迁移就绪，并已准备好接入 LangGraph。

## 阶段 M4：Agent + Workflow

**目标**：将 M2/M3 能力编排为四类职责清晰的 Agent 和可循环、可中断、可恢复的
LangGraph 自动阅卷 Workflow，同时完成 AI 出题候选生成和教师审核。

**独立验证**：Teacher 输入课程、知识点、难度、题型和数量后得到候选题目，必须经过校验
和教师审核；一份混合答卷能够按 LangGraph 节点完成客观/主观分支，低置信度结果暂停到
Pending Review，Teacher 复核后 Workflow 恢复并生成最终诊断。

- [ ] T065 在 `backend/app/ai/agents/state.py` 和 `backend/app/ai/workflows/state.py` 中定义共用 Agent 输入/输出类型和 LangGraph 状态，包括 Workflow ID、当前答案、上下文 ID、评分结果、置信度、复核状态、最终结果、诊断和错误；依赖 T060-T064 的持久化实体边界
- [ ] T066 [P] [US1] 在 `backend/app/ai/agents/supervisor.py` 中实现 Supervisor Agent 的路由和工具选择决策
- [ ] T067 [US1] 在 `backend/app/ai/agents/question_agent.py` 中实现 Question Agent 的检索、候选生成 Prompt 和结构化候选输出
- [ ] T068 [US1] 在 `backend/app/services/question_validator.py` 中实现 Question Validator 和候选题目状态转换，保留 Candidate Generation 并阻止 Automatic Publishing
- [ ] T069 [US3] 在 `backend/app/ai/agents/grading_agent.py` 中围绕上下文检索、评分校验和置信度输出实现 Grading Agent 编排
- [ ] T070 [US3] 在 `backend/app/ai/agents/reviewer_agent.py` 中实现 Reviewer Agent 对分数、理由和知识点的检查，以及重新评分决策
- [ ] T071 [US3] 在 `tests/unit/workflows/test_grading_workflow.py` 中增加失败优先的 LangGraph 路由测试，覆盖 Load Submission、Classify Question、Objective Rule Grade、Subjective Retrieve/Grade、Structured Validation、Confidence Check、Accept 和 Pending Review
- [ ] T072 [US3] 在 `backend/app/ai/workflows/grading_workflow.py` 中实现 LangGraph 阅卷节点和条件边，包括逐题迭代、统一结果和 Generate Diagnosis
- [ ] T073 [US3] 在 `backend/app/services/workflow_checkpoint.py` 中实现基于 T064 `WorkflowRun` 模型的检查点、暂停原因、重试次数和可恢复状态持久化
- [ ] T074 [US3] 在 `backend/app/services/review_service.py` 中实现 Pending Review 恢复、Teacher 确认/修改、Reviewer Agent 重新评分和最终结果持久化
- [ ] T075 [P] [US1] 在 `backend/app/api/question_generation.py` 和 `backend/app/ui/question_generation_view.py` 中提供 AI 出题请求和候选题目审核操作
- [ ] T076 [US3] 在 `backend/app/api/workflow.py` 中提供 LangGraph 阅卷启动、状态查询和恢复操作
- [ ] T077 [US3] 在 `backend/app/api/reviews.py` 和 `backend/app/ui/review_view.py` 中提供低置信度评分详情以及 Teacher 确认/修改操作
- [ ] T078 [P] [US1] 在 `tests/integration/test_question_generation_workflow.py` 中增加 Question Agent 契约和教师审核集成覆盖
- [ ] T079 [US3] 在 `tests/integration/test_langgraph_grading_workflow.py` 中增加完整自动阅卷 Workflow 集成覆盖，包括客观/主观路由、结构化校验失败、低置信度暂停、Teacher 恢复、重新评分和诊断

**M4 检查点**：完整 AI 路径具备状态管理和追踪能力：Question Agent 生成可审核候选题，
Grading Workflow 支持条件路由以及 Human-in-the-loop 暂停与恢复。

## 阶段 M5：工程增强与演示交付

**目标**：增加 MCP、邮件工具、审计日志、Agent Trace、评测看板、Docker Demo 和项目
文档，将 M0-M4 能力整理为可展示、可回归的交付版本。

**独立验证**：MCP 工具能够访问已授权的课程、考试和学生数据；关键业务操作具有审计和
Agent Trace；评测看板展示检索和阅卷实验；quickstart 可以从启动到完整演示闭环；全量
回归通过。

- [ ] T080 在 `backend/app/mcp/server.py` 和 `backend/app/mcp/registry.py` 中创建 MCP 服务/工具注册边界，加入授权和结构化工具结果处理
- [ ] T081 [P] 在 `backend/app/mcp/tools/knowledge_tools.py` 中实现 `search_questions` 和 `query_knowledge` MCP 工具
- [ ] T082 [P] 在 `backend/app/mcp/tools/report_tools.py` 中实现 `get_exam_result`、`get_student_profile` 和 `create_report` MCP 工具
- [ ] T083 [P] 在 `backend/app/mcp/tools/email_tool.py` 中实现 `send_email` MCP/工具适配器，明确收件人、模板和失败处理
- [ ] T084 在 `backend/app/models/audit_log.py`、`migrations/versions/` 和 `backend/app/services/audit_service.py` 中实现审计事件模型、持久化、脱敏和 180 天保留策略，以及阅卷、复核、发布和角色变更的审计钩子；敏感操作详情不得包含 API Key、凭证、答案正文或隐私字段
- [ ] T085 在 `backend/app/services/trace_service.py` 和 `backend/app/api/traces.py` 中基于 T063/T064 的 `AgentRun`/`WorkflowRun` 模型实现追踪采集和追踪视图序列化，覆盖 `request_id`、`user_id`、`workflow_id`、`model`、`latency`、`prompt_version`、`tokens`、`status`、`error` 字段；排除密钥、Authorization Header、完整 Prompt 和学生答案原文，并实现 Trace 默认 30 天保留
- [ ] T086 在 `backend/app/ui/evaluation_dashboard.py` 和 `backend/app/services/evaluation_service.py` 中实现检索与阅卷 Benchmark 对比评测看板
- [ ] T087 在 `scripts/demo_seed.py`、`scripts/run_demo.ps1` 和 `docker-compose.yml` 中实现 Docker Demo 配置、种子数据命令和一键演示入口
- [ ] T088 更新 `README.md`、`docs/architecture.md`、`docs/development.md` 和 `docs/evaluation.md`，说明三容器决策、Provider 边界、Agent 职责、Workflow 图以及 MVP/非 MVP 范围
- [ ] T089 按 `.specify/quickstart.md` 在 `docs/validation-report.md` 中记录端到端验证，包括启动、四种检索模式、候选审核、混合阅卷、人工复核、诊断、资源使用和 P95 响应阈值
- [ ] T090 [P] 在 `docs/observability.md` 中记录 Prometheus、Grafana 和 OpenTelemetry 的可选扩展点，不将其加入 MVP 必需容器
- [ ] T091 执行 `tests/` 中的全部单元、契约、集成、Benchmark、角色边界、Docker、资源上限和 P95 检查，并在 `docs/release-checklist.md` 中记录 `run_at`、数据集/模型/Prompt 版本、配置、环境资源、指标、状态、结果路径及失败时的脱敏诊断

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
- [X] T093 [P] [US1] 在 `backend/app/ui/knowledge_base_view.py` 中实现课程、知识库、资料上传和处理状态工作区，展示 PDF/TXT/Markdown 支持范围、来源追溯、Ready/Failed 状态和可理解的失败提示；仅适配既有课程/知识库契约，不修改 Service 或 API per US1/AC1、FR-008~FR-014、T039 (missing)
- [X] T094 [P] [US1] 重构 `backend/app/ui/question_view.py` 和 `backend/app/ui/exam_view.py`，将手填题目/考试 ID、原始 JSON 和 ISO 时间输入改为课程上下文、表格选中、审核状态、已审核题目筛选、草稿组卷和发布前校验的连续教师流程 per US1/AC2~AC4、FR-018、FR-020、FR-028、T025、T027 (partial)
- [X] T095 [P] [US2] 在 `backend/app/ui/results_view.py` 中实现学生成绩结果工作区，展示总分、单题得分、错题、AI 诊断、薄弱知识点、学习建议和知识点掌握情况，并明确标注 Pending Review 结果不得视为最终结论 per US2/AC3~AC4、FR-038、FR-040、T057 (missing)
- [X] T096 [P] [US3] 在 `backend/app/ui/review_view.py` 中实现教师低置信度阅卷复核工作台，展示复核队列、题目、学生答案、参考答案、评分标准、AI 分数、理由、知识点和置信度，并支持确认或修改后的状态反馈 per US3/AC4~AC5、FR-036、FR-037、T077 (missing)
- [X] T097 [P] [US2] 重构 `backend/app/ui/student_exam_view.py`，按题型提供可理解的答题控件和题目导航，增加考试进度、草稿保存、提交确认、提交后只读状态及重复提交提示，移除学生直接编辑题目 ID 到 JSON 的主要流程 per US2/AC1~AC2、FR-021、FR-022、SC-003、T029 (partial)
- [X] T098 [P] [US1] 在 `backend/app/ui/question_generation_view.py` 中实现 AI 出题条件填写、检索上下文不足提示、候选题列表、结构化字段预览和教师审核/退回修订操作；候选题必须显式保持 Candidate Generation 或 Pending Review 状态 per FR-024~FR-028、T075 (missing)
- [X] T099 [US3] 扩展 `backend/app/ui/results_view.py` 的教师结果工作区，支持按课程、考试和学生查看成绩、AI 阅卷详情、低置信度项、知识盲点和学情分析，并与学生结果视图共享中文状态映射 per FR-039、plan §5.2、plan §6 (missing)
- [ ] T100 [P] 在 `backend/app/ui/evaluation_dashboard.py` 中实现检索与阅卷 Benchmark 对比看板，按实验、模型、Prompt、数据集、指标、运行状态和结果路径展示可追溯信息，并区分失败、运行中和完成状态 per plan §7、T086 (missing)
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
- [X] T105 [HIGH] [US1] 在 `backend/app/ui/knowledge_base_view.py` 细化 T093：课程管理采用顶部搜索/新建工具行、左侧约 65% 课程表格（名称、简介、知识库数、更新时间）、右侧约 35% 选中课程详情与编辑表单；知识库导航复用已选课程，顶部课程与知识库下拉框，中部资料上传行及资料表格（文件名、格式、处理阶段、更新时间），下部折叠来源详情与失败原因；以真实 Uploaded/Parsing/Chunking/Embedding/Ready/Failed 显示阶段，不以文件上传成功代表知识库就绪；依赖 T103-T104、T022-T023，文件摄取和片段追溯依赖 T038-T039，未就绪时禁用对应操作 per FR-008~FR-014、US1/AC1、T093（参考 P4）(missing)
- [X] T106 [HIGH] [US1] 在 `backend/app/ui/question_view.py` 细化 T094 的题库布局：上方课程/题型/知识点/审核状态筛选与新建入口，下方约 60% 题目摘要表格和 40% 选中题详情；表格列为题干摘要、题型、分值、知识点、审核状态，详情依次为完整题干、逐项选项、参考答案、评分标准；选行自动绑定内部 ID，编辑选项使用文本行与正确答案选择控件，底部排列“保存 / 审核通过 / 退回修订”，删除收进独立确认区；依赖 T103-T105，复用 T024-T025，不要求用户编辑 JSON 或原始枚举 per FR-018~FR-020、FR-028、T094（参考 P4、P6）(partial)
- [X] T107 [HIGH] [US1] 在 `backend/app/ui/exam_view.py` 细化 T094 的考试布局：默认显示顶部课程/状态筛选和考试表格（名称、课程、开放时间、时长、题数、总分、状态）；选择草稿进入“基本信息 / 选择题目 / 发布检查”三个步骤，组卷区左侧约 60% 已审核题目表、右侧约 40% 已选题清单和题数/总分摘要，日期与时分使用可理解的控件并标注时区；发布前展示考试名称、开放时间、已审核题目数和确认区，未满足条件禁用发布；依赖 T103-T106，复用 T026-T027，保持服务对题目归属和审核状态的最终校验 per FR-020、US1/AC3~AC4、T094（参考 P5）(partial)
- [X] T108 [HIGH] [US2] 在 `backend/app/ui/student_exam_view.py` 细化 T097：入口为可参加考试表格与“开始或继续”，答题时采用精简顶部栏（考试名、保存状态、可用的截止时间、交卷）、左侧 200px 答题卡和右侧题目区；题号格不小于 44px，显示已答/未答数量与标记状态；单选和判断使用单选控件，简答使用至少 8 行且最小高 240px 的纯文本框，底部为上一题、标记、下一题；选题只切换当前题，草稿按当前答卷保存；交卷先显示内联确认区及未答题号，有未答时只能返回补答，成功后只读；依赖 T103-T104、T028-T029，遵守现有全部题目必答规则，计时只使用可靠截止信息，不新增自动交卷或个人计时规则 per FR-021~FR-023、US2/AC1~AC2、T097（参考 P3/P5 的入口；答题卡为 EduAgent 专属设计）(partial)
- [X] T109 [HIGH] [US1] 在 `backend/app/ui/question_generation_view.py` 细化 T098：上方显示当前课程，左侧约 30% 固定条件表单（课程、知识点、难度、题型、数量）和生成状态，右侧约 70% 候选题列表与选中题完整预览；检索来源使用下方折叠区域，候选题标题旁显示“待教师审核”，预览底部提供审核通过/退回修订及修订意见输入；检索不足或结构校验失败显示横幅并禁用审核通过；依赖 T103-T106、T067-T068、T075 的既有契约，不实现检索、生成或校验业务 per FR-024~FR-028、US1/AC2~AC3、T098（参考 P2 的任务分区，内容由 EduAgent 规格定义）(missing)
- [X] T110 [HIGH] [US3] 在 `backend/app/ui/review_view.py` 细化 T096：顶部考试/学生/复核状态筛选，下方左侧 240px 复核队列，右侧详情再按约 45% 学生答案与 55% AI 评分并排；详情顶行显示学生、考试、题号、实际置信度和“待人工复核”横幅，答案区保留原文换行，评分区显示分数、理由、知识点、参考答案、评分标准；检索依据为可展开的编号引用列表（来源文件、页码或段落、片段正文），修改分数与理由在原位置内联编辑，底部固定确认评分/保存修改/下一条；待复核保持原服务状态，不伪造新的标记接口；依赖 T103-T104、T050、T060-T062、T074、T077，结果刷新以保存成功返回为准 per FR-031、FR-034~FR-037、US3/AC4~AC5、T096（参考 P2 待批阅队列、P6 状态；证据复核为 EduAgent 专属设计）(missing)
- [X] T111 [HIGH] [US2] 在 `backend/app/ui/results_view.py` 细化 T095 的学生结果布局：顶部考试选择与结果状态横幅，其下总分、已评分题数、待复核题数三个紧凑摘要；主区分“逐题结果 / 错题与诊断”，逐题结果为摘要表格与选中题反馈，诊断区左侧薄弱知识点及真实掌握度图表、右侧错误原因和学习建议；待复核状态只显示已确认部分并说明最终成绩尚未形成，未生成诊断时显示明确空态；依赖 T103-T104、T054-T057、T060-T061，只读取当前学生授权结果，不把前端汇总作为最终成绩 per FR-033~FR-038、FR-040、US2/AC3~AC4、T095（参考 P3、P6）(missing)
- [X] T112 [HIGH] [US3] 在 `backend/app/ui/results_view.py` 细化 T099 的教师结果布局：顶部课程/考试筛选，首行已提交数、最终成绩数、待复核数及仅基于最终结果的平均分摘要；中部左侧约 65% 学生成绩表（学生、总分、结果状态、待复核数）和右侧约 35% 所选学生诊断摘要，下部知识点分布图及同数据表；每条待复核结果提供携带考试/答卷/题目上下文进入 T110 的入口；依赖 T103-T104、T110-T111、T054-T057 和对应教师查询契约，权限或数据缺失显示不可用态，不绕过服务查库聚合评分 per FR-039、plan §5.2、T099（参考 P2、P6）(missing)
- [X] T113 [MEDIUM] [US4] 在 `backend/app/ui/admin_view.py` 细化 T101：系统概览首行用户数、启用用户数、角色数，下一行数据库/缓存健康状态及检查时间；用户管理页为上方姓名/角色/启用状态筛选、左侧约 65% 用户表和右侧约 35% 用户编辑表单，角色管理单独显示角色说明表及选中用户角色勾选，运行状态单独显示依赖健康列表；创建为独立表单模式，停用/删除前内联显示目标用户和确认按钮；依赖 T103-T104、T020-T021，管理员菜单不提供 AI 审核/复核或学生答题入口 per FR-005~FR-006、US4/AC1~AC3、T101（参考 P1、P4）(partial)
- [X] T114 [MEDIUM] [US1] 在 `backend/app/ui/gradio_app.py` 增加教师概览的具体布局：标题行显示当前课程筛选，首行最多四个摘要（课程数、已发布考试、待审核题、待复核评分）；中部左侧约 2/3 使用紧凑课程卡片展示名称、简介、知识库数和进入课程入口，右侧约 1/3 为待办列表（类别、所属课程/考试、状态、处理入口）；下部为最近考试表与最终成绩概览；依赖 T103-T107、T109-T110、T112，统计限于已授权完整数据集，缺失数据为“暂不可用”而非零，不新增考勤、排课或预测分数 per US1、FR-039、T092、本次教师仪表板要求（参考 P2）(missing)
- [X] T115 [MEDIUM] [US2] 在 `backend/app/ui/gradio_app.py` 增加学生概览：左侧沿用“学习概览 / 我的考试 / 成绩与诊断”，主区首行可参加考试数、已提交数、可查看结果数，下方左侧约 2/3 为当前可参加考试表、右侧约 1/3 为最近结果列表和已确认诊断摘要；每场考试显示名称、课程、开放时间、时长及开始/继续按钮，每项结果带中文最终/待复核状态并可进入 T111；依赖 T103-T104、T108、T111，数据只属于当前学生，不引入缴费、家长通讯或排行榜 per FR-021~FR-023、FR-040、T092、本次学生仪表板要求（参考 P3）(missing)
- [X] T116 [MEDIUM] 在 `backend/app/ui/gradio_app.py` 和 `backend/app/ui/layout_view.py` 补齐顶部面包屑、搜索、消息与待办、快捷操作：顶部为品牌、当前页搜索框、消息数量和用户菜单，内容标题行显示“工作台 / 课程 / 当前页面”及最多两个当前页主操作；消息按钮展开内容区顶部的通知面板（类型、关联对象、状态、时间、查看入口），仅汇总当前用户可见的已加载待办与本次会话操作反馈，不建立消息推送/持久化已读服务；教师快捷入口为创建课程/创建考试，学生为继续作答/查看结果，管理员为创建用户/刷新状态；依赖 T103-T115 的相关页面，返回导航恢复原筛选与所选对象，未有可操作数据时不显示假计数或无效链接 per T092、SC-002/SC-003 的入口可达性、本次顶部布局要求（参考 P1-P3、P4-P6）(missing)
- [ ] T117 [MEDIUM] 在 `backend/app/ui/evaluation_dashboard.py` 细化 T100：上方实验/数据集/模型/检索模式筛选，下方指标对比表和对应柱状图并排，底部选中实验的配置、模型/提示词/数据集版本、运行状态和结果位置折叠详情；图表必须带指标单位、样本量和同条件比较说明，失败项单独列出、缺失指标显示“无数据”；依赖 T103-T104、T086 的评测结果及读取授权契约，未明确读取授权时隐藏入口，不因角色为管理员就授予 AI 业务权限，不新增评测计算/文件读取越权入口 per T086、T100、plan §7、Constitution V（参考 P1 的指标层级，评测指标由项目计划定义）(missing)
- [ ] T118 [LOW] 对 `backend/app/ui/` 的上述布局完成显示与交互验收：在 1440x900、1024x768、390x844 下检查桌面侧栏、平板收窄、手机折叠菜单；多列详情在窄屏依次重排，答题卡移至题目上方，长题干/文件名换行且表格仅在自身区域横向滚动；确认按钮不小于 44px、题号格尺寸稳定、焦点可见，通知/确认区关闭后回到原操作；运行现有 UI 检查及视图导入/组件构建检查，抽查三角色进入、返回、退出与待复核非最终状态，记录真实截图与未就绪依赖；依赖所有本轮已实施任务，保留业务未就绪任务未勾选，不修改 UI 以外文件 per T102、FR-004、US2/AC4、plan §6、本次响应式布局要求（参考 P1-P6 的页面分区）(partial)

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
- [ ] T122 在 `backend/app/ui/layout_view.py` 的共享工作台顶部增加开发模式视觉提示“当前为开发模式，请勿用于生产环境”，仅在 `DEV_MODE=True` 渲染；提示不得改变导航授权或后端请求校验 per FR-004、FR-006、plan §0、§6。
- [ ] T123 在 `.env.example` 中增加 `DEV_MODE=false` 及中文说明注释，说明仅用于本地演示、开启后会创建预设测试账号、上线前必须关闭并清理账号 per FR-002、FR-007。
- [ ] T124 编写 `docs/dev-mode.md`，记录开关、预设账号生命周期、快速登录行为、后端安全边界、测试/清理步骤，并链接 `docs/dev-mode-removal-checklist.md` per FR-001~FR-007、plan §0。

