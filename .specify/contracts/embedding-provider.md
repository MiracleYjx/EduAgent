# Embedding Provider Contract

## Purpose

为知识文档和查询提供可替换的 Embedding 能力。业务检索层不得直接依赖云端 API、本地
Hugging Face 模型、BGE 或其他具体 SDK。

## Required Operations

```text
embed_documents(documents) -> vectors
embed_query(query) -> vector
```

## Contract Rules

- `embed_documents` 返回的向量数量必须与输入文档数量一致。
- `embed_query` 返回的向量维度必须与知识片段 `embedding` 维度一致。
- 同一个 Provider 配置下，文档向量和查询向量必须使用兼容的模型和版本。
- Provider 错误、超时、空输入和维度不匹配必须返回可识别的失败状态。
- 业务层只依赖 Provider 抽象；替换 Provider 不得修改课程、知识库或评分业务规则。

## Traceability

每次索引或查询必须能记录 Provider 标识、模型版本和关联的文档/查询版本，以支持
Benchmark 回归。
