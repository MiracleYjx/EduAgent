# 检索 Benchmark 结果目录

本目录保存 T045 检索 Benchmark 的规范记录（格式见 `.specify/plan.md` §2）：

- `retrieval_<run_id>_<config>.json`：单次运行的完整记录，含配置、元数据、指标、逐查询
  结果与分析说明；运行失败同样写入 `status=failed`、`error_code` 与 `error_message`。
- `retrieval_summary.csv`：横向比较汇总，每次运行一行（失败运行同样记录）。
- `synthetic_benchmark.json` / `synthetic_benchmark.csv`：M0 合成阅卷样本，检索评测用例
  由它按规则派生（题干作为 query，包含参考答案的片段作为正样本）。

复现命令：

```bash
# 真实 Provider 运行（记录实际可用模式的指标与未就绪模式的失败原因）
python scripts/run_retrieval_benchmark.py --run-id <run_id>

# harness 自检运行：使用确定性替身 Embedding/Rerank 验证四模式管道
python scripts/run_retrieval_benchmark.py --self-test --run-id <run_id>
```

读取约定：

- `model_version=stub-hash-v1` 的记录是**管道自检**，不得作为模型质量对比结论。
- 失败模式不写入 0 分指标：`status=failed` 的记录其指标字段仅表示“无有效结果”。
- 语料与查询向量按运行写入开发库并在运行结束后清理；关键词模式不依赖 Embedding。
