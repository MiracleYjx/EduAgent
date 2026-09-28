# 出题来源与审核意见持久化契约（P4B.3）

## 写入与身份

AI 生成的每道题有一条 `question_generation_metadata`，每个被引用的知识片段有一条
`question_source_chunks`。来源行保存生成时的原始 `chunk_id`、`document_id`、`course_id`、
`source_order`、`content_snapshot`、`source_file`、`chunk_index`，以及链路提供时的可选
`retrieval_rank`、`score_kind`、`score_value`。`live_chunk_id` 仅用于活体片段关联，
不是历史身份；生成时引用必须存在且属于当前课程，否则整批拒绝，不静默跳过。

元数据记录实际 Provider 的 `provider_name`、`model`、可空 `model_version`、
`prompt_version`、`retrieval_mode`、`generated_at` 和请求标识。API 请求标识可为文本；
合法 UUID 原样入库，其他文本确定性映射为 UUIDv5，API 响应仍保留原始文本。

## 读取与状态

`GET /api/questions/{question_id}` 保留既有题目字段，增加：

- `sources_persisted: bool`：有已保存来源行时为 `true`。
- `source_status`：`persisted` 表示来源快照已保存；`history_unknown` 表示历史题没有
  生成元数据和来源记录，不能推断当时是否引用资料；`no_sources` 表示本次生成有元数据，
  但没有引用来源。
- `sources`：按 `source_order` 排序的来源快照列表；每项包含 `source_file`、
  `chunk_index`、`content_snapshot`、`source_order` 和 `source_deleted` 等展示字段，
  不暴露内部 `live_chunk_id`。无来源时为空列表。
- `revision_comments`：按 `commented_at`（同刻按记录 ID）升序的已保存审核意见。

生成响应中的每题 `sources_persisted` 仅在有引用且全部落库后为 `true`，
并以 `source_status=no_sources` 标识当次无引用。批次 `sources_persisted` 仅在每题均有
已保存引用时为 `true`；不能以批次布尔值反推某一道题的来源状态。历史题的
`history_unknown` 由详情读取给出，不能从一次性生成响应推断。

审核响应中的 `comment_persisted=true` 表示本次提供的非空意见已与题目状态一同落库；
`false` 表示本次没有意见，不等于持久化失败。退回修订必须提供非空、不超过 2000 字的
意见；审核通过可不提供意见。多轮审核意见追加记录，不覆盖旧意见。

## 删除与事务边界

删除 `DocumentChunk` 时，其关联来源的 `live_chunk_id` 置为 `NULL`，
`content_snapshot`、原始 `chunk_id` 与来源文件信息保留，UI 显示
“来源已删除，保留历史依据”。删除 `Question` 时来源、元数据和意见按外键级联删除。

生成事务包含整批 Question、来源快照和元数据：任一写入失败则整批回滚。
审核事务包含单题状态变更和本次意见：任一写入失败则同时回滚。
详情接口在教师授权范围内读取；UI 列表、选中和审核后均重新读取详情，不以临时响应
替代持久化事实，且仅展示最多 500 字的来源正文摘要。
