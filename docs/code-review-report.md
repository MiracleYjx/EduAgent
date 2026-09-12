# EduAgent 代码审查报告

审查日期：2026-09-12
审查依据：

1. `.specify/constitution.md`：仓库中不存在该路径；实际采用 `.specify/memory/constitution.md` 作为项目宪章。
2. `.specify/spec.md`
3. `.specify/plan.md`
4. `.specify/tasks.md`
5. `.specify/data-model.md`
6. `.specify/research.md`
7. `.specify/contracts/embedding-provider.md`
8. `.specify/contracts/rag-retrieval.md`
9. `.specify/contracts/agent-workflow.md`
10. `.specify/quickstart.md`
11. `.specify/checklist.md`
12. `docs/ui-layout-design.md`
13. `docs/dev-mode-removal-checklist.md`

审查范围：backend/app 下 67 个 Python 文件；tests 下 33 个 Python 测试文件；scripts 下 4 个文件；docs 下 3 个文件；docker-compose.yml、pyproject.toml、.env.example、alembic.ini、Dockerfile；另检查 migrations 下 4 个迁移源文件及 benchmark/results 下现有结果文件。
审查结论：❌ 存在风险

## 一、总体评价

当前代码已经形成可运行的 M0/M1 基础：三容器 Compose、JWT/RBAC、课程/知识库元数据、人工题库、考试组卷和学生答卷链路均有实现，现有 103 个测试通过。LLM Provider 和结构化 DTO 的基础抽象存在，但尚未接入任何业务 AI 流程；M2 RAG、M3 AI 阅卷、M4 Agent/LangGraph 和大部分 M5 评测能力仍未实现。已发现的关键风险包括 Backend healthcheck 只检查进程存活、学生考试查询没有目标学生范围、JWT 密钥没有进入类型化启动配置，以及任务清单中把多个 partial/missing UI 任务勾选为完成。依照当前证据，代码适合继续作为 M1 基线推进，但不适合宣称已经达到 M2 RAG 里程碑或完整 MVP 验收。

## 二、问题清单（按严重度分级）

### 高优先级（影响功能正确性、安全性或与设计文档严重偏离）

| 编号 | 问题 | 文件位置 | 偏离的设计文档 | 影响 | 建议修复方式 |
| :-- | :-- | :--- | :-------------------------- | :-- | :----- |
| H01 | RAG 摄取、DocumentChunk、Embedding、四种检索模式和 Rerank 均没有实现；对应 AI 包只有空的初始化文件，Document 也只有元数据。 | `backend/app/ai/embedding/__init__.py:1`、`backend/app/ai/retrieval/__init__.py:1`、`backend/app/models/document.py:20-45` | spec.md FR-010~FR-014；plan.md §2、§3；contracts/embedding-provider.md、rag-retrieval.md；tasks.md T031-T045 | 课程资料无法形成可检索上下文，US1 主链路和 M2 验收门槛无法运行。 | 按 T031-T045 实现解析、清洗、分块、Embedding Provider、DocumentChunk 迁移、四种检索模式和 Benchmark，并保留来源 ID。 |
| H02 | AI 阅卷、评分持久化、低置信度复核、诊断报告和 LangGraph Workflow 均缺失；现有 GradingResult 只是 DTO。 | `backend/app/schemas/ai.py:93-155`、`backend/app/ai/agents/__init__.py:1`、`backend/app/ai/workflows/__init__.py:1`、`backend/app/models/__init__.py:16-18` | spec.md FR-029~FR-040、SC-004~SC-008；plan.md §5；data-model.md §2、§4；contracts/agent-workflow.md；tasks.md T046-T079 | 提交后的答卷不会产生真实评分、统一成绩、复核结果或诊断，US2/US3 及 MVP 闭环中断。 | 按 T046-T079 先实现 Question Router 和客观规则评分，再实现主观 RAG/结构化评分、结果模型、复核 API、LangGraph 节点和恢复路径。 |
| H03 | Compose 将 /health 作为 Backend healthcheck，但该端点明确与数据库和 Redis 无关；依赖不可用时 Backend 仍可能被标记为 healthy。 | `backend/app/core/app.py:86-90`、`docker-compose.yml:63-74` | constitution.md I；plan.md §0 M0、Validation Gates 1；tasks.md T005、T012 | 启动编排可能在应用依赖未就绪时放行业务请求，违背“就绪检查”定义，故障会延迟到业务请求阶段。 | 增加同时检查配置、PostgreSQL 和 Redis 的 readiness 端点，Compose healthcheck 指向 readiness；保留独立 liveness 端点。 |
| H04 | “可参加考试”只按 Published 和时间窗过滤，student_id 仅验证学生账号存在；Exam 没有目标学生/范围关系。 | `backend/app/services/submission_service.py:368-403`、`411-429`；`backend/app/models/exam.py:28-44` | spec.md FR-021 及 Assumptions 205；plan.md §5.1；data-model.md 67 | 任意 Student 可能看到并开始所有已发布考试，无法实现“对目标学生开放”的业务边界。 | 增加明确的考试参与范围/分配关系或等价的领域规则，在列表、详情、开始答卷三个入口统一校验，并补充越权契约测试。 |
| H05 | JWT_SECRET_KEY 未作为 AppSettings 字段或必填启动校验项；配置模型设置 extra=ignore，security.py 另行读取原始环境变量，.env.example 也没有该配置。 | `backend/app/core/config.py:90-120`、`53-66`；`.env.example:1-15` | plan.md §0 配置与密钥；constitution.md III、项目约束；docs/dev-mode-removal-checklist.md 25-35 | 部署遗漏密钥时应用可以启动，但所有受保护 API 延迟返回 503；认证配置不具备统一类型、校验和可观测性。 | 将 JWT 密钥纳入 SecretStr 配置并在启动时校验长度/非空，补充 .env.example 占位项；security.py 只依赖已校验的 settings。 |

### 中优先级（影响可维护性或一致性）

| 编号 | 问题 | 文件位置 | 偏离的设计文档 | 影响 | 建议修复方式 |
| :-- | :-- | :--- | :------ | :-- | :----- |
| M01 | “上传资料”API 实际只登记文件名、格式和可选 storage_path，不接收文件内容，也不触发 Parser/Cleaning/Chunking/Embedding。 | `backend/app/api/knowledge_bases.py:107-126`、`367-388`；`backend/app/services/knowledge_base_service.py:475-532` | spec.md FR-010、FR-012；plan.md §2；tasks.md T022 与 T031-T039 | M1 的 Document 元数据能力容易被误认为资料摄取已完成；知识库永远停留在 Uploaded。 | 在 M2 接通 multipart 文件输入和 ingestion service；在 API/UI 中明确区分“登记元数据”和“可检索就绪”。 |
| M02 | DeepSeek Provider 和 Pydantic DTO 已实现，但没有 Question Agent、Grading Service 或其他业务消费者；Embedding/Rerank Provider 也没有实现。 | `backend/app/ai/llm/deepseek.py:53-133`、`backend/app/schemas/ai.py:29-155` | constitution.md III、IV；plan.md Provider 抽象和 §5；contracts/embedding-provider.md | Provider 替换和结构化输出只在孤立单元测试中成立，业务替换和输出门禁尚未被证明。 | 以应用服务为唯一业务入口接入 Provider，补齐 Embedding/Rerank 抽象，并让所有 AI 结果经 JSON/Pydantic/DTO 后再持久化。 |
| M03 | Benchmark 只有合成种子生成器，没有 Vector/Keyword/Hybrid/Rerank 或 Zero-shot/RAG/Hybrid 阅卷执行器；生成结果还包含 student_answer 原文。 | `scripts/generate_synthetic_benchmark.py:45-60`、`299-352`；`benchmark/results/synthetic_benchmark.json:1-160` | constitution.md V；plan.md §2.1、§7.1、§7.2；tasks.md T011、T045、T059 | 当前结果不能证明任何 AI 效果；同时与 plan.md 要求 Benchmark JSON/CSV 不含学生答案原文相冲突。 | 将合成输入种子与规范实验结果分离；结果只保留脱敏统计和摘要，补充 run_at、config、environment、metrics、status、result_path 和失败诊断。 |
| M04 | Phase 6/7 的 T092-T118 被标为 [X]，但自身标注 partial/missing；UI 设计文档又明确要求这些任务保持未勾选并说明本轮未执行实现。 | `.specify/tasks.md:363-407`；`docs/ui-layout-design.md:3-7`、`417-430` | tasks.md 完成定义 350-354；docs/ui-layout-design.md Phase 7 | 任务状态不能可靠反映交付状态，容易导致评审、里程碑和自动化验收误判。 | 保持设计文档语义一致，逐项以可运行证据更新勾选状态；布局预览、空态和业务闭环分开验收。 |
| M05 | 测试覆盖集中在 M0/M1、认证和 Provider；M2-M5 的 parser、embedding、retrieval、grading、workflow、review 和 result contract/integration 测试不存在。 | `tests/contract/:1`、`tests/unit/grading/__init__.py:1`、`tests/unit/workflows/__init__.py:1`、`tests/unit/models/test_models.py:35-52` | plan.md Validation Gates；tasks.md T031、T036、T040、T046-T079、T086-T091 | 关键状态机、来源追踪、低置信度恢复和诊断边界没有自动回归保护。 | 按里程碑补齐失败优先契约测试和混合答卷集成测试，不以 UI 空态测试代替业务测试。 |
| M06 | Black 检查失败，两个文件需要重排；运行环境还提示当前 Python 3.13 无法解析目标 Python 3.15 的格式检查。 | `backend/app/services/admin_service.py:145`、`tests/integration/test_teacher_setup.py:111-113` | tasks.md 完成定义；pyproject.toml 9、28-34 | CI/本地格式结果不一致，代码审查不能达到格式门槛。 | 统一项目支持的 Python/Black target-version，修复这两个文件并在 pyproject.toml 固化工具配置。 |
| M07 | 多个 UI 视图分别复制 _empty_state、_format_error、角色检查和时间格式化逻辑。 | `backend/app/ui/admin_view.py:51-83`、`exam_view.py:70-99`、`knowledge_base_view.py:57-95`、`question_view.py:68-87`、`review_view.py:44-73`、`student_exam_view.py:102-131` | plan.md §6；constitution.md 最小完整实现原则 | 状态文案、异常映射和权限提示可能逐步漂移，UI 维护成本上升。 | 抽取共享 UI 状态/错误/会话守卫工具，保留各页面特有的业务错误映射。 |
| M08 | dev 依赖只声明 black、httpx、pytest，没有声明本次审查所需的 mypy 和 Ruff，也没有统一配置。 | `pyproject.toml:28-41` | tasks.md T002、完成定义；constitution.md 合规门槛 | 新环境无法按项目声明复现完整静态检查。 | 将 mypy、ruff 和对应配置加入开发依赖/项目配置，固定 Python target 和检查命令。 |
| M09 | Alembic check 在当前本地配置下失败于 ModuleNotFoundError: psycopg2；pyproject.toml 声明的是 psycopg v3。 | `alembic.ini:1-5`、`migrations/env.py:24-59`、`backend/app/core/database.py:41-47` | plan.md §0、tasks.md T006/T012；data-model.md §4 | 本地迁移一致性无法独立验证；Compose 中的 psycopg URL 覆盖了这一差异，因此存在环境依赖不一致。 | 统一本地和 Compose 使用 postgresql+psycopg URL，并让迁移检查在无 Docker 的开发环境可重复执行。 |

### 低优先级（代码风格或优化建议）

| 编号 | 问题 | 文件位置 | 偏离的设计文档 | 影响 | 建议修复方式 |
| :-- | :-- | :--- | :------ | :-- | :----- |
| L01 | PostgreSQL 默认使用 POSTGRES_HOST_AUTH_METHOD=trust；端口虽绑定 127.0.0.1，但默认认证不适合直接复用到共享环境。 | `docker-compose.yml:7-12` | constitution.md I；plan.md §0 | 本地开发方便，但误将 Compose 配置用于共享主机时会降低数据库边界。 | 保持本地易用性的同时在示例配置中明确仅限本机，非本地环境要求密码认证。 |
| L02 | register_models() 是空函数，模型依靠模块副作用导入；随着 M2-M4 模型增加，注册责任不清晰。 | `backend/app/models/__init__.py:16-18` | plan.md Project Structure；data-model.md §1-2 | 迁移和测试可能漏导入新模型，问题直到运行时才暴露。 | 让注册函数显式导入并校验所有模型，或删除无效入口并在 migrations/env.py 固定导入路径。 |
| L03 | 测试产生 Starlette/httpx 兼容性弃用警告，考试 API 使用已弃用的 HTTP_422_UNPROCESSABLE_ENTITY。 | `backend/app/api/exams.py:260`；依赖见 `pyproject.toml:12、31` | tasks.md 完成定义；plan.md 工程质量要求 | 当前不影响通过结果，但会增加后续依赖升级噪声。 | 按当前 FastAPI/Starlette 版本替换常量并统一 httpx 兼容版本。 |
| L04 | SubmissionService 保留多个同义方法别名，API 也保留重复路径别名；兼容层边界未记录。 | `backend/app/services/submission_service.py:405-409`；`backend/app/api/submissions.py:398-420` | plan.md §5.1；最小复杂度原则 | 公开入口增多，调用方难以判断规范 API。 | 在文档中标记兼容期并设置单一规范入口，待调用方迁移后删除别名。 |
| L05 | Black 在 Python 3.13 下给出“目标代码为 Python 3.15”的安全解析警告，工具链目标版本未显式固定。 | `pyproject.toml:9-10` | constitution.md 可运行优先；tasks.md 完成定义 | 不同开发机可能得到不同格式检查结果。 | 在项目配置中显式声明 Black target-version 与 CI Python 版本。 |

## 三、与设计文档的符合度

### 3.1 与 spec.md 的符合度

状态含义：✅ 已有可验证实现；⚠️ 仅部分实现、依赖缺失或验收证据不足；❌ 当前代码缺失。

| 功能需求 | 实现状态 | 代码位置 | 备注 |
| :----- | :----- | :--- | :-- |
| FR-001 | ✅ | `backend/app/services/admin_service.py:100-181`；`backend/app/services/auth_service.py:141-171` | 管理员初始化和 DEV_MODE 账号初始化存在，没有自助注册，但满足“注册或初始化”。 |
| FR-002 | ✅ | `backend/app/api/auth.py:1-187`；`backend/app/services/auth_service.py:328-504` | JWT 登录、当前用户和失效令牌处理已实现。 |
| FR-003 | ✅ | `backend/app/domain/enums.py:8-14` | Teacher、Student、Admin 三角色存在。 |
| FR-004 | ✅ | `backend/app/core/security.py:119-310`；`backend/app/domain/permissions.py:1-233` | API 依赖和服务所有权检查均存在；UI 仍由后端重新校验 token。 |
| FR-005 | ✅ | `backend/app/services/admin_service.py:311-363`；`backend/app/api/admin.py:1-363` | 用户、角色、运行状态管理已实现。 |
| FR-006 | ✅ | `backend/app/domain/permissions.py:1-233`；`backend/app/ui/gradio_app.py:2527-2540` | Admin 菜单和权限不包含教师 AI 审核入口；AI 业务本身尚未实现。 |
| FR-007 | ✅ | `backend/app/api/auth.py:1-187` | 未发现 OAuth、SSO、多组织租户或复杂权限表达式。 |
| FR-008 | ✅ | `backend/app/services/course_service.py:1-340`；`backend/app/api/courses.py:1-288` | 教师课程 CRUD 已实现。 |
| FR-009 | ✅ | `backend/app/services/knowledge_base_service.py:250-406`；`backend/app/api/knowledge_bases.py:250-344` | 知识库元数据和课程绑定已实现。 |
| FR-010 | ⚠️ | `backend/app/api/knowledge_bases.py:107-126`、`367-388` | 只登记文件元数据，没有 PDF/TXT/Markdown 内容上传和解析。 |
| FR-011 | ✅ | `backend/app/services/knowledge_base_service.py:25-36` | 支持格式集合仅含 pdf/txt/md，未把 DOCX 作为主流程。 |
| FR-012 | ❌ | `backend/app/services/knowledge_base_service.py:475-532` | 只有 Uploaded 元数据登记，没有 Parser、Cleaning、Chunking、Embedding。 |
| FR-013 | ❌ | `backend/app/models/document.py:20-45`；缺少 `backend/app/models/document_chunk.py` | DocumentChunk 的字段和索引不存在。 |
| FR-014 | ⚠️ | `backend/app/models/document.py:25-52` | Course/KnowledgeBase/Document 关系可追溯，但没有片段来源链。 |
| FR-015 | ✅ | `backend/app/domain/enums.py:16-24` | 六种题型标识齐全。 |
| FR-016 | ⚠️ | `backend/app/services/question_service.py:290-327` | 人工题目支持优先题型，但没有对应评分器。 |
| FR-017 | ✅ | `backend/app/domain/enums.py:27-46` | 后续题型已定义，未阻塞 M1 人工组卷。 |
| FR-018 | ✅ | `backend/app/services/question_service.py:290-502`；`backend/app/api/questions.py:1-427` | 题库 CRUD 和状态转换存在。 |
| FR-019 | ✅ | `backend/app/models/question.py:22-58` | 题目要求字段已落到模型。 |
| FR-020 | ✅ | `backend/app/services/exam_service.py:506-524`、`590-662` | 只有 Approved 题目才能组卷和发布。 |
| FR-021 | ⚠️ | `backend/app/services/submission_service.py:368-429` | 有时间窗过滤，但没有目标学生范围，存在 H04。 |
| FR-022 | ✅ | `backend/app/api/submissions.py:398-735` | 学生答题、保存答案、提交答卷已实现。 |
| FR-023 | ⚠️ | `backend/app/models/submission.py:21-47`；`backend/app/models/answer.py:19-45` | 提交和答案已保存，评分结果实体不存在。 |
| FR-024 | ❌ | `backend/app/ui/question_generation_view.py:18-20`、`132-163` | UI 明确标注 AI 出题未就绪，没有可用 API/service。 |
| FR-025 | ❌ | `backend/app/ai/agents/__init__.py:1`；`backend/app/ai/retrieval/__init__.py:1` | Query Construction、检索、Agent、Validator、审核链路未实现。 |
| FR-026 | ⚠️ | `backend/app/schemas/ai.py:29-61` | DTO 有 Candidate Generation 状态，但没有实际生成器。 |
| FR-027 | ✅ | `backend/app/services/exam_service.py:614-622`、`650-662` | 人工题目发布前门禁正确；AI 候选路径尚不存在。 |
| FR-028 | ⚠️ | `backend/app/ui/review_view.py:142-165`；`backend/app/ui/question_generation_view.py:98-103` | 有空态和占位回执，没有候选题审核业务。 |
| FR-029 | ❌ | 缺少 `backend/app/services/grading/question_router.py`；相关目录 `tests/unit/grading/__init__.py:1` | 没有题型路由实现。 |
| FR-030 | ❌ | 缺少 `backend/app/services/grading/objective_grader.py` | 没有客观题确定性评分器。 |
| FR-031 | ❌ | 缺少 `backend/app/services/grading/grading_context.py` | 没有完整主观题评分输入组装。 |
| FR-032 | ❌ | `backend/app/ai/retrieval/__init__.py:1` | 没有 Hybrid Retrieval、Rerank 或 Grading LLM 链路。 |
| FR-033 | ❌ | 缺少 `backend/app/services/grading/result_aggregator.py` | 没有统一考试结果。 |
| FR-034 | ⚠️ | `backend/app/schemas/ai.py:93-150` | DTO 能做分数/置信度校验，但无业务消费和持久化门禁。 |
| FR-035 | ❌ | 缺少 `backend/app/services/grading/confidence_policy.py` | 没有置信度策略。 |
| FR-036 | ❌ | `backend/app/ui/review_view.py:153-165` | 只有明确“未就绪”回执，没有 Pending Review API。 |
| FR-037 | ❌ | 缺少 `backend/app/models/review_record.py`、`backend/app/services/review_service.py` | 没有最终评分和教师复核记录。 |
| FR-038 | ❌ | 缺少 `backend/app/models/diagnosis_report.py`、`backend/app/services/diagnosis_service.py` | 没有诊断报告。 |
| FR-039 | ⚠️ | `backend/app/ui/results_view.py:29-30`、`375-386` | 教师结果页面结构存在，但真实成绩和学情查询未接入。 |
| FR-040 | ⚠️ | `backend/app/ui/results_view.py:29-32`、`98-108` | 学生结果/诊断显示空态，没有后端结果实体。 |

用户故事结论：US1 为 M1 人工建题和答卷部分可运行，资料摄取和 AI 出题缺失；US2 可答题提交但没有评分、成绩和诊断；US3 当前不可运行；US4 的 Admin 管理、JWT 和 RBAC 基本可运行。SC-001、SC-004、SC-006、SC-007、SC-008 当前不满足；SC-005 的人工题目门禁满足但 AI 候选路径无法验收；SC-009 仅对已实现的 M1 接口有测试证据，不能外推到未存在的 AI 接口。SC-002/SC-003 没有真实可用性测量。

### 3.2 与 plan.md 的符合度

| 架构决策 | 符合度 | 说明 |
| :-------------------- | :----- | :-- |
| PostgreSQL + pgvector | ⚠️ | Compose 使用 pgvector/pg16，0001 迁移创建 vector 扩展；没有 DocumentChunk、向量列或检索实现。 |
| 三容器部署 | ✅ | `docker-compose.yml:3-74` 只有 postgres、redis、backend，依赖条件和端口均存在。 |
| Gradio UI | ✅ | `backend/app/core/app.py:92-96` 挂载 Gradio；未引入 React/Vue/Streamlit。 |
| Provider 抽象 | ⚠️ | LLM Base/Factory/DeepSeek 存在；Embedding、Rerank Provider 和业务应用服务缺失。 |
| 结构化输出 | ⚠️ | `DeepSeekProvider.generate_structured()` 和 DTO 已验证 JSON/Pydantic；没有出题/阅卷业务链路消费。 |
| 四类 Agent | ❌ | `backend/app/ai/agents/` 只有空初始化文件。 |
| LangGraph 状态图 | ❌ | `backend/app/ai/workflows/__init__.py:1`，没有 StateGraph 或条件边。 |
| M0 readiness | ⚠️ | Compose smoke 通过，但 /health 是存活检查，未检查 DB/Redis，见 H03。 |
| 排除技术 | ✅ | 未发现 React、Vue、Streamlit、Milvus、Elasticsearch、Nginx、独立 Worker、K8s。 |
| Benchmark 记录 | ⚠️ | 有 M0 合成种子；缺少四模式检索和三策略阅卷实验记录格式，见 M03。 |

### 3.3 与 constitution.md 五条原则的符合度

| 原则 | 符合度 | 说明 |
| :------ | :----- | :-- |
| 可运行优先 | ⚠️ | M0 smoke 和 Compose config 通过，三容器可启动；readiness 与实际依赖脱节。 |
| AI 能力优先 | ❌ | backend/app/ai 共 13 个 Python 文件、约 372 行，其中只有 LLM Provider 有实质代码；backend/app/ui 约 11,903 行，当前 AI/UI 行数约 3.1%/96.9%，且核心 AI 里程碑缺失。 |
| 模型可替换 | ⚠️ | LLM Provider 抽象符合原则；Embedding/Rerank 未实现，业务层替换能力尚未验证。 |
| 结构化输出铁律 | ⚠️ | Provider 层遵循 JSON Parse/Pydantic；没有业务 Service、状态门禁和结果持久化。 |
| 可评测原则 | ❌ | 只有可重复合成种子，缺少实际检索/阅卷对比、指标、环境、失败记录和分析。 |

### 3.4 与 tasks.md 的对应关系

| 已勾选/任务组 | 实际实现 | 备注 |
| :-------- | :----- | :-- |
| T001-T013 | ✅ | M0 骨架、Compose、Provider 基础、种子 Benchmark 和 smoke 有实现；readiness 语义仍有 H03。 |
| T014-T030 | ✅/⚠️ | M1 业务模型、API、服务、RBAC 和基础集成测试存在；T030 验证的是 Document 元数据，不是 M2 摄取。 |
| T031-T045 | ✅ 未勾选且确实未实现 | M2 parser、embedding、chunk、retrieval、rerank、四模式 Benchmark 均缺失。 |
| T046-T064 | ✅ 未勾选且确实未实现 | M3 grading、result、diagnosis、review 模型/API/Benchmark 均缺失。 |
| T065-T079 | ✅ 未勾选且确实未实现 | M4 Agent、LangGraph、checkpoint、人工恢复和集成测试均缺失。 |
| T080-T091 | ✅ 未勾选且确实未实现 | M5 MCP、audit、trace、评测看板数据接入和发布证据均缺失。 |
| T092-T102 | ❌ 已勾选但实际为 partial/missing | tasks.md 自身标注 partial/missing；如 T093、T095、T096、T098 明确依赖尚未实现的业务契约。 |
| T103-T118 | ❌ 已勾选但实际为 partial/missing | docs/ui-layout-design.md 明确要求本节任务保持未勾选；T105、T109-T117 标为 missing。 |
| T119-T124 | ✅ | DEV_MODE 配置、账号初始化、快速登录、提示和文档存在；生产移除仍需执行 checklist。 |

### 3.5 与 data-model.md 的符合度

| 实体/状态 | 符合度 | 说明 |
| :--- | :--- | :--- |
| User、Role、Course、KnowledgeBase、Document、Question、Exam、Submission、Answer | ✅ | M1 模型和 0002_m1_business_models.py 已存在，字段与 M1 设计基本一致。 |
| DocumentChunk | ❌ | 缺少模型、embedding、search_vector、metadata、来源关系和迁移。 |
| GradingResult、ExamResult、DiagnosisReport、ReviewRecord | ❌ | 缺少模型、迁移、服务和 API。 |
| AgentRun、WorkflowRun | ❌ | 缺少追踪实体和 checkpoint 状态。 |
| Question 状态机 | ✅ | `question_service.py:27-35` 实现 Draft/Pending Review/Approved/Needs Revision 的主要转换。 |
| Document 状态机 | ⚠️ | `knowledge_base_service.py:536-638` 有状态转换校验，但没有任何 Parser/Embedding 生产者推动状态。 |
| Submission/Answer 状态机 | ✅/⚠️ | M1 Draft/Submitted/Graded/Reviewed 骨架存在；Graded/Reviewed 是手动状态，没有真实评分 Workflow 驱动。 |
| Alembic 同步 | ⚠️ | 当前仅有 0001/0002 M1 迁移；alembic check 因本地 psycopg2 缺失退出，未能完成模型同步验证。 |

### 3.6 安全边界与业务依赖空态

未发现被 Git 跟踪的硬编码 API Key、密码或 Token；本地未跟踪 .env 存在配置值，但本报告不输出任何值。受保护 API 使用 Bearer JWT、角色/权限依赖，Gradio 操作前通过 `backend/app/ui/gradio_app.py:2290-2317` 重新验证 token 和用户状态。/health、/api/auth/login 属于预期公开入口，未发现其他明显免认证业务 API。DEV_MODE 默认关闭，快速登录只在开启时创建随机密码哈希账号，符合 `docs/dev-mode-removal-checklist.md:16-35` 的隔离要求。

依赖未就绪的 UI 空态处理总体正确：知识库资料上传在 `backend/app/ui/knowledge_base_view.py:165-179`、`691-700` 禁用；AI 出题在 `backend/app/ui/question_generation_view.py:98-103`、`135-163` 返回明确不可用态；复核和成绩页在 `review_view.py:153-165`、`results_view.py:29-32` 不构造假分数、假置信度或假成功。当前没有发现随机成绩、示例成绩或伪造“已就绪”结果。

## 四、类型与静态检查结果

| 检查项 | 结果 | 详情 |
| :---------------- | :-- | :----- |
| mypy backend/app/ | ✅ | 67 个源文件，Success: no issues found。 |
| ruff check backend/ tests/ scripts/ | ✅ | All checks passed。 |
| black --check backend/ tests/ scripts/ | ❌ | `backend/app/services/admin_service.py`、`tests/integration/test_teacher_setup.py` 共 2 个文件需要重排；运行时另有 Python 3.13/3.15 target 警告。 |
| pytest | ✅ | 103 passed，36.80 秒；2 个弃用警告。 |
| python -m compileall -q backend tests scripts | ✅ | 编译检查通过。 |
| docker compose config | ✅ | Compose 配置解析通过；命令输出中的本地凭据未纳入本报告。 |
| alembic check | ❌ | 退出失败：本地 DATABASE_URL 使用默认 PostgreSQL dialect 时找不到 psycopg2；不是迁移差异结论。 |

mypy 未发现错误不等于 AI 业务已实现；当前 Any 主要集中在外部 SDK、Gradio 回调和兼容输入边界，Ruff 也未报告未使用导入。未运行需要额外安装的 vulture，因此不能据此证明不存在动态未引用函数。

## 五、代码冗余统计

| 类别 | 数量 | 示例位置 |
| :--------- | :-- | :--- |
| 重复 UI 空态构建函数 | 7 个 _empty_state | `backend/app/ui/admin_view.py:51`、`exam_view.py:70`、`knowledge_base_view.py:57`、`question_generation_view.py:33`、`question_view.py:68`、`review_view.py:44`、`student_exam_view.py:102` |
| 重复 UI 错误格式化 | 5 个 _format_error | `admin_view.py:70`、`exam_view.py:99`、`knowledge_base_view.py:76`、`question_view.py:87`、`student_exam_view.py:131` |
| 重复 UI 角色守卫 | 4 个教师守卫及 1 个 Admin/Student 变体 | `knowledge_base_view.py:63`、`question_view.py:74`、`review_view.py:50`、`exam_view.py:76` |
| 重复 Service 课程加载 | 4 份 _load_course | `course_service.py:268`、`exam_service.py:560`、`knowledge_base_service.py:640`、`question_service.py:506` |
| 静态检查确认的未使用函数/常量 | 0 | Ruff 未报告 F401/F841；动态未调用路径未用 vulture 证明。 |
| 可确认的重复 Schema | 0 | API 请求模型与 AI DTO 字段相似但职责不同，暂不视为重复定义。 |
| 兼容别名 | 5 个 SubmissionService 同义入口 | `backend/app/services/submission_service.py:405-409`；属于可清理的兼容债务。 |

## 六、测试覆盖情况

| 层级 | 覆盖情况 | 缺口 |
| :--- | :--- | :-- |
| 单元测试 | ✅ M0 配置、数据库/Redis 辅助、重试、LLM Provider、M1 Service 基本覆盖 | 没有 parser、chunking、embedding、retrieval、grading、confidence、diagnosis、workflow 单元测试。 |
| 契约测试 | ⚠️ 认证、Admin 契约存在 | 没有 embedding-provider、RAG retrieval、grading API、review/result 契约。 |
| 集成测试 | ⚠️ M0 Docker smoke 与 T030 教师准备流程存在 | 没有资料 Ready、四模式检索、混合答卷、低置信度暂停/恢复和端到端结果诊断。 |
| Benchmark | ⚠️ 10 条以上可重复合成种子 | 没有真实四模式检索或三策略阅卷执行器，结果格式不满足 plan.md §7。 |
| 跳过/长期失败 | ⚠️ | M0 测试在缺少 PowerShell/Docker 时会主动 skip，逻辑位于 `tests/integration/test_m0_smoke.py:60-72`；本次环境实际通过，没有 xfail。 |
| 测试警告 | ⚠️ | Starlette/httpx 弃用警告，以及 `backend/app/api/exams.py:260` 的 422 常量弃用警告。 |

## 七、修复建议优先级

1. **第一批（高优先级，立即修复）**：修复 H03 readiness，补齐 H04 学生考试范围和 H05 JWT 配置校验；明确 M2-M4 未完成状态，先实现 DocumentChunk/摄取/RAG，再实现客观确定性评分、主观结构化评分、结果持久化和 LangGraph 复核恢复。
2. **第二批（中优先级，近期修复）**：修正 T092-T118 勾选状态；补齐 M2-M5 契约与集成测试；建立四模式检索和阅卷 Benchmark 规范记录；统一 Alembic/psycopg 配置和静态检查工具链；抽取重复 UI 状态/错误处理。
3. **第三批（低优先级，可选）**：收敛 Submission 兼容别名，处理弃用警告，显式固定 Black/Python target，明确 PostgreSQL trust 仅限本地开发，并整理 register_models 的模型注册责任。

## 八、结论

当前代码可以继续推进 M2 RAG，但必须把 M1 基线和未实现范围如实标注，不能以现有 UI 空态或合成 Benchmark 代替 M2 验收。推进前必须先解决 Backend readiness 与 JWT 配置边界，并设计考试目标学生范围；随后按 T031-T045 完成资料摄取、DocumentChunk、Embedding、四模式检索、Rerank 和可复现 Benchmark。只有在 RAG 上下文可追溯后，才应继续 T046-T079 的客观/主观阅卷、低置信度复核、统一结果、诊断报告和可恢复 Workflow。

## 第一批高优先级修复记录（2026-09-13）

本批已完成 H03、H04、H05，并按代码可运行性核对 M04 任务状态。H03 在 `backend/app/core/app.py:102-155` 增加依次检查配置、PostgreSQL 和 Redis 的 `/ready`，Compose 与 M0 冒烟目标已同步更新；`/health` 保持进程存活探针。H04 在 `backend/app/models/exam_participant.py:12-34` 和 `migrations/versions/0003_exam_participants.py:8-35` 增加考试学生分配及复合唯一约束，`submission_service.py:1115-1142` 统一限制列表、详情、开始、保存和提交。为兼容已有已发布考试，只有当一场考试完全没有分配记录时才向全体学生开放；一旦存在分配记录，仅允许列表中的学生参加，资格变化也会在提交时重新校验。H05 在 `backend/app/core/config.py:124-174` 增加类型化 JWT 配置、最小长度校验和安全脱敏，并由认证依赖和 UI 统一读取。

本批验证结果：`pytest tests/ -q` 为 122 passed；`mypy backend/app/`、`ruff check backend/ tests/` 和 `docker compose config --quiet` 均通过；容器启动后 `/ready` 返回 HTTP 200，随后已执行 `docker compose down`。测试变更说明见 `docs/test-change-record-high-priority.md`。
