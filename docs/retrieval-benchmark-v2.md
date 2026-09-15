# M2 检索 Benchmark v2 对比报告

## 1. 数据集说明

本次评测使用 DeepSeek 生成的《Python 基础教程》Markdown 文档，作为独立于 Query 的课程语料。教材摄取后得到 21 个 chunk，平均长度 346 字符；评测包含 20 个学生自然提问，覆盖 10 个章节。正样本由规则候选、DeepSeek 语义相关性判断和 BGE-large-zh-v1.5 Top-3 联合标注，LLM 阈值为 0.55，Top-3 片段仅在 DeepSeek 分值不低于 0.30 时强制入选。

标注结果平均每个 Query 1.6 个正样本（约 2 个）。本次没有 `out_of_scope` Query；指标按全部 20 个已标注 Query 计算。

## 2. 指标对比

运行标识为 `synthetic-python-basics-v2-20260915`，模型为 `bge-large-zh-v1.5`，Rerank 提示版本为 `llm-rerank-v1`。

| 模式 | Recall@5 | Recall@10 | MRR | NDCG@10 | p95 延迟（ms） |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `keyword_only` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 3.213 |
| `vector_only` | 1.0000 | 1.0000 | 0.9750 | 0.9655 | 9.426 |
| `hybrid` | 1.0000 | 1.0000 | 0.9750 | 0.9655 | 13.837 |
| `hybrid_rerank` | 1.0000 | 1.0000 | 1.0000 | 0.9920 | 12149.929 |

## 3. 诚实分析

### 3.1 `keyword_only` 为 0

`keyword_only` 使用 PostgreSQL `simple` 配置构造 `tsvector`。中文教材中的连续汉字通常不会按 Query 中的自然词切开，`plainto_tsquery` 因此无法命中这些连续文本，本次 20 个 Query 均返回空结果。

`.specify/plan.md` §2 原文规定“关键词检索使用 PostgreSQL `tsvector` 和 GIN 索引，覆盖语义检索容易遗漏的术语、专有名词、编号和精确词形”。计划没有把中文分词扩展列为 MVP 必选项；实现文件 `backend/app/ai/retrieval/keyword_search.py` 已进一步明确记录 `simple` 对连续中文文本的分词边界，以及需要空格分词或 `zhparser` 等中文分词扩展。该结果不是检索代码异常，而是已记录的中文分词技术债，运行记录保留了真实的零命中。

### 3.2 `vector_only` 为 1.0

语料只有 21 个 chunk，平均每个 Query 约 1.6 个正样本（约 2 个）。在 Top-5 中寻找少量正样本，且这些正样本由 BGE Top-3 规则参与标注，命中率天然偏高。因此 Recall@5/10 为 1.0 不能证明向量检索完美，更准确的解释是当前评测集合较小、问题较简单，并且标注规则与向量候选存在关联。

### 3.3 `hybrid` 未超过向量模式

关键词路对本数据集完全未命中，Hybrid 的融合输入实际上只剩向量候选，所以 Recall、MRR 和 NDCG 与 `vector_only` 相同。只有接入中文分词扩展，或使用更大且包含术语变体、编号和精确词形的课程语料，才能真正观察 Hybrid 对向量遗漏的补偿价值。

### 3.4 Rerank 的真实价值

Rerank 没有提升 Recall：基础向量召回已经达到 1.0。它把 MRR 从 0.9750 提升到 1.0000，提升约 2.5%；NDCG@10 从 0.9655 提升到 0.9920，提升约 2.7%。代价是 p95 延迟从 9.426 ms 增至 12149.929 ms，约增加 1300 倍。

结论是 Rerank 在当前集合上带来小幅排序收益，但延迟代价极高，不能默认对所有请求启用。

## 4. 局限与后续验证

- 21 个 chunk 的语料规模不足，无法充分体现 Hybrid 的优势。
- 中文 `simple` 分词限制使 Keyword Only 完全失效，本次不能据此评价中文关键词检索的上限。
- Query 数量和主题范围有限，且 Top-3 强制入选规则会使向量模式的评测偏乐观。
- 应在 M5 使用更大规模、真实课程资料和独立人工相关性标注重新评测，并补充中文术语回归集。

## 5. 对 M3 的建议

主观题阅卷不建议默认对所有请求启用 LLM Rerank。可先减少候选数，或评估本地 Cross Encoder 以降低单次调用成本；上线前应以真实延迟数据比较方案。另一种稳妥策略是只对低置信度、首条结果不足或多片段分数接近的请求触发 Rerank，其余请求沿用 Hybrid 结果。

## 6. 结论

M2 检索链路已经可以完成教材摄取、向量检索、关键词检索、Hybrid、LLM Rerank 和指标记录。当前质量证据仍有限：关键词路受中文分词技术债影响，向量路受小语料和标注规则影响。M5 需要补充大规模真实资料验证；Rerank 的延迟问题则应在 M3 阅卷链路中单独评估和限流。

结果文件：

- `benchmark/results/retrieval_summary_v2.csv`
- `benchmark/results/retrieval_synthetic-python-basics-v2-20260915_keyword_only.json`
- `benchmark/results/retrieval_synthetic-python-basics-v2-20260915_vector_only.json`
- `benchmark/results/retrieval_synthetic-python-basics-v2-20260915_hybrid.json`
- `benchmark/results/retrieval_synthetic-python-basics-v2-20260915_hybrid_rerank.json`
