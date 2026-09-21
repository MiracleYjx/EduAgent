# 开发与 Docker Demo 运行方案

采用双模式（P4.1）：开发时主机运行 Python、Docker 只运行 PostgreSQL/Redis；Demo 时
三个服务都在 Docker 中运行，Embedding 显式使用已有的云端 OpenAI-compatible 适配器。
这样开发仍可使用本地 BGE，Demo 镜像无需携带本地推理依赖或模型权重，也不新增项目依赖。
代价是 Demo 需要独立的云端 Embedding 配置；不能拿 DeepSeek 的 LLM 密钥代用。

## 1. 主机开发（本地 BGE）

首次复制 `.env.example` 为 `.env` 并填写连接信息，已有 `.env` 不要覆盖。
主机的 DATABASE_URL/REDIS_URL 使用实际映射到 127.0.0.1 的端口，不使用容器服务名。
填写 DeepSeek 和至少 32 字符的随机 JWT 密钥；不要提交 `.env`。
这一步不启动 backend 容器，因此无需填写 `DEMO_EMBEDDING_*`。

在项目根目录使用 Python 3.12 或更高版本，推荐独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,rerank-local]"
docker compose up -d postgres redis
```

`rerank-local` 是现有可选依赖组，安装 `sentence-transformers`，同时供 BGE Embedding
和备用 Cross Encoder 使用。只安装 `.[dev]` 不足以运行示例中的默认 BGE 链路。
安装失败时保留错误信息，不切换模型或使用替身冒充成功。

```dotenv
EMBEDDING_PROVIDER=huggingface
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_DIMENSION=1024
RERANK_PROVIDER=llm
RERANK_MODEL=deepseek-chat
```

首次启动后第一次编码/摄取时会下载 BGE 模型（不是启动 PostgreSQL 时下载）。请预留
GB 级磁盘空间及网络时间，实际大小以模型版本为准；下载超过 15 分钟仍未完成时停止并
报告阻塞，不自动更换下载源或模型。可在当前 PowerShell 设置项目缓存目录：

```powershell
$env:HF_HOME = Join-Path (Get-Location).Path ".cache/huggingface"
python -m alembic upgrade head
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

`.cache/` 不进入 Docker 构建上下文。默认 Demo 不使用主机模型缓存，不会在镜像构建时
下载 BGE。模型缓存完整后可设置 `HF_HUB_OFFLINE=1` 验证本地 Embedding 离线运行；
DeepSeek LLM Rerank 仍需要网络与有效密钥。

## 2. Docker Demo（云端 Embedding）

镜像只安装项目运行依赖，不装 dev 或 rerank-local。容器的 Embedding 配置固定映射如下；
主机 `.env` 中的 `EMBEDDING_PROVIDER=huggingface` 不影响 Demo：

| `.env` 中的 Demo 配置 | 容器内配置 |
| --- | --- |
| 无需填写 Provider | `EMBEDDING_PROVIDER=openai_compatible` |
| `DEMO_EMBEDDING_MODEL` | `EMBEDDING_MODEL` |
| `DEMO_EMBEDDING_BASE_URL` | `EMBEDDING_BASE_URL` |
| `DEMO_EMBEDDING_API_KEY` | `EMBEDDING_API_KEY` |

模型必须**实际默认输出 1024 维**，与现有 vector(1024) 一致。当前适配器不发送
`dimensions` 参数；仅设置 `EMBEDDING_DIMENSION=1024` 不会让远端模型改变输出维度。
第三方兼容服务需填写自己的 API base URL；地址留空时按现有适配器/SDK 默认端点处理。
Demo 的 Rerank 仍用 `.env.example` 中的 DeepSeek LLM 路线，不使用本地 Cross Encoder。
本地 BGE 与云端模型的向量不能混用。请在新课程/独立库演示，或按现有流程重新摄取
资料；不能用云端查询向量直接检索已有的 BGE 语料，即使两者都是 1024 维。

首次运行先准备 `.env`（见上一节）并填写 `DEMO_EMBEDDING_*`，再构建、迁移、启动
（无需改默认开发数据库的配置）：

```powershell
docker compose up -d postgres redis
docker compose build backend
docker compose run --rm backend python -m alembic upgrade head
docker compose up -d
docker compose ps
```

Compose 在容器内覆盖数据库/Redis 地址为服务名；主机开发仍使用 `.env` 的主机地址。
不要同时让主机 uvicorn 和 backend 容器占用同一个端口；需要并存时设置独立
`BACKEND_PORT`。已有镜像在更新 Dockerfile 后必须重新构建。

容器启动前会调用现有 Embedding 工厂检查配置和依赖，失败则不会启动 uvicorn。例如
未填写 Demo 模型/密钥，日志明确为 `EMBEDDING_PROVIDER_NOT_READY`，并列出
`EMBEDDING_MODEL`/`EMBEDDING_API_KEY` 缺失；对应需要补充 `.env` 的 `DEMO_*` 配置。
修复后重新创建 backend：

```powershell
docker compose logs --tail 30 backend
docker compose up -d --force-recreate backend
```

**容器 healthy 和 `/ready` 只验证基础设施；工厂检查也不验证密钥有效性、网络或真实模型输出。**
填写真实云端配置后，可显式发起一次查询向量调用（会访问配置的服务，可能收费）：

```powershell
docker compose exec -T backend python -c "import asyncio; from backend.app.ai.embedding.factory import create_embedding_provider; p=create_embedding_provider(); v=asyncio.run(p.embed_query('课程检索连通性检查')); print('embedding_dimension=', len(v))"
```

必须得到 1024 维；网络、认证或维度错误按现有 Provider 错误明确失败，不回退、不伪造向量。
之后通过教师 UI 上传课程资料并执行检索/出题/阅卷，验证 DeepSeek/Rerank 等完整调用链。
没有完成这些实际调用时，不宣称“完整 AI 链路已通过”。不要把下一节本地专用冒烟脚本直接
用于精简 Demo 镜像：该脚本还读取 sentence-transformers 元数据，镜像没有该依赖。

密钥仅从运行时 env 文件注入，不进入 Dockerfile、build args 或镜像。独立验收可指定
`EDUAGENT_ENV_FILE`（容器 env 文件）并同时使用 `docker compose --env-file <同一文件>`
控制 Compose 变量插值，配合独立 `COMPOSE_PROJECT_NAME` 及三个端口；不覆盖现有 `.env`。

## 3. 本地 Provider 冒烟与 Benchmark

下载完成后，在新进程中使用离线缓存执行真实摄取冒烟与四模式 Benchmark：

```powershell
$env:HF_HUB_OFFLINE = "1"
python scripts/smoke_rag_providers.py
python scripts/run_retrieval_benchmark.py
```

冒烟资料为 `docs/rag-smoke-course.txt`，执行真实解析、分块、向量化、Ready 持久化、
Hybrid 检索与 DeepSeek 重排。脚本只清理本次创建的课程与账号，在
`benchmark/results/provider_<run_id>_smoke.json` 保留各阶段来源、向量维度和重排分数。

Benchmark 使用真实 Provider，在 `benchmark/results/` 写入四模式 JSON 和 CSV 汇总。
失败模式保留错误码与失败原因，指标留空；`--self-test` 的替身结果仅用于管道自检。

默认使用 LLM Rerank；需要手动切换备用 Cross Encoder 时同时修改：

```dotenv
RERANK_PROVIDER=cross_encoder
RERANK_MODEL=BAAI/bge-reranker-base
```

备用模型首次使用也需下载；系统不会在 LLM 失败时自动切换到备用路线。
