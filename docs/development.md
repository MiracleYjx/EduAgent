# 本地开发环境

在项目根目录使用 Python 3.12 或更高版本。

## 本地开发依赖安装

基础依赖：

```powershell
python -m pip install -e ".[dev]"
```

如需本地 Rerank（Cross Encoder）：

```powershell
python -m pip install -e ".[dev,rerank-local]"
```

注意：Docker 镜像不包含 `rerank-local`。容器内使用 LLM Rerank 或云端 Provider；
本地环境安装额外依赖后，才可使用 BGE Embedding 和备用 Cross Encoder。

`rerank-local` 安装 `sentence-transformers`，供 BGE Embedding 和备用 Cross Encoder
共用；原有 `rerank` 依赖组继续可用。安装失败时保留错误信息并停止，不切换模型或
使用替身冒充真实 Provider。

复制 `.env.example` 为 `.env`，填写本地 PostgreSQL、Redis、DeepSeek 密钥和 JWT
密钥。Embedding 固定使用 `BAAI/bge-large-zh-v1.5`、1024 维；现有迁移无需修改。

```dotenv
EMBEDDING_PROVIDER=huggingface
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_DIMENSION=1024
RERANK_PROVIDER=llm
RERANK_MODEL=deepseek-chat
```

首次编码时，`sentence-transformers` 自动下载 BGE 模型，权重约 1.3 GB，请预留磁盘
空间与网络时间。下载超过 15 分钟仍未完成时停止并报告阻塞，不自动更换下载源或模型。
下载完成后模型缓存在项目目录 `.cache/huggingface`。PowerShell 用户可执行下面的命令
设置用户级缓存位置；重新打开终端后，`huggingface_hub` 会使用该目录：

```powershell
[Environment]::SetEnvironmentVariable("HF_HOME", "D:\YJX\MyCode\EduAgent\.cache\huggingface", "User")
```

Docker 的 backend 服务会把 `./.cache/huggingface` 挂载到容器的
`/root/.cache/huggingface`，因此本地已下载的 BGE 权重可复用。可设置
`HF_HUB_OFFLINE=1` 验证本地 Embedding 离线运行；DeepSeek LLM Rerank 仍需网络和有效密钥。

准备好 PostgreSQL 和 Redis 后执行：

```powershell
python -m alembic upgrade head
```

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
