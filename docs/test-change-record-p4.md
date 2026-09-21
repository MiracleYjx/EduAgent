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
