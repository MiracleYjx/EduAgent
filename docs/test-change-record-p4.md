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
