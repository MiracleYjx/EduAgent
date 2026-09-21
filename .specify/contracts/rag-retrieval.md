# RAG Retrieval Contract

## Purpose

统一语义检索、关键词检索、Hybrid Retrieval 和 Rerank 的输入输出，支持课程上下文
追踪和 Benchmark 对比。

## Retrieval Modes

```text
VECTOR_ONLY
KEYWORD_ONLY
HYBRID
HYBRID_RERANK
```

## Processing Contract

```text
Query
  -> Semantic Search (optional)
  -> Keyword Search (optional)
  -> Candidate Merge
  -> Weighted Score Fusion
  -> Top-K
  -> Rerank (optional)
  -> Final Context
```

## Required Result Fields

```text
chunk_id
course_id
document_id
content
semantic_score
keyword_score
fusion_score
rerank_score
rank
```

## Contract Rules

- `VECTOR_ONLY` 只能使用 `pgvector` 语义结果。
- `KEYWORD_ONLY` 只能使用 `tsvector + GIN` 关键词结果。
- `HYBRID` 必须合并去重后执行 Weighted Score Fusion。
- `HYBRID_RERANK` 必须在融合后的 Top-K 候选上执行 Rerank。
- 返回结果必须保留课程和原始资料来源，不允许只返回无来源自由文本。
- 不能检索到有效上下文时必须返回明确的空结果或不足状态，不得静默编造上下文。
