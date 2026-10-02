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

- SectionRange.chapter_id 为 UUID；start_order/end_order 为严格正整数（拒绝 bool），start_order <= end_order；使用资料/课程定义的明确小节顺序，不按标题字符串排序，也不把它理解成页码。
- chapter_ids 与 section_range 同时提供时取交集；章节不相交则明确空结果/范围不足，不自动忽略其中一个条件。
- 课程、知识库、文档、章节、小节范围、知识点各维度之间取交集；最终检索范围仍受已有课程/资料授权约束。
- v2.0 出题/阅卷业务先明确当前课程，再选章节和知识点，形成课程 -> 章节 -> 知识点三级限定；客户端章节不能扩大已有课程范围。
- chapter_ids 和 SectionRange 的章节均须属于当前授权课程；标识非法、跨课程或无法确认真实归属时返回明确输入/范围错误，不借空列表改成全课程检索。
- 小节区间合法但没有就绪资料/已定位 Chunk 时返回不足，允许教师补资料或修正标注；不得静默缩放区间、扩大到邻章或让 Prompt 代替过滤。
- knowledge_points 与保存标签采用相同输入规则：去首尾空白、拒绝空白/非字符串元素、按规范值去重；保留大小写、内部空白和原字符，不做子串、同义词或 Unicode 形式扩展。缺省 () 不增加标签条件。

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

### 章节身份与资料定位映射（T134 / G01）

持久事实以 [data-model.md](../data-model.md) §11 为准，承接 [plan.md](../plan.md) §8/§9 与 RAG 契约引用；字段与写入责任在数据模型定义，本契约规定查询消费。

- chapter_id 引用课程级 Chapter.id；登记时生成稳定 UUID，课程归属由 Chapter.course_id 决定。标题/文件路径变化及重摄取不改变身份；同课程多份资料经各自 Chunk 映射同章，同名标题不能自动合并身份。资料归属仍使用 Chunk.document_id，不通过标题或客户端课程声明推断。
- Chapter.sections 为受 Schema 校验的章内目录 [{section_order, title}]；section_order 为 1..N 的真实目录顺序，与源页码、chunk_index、解析器 section_index 分离。教师确认映射后写入 Chunk.chapter_id/section_order；当前目录和资料/Chunk 必须同课程。
- 章/节列只保存确认后的定位，标签只保存在 Chunk.metadata.knowledge_points；scope_confirmation.location/knowledge_points 分别保存相应真实教师 UUID 与 UTC 确认时间。模型建议不作为 SQL 归属，资料就绪也不表示已定位；相关内容/目录含义改变时由写入责任方同步重映射或清除受影响事实与确认，不能让下游猜测。
- 标识存在但不属于当前授权课程、查询端点不在非空小节目录内时返回 RETRIEVAL_SCOPE_INVALID；章节存在而尚无小节目录时，小节范围查询返回 RETRIEVAL_SCOPE_NOT_READY。合法目录区间内未形成已定位、就绪资料时返回实际空结果/范围不足，不能把“章节存在”当成教学依据。

### SQL 强制过滤与检索路径

- 扩展现有 backend/app/ai/retrieval/_filters.py 的统一范围消费边界，与课程/知识库/文档条件并列；先过滤再执行向量/关键词打分和 LIMIT/Top-K，不只在候选召回后裁剪。
- SQL 同时限定 Document.Ready、purpose=knowledge_base 及授权课程/资料范围；试卷原文件 purpose=paper_source 不生成 Chunk，不能作为教学依据混入。
- 有章节/小节限制时，SQL 使用 DocumentChunk.chapter_id 及明确 section_order 过滤；未知/null 定位不能假装命中指定章/节。
- 知识点标签使用已持久化的 Chunk.metadata.knowledge_points 字符串数组，通过 PostgreSQL JSONB 精确成员查询匹配任一规范标签；沿用 v1.0 metadata 列与 ORM 属性，查询时使用 ::jsonb 投影，不新增 Chapter–KnowledgePoint 关联表。未知和确认无标签分开表达，不以内容关键词命中或 Prompt 声称“符合知识点”代替归属。
- 同一 Query scope 必须传到 VECTOR_ONLY/KEYWORD_ONLY 的 SQL 查询；HYBRID 两路各自应用后才融合，HYBRID_RERANK 只重排已在范围内的候选。
- 重排/上下文整理不能另取范围外 Chunk 补足 Top-K；范围内没有足够结果就返回实际数量/不足，不生成替代依据。
- 单路调用仍支持既有文本或向量输入，由公共服务消费同一 v2.0 逻辑范围；不因它未使用混合 RetrievalQuery 而丢失过滤。
- 实施前需贯通 Query -> 过滤规范化 -> 两路 SQL -> 融合/重排 -> 来源持久化；不得只给 DTO 加字段，现有 resolve_filters 或适配层忽略新字段也必须显式拒绝，不能静默不生效。

~~~sql
-- :chapter_ids、:knowledge_points 非空时才添加各自谓词；
-- :range_chapter_id 非 null 时才添加小节谓词，以下是组合示意。
SELECT c.id
FROM document_chunks AS c
JOIN documents AS d ON d.id = c.document_id
WHERE d.status = 'Ready'
  AND d.purpose = 'knowledge_base'
  AND c.course_id = :authorized_course_id
  AND c.chapter_id = ANY(CAST(:chapter_ids AS uuid[]))
  AND c.chapter_id = :range_chapter_id
  AND c.section_order BETWEEN :start_order AND :end_order
  AND jsonb_typeof(c.metadata::jsonb
      #> '{scope_confirmation,knowledge_points}') = 'object'
  AND jsonb_typeof(c.metadata::jsonb -> 'knowledge_points') = 'array'
  AND (c.metadata::jsonb -> 'knowledge_points')
      ?| CAST(:knowledge_points AS text[]);
~~~

既有知识库/文档 Filters 继续作为 AND 谓词；示意省略分数和 LIMIT，实际两路须在这些 WHERE 条件内打分并选 Top-K。chapter_ids 内使用 ANY/IN，标签数组使用 ?| 精确匹配任一规范标签，BETWEEN 为章内闭区间；两列表及小节条件之间始终 AND。章已知而节未知可命中章级条件，不能命中小节区间；标签未确认、缺键/null 或确认无标签 [] 均不能命中非空标签条件。确认记录存在性是新标签范围的消费条件，不在查询时重复校验教师权限或完整 Schema；无标签条件时不添加上述确认/类型/成员谓词。

### Chunk 定位、索引与兼容迁移

- 当前实现尚无 Chapter、chapter_id/section_order 或新增标签过滤；本节和数据模型 §11 是后续 T161/T162 的共同设计，不声明已有物理表、索引或运行验证。
- 目标新增课程级 Chapter 和 document_chunks.chapter_id（UUID，可空、FK Chapter.id、RESTRICT）、section_order（正整数，可空）；section_order 非空必须有 chapter_id。课程/资料归属、小节目录成员及教师确认由现有知识库服务在写入事务校验，不新增独立章节服务。
- 建立 chapters(course_id) 及 document_chunks(course_id, chapter_id, section_order) B-tree 索引；保留既有 pgvector/HNSW、tsvector/GIN。metadata 沿用 JSON 列，标签匹配使用 ::jsonb 投影，不要求转换整列或新增标签 GIN，不承诺未测量的查询性能。
- 一个用于限定章/节的 Chunk 必须整体属于该章/节。分块先按真实边界隔离，再在边界内执行长度切分/重叠；短段合并、标题附带和重叠不得引入范围外正文。无法可靠分开时保持未知、等待教师校正；不能只标起点。
- 原文未变的定位校正可只更新元数据；需要重切分时重新生成同源正文/向量/search_vector，成功后原子替换。失败保留真实状态；已保存 QuestionSourceChunk 快照不改写，活体关联按既有来源契约处理。
- 历史章/节列保持 NULL，原 metadata 原样保留；标签缺键/null 为未知，[] 只表示真实确认后无标签。不得从旧 section_index/chunk_index、题目标签或模型推断回填。无新增范围时不强制 JOIN Chapter 或新增确认记录，有范围时明确排除未知并报告实际不足，不能退回全课程。
- 返回结果可增加 chapter_id、section_order 和真实 knowledge_points；既有必需字段、score/rank、chunk_id/document_id 及来源快照语义不变，当前定位不能冒充历史生成时的依据。

### 错误与验证边界

- 输入形状非法沿用 RETRIEVAL_INVALID_INPUT；章节归属无效可返回 RETRIEVAL_SCOPE_INVALID，未完成范围映射/消费返回 RETRIEVAL_SCOPE_NOT_READY；均含中文原因，不降级为无限制检索。
- 有效范围内无结果遵循上文空结果/不足语义；出题/改编不得编造引用，缺关键依据时不能批准，参见 [agent-workflow.md](agent-workflow.md)。
- 后续验证覆盖课程内稳定身份、多资料同章/同名不自动合并、目录修改、标签规范化与任一精确匹配，以及四种模式的 SQL 范围一致、条件交集、未知定位/标签、空结果、非法/跨课程范围和 Top-K 前过滤；性能依据真实样本测量，不宣称本次已验证运行。
- T134 仅同步本契约与数据模型的章节/知识点设计，保留 v1.0 正文及其他契约；本批次不写业务代码、不创建数据库迁移、不修改测试。后续实施与迁移留至授权批次，修改测试先形成 TCR。


### T161 可信定位生产入口

- 教师通过 `/api/knowledge-bases/courses/{course_id}/chapters` 登记/读取课程章节，`/chapters/{chapter_id}` 修订目录；同名标题保留独立 UUID。目录重新登记使受影响片段的章/节及定位确认归未知，独立知识点证据保留。
- `PATCH /chapters/{chapter_id}/titles` 是教师明确确认“原小节顺序及教学含义未变”的标题修正动作；目录长度和序号必须不变，保持片段定位和原确认。界面提供对应明确勾选操作，默认不选择，切换章节后重置。
- `PATCH /chunks/{chunk_id}/scope` 只提交待核对维度，定位与知识点分别生成真实当前教师身份和服务端 UTC 时间；缺省字段保持原事实，显式 null 清除对应核对，知识点 [] 记录确认无标签。
- `GET /documents/{document_id}/source-sections` 返回持久原稿按当前 parser/cleaner 生成的完整段落；`POST /documents/{document_id}/resplit` 接收 [{section_index, cut_points}]，切点相对于上述清洗段落，从 0 计数，严格递增且在段内。所有切分/长度重叠均不得跨这些真实边界。
- Markdown 摄取保留实际标题层级/路径作为候选；连续祖先标题可附到其后继子节，同级及不同章不合并。候选不写入可信章/节或标签；重切分成功事务替换正文、向量、全文字段，新块等待核对。失败保留旧块供诊断并记录 Failed，旧题来源快照不重写。


### T162 显式业务范围与阅卷任务意图（用户确认）

- 出题请求新增可空缺省的 `retrieval_scope`，字段为 `document_ids`、`chapter_ids`、`section_range`、`knowledge_points`；课程来自已经授权的当前出题课程。旧 `knowledge_points` 继续用于题库分类/生成提示，不自动投影成 SQL 标签条件。
- 阅卷触发请求使用同一 `RetrievalScope`，课程从实际答卷 → 考试 → 课程授权链取得。非空显式范围在创建任务前核对真实资料、章节与目录；所有题型均不能静默接受非法范围。后续检索按当前事实再次消费，章节目录改变后不会继续用旧目录查询。
- 初次受理将规范范围保存到既有 `WorkflowRun.checkpoint.retrieval_scope`，任务状态更新合并原 checkpoint；后台和新执行器读取该持久值，不能依赖请求对象或从历史来源/题目标签反推。旧任务缺键默认空；已存在但损坏的范围明确失败，不能变为空条件。
- 进行中任务的显式范围必须与已保存范围语义相同才可复用：章/资料/知识点按集合比较，小节按闭区间比较。不同范围返回 `GRADING_TRIGGER_CONFLICT`，不覆盖旧范围、不创建第二任务、不再调度。真正省略范围保持原复用；明确 `{}` 是显式空范围，不能伪装成省略。
- Query 保存唯一章/节/标签输入；统一 `_filters` 解析为内部不可变 SQL scope，两路及融合/重排共用。Keyword-only 传带范围的 Query 时允许空 embedding，且不会调用 Embedding Provider；旧 primitive 调用保持原用法。
