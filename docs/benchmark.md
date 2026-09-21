# Benchmark 可复现操作（P4A.3）

目标是可追溯、可重复执行和可比较，不是保证远端模型逐位确定。管道自检与模型质量证据必须分开；本仓库尚无真实教师评分标签，不能据此报告评分质量结论。现有 `benchmark/results/` 是历史证据，不覆盖它们。

## 1. 干净环境初始化

在仓库根目录使用 Python 3.12+。部署方式仍遵循 [development.md](development.md) 的 P4.1 双模式方案；以下是主机 Python + Docker PostgreSQL/Redis。先按开发文档配置 `.env`（不要覆盖已有文件），确保 `DATABASE_URL` 指向自己的开发库。

```powershell
python -m pip install -e ".[dev,rerank-local]"
docker compose up -d postgres redis
docker exec eduagent-postgres-1 pg_isready -U postgres
python -m alembic upgrade head
```

迁移负责安装现有 pgvector 扩展。初始化器不会改 `public` 的业务数据或自行安装扩展；缺 PostgreSQL/pgvector 时明确失败。调用账号需要创建/删除 schema 的权限。

先做不下载模型、不访问 LLM/云端的管道验收：

```powershell
python scripts/setup_benchmark_corpus.py --self-test --output-dir .cache/benchmark/corpus-stub
python scripts/run_retrieval_benchmark.py --self-test --manifest .cache/benchmark/corpus-stub/manifest.json --queries 999 --run-id stub-01 --output-dir .cache/benchmark/results
python scripts/run_retrieval_benchmark.py --self-test --manifest .cache/benchmark/corpus-stub/manifest.json --queries 999 --run-id stub-02 --output-dir .cache/benchmark/results
```

- 初始化会创建随机 `benchmark_<UUID hex>` schema，建立现有模型表，经生产 `KnowledgeBaseService.upload_document/ingest_document` 摄取 `benchmark/corpus/python_basics.md`。解析、清洗、分块、Ready、向量和全文字段均由正式服务负责；仅 Embedding 是明确标注的 stub。
- 默认分块为 500 字符、80 字符重叠，不新增数据库列或迁移。现在 corpus 只有这一份教材；`--corpus-dir` 可指向同布局的教材/查询/标注副本。
- 每次初始化必须用**新目录**，每次评测必须用**新 run-id**；目录存在就拒绝，不能覆写历史记录。`.cache/` 被 Git 忽略，需要共享时自行复制新产物到新的证据目录。
- 每次评测在 `<output-dir>/<run-id>/` 生成四种模式的 JSON 与 `retrieval_summary_v2.csv`。JSON 内嵌完整 manifest，逐查询同时记录真实 UUID 与稳定内容标识。初始化/校验失败非零退出；评测装配失败保留 `failure.json`，没有伪造的质量指标。单模式失败也写 JSON/CSV 的失败状态。
- `--queries` 是读取查询清单的上限，之后仍按原规则排除 out-of-scope/无正样本查询；用 999 可覆盖目前全清单。指标仍为 Recall/Precision@5、@10、MRR、nDCG@10、延迟 p95。既有执行器为计算 @10 实际取 top 10，报告记 `effective_top_k=10`；`--top-k` 不改变这一既有口径。
- `--self-test` 结果标为 `pipeline_selftest`，只能证明管道，不是模型质量结论。没有 manifest 时会提示先初始化，不再要求数据库里存在旧 UUID。

## 2. 使用实际 Provider

按开发文档配置现有 Embedding/LLM/Rerank Provider。然后去掉两处 `--self-test`，用新的目录：

```powershell
python scripts/setup_benchmark_corpus.py --output-dir .cache/benchmark/corpus-provider-a
python scripts/run_retrieval_benchmark.py --manifest .cache/benchmark/corpus-provider-a/manifest.json --queries 999 --run-id provider-a-01 --output-dir .cache/benchmark/results
```

本地 BGE 首次运行需要下载模型、联网和足够磁盘/内存；缓存后的再次运行不需要重复下载。云端 Embedding 需要已有 Provider 支持的配置/有效凭据，输出必须兼容现有 1024 维列；不会自动降级为 stub。真实 Embedding/LLM 可能计费。只评估不依赖 LLM 的三路时可加 `--configs vector_only keyword_only hybrid`。

元数据从实际 Embedding/Reranker/底层 LLM 实例取得，记录模型、调用入口、查询前缀/批大小、候选数、融合权重等生效参数。云端端点只保存不透明摘要，不写 URL/密钥。LLM 重排未声明 Prompt 版本时如实为 unknown，不猜版本；报告保留实现追溯信息。Benchmark 对多次异步重排共享事件循环，仍调用原生产检索与重排实现。

## 3. manifest 与比较方法

`manifest.json` 是**本次摄取事实**，不是待导入的固定 UUID：

- `schema`、`corpus.document_id`、`corpus.chunks[].chunk_id`：本次数据库真正生成的身份，评测只查询这些资料。
- `stable_id`：片段序号和精确内容摘要，在相同输入重新摄取后保持一致。
- `inputs` / `input_fingerprint`：源文件摘要、分块参数、稳定片段、查询和相关性标签；不含时间、schema、数据库 UUID。
- `embedding`：本次摄取实例元数据；查询实例必须与其兼容。不允许同维但不同模型的向量混用，更换 Embedding 必须重新初始化。
- `queries` / `annotations`：评测快照。原 `chunks.json` 的旧 UUID **只用作标注文件内部关联键**，通过片段序号及精确内容对应映射成新 UUID，数据库不需要旧记录。内容/片段数变化会失败，必须重新维护标注，不能模糊套用旧标签。

重复同一 manifest，用不同 run-id。比较 JSON 的 `reproducibility.comparison_fingerprint`、查询集合和指标；比较不同 Provider 时，应保持 `input_fingerprint` 与查询集合相同，只允许计划内的 Provider/模型/配置差异，配置指纹不同是预期的，**不是跨 Provider 比较的阻断条件**。表格汇总提供指标，JSON 提供全部配置依据。

跨新 schema 比较逐查询命中时用 `stable_chunk_ids/stable_relevant_ids`，不能直接比较随机 UUID。生产检索同分时可能以 UUID 排序，新摄取的同分名次可能变化；远端模型、浮点数、硬件和延迟也不保证完全相同。本批不改排序/检索核心以制造一致。建议对严格回归复用同一 manifest，并记录代码提交、Python/数据库/模型库版本及可用的远端模型修订；模型别名相同不保证云端权重永远不变。

## 4. 评分标签与质量指标

评分样本在 `benchmark/corpus/grading_samples.json`。复制到新文件供教师逐题评阅，再使用 `--dataset` 指向副本：

1. 保留题目、rubric、学生答案、满分与 sample_id；教师依据 rubric 填写 `teacher_score`（与满分同单位、范围内）。未完成标注仍为 null。
2. 逐样本 `label_source` 改成 `teacher`，保留 `reference_score` 的合成参考分含义，不把它复制为“人工分”。数据集 metadata 说明混合或教师标注，更新 version，并在受控标注记录中保存教师/时间/复核信息。
3. 报告 `ground_truth.label_sources` 来自实际样本，不仅从顶层描述推断。无教师分时 MAE/RMSE/一致率全为 null；有标签时只使用成功预测且有教师分的样本，记录 effective_sample_count。MAE 为绝对误差均值，RMSE 为均方根误差，一致率沿用绝对差 ≤ 1 分的口径。

```powershell
python scripts/run_grading_benchmark.py --mode selftest --dataset path/to/teacher_samples.json --run-id labels-pipeline-01 --results-dir .cache/benchmark/grading-selftest
python scripts/run_grading_benchmark.py --mode real --corpus .cache/benchmark/corpus-provider-a/manifest.json --dataset path/to/teacher_samples.json --run-id teacher-provider-a-01 --results-dir .cache/benchmark/grading-provider-a
```

real 模式可直接复用上述隔离 manifest；旧 M2 清单读取兼容保留。selftest 或实际注入 stub 时，无论有无教师标签，`evidence_kind=pipeline_selftest`，计算出的数字只验证公式/流程，不能作为质量结论。不同评分 Provider 比较必须使用同一人工标注集、策略和检索配置，并报告失败率/有效样本数，不把缺失预测当零分。

## 5. 清理与故障恢复

成功初始化后 schema 保留，供新进程重复查询；不要把它当业务资料库。初始化失败只删除该次创建的 schema。评测失败不删除语料，可修正环境后换 run-id 重试。需要释放评测数据时显式执行：

```powershell
python scripts/setup_benchmark_corpus.py --cleanup .cache/benchmark/corpus-stub/manifest.json
```

此操作不可恢复地删除**对应 Benchmark schema**（可重新摄取），保留 manifest 和结果；拒绝 public 等非受控命名。schema 已删除或连接到了另一数据库时评测明确失败，不回退到 public。只清理自己创建且确认不再使用的 manifest。
