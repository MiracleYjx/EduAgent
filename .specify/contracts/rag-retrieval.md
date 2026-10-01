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

## v2.0 章节范围限定

本节对应 [spec.md](../spec.md) FR-024/FR-025 扩展及 FR-046、FR-047；引用 [plan.md](../plan.md) §8、§9 的教学依据范围。
上文四种检索模式、Weighted Score Fusion、Rerank、来源追溯和空上下文规则保持不变；以下新增输入默认不设额外范围，不改变 v1.0 调用。

### 输入扩展与范围语义

保留 RetrievalQuery.text/embedding，以及 RetrievalFilters 的 course_ids、knowledge_base_ids、document_ids。
为 RetrievalQuery 增加以下具有兼容默认值的逻辑字段；公共输入经 Pydantic 校验，再由检索服务传递为 SQL 条件。

| 字段 | 类型 / 默认 | 语义 |
| :--- | :--- | :--- |
| chapter_ids | tuple[UUID, ...] / () | 课程内真实章节标识；列表内取并集，空列表表示未增加章节限制 |
| section_range | SectionRange / null | 单个章节内的小节序号闭区间 {chapter_id, start_order, end_order} |
| knowledge_points | tuple[str, ...] / () | 规范知识点标签；列表内匹配任一标签，空列表表示未增加知识点限制 |

- SectionRange.chapter_id 为 UUID；start_order/end_order 为正整数，start_order <= end_order；使用资料/课程定义的明确小节顺序，不按标题字符串排序，也不把它理解成页码。
- chapter_ids 与 section_range 同时提供时取交集；章节不相交则明确空结果/范围不足，不自动忽略其中一个条件。
- 课程、知识库、文档、章节、小节范围、知识点各维度之间取交集；最终检索范围仍受已有课程/资料授权约束。
- v2.0 出题/阅卷业务先明确当前课程，再选章节和知识点，形成课程 -> 章节 -> 知识点三级限定；客户端章节不能扩大已有课程范围。
- chapter_ids 和 SectionRange 的章节均须属于当前授权课程；标识非法、跨课程或无法确认真实归属时返回明确输入/范围错误，不借空列表改成全课程检索。
- 小节区间合法但没有就绪资料/已定位 Chunk 时返回不足，允许教师补资料或修正标注；不得静默缩放区间、扩大到邻章或让 Prompt 代替过滤。

~~~json
{
  "chapter_ids": ["11111111-1111-4111-8111-111111111111"],
  "section_range": {
    "chapter_id": "11111111-1111-4111-8111-111111111111",
    "start_order": 2,
    "end_order": 4
  },
  "knowledge_points": ["牛顿第二定律"]
}
~~~

示例只描述新增查询字段，调用仍须携带原 text/embedding 和有权限的课程/资料 Filters。
查询中的章节/知识点限制是唯一输入事实源；服务转成内部 SQL scope，不在 Query 与 Filters 再维护两份可独立改写的章节条件。

### SQL 强制过滤与检索路径

- 扩展现有 backend/app/ai/retrieval/_filters.py 的统一范围消费边界，与课程/知识库/文档条件并列；先过滤再执行向量/关键词打分和 LIMIT/Top-K，不只在候选召回后裁剪。
- SQL 同时限定 Document.Ready、purpose=knowledge_base 及授权课程/资料范围；试卷原文件 purpose=paper_source 不生成 Chunk，不能作为教学依据混入。
- 有章节/小节限制时，SQL 使用 DocumentChunk.chapter_id 及明确 section_order 过滤；未知/null 定位不能假装命中指定章/节。
- 知识点标签采用持久化的结构化列表（目标映射为 Chunk 元数据 knowledge_points），在 SQL 中做明确标签匹配；不以内容关键词命中或 Prompt 声称“符合知识点”代替归属。
- 同一 Query scope 必须传到 VECTOR_ONLY/KEYWORD_ONLY 的 SQL 查询；HYBRID 两路各自应用后才融合，HYBRID_RERANK 只重排已在范围内的候选。
- 重排/上下文整理不能另取范围外 Chunk 补足 Top-K；范围内没有足够结果就返回实际数量/不足，不生成替代依据。
- 单路调用仍支持既有文本或向量输入，由公共服务消费同一 v2.0 逻辑范围；不因它未使用混合 RetrievalQuery 而丢失过滤。
- 实施前需贯通 Query -> 过滤规范化 -> 两路 SQL -> 融合/重排 -> 来源持久化；不得只给 DTO 加字段，现有 resolve_filters 或适配层忽略新字段也必须显式拒绝，不能静默不生效。

### Chunk 定位、索引与兼容迁移

- 当前代码的 DocumentChunk 未定义 chapter_id/section_order，现有 _filters.py 只处理资料状态与课程/知识库/文档；本节定义新增设计，不宣称已实现 SQL 章节过滤。
- 目标新增 document_chunks.chapter_id（UUID，可空）、section_order（正整数，可空）；章节身份和小节顺序必须来自真实资料结构/教师核对，不能凭模型推断填历史值。
- 增加支持课程/章节/小节过滤的复合索引 (course_id, chapter_id, section_order)，其前缀覆盖课程/章节访问；保留既有 pgvector/HNSW、tsvector/GIN，不引入 Milvus/Elasticsearch。
- 章节归属、知识点标签及小节序号的持久化/约束需在后续数据模型和迁移中补齐；本步骤不创建 Chapter/Section 实体、索引或迁移，也不将“增加字段”误称为已经建立索引。
- 一个用于限定章/节的 Chunk 必须具有可靠归属。跨章节/小节的文本须按真实边界重新切分/定位，不能只标起点而把范围外正文送入模型。
- 历史未定位 Chunk 保持 null/未知：无新增范围时保留 v1.0 读取，有章/节限制时明确排除并报告定位/资料不足；不回填虚假 chapter_id 或使用当前标签冒充生成时来源。
- 结果可增 chapter_id、section_order 和真实知识点定位；既有必需结果字段、score/rank 和来源快照语义不变，原出处仍可追溯。

### 错误与验证边界

- 输入形状非法沿用 RETRIEVAL_INVALID_INPUT；章节归属无效可返回 RETRIEVAL_SCOPE_INVALID，未完成范围映射/消费返回 RETRIEVAL_SCOPE_NOT_READY；均含中文原因，不降级为无限制检索。
- 有效范围内无结果遵循上文空结果/不足语义；出题/改编不得编造引用，缺关键依据时不能批准，参见 [agent-workflow.md](agent-workflow.md)。
- 验证四种模式的 SQL 范围一致、条件交集、未知定位、空结果、非法/跨课程范围和 Top-K 前过滤；性能依据真实样本测量，不宣称本次已验证运行。
- 本步骤只追加契约，保留 v1.0 内容及六个新契约；后续代码/数据库/测试修改另行实施，修改测试先形成 TCR。
