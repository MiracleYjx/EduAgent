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

## v2.0 改编与导入来源

本节对应 [spec.md](../spec.md) FR-041、FR-043、FR-046、FR-049；实体引用 [data-model.md](../data-model.md) §7.1–§7.5、§8.2、§9。
上文 QuestionSourceChunk 的引用快照、Provider 真实身份、事务及 history_unknown/no_sources 语义保持不变；以下为待实施的增量关系。

### 原题改编的父题来源

~~~text
Question（派生题）
  -> QuestionSourcePaper.derived_question_id
  -> QuestionSourcePaper.source_question_id
  -> Question（父题）
~~~

- QuestionSourcePaper 是父子题关系，不是原试卷文件表。字段为 id、derived_question_id、source_question_id、adaptation_type、created_at；关联真实 Question，不能以文件 ID 代替父题 ID。
- 唯一约束 (derived_question_id, source_question_id)；两题同课程、不可自引用、来源图无环。允许多个真实父题，不重复登记同一父子对。
- adaptation_type 仅 rewrite / translate / extend，表示真实改写/翻译/扩展；不得为手工独立题补一个虚假父题。
- 新改编候选与父题来源同事务保存；任一来源无效、跨课程或形成环则整批拒绝。并发维护同课程来源图先锁定 Course 再核对，沿用模型规则，不引入独立锁服务。
- 父题不被覆盖；source_type=adapted 的新题必须至少一条有效父题关系，且重新核验答案、选项、图片条件、解析与 Rubric，教师批准后才可发布。
- 教学资料仍由派生题自己的 QuestionSourceChunk 保存本次真实引用。父题来源不等于教学依据，不能把父题快照或历史未知材料冒充本次实际检索 Chunk。
- 不自动复制父题的批准状态/核验结论；父题待补全、缺依据或已删除活体资料不能作为改编题自动通过理由。
- 批准/发布后不能原地更换父题关系改写来源；受保护父题/关系不得硬删除，Closed/Archived 不解除历史保护。

### 试卷导入来源

正式题的唯一导入关联采用已确认的 ExtractedQuestion.question_id；derived_question_id 只用于上述 QuestionSourcePaper，不新增同义外键。

~~~text
Question（source_type=paper_imported）
  <- ExtractedQuestion.question_id（唯一、Corrected 时非空）
  -> ExtractedQuestion.paper_import_id
  -> PaperImport.document_id -> Document（purpose=paper_source）
  -> ExtractedQuestion.source_page_ids -> SourcePage

Question -> QuestionAsset -> SourcePage（及真实原图/region）
~~~

- 一个 Corrected 暂存题唯一对应一个正式原题；Question 通过 imported_extracted_question 反向关系访问，不重复存另一份导入外键。
- ExtractedQuestion 的 source_page_ids 是 JSON UUID 数组；同一题可跨多页，同页可供多题使用。服务核对页确属同次导入及课程，不能声称 JSON 元素已有数据库外键。
- 原卷通过同课程 PaperImport/Document 保存；原文件路径只读投影 Document.storage_path。纸卷来源与知识 Chunk 来源分开，不自动生成 Embedding/伪造知识引用。
- 教师校正确认后，正式 Question、原页/题图关联、ExtractedQuestion.question_id 及 Corrected 同事务提交；重复 commit 不重复建题，遵循 [paper-import.md](paper-import.md)。
- 可无答案入库为 Draft/待补全；入库成功、PaperImport.Ready 和题目审核批准互不等同。确认后的导入记录不因正式题编辑反向覆盖。
- 改编后沿父题图追溯父题原卷/页图；若有多个父题分别展示，不把派生题伪装成直接由同一暂存题导入。
- 图片位置/原图身份保持；授权访问遵循 [file-storage.md](file-storage.md)，学生不能因来源追溯取得含答案整卷。

### source_type 分类与兼容读取

| source_type | 分类语义 | 新记录要求 |
| :--- | :--- | :--- |
| manual | 教师独立手工创建 | 不凭空建立导入/父题/AI 调用来源；引用资料时保留真实引用 |
| ai_generated | 依据知识库生成的新题 | 保存本次真实生成元数据与资料引用；依据不足明确反馈，不造来源 |
| paper_imported | 教师确认的试卷原题 | 有唯一 Corrected ExtractedQuestion 及原卷/原页关系；OCR/LLM 辅助解析不改变来源分类 |
| adapted | 基于已有原题创建的新候选 | 有有效 QuestionSourcePaper 父题，手工或 AI 改编均保留真实关系和本次资料依据 |
| null（历史） | 既有记录无可靠类型证据 | 保留未知，不一律回填 manual 或凭内容/模型名称猜测类型 |

- source_type 表示题目的来源路径，不代表审核通过、内容可靠或来源记录全部完整；新记录由服务按真实创建路径确定，客户端不能任意改写分类掩盖来源。
- 题型/内容维护不原地转换来源身份。要改编另建子题，补充答案不把导入原题改标 ai_generated。
- 既有 sources、sources_persisted、source_status 继续仅表示 QuestionSourceChunk 教学引用，不能因有父题/原卷就改为 persisted。
- v2.0 详情增加 source_type、parent_sources、paper_source；parent_sources 含真实父子关系/改编类型，paper_source 为当前题直接导入关系或 null，祖先导入分别随父题追溯。
- 来源不存在/未知/文件缺失分别显示，不造原卷或页号。history_unknown 不表示不存在导入来源，也不允许用新父题关系改写旧的知识引用未知状态。
- 详情只返回授权来源描述/文件标识，不暴露本机路径；记录存在且文件丢失仍保留来源关系与 FILE_MISSING 诊断。

### 删除、持久化与验证边界

- DocumentChunk 删除仍仅使 live_chunk_id 置空，引用快照保留；这是教学引用生命周期，不删除父题关系或原卷来源。
- 对允许删除的未受保护题，上文来源/元数据/意见的既有级联保持；新增父题/导入/原页关系采用模型约束，存在引用则不能绕过 RESTRICT/发布保护硬删。
- 文件清理需核对全部引用；共享原图不能随一条题图关系删除。受发布/历史保护题内容与来源规则引用 [exam-assembly.md](exam-assembly.md)。
- 校正与批准、来源与核验、导入与知识摄取各自保留真实状态；不把事务失败处理成“未引用资料”的正常结果。
- 验证同课程/无环/重复关系、并发来源图、跨页追溯、重复入库、历史未知及教学快照删除语义；本步骤不修改代码、模型、迁移或测试。
