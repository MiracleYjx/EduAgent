# P4 测试变更记录（TCR）

## P4.1 Docker 默认运行方案

日期：2026-09-21。选择方案 A：主机开发使用已有 dev/本地模型依赖，Docker Demo 使用已有 OpenAI-compatible 云端 Embedding。只改部署配置与文档，不改业务代码、Provider 实现或依赖声明。

### 必要性与覆盖计划（测试变更前）

- 原镜像只装 dev，而示例默认本地 BGE，导致基础健康检查通过但 Embedding 缺依赖。新 Compose 显式选择云端 Demo 配置，Docker 启动先消费现有工厂的配置/依赖检查，不调用远端 API。
- 新增 Compose 实际配置装配测试：从独立临时 env 文件解析，验证主机 BGE 与 Demo 云端映射分离、密钥只在运行环境、缺 Demo 配置不回退成主机模型；缺失模型/密钥使用真实工厂明确报错。Compose CLI 不可用时明确 skip，不伪造装配结果。
- 真实容器验收使用独立 Compose 项目、端口和数据库，不影响默认开发 PostgreSQL/Redis，不改用户 .env。验证镜像构建、三容器启动/健康、迁移，以及未配置 Demo Embedding 时 backend 明确失败。
- 不以 /ready 或构造 Provider 成功宣称真实 AI 可用。未提供云端有效配置时按任务允许的“明确失败提示”验收；不借用 DeepSeek 密钥、不调用收费模型、不生成 Benchmark 质量结论。
- 完整门禁：pytest tests/ -q、mypy backend/app/、ruff check backend/ tests/，并保留既有测试。

### 已执行的自动验证

- `tests/contract/test_docker_demo_config.py` 新增 3 项：真实 Compose CLI 装配云端配置；缺模型和缺密钥两个反例使用真实 Embedding 工厂验证 `EMBEDDING_PROVIDER_NOT_READY`。不注入 Provider 替身、不访问云端。
- `pytest tests/ -q`：1401 passed，1 skipped，7 warnings（252.93 秒）。唯一 skip 是既有 M0 固定隔离环境门禁；本批使用另外的独立 Compose 项目执行真实容器验收，不放宽 M0 门禁。
- `mypy backend/app/`：125 个源文件通过；`ruff check backend/ tests/`：通过。
- 基线为 `deepcode` / `0a93f2f`。开始时无已跟踪改动，但有既存未跟踪的 README.md、README.zh-CN.md、docs/m0-m4-retrospective.md；保持原样且不纳入本次提交。

### 真实 Docker 验收

- 使用独立项目 `eduagent-p41-20260921`，端口 25432 / 26379 / 28000，自有 PostgreSQL/Redis 卷；运行时 env 位于忽略的 `.cache/p41-validation/`，仅含测试值，不读取真实云端凭据、不修改用户 `.env`。
- `docker compose ... build backend` 通过。镜像只安装已有运行依赖；容器内实测没有 sentence-transformers、pytest、`/app/.env` 或主机 `.cache`。
- 缺少 Demo 模型/密钥时 `up -d` 后 backend 无法健康启动；直接运行镜像默认 CMD 返回退出码 1，日志为 `EMBEDDING_PROVIDER_NOT_READY：缺少 EMBEDDING_MODEL, EMBEDDING_API_KEY 配置`。PostgreSQL/Redis 仍健康，不回退到主机 BGE。
- 用完整格式的测试配置重新启动，三容器均 healthy，`GET http://127.0.0.1:28000/ready` 返回 status=ok；干净隔离库 `alembic upgrade head` 通过，`alembic current` 为 `0010_review_round_ids (head)`。
- 测试配置指向容器回环不可达端口 `http://127.0.0.1:9/v1`。通过生产 Provider 执行 `embed_query`，退出码 1，明确返回 `EMBEDDING_FAILED：云端 Embedding 调用失败：APIConnectionError`；未调用云端收费模型，也未伪造向量。
- 验收结论按任务允许的“明确失败提示”路径通过；未验证有效云端凭据下的完整 AI 成功链路。文档给出实际调用命令、1024 维约束、重摄取要求与首次本地模型下载说明。
- 验收结束已核对 Compose 项目标签并删除本批临时容器、网络及两个测试数据卷；原 `eduagent-postgres-1` / `eduagent-redis-1` 保持健康。保留构建镜像缓存，未删除任何开发数据。

## P4.2 Provider 元数据可信化

日期：2026-09-21。基线 `deepcode` / `3c5343e`；已跟踪工作区干净，原有三个未跟踪文档不修改、不提交。

### 必要性与覆盖计划（测试变更前）

- 默认 Question/Grading Provider 在内部调用边界解析，Agent 从构造期 `None` 读取模型会丢失身份；Benchmark 从配置代填 DeepSeek 模型会把替身误记为真实模型。
- 增加实例级 `describe()`：provider/model 来自实例，prompt_version 来自本次提示构造方，call_path 是实际 Python 调用方法的限定名，不是远端调用成功证明，不输出 URL/密钥。空身份统一 `unknown`；自检明确 `stub`。DeepSeek 只增加描述方法，不改生成/重试/回退；存在 fallback 而无法证明最终路由时 provider/model 保守标记 `unknown`。
- Question 内部返回本次解析的实例元数据；SubjectiveGrader 仅增加可选元数据回传，GradingAgent 消费实际调用信息，不另建 Provider、不改评分/出题逻辑、算法或指标。客观题仍不带 LLM 模型字段。旧 duck-typed 替身未实现 describe 时，只取显式模型与实际方法入口，Provider 记为 unknown。
- Benchmark JSON 记录四项来源信息；CSV 保留既有列，model/prompt_version 与 JSON 同源。未解析 Provider 的失败记录为 unknown；不更改已有结果文件。工厂未注册的 openai_compatible 仍明确失败，不暗中别名到 DeepSeek。
- 新增测试覆盖 DeepSeek 实例默认模型、提示版本及真实方法入口；stub 不代填 DeepSeek、空模型 unknown、带 fallback 时不误报最终 Provider；Benchmark 工厂返回与配置不同的实例、显式替身、自检、JSON/CSV 同源及装配失败。
- Agent 测试覆盖默认工厂实际实例、注入路径、逐次解析不串身份；原“无模型为 None”断言按新合同改为 unknown，保留有模型和所有业务断言。真实 SDK 调用使用受控传输，不访问云端、不下载模型、不生成质量结论。
- 注入整个评分器、未提供调用元数据时，模型与提示版本都不可证明，均记录 unknown；对应调整原评分器替身测试的提示版本断言，真实评分器仍验证原版本。非 SDK 客户端也可能是自定义客户端，不能一律推断为 stub，因此用 unknown；显式自检 Provider 自报 stub。
- 门禁：聚焦测试、pytest tests/ -q、mypy backend/app/、ruff check backend/ tests/；不修改 P4.1 Docker 方案或 tasks.md。

### 覆盖落地

- `tests/unit/test_llm.py`：5 项新增用例，覆盖注册名与实例身份分离、显式 stub、None/空白模型与未声明身份。
- `tests/unit/test_deepseek.py`：4 项新增用例，受控 SDK 请求验证实例模型与实际请求一致（配置后续修改不影响身份）；未知客户端、fallback、空模型不误报真实模型。
- `tests/unit/agents/test_question_agent.py` / `test_grading_agent.py`：3 项新增用例，验证默认工厂实际解析的模型回传、同一 Agent 逐次调用不串用、注入评分器不冒认未使用的 Provider；只调整两处不可信旧元数据断言，保留业务断言。
- `tests/contract/test_grading_benchmark_contract.py`：6 项新增用例，覆盖显式注入/工厂解析 × 有模型/空模型、默认 selftest 标注、未注册 Provider 失败；验证生产报告写入函数产出的 JSON/CSV，结果只写 pytest 临时目录。
- 共新增 18 项参数化展开用例。聚焦运行上述 5 个测试文件：101 passed；mypy 125 个源文件通过，ruff（额外包含 Benchmark 脚本）通过。
- DeepSeek 的请求、JSON 校验、重试和回退实现未改；SubjectiveGrader 的新增回传只涉及追踪元数据，不改变成绩、置信度或业务事务。未修改依赖、Docker 配置、迁移、既有 Benchmark 结果或 tasks.md。

### 最终门禁

- 最终代码 `pytest tests/ -q`：1419 passed，1 skipped，7 warnings（248.70 秒）；唯一 skip 仍为既有 M0 固定隔离环境门禁，未放宽或删除。
- `mypy backend/app/`：125 个源文件通过。
- `ruff check backend/ tests/ scripts/run_grading_benchmark.py`：通过（包含要求的 backend/tests 范围及本批脚本）。
- 不做云端模型质量声明；本批没有调用真实收费模型。P4A.3 未执行。

## P4A.3 Benchmark 可复现化

日期：2026-09-21。基线 `deepcode` / `c746cba`；无已跟踪改动，既存三个未跟踪文档保持原样。

### 必要性与覆盖计划（测试变更前）

- 旧清单中的 UUID 属于旧数据库，不能作为新环境的前置条件。新增初始化入口，创建自有 PostgreSQL schema，调用生产 KnowledgeBaseService / IngestionService 摄取教材；只替换外部 Embedding/Reranker 做管道自检，不手工写入 chunk 或 Ready 状态。
- 现有教材按生产默认参数分块，已只读核对与旧标注清单的 21 个片段逐个内容一致。通过 chunk_index + 精确内容验证建立标注映射，manifest 保存数据库实际 UUID、稳定内容标识、运行时 Provider 元数据、分块配置、输入摘要、查询与标注快照。改变内容时拒绝沿用旧标注，不模糊匹配或猜测。
- 评测显式消费 manifest 和隔离 schema；结果内嵌 manifest，并附稳定片段标识与比较指纹。指纹区分输入与 Provider/配置，不含随机 UUID、时间或延迟；它只辅助选取比较对象，不保证远端模型或同分排序逐位确定。保留原检索算法、指标定义和既有结果。
- 结果写入新的运行目录，拒绝覆盖旧文件；失败保留失败状态，无效指标不伪造。初始化失败仅清理本次创建的 schema；成功后保留 schema 供重复查询，显式 cleanup 只允许 manifest 中受控命名的 schema。
- 真实 PostgreSQL 测试覆盖初始化与 Ready/向量/全文索引、运行时 UUID 与标注映射、manifest 驱动四模式、不同新 schema 可比较、同一 manifest 重复执行、内容不匹配与缺失 schema 明确失败、Provider 不兼容拒绝、不覆盖已有输出及清理边界。测试写入 pytest 临时目录；不访问收费模型、不下载模型。
- 评分沿用现有教师标签指标口径，不把 reference_score 当人工标签；新增持久化报告断言：无标签三指标 null、有明确教师标签的已知误差得到准确 MAE/RMSE/一致率、selftest 始终标注为管道证据；报告样本实际标签来源，避免仅看数据集总描述误判。
- 门禁：聚焦测试、pytest tests/ -q、mypy backend/app/、ruff check backend/ tests/，额外检查本批脚本。不修改业务核心、依赖、列、迁移、Docker、tasks.md 或 P4.2 Provider 接口/身份来源。

### 实施中的验证发现与边界

- 第一轮聚焦 53 项通过后，使用循环亲和的受控 LLM Provider 验证多查询重排，确实复现第二条查询切到新循环导致失败。仅在 Benchmark 装配层注入共享 asyncio.Runner 的 Reranker 桥接，继续复用 HybridRerankRetriever 的原流程及原 rerank_async；生产算法、Provider 和 Prompt 均未改。
- real 模式注入 stub 也标记 pipeline_selftest，不把命令行模式当成真实模型证据。评分 real 入口增加读取本批隔离 manifest 的能力，以便新环境的人工标签评测复用同一资料；保留旧 M2 清单读取兼容，不改指标公式与 P4.2 身份来源。
- 补充输入/标注快照一致性反例、不同生效融合配置的比較身份、旧标注关联键任意替换但初始化成功、CLI 初始化及 cleanup 验证；不要求不同模型的比较指纹相等，不引入源码提交相等门禁。
- 最终审阅补充旧 M2 清单不存在/JSON 损坏的错误合同测试：新增 manifest 分派仍保留 GRADING_BENCHMARK_CORPUS_NOT_READY，不把既有失败改成笼统装配失败。

### 覆盖落地与命令行验收

- `tests/integration/test_benchmark_reproducibility.py` 新增 17 例：真实 PostgreSQL 隔离 schema、生产摄取生成 21 个 Ready 片段（1024 维向量和全文字段均存在）、标注映射、四模式读取、重复运行、新 schema 的稳定比较身份、查询/融合配置变化、Provider 不匹配、缺 schema、拒绝清理 public、源内容变化清理本次 schema、拒绝覆写、两个真实 CLI 入口、评分读取同一 manifest、LLM 多查询共享循环及 2 个快照不一致反例。
- `tests/contract/test_grading_benchmark_contract.py` 新增 4 例：无人工标签的持久化指标全部 null；已知误差 [-1,3] 对应 MAE=2.00、RMSE=2.24、一致率=0.5000，并保留 selftest 标识；旧清单缺失/损坏错误码兼容。共新增 21 例，未删除或放宽原测试。
- 独立命令行运行真实 setup，将材料摄取到 `.cache/p4a3-smoke/corpus/manifest.json` 所属自有 schema；随后运行 `verified-01`、`verified-02` 两组，每组四模式、20 个有效查询全部成功。两组配置比较指纹一致，非延迟指标一致；报告位于忽略的 `.cache/p4a3-smoke/results/`，不提交到历史结果目录。
- 外部 Embedding/LLM 为明确 stub：实测证明摄取、数据库、检索、指标与产物链路，不证明真实模型质量，不调用收费 Provider、不下载模型。人工标注数据仍缺，文档说明如何补充；既有 corpus/结果文件完全未修改。

### 最终门禁与清理

- 最终代码 `pytest tests/ -q`：1440 passed，1 skipped，8 warnings（261.51 秒）；相对 P4A.2 的 1419 passed 增加 21 例。唯一 skip 仍为既有 M0 固定 Compose 隔离环境门禁；警告来自既有 FastAPI/SQLAlchemy/Pydantic 路径，不放宽测试或掩盖失败。
- `mypy backend/app/`：125 个源文件通过；`ruff check backend/ tests/ scripts/benchmark_corpus.py scripts/setup_benchmark_corpus.py scripts/run_retrieval_benchmark.py scripts/run_grading_benchmark.py` 通过；`git diff --check` 通过。
- 已经用新 cleanup 入口删除本批手工验收创建的 `benchmark_21370544a6fd4ee98f3d856ebb18236a` schema。该临时语料不保留，可重新摄取；manifest 与 JSON/CSV 证据保留在 `.cache/p4a3-smoke/`。测试自建 schema 由 fixture 清理，未删除业务资料；原 PostgreSQL/Redis 容器仍 healthy。
- 本批变更仅 8 个评测脚本/文档/测试文件；生产 backend、数据库迁移/列、依赖、Docker、历史 benchmark corpus/results、tasks.md 均未修改。P4A.1/P4A.2 既有改动保持不变；P4B 未执行。

## P4B.1／P4.4 出题检索模式

日期：2026-09-26。基线 `deepcode` / `8445c50`。选择方案 B：保留出题 helper 已公开的四模式能力，而非缩减为 Hybrid；生产生成默认仍为 Hybrid。基线有三个既存未跟踪文档，经用户同意原样保留且不纳入本批提交。

### 必要性与覆盖计划（测试变更前）

- `build_generation_context` 的 Keyword Only 不应创建/调用 Embedding Provider；Vector Only 仅传向量，Hybrid 与 Hybrid+Rerank 传 `RetrievalQuery`（文本与向量），不以错误查询类型造成隐性失败。
- Hybrid+Rerank 复用 Hybrid 融合结果，候选上限与最终 top_k 分开；在当前事件循环调用 `rerank_async`，保留课程过滤、来源片段与 Prompt 白名单，不回退到未重排的成功结果。空候选时不装配 Reranker，仍按既有上下文不足合同失败；Reranker 未就绪/执行失败时返回脱敏且含来源码的明确错误。
- 新建 `tests/contract/test_question_agent_retrieval_modes.py`：以实际 Question Agent 出题入口参数化验证四种模式、检索参数/来源/课程过滤、关键字无 Embedding（含工厂失败陷阱）、真实 LLM 异步重排适配器在事件循环内重排，并覆盖空候选、不就绪及执行失败；不访问收费模型或修改数据库。保持既有出题生成/审核、评分与检索层测试及断言不变。
- 门禁：聚焦出题及相关检索测试、`pytest tests/ -q`、`mypy backend/app/`、`ruff check backend/ tests/`、`git diff --check`。仅门禁全绿后显式暂存 P4.4 文件、单独提交并推送 `origin/deepcode`；不触及评分算法、图结构、依赖或 tasks.md。

### 覆盖落地及最终门禁

- 新增 `tests/contract/test_question_agent_retrieval_modes.py`，参数化覆盖四种模式的出题成功、查询类型、课程过滤及 Embedding/Rerank 实际调用；验证 Keyword Only 不依赖 Embedding Provider、Hybrid+Rerank 真实异步 LLM 适配器、融合候选上限/最终来源白名单、空候选、不就绪、未知来源和非法参数的明确失败。新增 12 项展开用例；没有删减或放宽原测试。
- 聚焦新契约、原出题 Agent 和 Reranker 测试：65 passed。`mypy backend/app/`：125 个源文件通过；`ruff check backend/ tests/`、`git diff --check`：通过。
- 首次 `pytest tests/ -q`：1364 passed、1 skipped、88 errors；首个错误为现有文件型 SQLite 测试收到 Kernel 临时目录的 Windows 扩展前缀 `//?/`，其已有 `sqlite:///{path}` 构造无法打开文件；不涉及出题变更。只在本批 `.cache/p4b1-validation/` 设置 `PYTEST_ADDOPTS=--basetemp=D:/YJX/MyCode/EduAgent/.cache/p4b1-validation/pytest-full-fixed-temp`，先重跑首个失败用例通过，再用同一 `pytest tests/ -q` 入口完成全量门禁：**1452 passed，1 skipped，7 warnings**（272.46 秒）。未改变原测试/环境依赖，跳过仍为既有 M0 隔离 Compose 配置要求。
- 本批只修改 Question Agent 的检索装配与错误映射；生产出题服务默认 Hybrid 保持不变。未更改评分、底层检索/重排算法、Docker、迁移、依赖、历史 Benchmark、P1/P2/P3/P4A 代码或 tasks.md。三个既存未跟踪文档保持原样且不纳入提交。
