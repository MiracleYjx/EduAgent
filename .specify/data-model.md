# EduAgent 数据模型与状态设计

**Feature**: [spec.md](spec.md)

**Plan**: [plan.md](plan.md)

## 1. 核心业务实体

### User 与 Role

- `User`：平台用户，关联身份信息和一个或多个角色。
- `Role`：`Teacher`、`Student` 或 `Admin`，决定允许的操作。
- Admin 只管理用户、角色和运行状态，不参与 AI 出题审核或 AI 阅卷复核。

### Course、KnowledgeBase、Document、DocumentChunk

- `Course`：教师管理的课程，关联知识库、题目和考试。
- `KnowledgeBase`：与课程绑定的可检索教学内容集合。
- `Document`：教师上传的 PDF、TXT 或 Markdown 资料，记录来源和处理状态。
- `DocumentChunk`：资料切分后的知识片段，至少包含：
  `id`、`course_id`、`document_id`、`content`、`metadata`、`embedding`、
  `search_vector`、`created_at`。

关系：

```text
Course 1 ── 1..N KnowledgeBase
KnowledgeBase 1 ── 1..N Document
Document 1 ── 1..N DocumentChunk
```

每个 `DocumentChunk` 必须能够回溯到课程和原始资料，以便出题和阅卷引用上下文。

### Question 与 Exam

`Question` 至少包含：

```text
id
course_id
type
content
options
reference_answer
scoring_rubric
difficulty
knowledge_points
score
status
created_by
```

支持的题型标识为：

```text
SINGLE_CHOICE
MULTIPLE_CHOICE
TRUE_FALSE
FILL_BLANK
SHORT_ANSWER
ESSAY
```

MVP 优先实现 `SINGLE_CHOICE`、`TRUE_FALSE` 和 `SHORT_ANSWER`。AI 生成的题目必须先
处于候选或待审核状态，只有教师审核通过后才能用于正式题库和考试。

AI 出题的持久化附属表（迁移 `0011_question_source_persistence`，历史题不回填）：

- `QuestionSourceChunk`（`question_source_chunks`）：多行对应一个 `Question`，
  `question_id → questions.id` 级联删除。保存原始 `chunk_id`、`document_id`、
  `course_id`、`source_order`、`content_snapshot`、`source_file`、`chunk_index`，
  可选 `retrieval_rank`/`score_kind`/`score_value`。`live_chunk_id → document_chunks.id`
  在资料片段删除时置空，但原始身份和正文快照保留。`(question_id, chunk_id)`、
  `(question_id, source_order)` 唯一；索引 `(course_id, question_id)`。
- `QuestionGenerationMetadata`（`question_generation_metadata`）：与 `Question` 一对一，
  `question_id` 同时为主键和级联删除外键。记录 `request_id`、`prompt_version`、
  `provider_name`、`model`、可空 `model_version`、`retrieval_mode`、UTC `generated_at`。
  非 UUID 请求标识按固定命名空间映射为 UUIDv5，API 仍回显原文。
- `QuestionRevisionComment`（`question_revision_comments`）：与 `Question` 一对多，
  `question_id` 级联删除，`commented_by → users.id`；`comment` 非空且不超过 2000 字，
  `commented_at` 为 UTC 时间。索引 `(question_id, commented_at)`；每轮退回追加新行。

生成时题目、来源和元数据同事务提交；审核时状态和意见同事务提交。详情读取遵循教师归属权限。

`Exam` 关联一个课程和多道已审核题目，并定义学生可参加的状态或范围。

### Submission 与 Answer

- `Submission`：学生对一次考试的提交记录，关联学生、考试和提交状态。
- `Answer`：学生针对单道题的答案，关联题目、提交和评分结果。

提交必须能追踪到每道题的答案处理状态，支持多题阅卷 Workflow 推进和失败恢复。

## 2. AI 与评测实体

### GradingResult

`GradingResult` 记录单道题的客观或主观评分结果，至少包含：

```text
answer_id
question_type
score
max_score
reason
correct_points
missing_knowledge_points
suggestions
confidence
validation_status
review_status
retrieved_context_ids
```

主观题结果只有在结构化解析和 Schema 校验通过后，才能进入统一成绩或诊断报告。
客观题结果必须记录确定性规则评分来源，不得依赖 LLM 意见。

### DiagnosisReport

`DiagnosisReport` 根据考试最终评分生成学生学习反馈，至少包含：

```text
submission_id
student_id
mastery_by_knowledge_point
weak_knowledge_points
error_reasons
learning_suggestions
status
```

诊断报告只能使用已接受或已人工复核的最终评分。

### ReviewRecord

`ReviewRecord` 记录教师对低置信度评分的确认或修改：

```text
grading_result_id
reviewer_id
original_score
final_score
decision
comment
created_at
```

`reviewer_id` 必须对应 Teacher；Admin 不得替代教师完成 AI 业务复核。

### AgentRun 与 WorkflowRun

- `AgentRun`：记录 Agent 类型、输入摘要、输出校验状态、模型或 Provider 标识、耗时、
  错误和关联 Workflow。
- `WorkflowRun`：记录 `workflow_id`、`submission_id`、当前节点、当前答案、状态、重试
  次数、暂停原因和最终结果引用。

## 3. 检索数据与逻辑

每个 `DocumentChunk` 同时持有：

- `embedding`：用于 `pgvector` 语义近邻检索。
- `search_vector`：用于 PostgreSQL 全文关键词检索和 GIN 索引。
- `metadata`：用于课程、资料、章节或其他来源过滤。

检索结果至少包含：

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

检索必须保留 `chunk_id`，以便评分结果和 Agent Trace 引用最终上下文来源。

## 4. 状态转换

### Question 状态

```text
Draft
  ↓
Candidate Generation
  ↓
Pending Review
  ├── Approved -> Published / Available for Exam
  └── Needs Revision -> Candidate Generation
```

未经过 `Approved` 的题目不能发布、组卷或对学生开放。

### Document 状态

```text
Uploaded
  ↓
Parsing
  ↓
Chunking
  ↓
Embedding
  ├── Ready
  └── Failed
```

空文件、损坏文件、不受支持格式、无法解析文本或没有有效知识片段时必须进入 `Failed`
或明确的不可用状态，不得伪装成可检索知识库。

### GradingResult 状态

```text
Pending
  ├── Objective -> Accepted
  └── Subjective
          ↓
      Validated
          ↓
   Confidence Check
      ├── High -> Accepted
      └── Low -> Pending Review
                    ├── Confirmed -> Final
                    ├── Modified -> Final
                    └── Re-grade -> Validated
```

结构化校验失败不得进入 `Accepted` 或 `Final`；可重试时回到评分节点，否则进入明确的
失败状态并保留错误信息。

### WorkflowRun 状态

```text
Queued
  ↓
Running
  ├── Paused: Pending Review
  ├── Failed
  └── Completed
```

教师完成复核后，`Paused` 的 WorkflowRun 必须能够恢复到 Reviewer/Re-grade 后续节点。

## 5. 关键校验规则

- `course_id` 必须贯穿知识库、资料、知识片段、题目、考试和评分引用链。
- `embedding` 和 `search_vector` 必须属于同一个知识片段内容版本。
- `fusion_score` 只能由语义和关键词候选计算，`rerank_score` 只能在候选合并后产生。
- Objective 题的评分结果必须可由学生答案、标准答案和题目分值重算。
- Subjective 题必须具备题目、标准答案、评分标准、学生答案和课程上下文。
- 评分结果的 `score` 不得小于 0 或大于 `max_score`。
- `confidence` 必须位于 0 到 1 范围内；低于配置阈值时必须进入人工复核。
- 诊断报告不得引用未通过结构化校验或仍处于待复核状态的评分。

## 6. v2.0 扩展数据模型（2026-10-01）

本节及后续扩展只约束 v2.0；上文第 1–5 节作为 v1.0 基线逐字保留。设计依据为 [spec.md](spec.md) 的 FR-041 至 FR-052、SC-010 至 SC-014 和 [plan.md](plan.md) 的 §8–§14、数据模型引用。以下是目标模型，不表示当前代码/数据库已经有这些字段；本次不创建模型代码、迁移、契约或任务。

用户本轮确认：发布即冻结，Submission/历史结果继续保护；允许补充必要关联和发布依据；要点独立四舍五入，尾差由教师确认，不自动分配。

### 通用类型与责任边界

- 新实体主键为应用生成 UUID，非空；日期使用 UTC、PostgreSQL TIMESTAMPTZ。created_at 默认为真实创建时间；有 updated_at 的实体仅在成功提交实际修改时更新。
- 枚举沿用现有“字符串值 + 数据库 CHECK + DTO 校验”的约束方式。引用不存在、越权、跨课程或状态不合法必须明确失败；NULL 表示未知/尚未获得，不能以 0、空字符串或虚假来源代替。
- 文件定位统一为应用持久根目录下的相对路径（最长 1024 字符），不是临时路径或客户端可直接访问的 URL。文件服务负责读写与业务授权；迁移只改变文件定位，不改变原页/题图身份。文件缺失显式报告，不借改写导入成功历史掩盖丢失。
- 下文 PK、FK、UNIQUE 和单行范围 CHECK 属于数据库约束；“同课程/页面属于本次导入/无循环/教师归属/发布冻结”等跨行规则由相应服务在事务内校验。不能把 JSON 数组内的 UUID 误称为已经具有逐元素外键。
- 新增来源与发布依据的外键采用 RESTRICT，存在有效引用时不得硬删除。此规则针对 v2.0 新关系，不改写 v1.0 已有外键；文件清理还须确认没有其他业务引用，不能因删除一条关系就删掉共享原图。
- page_count 上限 50、每题资产最多 5 张沿用已确认准入限制，不声明性能已达标。索引以下列出的访问路径为限；UNIQUE 已提供的索引不重复建立。

## 7. 六个新增 / 升级实体

### 7.1 PaperImport（paper_imports）

用途：独立试卷导入任务，与知识库摄取分开；一个任务对应一份持久原文件，关联 Course、上传者 User、SourcePage 和 ExtractedQuestion。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 试卷导入任务身份 |
| course_id | UUID；非空；FK Course.id | 所属课程 |
| uploaded_by | UUID；非空；FK User.id | 上传教师；课程归属由服务校验 |
| document_id | UUID；非空；UNIQUE，FK Document.id | 必要新增关联；必须指向同课程、purpose=paper_source 的原文件 Document |
| original_filename | String(255)；非空 | 原文件名的来源记录；不作为存储地址 |
| original_file_path | String(1024)；非空；只读关系属性 | 映射关联 Document.storage_path，不重复存储第二个可变路径；Document 是原文件定位唯一事实源 |
| page_count | Integer；可空；无默认 | 解析前未知；已知时 1–50，进入 Pending Review/Ready 时必须确定 |
| status | String(32)；非空；默认 Uploaded | 见下述独立导入状态机 |
| error_code | String(64)；可空 | 失败时为非空明确错误码 |
| error_message | Text；可空 | 失败步骤和可处理原因；不得包含凭据 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 创建时间 |
| updated_at | TIMESTAMPTZ；非空；当前 UTC | 成功状态/内容变更时间 |

索引/约束：UNIQUE(document_id)；索引 (course_id, created_at)、(course_id, status)。上传者归属、Document 用途及文件可访问性在创建事务中核对；原文件可靠保存后才能记录 Uploaded。页面与暂存题分别通过 paper_import_id 关联，不能在业务层猜测来源。

状态枚举：Uploaded、Parsing、Extracting、Pending Review、Ready、Failed、Rejected。Rejected 按本轮最后的状态图纳入，表示教师拒绝整份导入，与技术失败区分。

```text
Uploaded -> Parsing -> Extracting -> Pending Review -> Ready
     任一活动处理阶段 -> Failed       Pending Review -> Rejected
```

- 正常前进只允许相邻状态；Uploaded/Parsing/Extracting/Pending Review 遇处理或持久化失败可进入 Failed，保存错误码、步骤和可用原文件/中间结果。
- Pending Review 可逐题校正/确认；只在全部暂存题为 Corrected（已关联正式 Question）或 Rejected，且至少一题成功入库后进入 Ready。全部题被拒绝，或尚无 Corrected 记录时教师拒绝整卷，进入 Rejected，不伪称 Ready；已有确认入库题时仅能处置剩余暂存题，不能借整卷拒绝删除正式题。
- Ready、Failed、Rejected 是该任务终态，不增加无界重试或 Ready 后回退。要重新处理，创建新的导入尝试并保留旧记录；已存在正式题不得被再次确认动作重复创建。
- Ready 表示导入校正确认完成，不表示所有题有答案或已审核。无答案题入库为 Draft/待补全；Uploaded、Document 原文件 Ready 都不等于题库入库完成。
- Ready 时页号覆盖 1..page_count，页图/来源可访问，非拒绝题均有已确认来源；一次失败不能被新状态掩盖为完成。

### 7.2 SourcePage（source_pages）

用途：原试卷页图。PaperImport 1:N SourcePage；ExtractedQuestion 可引用多页，同页可被多题引用。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 来源页身份 |
| paper_import_id | UUID；非空；FK PaperImport.id | 所属导入 |
| page_number | Integer；非空 | 1 开始；必须不超过已确定 page_count |
| image_path | String(1024)；非空 | 持久原页图相对定位 |
| width | Integer；非空 | 原页像素宽度；>0 |
| height | Integer；非空 | 原页像素高度；>0 |
| ocr_text | Text；可空 | 真实 OCR 文本；未走 OCR 时为 NULL，不将 PDF 文字层伪称 OCR |
| ocr_confidence | Numeric(5,4)；可空 | 真实可获得置信度规范至 [0,1]；未知为 NULL，不表示实际准确率 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 原页记录创建时间 |

索引/约束：UNIQUE(paper_import_id, page_number)，同时提供按导入/页号查询索引，不另建重复索引；CHECK page_number>=1、width>0、height>0、ocr_confidence 在 [0,1] 或 NULL。纸张/页图与所属课程继承 PaperImport 授权。

无独立 status 字段或状态枚举：页图是来源资产，生成/识别进度属于 PaperImport；OCR 缺失不是零置信度，空白页不自动视为解析失败。经题目校正确认或发布引用后，页号、原页身份与图像内容不可原地替换；如需新来源创建新导入/页记录。文件迁移可改变定位，但必须保持同一原图与引用。

### 7.3 ExtractedQuestion（extracted_questions）

用途：已拆出的暂存原题，对应规格 ImportedQuestionDraft 的概念；人工校正备注不冒充独立完整审计日志。PaperImport 1:N ExtractedQuestion；与 SourcePage 的多对多关系以 source_page_ids 为唯一存储表达。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 暂存原题身份 |
| paper_import_id | UUID；非空；FK PaperImport.id | 所属导入 |
| source_page_ids | JSONB UUID 字符串数组；非空；默认 [] | 跨页原题的来源页；确认时非空、去重，按 page_number 顺序排列 |
| question_type | String(32) 题型枚举；可空 | 未可靠识别可为 NULL；Corrected 前须为有效题型 |
| content | Text；可空 | 原题题干；Corrected 前非空且完成核对 |
| options | JSONB 对象/数组；可空 | 保留已有题型选项形状和顺序；选择题确认前必须完整 |
| reference_answer | Text；可空 | 可没有答案，保持待补全，不能假造标准答案 |
| scoring_rubric | Text；可空 | 原评分标准；缺失主观题标准则保持待补全 |
| score | Numeric(8,2)；可空 | 原分值未知可为 NULL；已知 >0；转正式题前由教师明确正数分值 |
| status | String(32)；非空；默认 Extracted | 见下述校正状态机 |
| correction_notes | Text；可空 | 人工校正/拒绝理由，不以备注替代正式答案或结构化字段 |
| extracted_by | String(16)；非空 | OCR、LLM；确定性文字 PDF 提取记 TEXT，真实记录结构化内容生产者 |
| extraction_confidence | Numeric(5,4)；可空 | [0,1] 或 NULL；不将模型自报置信度当成校正通过证明 |
| question_id | UUID；可空；UNIQUE，FK Question.id | 必要新增：确认入库后的正式题；同一暂存题只创建一次 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 暂存题创建时间 |
| updated_at | TIMESTAMPTZ；非空；当前 UTC | 校正/确认的成功变更时间 |

索引/约束：索引 (paper_import_id, status)；UNIQUE(question_id) 在非 NULL 时保证一条正式原题只绑定一个导入暂存题；CHECK JSON 数组类型、允许题型/提取方式、置信度 [0,1]、score>0（或 NULL）及金额精度。source_page_ids 不另维护一份可分叉的关联表；服务确认每个 UUID 都是同一 PaperImport 的真实 SourcePage，不能用跨导入页补位。JSON 元素无 SQL FK，删除被数组引用的页也必须在同一服务边界拒绝。

```text
Extracted -> Pending Correction -> Corrected -> 正式 Question（Draft）
                         \
                          -> Rejected
```

- 枚举只有 Extracted、Pending Correction、Corrected、Rejected；Rejected 是 Pending Correction 的拒绝分支，不是 Corrected 的正常后继。暂存题经过结构化校验后进入 Pending Correction。
- Pending Correction 可反复编辑并维持状态；教师核对题型、题干、选项、原页/跨页/图片关联和分值后才可 Corrected。无答案仍可确认入库，但正式题保持待补全，不能 Approved。
- Corrected 与 Question 创建/关联同事务提交。Corrected 必须 question_id 非 NULL，其他状态必须 NULL；失败整体回滚，重复确认返回既有 Question，不创建重复正式题。
- Corrected、Rejected 终态不原地改写；拒绝须有 correction_notes。之后编辑正式题走 Question 审核/冻结规则，不反向覆盖原导入校正依据；新的改编另建候选。
- source_page_ids 只表达页来源，页中题号/边界、核验及图片条件的接口 DTO 后续在契约定义，不把 OCR 文字、未知答案或备注直接当成已批准题。

### 7.4 QuestionAsset（question_assets）

用途：题目的原始图示/表格资产；Question 1:N QuestionAsset，SourcePage 1:N QuestionAsset。每条关系明确归属题目，改编可用新关系复用同一持久原图。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 题图关系身份 |
| question_id | UUID；非空；FK Question.id | 对应题目 |
| asset_type | String(16)；非空 | figure、table、diagram |
| file_path | String(1024)；非空 | 持久原图/裁图定位；与原页原图关系保留 |
| width | Integer；非空 | 资产图片像素宽度；>0 |
| height | Integer；非空 | 资产图片像素高度；>0 |
| caption | Text；可空 | 图示说明；不能将不可靠推断冒充原题条件 |
| student_visible | Boolean；非空；默认 false | 教师显式核对是否允许学生展示；不是题目/课程访问授权，不允许借此开放源卷或完整原页 |
| source_page_id | UUID；可空；FK SourcePage.id | 可追溯原页；原题导入必须有真实原页，不伪填历史未知来源 |
| region | JSONB；可空 | 形如 {bbox:[x0,y0,x1,y1]}，在 source_page 原图坐标系表示裁图区域 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 资产关系创建时间 |

索引/约束：索引 question_id、source_page_id；CHECK asset_type 枚举、width/height>0、region 为对象或 NULL。有 bbox 时需 source_page_id 非 NULL，四个坐标满足 0<=x0<x1<=页宽、0<=y0<y1<=页高；同课程和页尺寸由服务校验。引用整页图可 region=NULL，不能捏造裁图框。

无独立状态枚举：是否可修改继承 Question 的 Approved/发布保护；文件是否缺失由文件服务显式报告，图像条件是否可靠由结构化核验/人工校正表达，不靠 caption 或创建成功判断理解完成。新增/替换/删除资产、修改 bbox/图示条件须重核验题干、答案、解析和评分标准；Approved 内容守卫扩展到题图，受发布保护时禁止变更题图集合、图像内容、关联或覆盖原文件。每题最多 5 张由事务内锁定题目后确认计数，不能用单行 CHECK 伪称已限制集合大小。

T154 已确认展示决策：QuestionAsset.student_visible 默认关闭（Python 与数据库均为 false，旧记录也保持 false），教师在合法校正/修订阶段显式开关，T159 提供校正 UI。学生 DTO 与文件 GET 均须同时满足 true、现有开放考试/分配/时间窗或本人已提交答卷的真实题目关联，以及允许展示区域校验；false 不能返回资产标识或字节。教师读取始终看到本课程全部题图，学生不能改开关。

源卷/完整原页仍仅教师可读；完整原页引用（region=null 或完整页框）及其已登记相同字节/定位别名不得凭 true 改作学生题图。需建立仅含本题允许信息的真实裁图；裁切本身不证明无答案，教师显式 true 表示已核对可展示。未关联原页的独立题图同样默认关闭；已知源卷/原页的字节别名仍被拒绝。展示开关是授权意图，不是 G05 理解/人工核对记录，不凭开关生成核对事件或批准题目；单独切换开关不改变图像上下文修订。修改/新建图像关联需重新显式确定展示许可并遵守既有 Approved/发布/历史保护。

### 7.5 QuestionSourcePaper（question_source_papers）

用途：原题改编父子关系，沿用用户指定名称；Question（子题）N:M Question（父题），不是原试卷文件表的替身。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 父子题来源关系身份 |
| derived_question_id | UUID；非空；FK Question.id | 新建改编候选，不能覆盖父题 |
| source_question_id | UUID；非空；FK Question.id | 真实来源父题 |
| adaptation_type | String(16)；非空 | rewrite、translate、extend |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 改编来源记录创建时间 |

索引/约束：UNIQUE(derived_question_id, source_question_id)；索引 source_question_id，派生题查询复用唯一索引前缀；CHECK derived_question_id != source_question_id 和改编类型枚举。两题同课程、父题真实存在、父子图无循环由服务在事务内检查；同课程变更父子来源关系时先锁定 Course，再核对父题图，防止并发插入不共享端点的关系形成环；不引入独立锁服务。

无独立状态枚举：关联随派生题候选创建，审核沿用 Question 状态；来源变化需重新核验并审核，批准/发布后关系不得原地更换以改写来源。source_type=adapted 的新题必须有父题关系。原试卷和页码经父题 -> ExtractedQuestion -> PaperImport/SourcePage 追溯；教学资料仍经 QuestionSourceChunk 追溯，不以父题关系伪造知识依据。父题缺答案/依据不能被视为改编题自动通过核验的理由。

### 7.6 ExamQuestion（exam_questions，原关联表升级）

用途：在原 exam_questions 上保留考试/题目身份，升级为带主键、题序、考试内分值的关联实体；不是另建一套重复关系。Exam 1:N ExamQuestion，Question 1:N ExamQuestion。

保留原 exam_id 外键的 ON DELETE CASCADE 和 question_id 外键的 ON DELETE RESTRICT；发布/历史考试的删除拒绝由服务端关系保护执行，不能把原 FK 行为改成全局禁止草稿删除，或误称数据库级联本身会识别发布状态。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 升级后的关联实体身份 |
| exam_id | UUID；非空；FK Exam.id | 本场考试 |
| question_id | UUID；非空；FK Question.id | 同课程、审核通过的题目 |
| order_index | Integer；非空 | 1 开始的显式题序 |
| score | Numeric(8,2)；可空（仅草稿） | 草稿 NULL 取 Question.score；发布事务必须落实为本场正数分值，以后不动态取题库默认 |
| base_score | Numeric(8,2)；可空（仅草稿） | 必要新增：发布采用的题库基准满分，>0；用于比例换算与解释 |
| published_knowledge_points | JSONB 字符串数组；可空（仅草稿） | 必要新增：发布时知识点归属；允许真实空数组，不能随题库元数据变化 |
| scoring_basis | JSONB 对象；可空（仅草稿） | 必要新增：经 Pydantic 校验/教师核对的本场评分规则与要点、舍入及尾差确认依据 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 关联记录创建时间 |

索引/约束：UNIQUE(exam_id, order_index)、UNIQUE(exam_id, question_id)；前一 UNIQUE 已提供 (exam_id, order_index) 索引，不重复建立；索引 question_id 支撑冻结引用查询。CHECK order_index>=1、score/base_score 正数且不超 999999.99（或 NULL），发布前题序必须连续为 1..题数。同课程、Question 已审核/补全、有效题图、组卷约束及发布字段完整性由服务校验。

无独立 status 字段：关联可变性从 Exam 状态/引用关系派生。草稿可调整题序、替换题目与本场分值；发布原子固定 score、base_score、published_knowledge_points、scoring_basis 及本场题序，任何一项无法确认均不能发布。发布后的关联冻结，Closed/Archived 不解冻；Submission、Answer、评分或复核等历史关系存在时不能通过删除考试/答卷绕过冻结。

scoring_basis 只保存本场已核对评分依据，不保存完整题干/选项/题图内容快照，不新增题目版本；题目内容和题图仍由下述服务端冻结保护。用于评分/复核的最大分值、比例基准及统计知识点只读取本场固定数据，不从题库维护结果重新推断。

scoring_basis 的最小结构（不是完整内容快照）：

| 属性 | 类型 / 要求 | 语义 |
| :--- | :--- | :--- |
| kind | objective / subjective | 与题型路由一致，客观题不调用 LLM |
| rounding_mode | 固定 ROUND_HALF_UP | 数值换算的确定性舍入规则 |
| points | 结构化数组；允许定性标准为空数组 | 每项保存稳定 key/说明、base_points、default_points、confirmed_points；金额为两位小数字符串，经校验转 Decimal |
| additive | Boolean | 明确要点是否可加总；不得对重叠/门槛 Rubric 猜测加总方式 |
| rounding_delta | 可空的两位小数字符串 | 本场满分减默认可加总要点之和，可为负；定性或不可加总时为 NULL |
| confirmation | 可空的结构化对象 | 含 teacher_id、confirmed_at、reason；有尾差时必须有真实教师确认及明确处置，不伪造确认 |

要点 key 唯一；数值为有限非负金额，base_points 不超过 base_score，default_points/confirmed_points 不超过本场 score；未知或定性要点不伪填 0。可加总标准须核对合计，confirmation 必须来自该课程有权限的真实教师，JSON 内的 teacher_id 不冒充已具有数据库外键。

score、base_score 和 published_knowledge_points 以上述关联列为事实源，不在 JSON 内另存可独立改写的副本。定性/非加总 Rubric 的本场对应方式须人工核对；来源仍为受冻结保护的 Question.scoring_rubric，不从自由文本解析得分核心字段。

## 8. 既有实体的 v2.0 扩展

### 8.1 Document：用途分离与知识库条件约束

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| purpose | String(32)；非空；v1.0 兼容默认 knowledge_base | knowledge_base 或 paper_source；新试卷创建时显式 paper_source |
| knowledge_base_id（调整） | UUID；按 purpose 条件可空；FK KnowledgeBase.id | knowledge_base 必须非空且同课程；paper_source 必须 NULL，不虚构知识库 |
| storage_path（复用） | String(1024)；沿用字段 | 文件定位唯一事实源；paper_source 创建 PaperImport 时必须非空且原文件已持久保存 |
| paper_import（关系） | 一对一反向关系；不新增重复 FK | 由 PaperImport.document_id 唯一关联 |

数据库条件 CHECK：
```text
(purpose = knowledge_base AND knowledge_base_id IS NOT NULL)
OR (purpose = paper_source AND knowledge_base_id IS NULL)
```

- knowledge_base 保留 v1.0 Parser -> Cleaning -> Chunking -> Embedding、Document 状态和课程检索来源；资料用途默认过滤仍为 knowledge_base，不能将试卷混入原知识库列表、检索或摄取。
- paper_source 为原文件元数据，不生成 DocumentChunk/Embedding；文字 PDF 优先文本提取、扫描页 OCR，再由 PaperImport 编排拆题和校正。只有扫描路径称为 OCR，不能要求所有文字 PDF 都走 OCR。
- paper_source 的 Document.status 只表达原文件保存/可用性：Uploaded -> Ready 或 Failed，禁止 Chunking/Embedding；原文件 Ready 不表示 PaperImport Ready，也不表示题目批准。导入处理阶段只写 PaperImport，避免两份流程状态成为互相矛盾的事实源。
- purpose 在被摄取、导入或题目/考试引用后不可直接转换；更换用途须创建对应新文档/流程。原文件定位迁移在 Document 更新，PaperImport.original_file_path 关系属性同步读到新定位，不做两份路径回填。
- 原课程/知识库实体及引用保持；仅 paper_source 扩展允许 knowledge_base_id=NULL，不能削弱教学文档的非空知识库约束。新增索引 (course_id, purpose)；跨表课程相等及 paper_source 禁止产生 Chunk 的校验属于写入服务，不能误标为普通 CHECK 已执行。

### 8.2 Question：来源、导入关联与批准时间

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| source_type | String(32)；新题非空，历史未知可 NULL | manual、ai_generated、paper_imported、adapted；只有真实证据支持时回填历史分类 |
| frozen_at | TIMESTAMPTZ；可空 | 当前一次 Approved 的真实批准/内容冻结时间；不能以迁移执行时间伪造历史批准时间 |
| imported_extracted_question（关系） | 0..1 反向关系；不新增重复 FK | 通过 ExtractedQuestion.question_id 的唯一关联回溯原试卷/页码 |

新题显式由入口确定 source_type：人工创建 manual，知识库新题 ai_generated，导入确认 paper_imported，原题改编 adapted；分类不是教师审核状态。paper_imported 必须有 Corrected 暂存题，adapted 必须有 QuestionSourcePaper，manual/ai_generated 不伪造这些关系。历史 NULL 显示来源未知，不因缺少来源行就推断为 manual；保留 QuestionSourceChunk 的 persisted/history_unknown/no_sources 语义。导入确认的跨实体要求在同一事务提交前按最终一致状态验证，不能用循环创建要求阻止合法的 Question 与 Corrected 关联写入。

新流程 Approved 时，在审核事务内设置 frozen_at=该次真实批准时间；未受发布保护的题退回 Needs Revision 时清空当前 frozen_at，重新批准记录新时间。冻结拒绝实际以 status 和发布引用查询为准，不能以 frozen_at 非空作为唯一守卫。历史 Approved 缺少真实批准时间可为 NULL，但仍执行 Approved 守卫，不能因此开放内容更新。Question.score 继续使用 Numeric(8,2)，正数且最多两位小数；不新增吞掉非法金额的自动截断默认值。

## 9. 冻结生命周期、修订与分值换算

### 9.1 发布即冻结，历史引用继续保护

保护状态不新增 Question 布尔字段，通过关系查询计算：
```text
protected(question) :=
存在 ExamQuestion，且该 Exam 已 Published / Closed / Archived
或存在该关联的 Submission / Answer / 最终评分 / 复核等历史引用
```

当前考试状态流为 Draft -> Published -> Closed/Archived；Closed -> Archived，终态不能回 Draft。无需等到出现 Submission 才开始保护，Submission（包含答题草稿）和历史结果是继续保留保护的依据，而不是首次冻结开关。

- Approved 状态六类内容字段守卫及 QUESTION_APPROVED_IMMUTABLE 保留；退回修订前先查 protected。受保护时拒绝 Approved -> Needs Revision、内容/解析/答案/Rubric/分值、题图集合及父题来源的原地改写/删除，返回真实受保护原因，由后续契约定义具体错误码。
- 未受保护题：Approved -> Needs Revision -> Pending Review -> Approved；在 Needs Revision 编辑后必须重新核验和审核，不能通过只修改状态恢复旧核验为有效。Draft/Pending Review 的可编辑行为保留。
- 受保护题需要新的内容时，新建派生候选，保存 QuestionSourcePaper 父题关系，重新核验与批准；既有考试继续引用旧 Question/ExamQuestion，不创建完整题目版本或考试内容快照。
- difficulty、knowledge_points 的题库维护仍允许；历史统计读取 published_knowledge_points，评分读取固定本场分值/标准，不随题库元数据维护漂移。
- 保护持续至发布考试/历史依据的合法保留期结束并实际无保护引用；本版不新增自动失效/删除机制。Closed、Archived、撤掉界面入口或删除一个 Submission 都不能解冻。保留期内拒绝会破坏引用的物理删除，不以“无正在考试学生”替代“无历史引用”。
- 发布与题目退回/内容编辑、资产变更共用涉及 Exam/Question 的事务锁及固定顺序，检查引用与提交变更之间不得竞态放行；锁只服务于当前一致性约束，不另建锁服务。

### 9.2 Numeric(8,2)、ROUND_HALF_UP 与尾差确认

金额输入检查正数、两位小数及 Numeric(8,2) 范围；题库/考试满分上限 999999.99。计算使用 Decimal 和至少 28 位中间精度，先算基准要点 * score，再除以 base_score 后一次量化；不能先保存截断比例再乘要点，避免例如 0.03 * 1 / 6 = 0.005 的舍入边界失真。r 只说明比例语义，中间比例不先四舍五入到两位，也不用浮点字符串/正则替换 Rubric 中的数字。
```text
草稿有效分值 = ExamQuestion.score 若非 NULL，否则 Question.score
发布：保存 score=有效分值，base_score=当时题库满分
r = score / base_score（正数，保留中间计算精度）
默认换算要点 = quantize(基准要点分值 * score / base_score, 0.01, ROUND_HALF_UP)
评分结果金额 = quantize(依据本场标准得到的分数, 0.01, ROUND_HALF_UP)
```

- 明确数值要点须来自 Pydantic 校验后的结构化标准或教师核对，不能从自由文本猜权重；定性 Rubric 保留原语义及基准/本场满分比例，不凭空生成分值要点。
- 可加总要点的基准总和先核对题库满分，再逐项独立四舍五入。若换算合计有尾差，展示本场满分、默认要点、尾差；教师必须明确确认尾差如何处置并核对最终要点合计。未确认不能发布，不自动在某一要点补差或隐藏差异。
- 例如三个基准 1.00 分要点改为满分 10.00，默认 3.33/3.33/3.33，尾差 0.01；教师可明确选定一个要点调整为 3.34，确认后形成 3.34/3.33/3.33。该调整记录为教师处置，不宣称是自动四舍五入结果。
- scoring_basis 保存标准种类/已核对要点、默认换算值、教师确认值、ROUND_HALF_UP、尾差及确认教师/UTC 时间/处置理由；明确哪些要点可加总。发布时冻结，经教师确认的标准与本场 score 一并用于主观评分和复核，不能重新读取可变题库或二次乘比例。
- 客观题沿用确定性答案匹配，按本场 score 给分，不调用 LLM；主观题按本场已核对标准评分。分数必须在 [0,score]，非法或越界结果明确失败/待核对，不以裁切数值伪造评分成功。
- 整卷总分用有效本场分值/最终得分作 Decimal 求和；未完成、失败、依据不足、待复核不当作零分。统计分母使用同一固定满分与发布知识点；舍入和尾差处理不能改写既有考试结果。

## 10. 兼容迁移与后续边界（只定义，不执行）

- Document 老数据均为 v1.0 教学用途，purpose=knowledge_base；保留原知识库关系。历史 Question.source_type/frozen_at 只按真实证据填写，未知保持 NULL；Approved 保护不依赖补齐旧时间。
- exam_questions 原考试/题目对保留，新增 id 和 order_index；历史题序以当前 v1.0 的 Question.created_at、Question.id 排序为迁移基准，不声称随机数据库顺序就是原题序。如有真实更强历史顺序证据优先核对，不能改写已经确认的历史显示。
- 草稿新增字段可待发布时确定；已发布/历史考试必须盘点当时分值、标准、题图、知识点及成绩证据。能核对则固定本场依据，不能以当前题库值冒充历史值。未知场景显式报告并要求人工核对，不新增伪造分值/历史来源来通过迁移；在依据未收敛前不改写既有评分或宣告迁移完整。v1.0 历史读取仍保留真实记录；未核对的历史考试不能被声明为已满足 v2.0 发布依据或直接按新标准重评。
- 以上额外字段只是来源与稳定评分所需的最小关联/依据，未新增完整快照、版本、独立检索/锁服务，也未修改 v1.0 原实体条文。QuestionValidationResult、ManagedFile/BackupSet 等其余规格概念的契约/进一步模型设计不以本次六实体定义视为全部完成。
- v2.0 任务追加留给后续 /speckit.tasks；下一步同步六个新增契约及三个现有契约扩展。此文档定义必须在后续代码、迁移、契约和测试中实现并验收；如修改测试先形成 TCR，本次无测试内容变更。

## 11. T134：章节定位与知识点映射（G01，2026-10-01）

本节补齐 FR-024/FR-025 扩展、FR-046/FR-047 和 CHK001；承接 [plan.md](plan.md) §8/§9 及其中的 RAG 契约引用，查询合同见 [rag-retrieval.md](contracts/rag-retrieval.md)。只定义 v2.0 目标模型；第 1–5 节、现有六实体及 v1.0 来源快照语义保持原文。本批次按文件范围只更新数据模型与契约，plan 的既有链接由本节承接。

### 11.1 Chapter（chapters）：课程内的稳定章节身份

采用一个最小 Chapter 实体登记教师确认的课程章节；小节目录用受 Pydantic 校验的 JSONB 保存，不新增 Section 表、知识点表或独立章节服务。章节属于 Course，不专属于某一份 Document；同课程多份教材/讲义可经各自 Chunk 映射到同一章节，资料归属始终来自 Chunk.document_id。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 首次确认登记时由应用生成并持久化；改标题、文件名/路径变化和重摄取不重新生成 |
| course_id | UUID；非空；FK Course.id，RESTRICT | 章节的唯一课程归属，登记后不可原地转移到其他课程 |
| title | String(160)；非空 | 教师确认的真实章节名称；去首尾空白后非空；标题不是身份，不施加跨资料同名合并 |
| sections | JSONB；非空；默认 [] | 章内小节目录，Schema 为 [{section_order: 正整数, title: 非空字符串}]；[] 表示当前没有可用于小节范围查询的目录 |
| confirmed_by | UUID；非空；FK User.id，RESTRICT | 最近一次确认当前章节目录的真实教师，来自认证上下文 |
| confirmed_at | TIMESTAMPTZ；非空 | 最近一次确认当前目录的真实 UTC 时间，不由客户端指定 |
| created_at / updated_at | TIMESTAMPTZ；非空 | 沿用 §6 的真实创建/成功修改时间规则 |

- 数据库提供 PK、FK、sections 为 JSON 数组的 CHECK，以及 chapters(course_id) 索引；不因标题相同强制身份相等。目录内 section_order 必须为严格整数（不接受 bool），按真实小节顺序连续为 1..N、唯一；title 按上述非空规则校验。JSON 元素 Schema/顺序与教师课程权限由知识库服务在写入边界校验，不宣称 JSON 元素具有数据库 FK。
- section_order 是教师确认的课程章节目录序号，不是页码、chunk_index、解析器 section_index 或标题字符串排序；源文件的“3.2”等原编号通过真实标题/定位映射到目录序号，不能直接当整数写入。
- 首次摄取只能提出章节/目录候选；教师选已有 Chapter.id 或确认新登记后才建立可信映射。同名、同一标题层级或模型判断不能自动合并不同章节；不同资料映射到同章/同节须由教师明确确认。
- 标题修正且小节含义不变时保留 id/section_order；目录插入、重排、合并或边界改变时，教师须同步核对受影响 Chunk。原映射不能继续冒充新序号：在同一事务内据真实证据重映射，或清空受影响 section_order，并由本次教师操作重新记录仍可信章级定位的确认；若章节归属也未核对，则一并清空 chapter_id 与定位确认，待重新确认。独立知识点记录不因此被改写。
- Chapter 被活体 Chunk 引用时禁止硬删除；不得用改章节课程绕过同课程约束。既有 Document/Chunk 删除及 QuestionSourceChunk.live_chunk_id 置空后保留历史快照的规则继续适用。

关系：

~~~text
Course 1 ── 0..N Chapter
Chapter 1 ── 0..N DocumentChunk（每个 Chunk 为 0..1 Chapter）
Document 1 ── 0..N DocumentChunk ── 0..1 Chapter
~~~

Chapter 与知识点不新增关联表；章内知识点选项从同章、就绪教学 Chunk 的已确认 metadata.knowledge_points 去重汇总，筛选仍逐个 Chunk 匹配，不能把同章其他片段自动当作已标注该知识点。章节可以已登记但尚无可用资料；不以 Chapter 存在宣称有检索依据。Document 与 Chapter 的多对多资料覆盖关系由 Chunk 派生，不增加第二份独立可写关联。

### 11.2 DocumentChunk 增量字段、约束与最小索引

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| chapter_id | UUID；可空；默认 NULL；FK Chapter.id，RESTRICT | 可信的课程章节映射；NULL 为尚未定位，不能填随机/虚构章节 |
| section_order | Integer；可空；默认 NULL | 当前 Chapter.sections 中的正整数序号；NULL 为尚无可信小节定位 |

- 单行 CHECK：section_order IS NULL，或 section_order > 0 且 chapter_id IS NOT NULL。Chapter 与 Chunk、Document、KnowledgeBase 的课程相等、Document.purpose=knowledge_base，以及非空 section_order 确属当前目录，由知识库服务在同一写入事务内校验；检索层消费该已确认映射。
- chapter_id 已知、section_order 未知是合法状态：可命中章级条件，不能命中小节区间；两者都未知的 Chunk 仍可用于未指定章/节的原检索。未知章节不妨碍独立确认知识点。
- 新增 B-tree 索引 ix_document_chunks_course_chapter_section(course_id, chapter_id, section_order)，前缀覆盖课程/章节过滤；保留原 document_id/chunk_index 唯一约束、来源列及 pgvector/HNSW、tsvector/GIN 索引。
- metadata 沿用现有 JSON 列和 ORM 属性 chunk_metadata；章/节以新增列为唯一可写事实，不在 metadata 再保存可独立修改的 chapter_id/section_order。标签在 SQL 使用 metadata::jsonb 投影，不强制转换整列或新增标签 GIN 索引；本任务不承诺查询性能。

### 11.3 Chunk 元数据标签与教师确认 Schema

保留 original_filename、location、section_index、start_char/end_char 等原来源键；新增以下键，其他 v1.0 元数据不受此局部 Schema 限制：

| JSON 路径 | Schema / 空值 | 语义 |
| :--- | :--- | :--- |
| knowledge_points | list[str] / null；历史允许缺键 | 当前 Chunk 的真实规范知识点标签；缺键/null 为未知，[] 为确认后无标签 |
| scope_confirmation.location | {confirmed_by: UUID, confirmed_at: UTC ISO-8601} / null；允许缺键 | 当前非空章/节定位的教师确认；不重复保存定位值 |
| scope_confirmation.knowledge_points | 同上 / null；允许缺键 | 当前非 null 标签数组的教师确认；与定位确认独立 |

- 标签写入与查询在各自输入边界采用同一规则：去首尾空白，拒绝空白/非字符串元素，按规范值去重；保留字符、大小写及内部空白，不做同义词、子串、Unicode 形式或内容关键词推断。教师依据当前课程资料明确选择标签；本轮不新增知识点字典实体。
- 模型提议只供教师核对，不能直接写成可过滤事实。确认记录由已认证、具有该课程管理权限的教师操作产生；身份和时间不可由客户端伪造。标签值或定位值与对应确认记录同事务保存，保留原文定位供核对；不得只有 confirmed=true 而无真实确认者/时间。
- 新写入的可信章/节与标签必须有对应确认记录；仅确认章节但小节仍未知时保留 section_order=NULL。修改该维度的内容/映射后需重新确认；未经确认的新候选不替换当前事实。原正文/真实边界变化使相关旧事实不再成立时，先清除受影响章/节或标签和对应确认记录，不能继续使用旧确认；原始来源与已保存题目引用快照不改写。
- 历史缺标签不批量填 []；既有同名标签键若无真实确认记录也仍属未确认，不能在启用新过滤时自动升级为可信标签。不把 Question.knowledge_points、文件名或当前模型猜测当作历史 Chunk 标签。历史异常形状须报告并核对，不能静默转成合法空数组。

~~~json
{
  "knowledge_points": ["牛顿第二定律"],
  "scope_confirmation": {
    "location": {
      "confirmed_by": "22222222-2222-4222-8222-222222222222",
      "confirmed_at": "2026-10-01T02:00:00Z"
    },
    "knowledge_points": {
      "confirmed_by": "22222222-2222-4222-8222-222222222222",
      "confirmed_at": "2026-10-01T02:00:00Z"
    }
  }
}
~~~

示例为真实核对后写入的 Schema 示意，不是可回填历史数据的默认教师或时间。知识点任一匹配使用 JSONB 数组精确成员关系，形如 jsonb_typeof(metadata::jsonb #> '{scope_confirmation,knowledge_points}') = 'object' AND jsonb_typeof(metadata::jsonb -> 'knowledge_points') = 'array' AND (metadata::jsonb -> 'knowledge_points') ?| :规范标签数组；有知识点条件时，未确认、未知或 [] 均不命中。查询消费已保存的确认标记，不重复执行写入边界的教师权限/完整 Schema 校验。空查询列表不增加该谓词，不能把合法缺省改成“必须已标注”。

### 11.4 摄取、跨章切分与兼容迁移责任

- 解析/清洗保留真实标题层级、原页/段落定位；分块器先按真实章/节边界隔离正文，再在边界内执行原长度切分和重叠。重叠、短段合并与标题附带不得跨越已确定边界；不得只标起点而把下一章/节正文送入限定上下文。无法可靠分开的片段保持未知定位，等待教师核对，不能捏造边界。
- 摄取编排生产候选及来源证据，不自行决定课程章节身份或教学标签；现有知识库服务负责登记/读取 Chapter、教师校正、同课程事务校验与 Chunk 写入，API/UI 沿用知识库管理职责接入。无定位的正文仍按原 Uploaded -> Parsing -> Chunking -> Embedding -> Ready/Failed 处理，不新增强制定位终态；Document.Ready 只证明原摄取就绪，不证明已定位。
- 教师根据真实资料边界核对并提交章/节/标签。只改元数据且正文不变时保留原 Chunk/向量；需要重切分时，新正文、embedding、search_vector 必须同源且就绪后原子替换，失败保留真实失败状态，不发布半更新的检索数据。已保存来源遵循 [question-source-persistence.md](contracts/question-source-persistence.md)，不以新切片/当前标签改写历史依据。
- 迁移仅建立目标表、可空定位列、约束和上述最小索引；旧 Chunk 的 chapter_id/section_order 全部保持 NULL，原 metadata 原样保留，不从旧 section_index 或 chunk_index 批量回填，也不为每份旧资料虚构 Chapter。后续有真实教师核对时才分批登记/回填。
- 有章/节条件时排除未知定位，有知识点条件时排除未知标签；无新增条件时不强制 JOIN Chapter 或检查新确认记录，保留 v1.0 可读性。有效限定范围内就绪资料不足时返回实际结果/不足，禁止退回全课程、邻章或替代来源。
- 本节为后续章节定位生产、迁移和范围消费提供共同设计依据；对应验证覆盖稳定身份、多资料同章、目录修订、跨章/节及重叠、独立标签确认、四种检索过滤与旧未知数据。不新增或修改测试；实施测试前由后续任务形成 TCR，不以文档定义宣称数据库/运行已通过。

## 12. T135：语义核验与人工处置持久设计（G02，2026-10-01）

本节对应 FR-047、FR-028 扩展和 CHK002，承接 [plan.md](plan.md) §9/§10；节点与审核消费见 [agent-workflow.md](contracts/agent-workflow.md)。采用用户确认的独立 QuestionValidationResult、报告内受校验的教师处置 JSON，以及 Question.validation_revision 加轮次的对应规则。本节只定义目标模型，不创建业务代码、数据库迁移或测试；第 1–11 节及 v1.0 教师文字意见职责保持原文，不引入完整题目版本或发布内容快照。

### 12.1 Question.validation_revision：核验输入修订号

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| validation_revision | BigInteger；非空；默认 0；CHECK >= 0 | 由 Question 写入服务维护的单调递增核验输入修订号；0 不表示已有有效核验 |

- type、content、options、reference_answer、scoring_rubric、score、后续独立 analysis，以及实际参与核验的题图集合/区域、已核对图片条件、父题教学依据或关键来源关联发生实际变化时，在同一事务内递增一次。相关资产/依据写入方须锁定所属 Question 并同步推进，不能只在题干编辑入口处理失效。
- difficulty、knowledge_points 的题库分类维护仍允许；它们不作为本节四项语义核验的输入，不因单纯分类维护使旧核验失效。若操作实际替换教学依据，则按依据变化推进修订号。文件路径迁移但内容/身份未变、Trace 删除、Provider/构建版本变化不推进修订号。
- Needs Revision -> Pending Review 的重新送审也推进修订号，确保不能仅切换状态恢复旧结论。初次 Draft/候选送审沿用当前修订号；Approved 退回先执行 §9 的发布/历史保护，真正重新送审时按本规则处理。
- 修订号不保存各版题干/答案，也不用于回滚、跨组件版本相等门禁或证明语义正确。内容改动后改回原值仍经过递增，不得把旧报告重新认作当前；旧报告保持原事实，以修订号差异派生失效状态。

### 12.2 QuestionValidationResult（question_validation_results）

Question 1 -> 0..N QuestionValidationResult；每行对应一次正式核验调用，包含执行中和最终结果，不与 CandidateValidationResult 的字段校验结果混同。尚未持久化的候选只返回核验 DTO/建议；不生成虚构 Question FK，不将预览通过直接作为正式批准依据。暂存题的图片理解/人工核对承载由 G03/G05 补齐，正式 Question 核验在真实题目及来源关联建立后执行。

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| id | UUID；非空；PK | 应用生成的核验报告身份 |
| question_id | UUID；非空；FK Question.id，CASCADE | 被核验题目；课程归属由 Question 派生，不再维护可改写的重复 course_id |
| input_revision | BigInteger；非空；CHECK >= 0 | 启动时捕获的 Question.validation_revision，完成时不可换成当前值 |
| run_no | BigInteger；非空；CHECK > 0 | 同一 Question 的单调递增调用轮次；UNIQUE(question_id, run_no) |
| outcome | String(32)；非空；CHECK | running / passed / failed / technical_error；由服务判定，区别执行状态与教师审核状态 |
| input_refs | JSONB；非空对象 | 当轮实际输入字段目录、证据及人工处置引用，Schema 见 §12.3；不存完整题目内容副本 |
| checks | JSONB；可空 | 正式成功返回时保存四项分项结果；running/没有合法业务输出的技术失败为 NULL，不能填 [] 冒充已检查 |
| issues | JSONB；可空 | 合法输出的问题数组；合法无问题为 []，未获得合法输出为 NULL |
| error | JSONB；可空 | 技术失败的真实 {code, message, stage, retryable, cause}；cause 可空，说明脱敏但不替换原错误分类 |
| executor_kind / executor_name | String(16) / String(64)；非空 | kind 为 service / agent，name 为真实执行组件；机器报告不冒充教师执行 |
| requested_by | UUID；可空；FK User.id，RESTRICT | 真实认证发起者；系统事件没有用户时为 NULL，不借题目创建者伪造 |
| agent_run_id | UUID；可空；FK AgentRun.id，SET NULL | 有真实 AgentRun 才关联；Trace 清理只清空活体链接 |
| provenance | JSONB；非空对象 | {agent_type, provider_name, model, model_version, prompt_version} 的实际执行来源；不可得成员为 null |
| manual_dispositions | JSONB；非空数组；默认 [] | 仅由真实教师操作追加的结构化处置，Schema 见 §12.4 |
| created_at | TIMESTAMPTZ；非空 | 调用启动、running 行创建的真实 UTC 时间 |
| completed_at | TIMESTAMPTZ；可空 | 最终执行结束的真实 UTC 时间，非空时 >= created_at |

数据库提供 PK/FK、轮次 UNIQUE（其索引覆盖按题目查最新轮次/历史）、修订号/轮次范围、JSON 对象/数组类型及状态时间 CHECK。running 要求 completed_at、checks、issues、error 为 NULL；passed/failed 要求 completed_at、checks、issues 非空且 error=NULL；technical_error 要求 completed_at、error 非空，没有合法输出时 checks/issues 为 NULL。数组元素、四项完整性、字段/证据对应及教师授权在内容核验服务的输入/输出写入边界校验，不宣称 JSON UUID 已有逐元素 FK。

已结束报告的 input_revision、输入引用、原始 checks/issues/error、执行来源和时间保持不可改写；后续仅追加 manual_dispositions，不把旧 failed 改成 passed。报告是题目的自有核验子记录，题目经现有删除/发布历史保护校验后合法删除才随之级联；这不改变 §6 对外部来源/发布依据的保留规则。AgentRun 可能按 Trace 保留期清理，报告中的执行来源仍保留，不能只靠短期 Trace 保存业务核验事实。

### 12.3 输入与证据、分项结果 Schema

- input_refs = {fields: list[字段名], evidence: list[Evidence], manual_context: list[{validation_result_id, disposition_id}]}。fields 精确列出实际使用的题型/正文/选项/答案/Rubric/解析/基准分值及图像条件输入；通过 question_id + input_revision 绑定当时内容，不复制另一份独立可写题干/答案。历史报告展示当时修订号、结果和证据，不声称能重建未保存的历史题干，不把当前题干展示成当时输入。
- Evidence = {evidence_id: UUID, kind: chunk/question_source_chunk/question_asset, source_id: UUID, source_data: 对应类型的来源对象}；服务为本报告分配唯一 evidence_id，source_id 为真实资源主键。chunk/来源快照的 source_data 含原 chunk_id、document_id、course_id、source_file、location（真实未知时 null）、content_snapshot；question_asset 的 source_data 含 file_id、source_page_id/region（可空）、image_review_ref（真实持久核对引用）及实际输入的已确认条件。按 kind 校验对应字段，不用无约束 JSON 或虚构身份代替来源。教学片段保存原 chunk_id、document_id、course_id、source_file/定位和当轮实际使用的 content_snapshot；若引用既有 QuestionSourceChunk，保留其真实 source_id 和原快照，不以当前活体 Chunk 替换。新增检索证据存本报告，不反写“生成时来源”。
- 图像 Evidence 保存 QuestionAsset/file_id、真实 SourcePage/region（可空）和实际已核对条件的持久引用；不保存临时路径、含答案源卷公开 URL 或全量 Base64。条件/原图对应与失效共同遵守 [vision-capability.md](contracts/vision-capability.md)，具体核对持久映射由 T138 补齐，不用 caption/模型布尔值替代。
- 来源必须属于本题授权课程，并确实进入本次输入。JSON source_id 由服务核对真实存在/归属；checks/issues.evidence_refs 只能引用本报告 evidence_id，未知证据不得编造。片段删除或重切分后保留报告证据快照；关键图像缺失/条件变化如实阻止批准，不以替代图片补足。
- checks = [{kind, verdict, reason, evidence_refs}]：kind 恰好覆盖 answer_correctness、condition_sufficiency、option_ambiguity、rubric_clarity，唯一且完整；verdict 沿用 pass/fail/insufficient_evidence/needs_review，reason 非空。无适用选项的题型说明无该检查对象，不能假装执行选项检验；未获得结论不能标 pass。
- issues = [{issue_id, code, field, severity, message, evidence_refs}]：issue_id 由服务为本报告生成并固定，severity 为 info/warning/error，其他原因文本非空；不能以降低 severity 隐藏 fail/缺依据。passed 要求四项 verdict=pass、无未解决 warning/error、必要字段/依据及图片核对完整；非选择题的 option_ambiguity=pass 仅表示无选项歧义问题，reason 必须明确其不适用；failed 保存实际问题/不足，不将 Provider 故障归类为内容错误。
- provenance 来自真正执行的组件和 Provider 调用记录，未知保持 null；未调用模型不能将配置中的模型填成已执行事实。Provider/模型/Prompt 版本只供追溯，不是内容正确性的相等门禁，也不与 GradingResult 的校验状态混用。

### 12.4 教师处置：报告内追加 JSON，原教师意见职责保留

ManualDisposition 的 Schema：

| 成员 | 类型与责任 |
| :--- | :--- |
| id | 服务生成 UUID；同一报告内唯一 |
| input_revision | 接受处置时捕获的当前修订号；必须与被处置报告一致 |
| issue_ids / check_kind | 本报告 issue_id 列表 / 可空检查种类；至少明确关联一个问题或检查 |
| action | request_revision / provide_evidence / resolve_issue；不包含 approve 或修改机器 verdict |
| reason | 教师明确给出的非空说明，最长 2000 字；不能用按钮布尔值代替 |
| evidence_refs / additional_evidence | 本报告真实证据引用 / 教师补充的同课程真实 Evidence；提供依据或解决问题时不能为空 |
| handled_by / handled_at | 认证教师 UUID / 服务端真实 UTC 时间；课程权限在写入事务校验 |
| revision_comment_id | 可空 UUID；仅当同次真实教师文字意见已保存时关联，服务核对同题及真实作者，不改意见表字段 |

处置时锁定 Question 与报告，要求报告为已结束的 passed/failed 且仍对应当前修订号和最新轮次；running/technical_error 不允许伪造内容问题处置，查看真实进度/错误并显式启动新核验。历史报告可读，但不能给旧问题补一个确认就放行当前内容。追加事件不得覆盖/删除旧问题或原处置；JSON 元素身份/归属由服务负责，客户端不能指定他人身份、时间或伪造意见关联。

request_revision 仍走已有合法状态转换并保留非空真实教师意见；必要时处置、文字意见和状态同事务提交。机器 failed 的自动退回只保存结构化报告与状态，不把 requested_by 当作 commented_by，不制造 QuestionRevisionComment。提供依据/resolve_issue 只记录教师处置，按既有契约重新核验后才可批准，不直接翻转旧 outcome/can_review。历史处置只作为历史事实；下轮显式选入的处置及证据记录在 manual_context，内容变化后不能自动继承“已解决”。

~~~json
{
  "id": "33333333-3333-4333-8333-333333333333",
  "input_revision": 2,
  "issue_ids": ["44444444-4444-4444-8444-444444444444"],
  "check_kind": "answer_correctness",
  "action": "provide_evidence",
  "reason": "已对照教材原文补充此处公式的适用条件，提交重新核验。",
  "evidence_refs": ["55555555-5555-4555-8555-555555555555"],
  "additional_evidence": [],
  "handled_by": "22222222-2222-4222-8222-222222222222",
  "handled_at": "2026-10-01T03:00:00Z",
  "revision_comment_id": null
}
~~~

示例只说明 Schema，不是可回填历史身份/时间或自动判定问题已解决的记录。

### 12.5 轮次、并发完成与状态事务

1. 正式核验以已持久、处于 Pending Review 的 Question 为入口；授权和既有字段/状态检查通过后，锁定 Question，捕获 input_revision、实际输入/证据，分配 MAX(run_no)+1 并保存 running 行。轮次只在题目锁内分配，UNIQUE 为最后约束；随后提交并释放锁，再执行外部调用，不跨模型等待长期持锁。
2. 内容修改、相关资产/依据变更、重新送审、启动新轮次、人工处置和批准共同遵守 Question 的事务锁及 §9 固定锁顺序。完成时重新锁定并比较报告 input_revision 与当前修订号、run_no 与本题最新轮次；只有对应当前修订号/最新轮次且 Question 仍 Pending Review 时才推进预期状态；迟到、被替代或教师已合法改变状态的报告可保存真实最终结果，但不得覆盖当前题目状态或当作当前批准依据。
3. 当前轮次 passed 保持 Pending Review；failed 的原报告和合法 Pending Review -> Needs Revision 同事务保存，任一数据库写入失败全部回滚。running 行或数据库提交失败不能被宣传成已保存最终报告/已退回；保留真实持久化错误，不能沿用旧通过结果。
4. technical_error 保存真实技术错误和执行事实，题目仍保持 Pending Review；只阻止批准，不伪造“答案错误”或自动 Needs Revision。取消、超时和实际执行失败按真实阶段记录；未完成/失联运行仍显示 running/待处理，不能凭旧结果声明成功，不新增无限重试或自动替换 Provider。
5. 当前报告 = 本题最大 run_no 的报告且 input_revision == Question.validation_revision；无报告、修订号不符、running、failed、technical_error 均不可据旧 passed 放行。不因较新轮次失败或尚未完成而回退到较早成功；当前报告存在 provide_evidence/resolve_issue 处置但尚未产生新轮核验时也禁止批准，此待重核验状态从处置数组派生。is_current/stale、can_review、requires_manual_review 为服务读时投影，不新增可独立修改的缓存成功布尔字段。

### 12.6 批准门禁、历史数据与后续边界

- can_review 由服务根据当前报告、实际字段/教学依据/图片核对和未解决问题计算，只有当前 passed、没有待重核验的人工处置且满足全部前提、Question 仍 Pending Review 才允许教师批准；模型 can_review/requires_manual_review 是辅助输出，不能作为直接数据库决策。结构化校验成功也不替代语义核验或教师最终批准。
- 所有新批准路径共用该判定：现有 QuestionService 状态更新（含请求 Approved）、questions 的 approve、question_generation 的候选审核及后续导入/改编入口；不能只在 UI 或一个路由校验。Approved 守卫、发布/历史保护及教师权限继续执行；Agent 无权自动批准。
- 历史 Question 仅初始化 validation_revision=0，不虚构报告、通过结论、执行来源或教师处置。已有 Approved/发布考试的状态和历史结果不因缺报告改写或自动退回，读取展示“历史核验未知”；未批准历史题进入 v2.0 新批准或合法修订后重新批准时，必须建立当前报告，不把旧字段 Validator 当作语义核验。
- 本节只定义 v2.0 目标设计与既有入口的应用边界；后续 T163/T166/T167 接线及迁移/验证另行实施。修改测试前形成对应 TCR；本批次仅做文档静态检查，不宣称当前批准接口已具备新门禁。

## 13. T136：校正字段与正式题解析设计（G03，2026-10-01）

本节对应 FR-018/028/041/042/049 和 CHK003，补齐 §7.3/§7.4/§8.2 的目标字段；接口消费见 [paper-import.md](contracts/paper-import.md)。采用用户确认的独立题号/解析列、受校验 JSONB 知识点/来源区域/暂存资产，以及正式 Question 的可空解析；不新增暂存资产表。真实来源页已核对但像素边界未知时允许确认入库，缺失解析不自动补写。本节仅追加设计，保留第 1–12 节原文，不创建代码、迁移或测试。

### 13.1 ExtractedQuestion 的校正扩展

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| question_number | Text；可空；无默认 | 原卷题号原文，如“01”“一、3(2)”；非 null 时非空，不转整数、不强制导入内唯一，不作为题序 |
| analysis | Text；可空；无默认 | 可获得并经校正的原题解析；非 null 时非空，与题干/答案/Rubric/备注分别保存 |
| knowledge_points | JSONB 字符串数组；可空；无默认 | null 为尚未登记/未知，[] 为明确保存的空标签列表；课程内规范标签，非空字符串、去重并保留顺序 |
| source_regions | JSONB 对象数组；可空；无默认 | null 为可靠题目边界未知，[] 为未登记局部框、只使用已核对页来源；元素 Schema 见 §13.2 |
| assets | JSONB 对象数组；可空；无默认 | null 为题图关联尚待核对，[] 为本暂存题不关联题图；0–5 项，元素 Schema 见 §13.3 |

这五个字段分别持久保存，不另建无结构的 correction_data 或把业务 JSON 塞进 correction_notes。备注仍只负责人工说明/拒绝理由；extracted_by 仍记录真实提取生产者，不能因教师编辑而改成另一种机器来源。未知与空列表的含义由字段定义决定，不将空列表当成教师核对完成的证明。

数据库只约束列类型、knowledge_points/source_regions/assets 的数组或 NULL 形状，以及 assets 非 NULL 时长度 <=5；数组元素、非空文本、枚举、身份/课程/页归属和有限坐标由 Pydantic 与写入服务边界校验。知识点标签复用既有 Question 的修剪、去重与最长 160 字规则，不新增 KnowledgePoint 关联表，不因原题标签写入教学 Chunk.metadata。

PATCH 省略字段保持原值；显式 null 仅清除本表允许缺失的字段，[] 明确替换为空列表。source_regions/assets 提供数组时整体替换，经整组校验后同事务保存；不隐式合并、排序或截断坏元素。source_page_ids 仍是 §7.3 的唯一页来源表达，不重复保存关联表；修改页来源时同时核对当前区域/资产，仍指向移除页的引用必须在同次修改中处理，否则拒绝整次修改。source_page_ids 不接受 null，确认入库前必须真实、非空且符合既有顺序/同导入规则。

### 13.2 题目来源坐标

SourceRegion = {source_page_id: UUID, bbox: [x0, y0, x1, y1]}。source_page_id 必须出现在本题 source_page_ids 中，且对应同一 PaperImport 的真实 SourcePage；每个框表示原题文字/选项等在该页的一块真实区域，同页可有多块，跨页按 page_number 排列，同页保留明确的阅读顺序。

- 坐标基于持久 SourcePage.image_path 对应原页图：左上角为原点，x 向右、y 向下，单位为像素，允许有限小数；必须满足 0<=x0<x1<=width、0<=y0<y1<=height，拒绝布尔值、NaN/Infinity、零面积和越界值。
- OCR/PDF 提取生产者负责将旋转、缩放或其他原生坐标映射到该页图尺寸；转换依据不足时保留 null/未知并交教师核对，不猜归一化比例，不把整页框冒充准确题目边界。
- source_regions 只补充页内位置，不能反向悄悄增删 source_page_ids。已提供区域必须全部合法；来源页已核对且满足其他确认条件时，null/[] 不单独阻止入库，不强迫教师捏造像素框。文件/页丢失、跨导入来源或非法已有框不能用“边界未知”绕过。
- 题目文字边界 source_regions 与题图裁切 region 是不同信息；不从文字框自动生成题图，不以页级定位授权学生访问含答案原页。校正确认后区域与页来源作为原导入依据保留，不随正式题编辑反写；原页身份/内容及删除保护继续遵守 §7.2/§7.3。

### 13.3 暂存资产 Schema、顺序与正式映射

StagedAsset = {id: UUID, file_id: 非空不透明字符串标识, asset_type: figure/table/diagram, source_page_id: UUID, region: {bbox: [x0,y0,x1,y1]} | null, caption: str | null, student_visible: bool = false}；assets 数组顺序就是题图顺序。

- id 由服务在暂存关联首次建立时生成，同一数组内唯一；创建请求省略 id，由服务返回。编辑只接受本暂存题已有 id 或服务新建项，不能借其他题 id 更换归属。该 id 表示持久暂存关联，Corrected 前不宣称已有 QuestionAsset 行；转入时沿用为 QuestionAsset.id，以便 G05 的实际理解/核对引用保留关联，不新增另一套暂存关系表。
- file_id 消费 [file-storage.md](contracts/file-storage.md) 的稳定授权标识，不接受客户端路径/外部 URL。服务核对可靠文件、真实图像及同导入来源；source_page_id 必须在本题 source_page_ids 内。原图/裁图均保留原页对应，region 的 bbox 使用 §13.2 同一像素约定；整页引用可为 null，但不得向学生暴露含答案或其他不应展示的信息。
- width/height 从实际资产图像读取，用于正式 QuestionAsset 的尺寸；裁图 region 使用原页尺寸校验，不能把裁图自身坐标当原页坐标，也不能从不可靠框伪造文件或尺寸。caption 只作说明，不是已确认图像条件。
- 确认入库前 assets 必须完成关联核对：无题图显式保存 []，有题图保存 1–5 个可靠关联；null 保持待校正。图像语义理解失败/尚待条件核对不伪装为通过，可按既有待补全规则入库 Draft，审核仍消费真实 G05 核对证据。G05 的 image_assessment 结构、身份/时间/条件及失效关联由 T138 补齐，本节不将其塞入 caption 或预定另一张结果表。
- QuestionAsset 补 order_index：Integer、新资产非空，历史图序未核对可 NULL；CHECK 为 NULL 或 1..5，UNIQUE(question_id, order_index)。创建时从暂存数组依次写 1..N，预览/考试/核验/阅卷按该序读取；后续排序也是题图集合变更，遵守 Approved/发布保护与 §12 修订号规则。每题最多 5 图仍按 §7.4 在题目锁内校验，不以唯一约束代替计数。
- student_visible 在暂存 JSON 中受严格布尔校验，省略默认 false；校正 PUT 仅由本课程教师在合法状态修改。T158 入库时显式承接到 QuestionAsset.student_visible，终态原 assets 保留原校正许可记录，后续正式题开关只改正式列，不能反写原记录。暂存图即使 true，在真实确认建立 QuestionAsset 之前仍仅教师读取。
- 转入通过 T137 的统一文件映射将同一真实图像绑定正式 QuestionAsset，规范定位沿用 §7.4，不新增可分叉路径事实源；文件标识不等同于资产关系 id。共享字节不丢失来源或授权，移除暂存关联不直接删除仍被原页/正式题引用的文件。Corrected 之后 assets 保留校正时的来源记录，正式题维护不反写该数组。

以下为校正字段的 Schema 示例，假设真实来源页尺寸为 1000×1400 像素；身份/文件标识仅作示意，不能据此回填或宣称文件/教师核对已存在：

~~~json
{
  "question_number": "01",
  "analysis": null,
  "knowledge_points": ["牛顿第二定律"],
  "source_page_ids": ["11111111-1111-4111-8111-111111111111"],
  "source_regions": [
    {"source_page_id": "11111111-1111-4111-8111-111111111111", "bbox": [80, 120, 900, 480]}
  ],
  "assets": [
    {
      "id": "66666666-6666-4666-8666-666666666666",
      "file_id": "opaque-example-file",
      "asset_type": "diagram",
      "source_page_id": "11111111-1111-4111-8111-111111111111",
      "region": {"bbox": [100, 200, 700, 400]},
      "caption": null
    }
  ]
}
~~~

### 13.4 Question.analysis 与确认入库映射

| 目标 | 持久映射与责任 |
| :--- | :--- |
| Question.analysis | 新增 Text、可空、无默认；ExtractedQuestion.analysis 按实际值转入，不从答案/Rubric/OCR/备注推断 |
| Question.knowledge_points | 复用 v1.0 非空 JSON 字符串数组；已登记标签按既有规则转入，暂存 null 转为 []（尚未登记标签），暂存原始未知仍可回溯，不把 [] 宣称为已确认无知识点 |
| 原题号/原题位置 | question_number、source_page_ids、source_regions 留在终态 ExtractedQuestion，通过 imported_extracted_question 回溯；不新增 Question 上可独立改写的来源副本，不代替 ExamQuestion.order_index |
| QuestionAsset | 每项建立同 Question 的正式关联，沿用暂存 id，按数组顺序写 order_index；asset_type/caption/source_page_id/region 按实际值、尺寸按实际文件、定位按统一文件服务写入 |

在既有同一批次 commit 事务内，校验当前暂存内容与可靠来源，创建 Question(Draft，source_type=paper_imported)、正式资产及其文件资源映射，再写 question_id/Corrected；任何一步失败整体回滚，保留已保存原卷/页图及此前校正结果。重复确认返回同一正式题和资产，不重建、重排或覆盖其后续修订；Corrected/Rejected 不再接受校正 PATCH。缺题号、解析、知识点或像素边界不单独改变完成判定；题型/题干/选项/明确分值/真实来源及题图关联核对仍为确认条件，答案/Rubric/图像必要条件不足按既有流程保持 Draft/待补全，绝不自动 Approved。

### 13.5 解析审核、冻结与旧数据边界

- analysis 是正式内容字段，必须进入教师详情、编辑与审核输入；有值时与题干/答案/Rubric/实际依据共同核验，修改或清空按 §12 同事务递增 validation_revision。缺解析可为 null，表示未提供，不自动生成解析、不作为所有题一律必须非空的新门禁；有错误/矛盾解析仍须修订。
- 解析新增、替换或清空纳入服务端 Approved 守卫，沿用 QUESTION_APPROVED_IMMUTABLE；无保护引用时先退回 Needs Revision 再编辑/重核验/重审。已发布或存在历史引用时遵守 §9 及 [exam-assembly.md](contracts/exam-assembly.md)，拒绝原地解析变更，保持发布时真实文本或 null；需要新解析时创建派生候选，不新增完整题目版本/内容快照。
- 解析是教师审核/结果解释信息，不作为学生作答接口中可提前取得的答案信息；结果展示按既有权限和结果可见规则消费。题库知识点维护和考试发布知识点冻结仍按 §9，解析新增不改变 v1.0 这项职责。
- 后续迁移只给历史 Question.analysis 置 NULL，不从参考答案、Rubric、correction_notes、旧 Prompt/Trace 或当前模型回填“历史解析”；已有 Approved/发布状态和结果保持。历史暂存扩展若确有旧数据，新字段保持 NULL/未知；不合成题号、框、资产或确认记录，不自动重跑旧 commit。
- 历史 QuestionAsset 若有真实既定图序，可按证据补 order_index；无图序证据时明确待核对，不按 UUID/创建时间冒充历史顺序，也不因新列要求改写已有考试。历史未知图序保留 NULL，阻止未经核对的新发布而不改写既有考试；新资产字段约束在后续数据盘点/迁移中落实。
- 本节由 T153/T154/T158 等后续实施字段与接线，T138 处理图像理解/核对，T137 处理文件物理承载。后续验证覆盖重启读取、PATCH 省略/null/[]、跨页坐标、同导入资产/顺序、批次回滚及幂等、缺解析/未知边界和解析冻结；测试变更先形成 TCR，本任务仅做文档静态检查。

## 14. T137：文件身份、导出与备份登记设计（G04，2026-10-01）

对应 FR-044/045、CHK004 和 [file-storage.md](contracts/file-storage.md)，同步 [plan.md](plan.md) §11。采用用户确认的方案：复用资源身份和定位，仅新增 ExportFile；ManagedFileView 是服务投影，BackupSet 是磁盘 manifest.json，不建 ManagedFile/BackupSet 表。本节定义 v2.0 目标，不执行搬移、备份、恢复或清理，不修改第 1–13 节/v1.0 原文。

### 14.1 file_id 的唯一资源映射

file_id 是服务生成/投影的非空不透明字符串（最长 64 字符）；本版服务内部规范为下列前缀加资源 UUID 的小写 32 位 hex，客户端只回传已提供标识，不自行构造或依赖其格式。前缀区分表间相同 UUID，不新增随机标识映射表，不将文件名/相对路径编码进标识。

| file_id / 资源 | 定位唯一写入源 | 归属与授权来源 |
| :--- | :--- | :--- |
| `d_<Document.id.hex>` | Document.storage_path | Document 的课程/知识库或 PaperImport 用途；原试卷仅教师管理权限 |
| `p_<SourcePage.id.hex>` | SourcePage.image_path | PaperImport.course_id；完整原页不因考试权限向学生开放 |
| `a_<资产 id.hex>`（暂存） | ExtractedQuestion.assets 中该 id 的服务端 file_meta.storage_path | 所属 ExtractedQuestion/PaperImport；真实同导入页及教师权限 |
| `a_<QuestionAsset.id.hex>`（正式） | 普通资产为 QuestionAsset.file_path；沿用暂存 id 的导入资产见下文 | 活体 Question 的课程/考试/本人结果引用；无学生可见关联时只保留合法来源管理访问 |
| `e_<ExportFile.id.hex>` | ExportFile.storage_path | ExportFile 的真实课程/考试/答卷及 audience，见 §14.3 |

- §13 的暂存 id 在入库时沿用，a_ 标识不变。对沿用暂存 id 的原题资产，其原导入资产 file_meta.storage_path 保持定位事实源；正式 QuestionAsset.file_path 为只读关系投影，不再存第二份可独立更新的路径。服务按 ExtractedQuestion 的真实资产 id 定位来源；关联存在时不能因路径缺失改读另一列/旧目录。普通人工/派生资产仍以自己的 file_path 为事实源；复用字节的新关系分配新资产 id。
- 暂存源是实际持久 JSON 资源，不伪称有 SQL 资产行。正式行存在时服务核对同题确认关联；原暂存来源继续保留。正式资产合法解除后，原导入资产仍可按教师权限追溯，不能把它当作学生可见题图。资源 id/课程不从文件名、公开请求或缓存猜测。
- ExtractedQuestion.assets 的公共 StagedAsset Schema 保持 §13；服务端持久记录另含 file_meta，公共 CorrectionPayload/View 不接收或泄露其中的路径/迁移原定位。PATCH 按已有 id 保留服务端元数据，新项由服务建立；不能用公开数组整体替换擦除内部文件登记。增加 GIN(assets) 支持服务按资产 id/file_id 定位及引用核对，不宣称 JSON 元素具有 FK。
- 字节改变必须新建文件/资产身份，不能覆盖已有物理文件或复用旧 a_ 标识指向另一张图。caption/条件维护和顺序变更仍走审核/核验规则；修改裁图形成新图时创建新资产关系，保留原导入依据。迁移只改物理定位，file_id、原图内容、来源坐标和核对身份保持。
- ManagedFileView.resource_type 取 document/source_page/staged_asset/question_asset/export，resource_id 是真实持久资源 UUID；course_id 由关系派生。availability 读时按真实定位/字节判为 available/missing/history_unknown，未知与已确认缺失分开；没有业务资源的标识为 404。暂存 id 由服务生成并禁止跨题复制；解析出多个不符合确认关联的资源时返回 FILE_REFERENCE_CONFLICT（409），不取首条碰巧命中的记录。授权先于字节读取，不能因同字节的另一个来源有权限而绕过当前资源/考试边界。

### 14.2 文件元数据、迁移记录与写入失败

Document、SourcePage、普通 QuestionAsset、ExportFile 增加 file_metadata：可空 JSONB 对象，无历史默认；暂存原图在服务端 file_meta 保存同一元数据，另含唯一 storage_path。导入正式资产的元数据与定位同样投影原导入资产记录，不维护重复可写副本。

FileMetadata = {media_type: str|null, size_bytes: int|null, sha256: str|null, migration: MigrationRecord}。size_bytes 非负，sha256 为真实字节 SHA-256 小写 64 位 hex；未知为 null。新文件完成写入/关闭并核对后记录真实类型、长度和摘要；旧数据不能填 0/假摘要。Document 原文件名复用已有字段，资产未知名称显示 null，导出名由 ExportFile 保存。摘要只核对字节完整性，不替代授权、来源真实性或业务验收。

MigrationRecord = {status, latest_attempt: MigrationAttempt|null}：

- status 为 not_required/not_migrated/migrated/missing/history_unknown/failed。not_required 表示原生持久文件无需迁移，不伪造迁移事件；历史真实旧定位待迁移为 not_migrated，完成真实复制/引用提交后为 migrated。
- MigrationAttempt = {operation_id: UUID, source_locator, target_relative_path, started_at, verified_at|null, committed_at|null, error|null}；时间是实际 UTC，error 含 code/message/stage。source_locator 可是历史本机定位，仅服务端保存；不是第二个当前路径，不返回给公共文件响应。数据库约束对象或 NULL，元素/状态与时间一致性由写入服务校验。
- 确认旧来源、复制到唯一新定位，核对源/目标长度与摘要（已有可信摘要也需一致），再在同事务更新所有指向该共享字节的定位事实源及迁移结果。失败保留原指针/字节及目标中间结果诊断，不修改原材料，不凭目标文件存在宣称 migrated。重复操作核对已经提交的目标与实际字节，不重拷/覆盖，不自动按同名文件补缺。
- 终态 ExtractedQuestion 的公开校正字段不变；文件服务可更新其内部定位/迁移元数据，此例外只服务物理迁移，不允许改变 file_id/原图或反写正式题修订。§13 的原导入依据冻结继续成立。
- 可靠保存后才建立可读业务引用。每次落盘/迁移在对应目录记录服务端 OperationReceipt（JSON：operation_id、拟建资源身份、真实已存在归属/发起者/阶段、候选定位、错误与 UTC 时间）；先记录归属再写内容，拟建 id 不代表 SQL 行已存在，数据库失败时收据和材料仍保留。收据只是操作证据，不能凭它返回 Uploaded/Ready/Corrected 或创建成功的 FK；引用提交后才记 committed，stage 为 prepared/written/committed/failed。业务提交后收据更新失败须同时报告已提交事实和收据错误，保留原收据，不伪称无引用或自动重建。
- 接收时数据库/归属无法建立，或收据/字节写入失败，直接保留真实失败，不宣称已接收。收据不作为第二个定位/授权事实源；未归属中间材料需人工核对，不自动删除或据收据恢复业务成功。DB 中迁移失败记录写不进去时，失败证据保留于收据，不能把旧成功状态当本次成功。

### 14.3 ExportFile（export_files）：仅新增必要导出登记

当前代码未有可复用的导出实体；本表仅承载实际生成的导出文件，不新增导出格式、后台任务或业务版本。

| 字段 | 类型 / 空值 / 约束 |
| :--- | :--- |
| id | UUID；非空 PK；每份真实导出身份，e_ 标识由此投影 |
| course_id / exam_id / submission_id | 可空 UUID FK，RESTRICT；三者恰好一个非空，定位真实归属资源，其余归属沿关系派生 |
| created_by | 非空 UUID FK User.id，RESTRICT；实际认证发起者，不借资源所有者伪造 |
| audience | String(32)，非空 CHECK teacher_only/submission_owner；后者要求 submission_id 非空 |
| original_filename | String(255)，非空；真实导出展示名，不作为目录/授权来源 |
| storage_path | String(1024)，可空；ready 时须为规范持久相对定位，不另存重复路径 |
| file_metadata | JSONB 对象，可空；ready 时非空并有实际类型/长度/摘要，与 §14.2 同一 Schema |
| status | String(16)，非空，默认 writing；CHECK writing/ready/failed |
| error | JSONB 对象，可空；真实 code/message/stage，failed 必须有值，writing/ready 为 NULL |
| created_at / completed_at | UTC TIMESTAMPTZ；开始非空，结束可空且 >= 开始；writing 无结束，ready/failed 有真实结束 |

约束：三者恰一归属 CHECK、状态/路径/错误/时间 CHECK；归属 FK 分别建索引，主键供文件读取，不另建重复 file_id 索引。课程、考试或答卷真实存在/权限在创建与读取时校验，归属创建后不可转挂。teacher_only 仅对应管理权限；submission_owner 允许该答卷本人按既有结果可见规则读取，教师仍须有实际管理权限，不能让考试内其他学生读取。created_by 是来源，不是独立授权名单。

writing 行可用于记录真实在途生成；只有内容可靠落盘且 ready 事务提交后才对外提供可读文件引用。猜到 writing/failed 的 e_ 标识也不返回字节，授权后返回 FILE_NOT_READY（409）及真实状态/原因，不冒充文件缺失。崩溃未完成保持真实 writing/待处理，不自动 ready；重复生成新导出不得覆盖旧文件。ready 后定位仅可依法迁移，内容/归属不能覆盖；现有受保护引用继续执行，失败文件/收据不自动清理。

### 14.4 共享字节与删除一致性

- 共享的是同一个规范相对定位的字节；每个业务关联仍有自己的 file_id/权限。相同摘要的不同文件不自动合并；同路径的不同引用不因一条关系删除而消失。文件服务枚举 Document、SourcePage、暂存/终态来源资产、正式资产、ExportFile 及未完成写入收据，不能只数活体 QuestionAsset 或缓存引用计数。
- 关联建立/解除、定位迁移和字节删除在规范定位上序列化，复用 PostgreSQL 事务级 advisory lock（定位按统一规则产生锁键）并按既有业务锁序核对发布/历史保护；锁内重读实际资源，不新增锁基础设施。共享迁移一次更新全部定位事实源，关系投影不重复写入。
- 解除一条题图关系与删除物理字节分开处理；仍有原导入来源、其他题/原页、导出或有效历史引用时字节删除返回 FILE_IN_USE。物理清理仅在全部合法引用已解除、写入/迁移已结束且操作获授权后执行；DB 解除失败不能先删字节，清理失败保留可追溯材料/错误。
- 新关联不能复用已失效资源或凭收据/任意路径重新挂接；删除前的冻结查询和关联写入共同遵守锁。未知历史共享关系不能被推断为无引用，需先盘点。缺字节保留业务记录/原成功历史并报告 FILE_MISSING，不改用旧目录、近似文件或替代图。

### 14.5 BackupSet：磁盘清单与同一写入窗口

每次备份在独立备份目录（备份根加 backup_set_id）保存 manifest.json、database.dump 和 files/，备份根不得位于被复制的业务根子树。不建备份表；manifest 与数据库/文件来自同一次操作，不用 Git/构建版本相等替代关联核对。

| manifest 成员 | 结构 / 语义 |
| :--- | :--- |
| manifest_version / backup_set_id | 固定 Schema 版本 1 / 本次 UUID；格式校验与备份集配对，不作组件版本门禁 |
| outcome | creating/complete/incomplete/failed；最终结果写入后只读，不能覆盖失败为成功 |
| started_at / window_started_at / window_finished_at / completed_at | 真实 UTC；尚未到达的阶段为 null |
| database | null 或 {relative_path, size_bytes, sha256, schema_revision}；PostgreSQL 自包含 dump，schema_revision 为真实迁移记录，仅追溯 |
| references | [{file_id, resource_type, resource_id, owner, relative_path|null, availability, migration_status}]；来自同一数据库/暂存记录，owner 保存真实课程及相应导入/题目/考试/答卷引用 |
| files | [{relative_path, size_bytes, sha256}]；相对业务根的唯一物理文件清单，同路径共享字节只复制一次，引用不丢失 |
| operation_receipts | [{relative_path, operation_id, stage}]；在途/失败收据及材料纳入 files，不能藏掉数据库未接收的残留 |
| issues | [{code, file_id|null, relative_path|null, stage, message}]；逐项记录缺失、未知、未迁移、冲突和技术失败，无问题才 [] |

所有清单经受校验 Schema；路径不能绝对/越根，长度非负，已复制项摘要真实。complete 必须有已核对 database、有效阶段时间，所有有效文件引用对应真实 files，issues=[] 且无未归属材料；失败状态/空清单不能通过手改 outcome 升级。缺失/未知项保持 null/诊断，不造文件或合成合法引用。manifest 格式不含凭据/连接串；备份集本身含业务数据，按已有受控运维访问处理。

1. 创建 creating 清单，暂停全部业务写入口，等待在途导入/生成/评分/文件/导出/迁移事务结束；随后停止所有应用写进程与写脚本，确认无未受控写入。单个 UI 开关或只等 HTTP 请求结束不足以证明窗口；超时/无法排空真实失败，不无限等待或伪造完成。
2. 在持续停写窗口内生成 PostgreSQL dump，并枚举/复制 uploads、papers、assets、exports 的实际引用、字节和收据；既有 DB 的题目/来源/答卷/评分/复核/核验关系整体保留。每个 active 文件引用可对应唯一实际字节，共享定位统一核对；未迁移外部临时文件不能被漏掉后报告 complete。
3. 核对 dump/文件长度与 SHA-256、引用归属和收据归属。原业务 Failed/Rejected/Draft/待处理状态不是备份失败，按真实状态保存；缺文件、来源未知、定位冲突、未覆盖材料使备份 incomplete，dump/复制/清单写入失败为 failed，保留已产生材料与原因。
4. 只有全部目标已核对、无完整性缺口时发布 complete 清单；失败/incomplete 清单也保留且不作成功制品。最终清单先可靠写入再发布，清单落盘失败也不能宣称 complete；窗口结束后才恢复原环境写入。Redis 缓存不是业务事实源，不从缓存补成功状态。

清单开始态示例（仅 Schema 示意，不是已执行备份）：

~~~json
{
  "manifest_version": 1,
  "backup_set_id": "77777777-7777-4777-8777-777777777777",
  "outcome": "creating",
  "started_at": "2026-10-01T05:00:00Z",
  "window_started_at": null,
  "window_finished_at": null,
  "completed_at": null,
  "database": null,
  "references": [],
  "files": [],
  "operation_receipts": [],
  "issues": []
}
~~~

### 14.6 隔离恢复、核对与失败保留

1. 校验同一个备份集 manifest/dump/files 的 Schema、路径、长度、摘要与完整性结果；incomplete/failed 集只能隔离调查，不能宣称完整恢复或开放正常写入。用户授权实际恢复后才执行，本任务不进行恢复。
2. 恢复到新的隔离数据库和持久根，保留原环境，禁止后台任务/应用写入。导入 dump 和实际 files，固定相对定位与原 file_id；只改变根配置，不将源机器绝对旧路径当新根内文件，不寻找其他备份集补缺。
3. 从恢复数据库重建同一资源映射，核对 references 与实际资源/FK/课程/来源页/题图/导出归属、共享定位及字节；题目/考试/答卷/评分/复核关系和真实未知/业务失败状态保持。旧 Approved/历史结果不自动重算，失联运行不虚构成功；收据作为材料读取，不自动恢复业务提交或重跑生成。
4. 写独立 restore-report.json（restore_id、backup_set_id、真实开始/结束 UTC、结果 verified/failed、逐项 issues），原 manifest 保持只读。核对失败或写报告失败不切换，保留隔离环境和原环境，禁止清理原文件；哈希一致不替代业务对应和授权检查。
5. 仅核对通过后在停写窗口切换数据库/根配置，并验证服务真实加载新环境及授权文件/业务关系可读，之后才开放正常写入。切换/启动失败保留原配置与两套材料，写入开放前可退回原环境；若新环境已接收真实写入，不自动退回旧环境丢弃新数据，另行制定恢复处置。

具体工具在 T147–T151/T191 实施与演练，测试变更先形成 TCR。T137 只定义模型/契约与 plan §11 引用，T140 同步已确认 exports 目录文字；本次文档静态检查不证明存储、备份或恢复已运行。

## 15. T138：图片理解与人工核对持久设计（G05，2026-10-01）

本节对应 FR-043/047/049、CHK005，补齐 §7.3/§7.4、§12.3、§13.3 的图片核对承载；契约见 [vision-capability.md](contracts/vision-capability.md)、[paper-import.md](contracts/paper-import.md)。采用用户确认的题目层受校验 JSONB、独立图像上下文修订号及当前真实核对证据转入；不新增图片核验表或暂存资产表。第 1–14 节原文保留，本节是尚待 E1/E2/E3 实施的增量设计，不创建代码、迁移或测试。

### 15.1 承载与唯一事实源

| 所属实体 / 新增字段 | 类型 / 空值 / 默认 | 语义 |
| :--- | :--- | :--- |
| ExtractedQuestion.image_assessment | JSONB；可空；默认 NULL | 本暂存题整组 1–5 张题图的理解轮次、人工核对与失效依据；NULL 表示尚未建立记录，不表示已通过 |
| Question.image_assessment | JSONB；可空；默认 NULL | 本正式题的同形记录，及合法导入时对当前暂存核对的只读绑定；不在每个 QuestionAsset 复制跨图结果 |

- ImageAssessment = {context_revision: 非负严格整数, runs: list[ImageUnderstandingRun], manual_checks: list[ImageManualCheck], imported_review: ImportedImageReview | null}。首次真实操作建立 {context_revision:0, runs:[], manual_checks:[], imported_review:null}；无记录的响应可投影计数 0，不能合成调用或教师核对。建立后不因删图清空历史或置 NULL；暂存题 imported_review 恒为 null，该绑定仅用于正式导入题。
- 数据库仅为两列提供 NULL/JSON 对象类型 CHECK；元素 Schema、计数/身份唯一性、关联与时间由服务写入边界校验。读取按所属题主键，不为本任务新增 JSON 索引。JSON 中 UUID 不是 SQL FK，不宣称数据库逐元素保证引用完整。
- 一次调用可同时理解多张图，跨图条件和问题只在所属题保存一份；每项通过稳定 StagedAsset.id / QuestionAsset.id、file_id 和 image_index 对应实际输入。暂存图无正式 QuestionAsset 行时，不伪造其 FK。asset_type/caption 仍属于资产，caption 不替代条件或核对证据。
- G02 的 Evidence、实际执行来源、技术错误及教师身份/UTC 规则复用为结构规则；QuestionValidationResult 仍只属于真实正式 Question，保存四项语义核验，通过 image_review_ref 引用本节证据，不承载暂存调用。QuestionRevisionComment 继续只记录真实教师意见，不写机器结果。
- 记录继承所属题/导入的课程权限，只有管理教师查看完整条件、答案相关问题和核对记录；学生接口只使用授权题图及既有结果可见信息，不返回本 JSON、原卷定位或未经授权的源页。

### 15.2 图像上下文与输入引用

- context_revision 是所属题的图像输入单调计数，不是文件版本、Question.validation_revision 或完整题目版本。题图新增/移除/替换/排序、原图身份、asset_type、来源页/region、实际理解的文字或来源定位变化，在同一业务事务内递增一次；A→B→A 仍递增，不复用 A 的旧核对。
- 首版理解上下文固定纳入题型、题干、选项、参考答案、Rubric、analysis、基准 score 和各图 caption（允许真实 null）；导入还纳入真实 source_page_ids/source_regions。正式导入题通过不可改写的 imported_extracted_question 读取原来源定位。仅传入本题文字和授权题图，不把整份含答案原卷附加给 Provider。后续消费者不从当前题干重建历史输入。
- difficulty/knowledge_points 分类维护、question_number、文件路径迁移、Trace 删除、Provider/构建/Prompt 版本变化不改变图像上下文；改变实际文字/图片输入则按前项递增。原图字节变化按 §14 建立新文件/资产身份，不覆盖旧 file_id。
- ImageInputRefs = {text_fields: list[实际字段名], images: list[ImageInput]}；记录实际输入目录，不另存可独立编辑的题干/答案副本。ImageInput = {asset_id: UUID, file_id: 非空字符串, image_index:1..N, asset_type, source_page_id: UUID|null, region: bbox对象|null, width:正整数|null, height:正整数|null, mime_type:真实图像格式|null}。images 顺序与当前整组图一致，id 唯一、数目 1–5；暂存来源必须同导入，正式资产必须属于本题及授权课程。
- 预检尚未读取尺寸/格式时在技术失败记录中保留 null；合法 completed 结果和人工核对必须取得真实非空尺寸/格式。width/height 来自真实题图；bbox 沿 §13.2 原图/SourcePage 像素规则。裁图到原页有真实映射才可保存原页 evidence_region；有 SourcePage 但映射未知时保留 null，不猜整页框或归一化比例；无 SourcePage 时以实际资产原图坐标校验。必要条件可在框未知时经原图核对，不凭未知框掩盖缺图。
- 未保存各版题干，历史只展示当时修订号、输入引用、原始结果/问题及核对事实，不把当前题干冒充历史内容。旧引用保持原来源，删图后不自动改指向新图；文件可读性和必要条件不足如实展示。

### 15.3 ImageUnderstandingRun：真实调用与失败

| 成员 | Schema / 责任 |
| :--- | :--- |
| id / run_no | 服务生成 UUID；run_no 为本所属题跨修订单调递增正整数；同题内均唯一 |
| context_revision / task / input_refs | 启动时捕获的修订号、真实非空任务说明及 ImageInputRefs；完成时不换成当前输入 |
| outcome / result / error | running / completed / technical_error；result 为受校验理解对象或 null；error 为 §12 的 {code,message,stage,retryable,cause} 或 null |
| executor_kind / executor_name / requested_by | service/agent、真实组件名、真实认证发起者 UUID 或 null；系统调用不借教师/题目创建者伪造发起者 |
| agent_run_id / provenance | 真实 AgentRun.id 或 null；{provider_name,model,model_version,prompt_version} 的实际调用事实，不可得为 null |
| started_at / completed_at | 真实 UTC ISO-8601；开始非空，结束可空且 >= 开始 |

- 授权/真实资产归属校验通过后，在检查能力/传输和外部调用前登记 running；预检取得尺寸/格式后只补充该轮原输入的真实事实，不更换修订号或资产，读不到时保留 null。未实际调用模型时 provenance 的 Provider/model 成员保持 null，不把配置值写成已执行事实。执行实际调用后保存真实来源；AgentRun 清理不抹掉 JSON 的执行事实；保存的真实 trace UUID 仅供历史追溯，读取不宣称活体 Trace 仍存在。
- running 要求 result/error/completed_at 为 null；completed 要求合法 result、真实结束时间且 error=null，只代表调用和输出校验完成；technical_error 要求真实 error/结束时间、result=null。不支持、未就绪、传输失败、缺图、取消/超时、输出非法分别保存原错误，不伪造空成功结果或内容错误。
- result 保存实际 VisionResult 的 observations、conditions、unresolved_issues、requires_manual_review。observations 含 image_index/kind/description/bbox；conditions 含 image_index/text/evidence_region。服务按本轮 ImageInputRefs 绑定 asset_id，分别给条件与问题生成稳定 condition_id / issue_id；模型不产生持久资源身份。问题保存 {issue_id,asset_ids:list[UUID]|null,message:非空字符串}，null 表示整组或无法可靠归属，不猜具体图片。
- 条件只来自实际原图，不凭空补数值；requires_manual_review=false 也不能代替教师核对。有无法辨认、矛盾或缺失条件时保留实际问题和 true；非法结构/越界/错误对应按 VISION_OUTPUT_INVALID 处理。模型给出的语义问题不归类为 Provider 技术错误。
- 除 running 补齐实际结束事实外，已结束轮次的输入、原结果/错误、来源和时间不可改写。新调用追加新轮次，不把人工补充塞进旧机器 conditions，不把技术失败改为 completed。

### 15.4 ImageManualCheck：真实教师核对证据

| 成员 | Schema / 责任 |
| :--- | :--- |
| id / check_no | 服务生成 UUID；check_no 为本所属题跨修订单调递增正整数；同题唯一 |
| context_revision / run_no / run_id / input_refs | 核对时的当前修订及整组输入；run_no 为本题当前最大轮次（从未调用时 0）；run_id 仅在该轮与当前修订相符时引用真实 id，否则 null |
| status / confirmed_conditions | confirmed / unresolved；确认条件为 [{condition_id:服务生成UUID,asset_id,text:非空,evidence_region:合法bbox或null,source_condition_id:实际机器条件UUID或null}] |
| image_findings | 每个当前资产恰一项 {asset_id,finding,reason:非空}；finding 为 conditions_confirmed / no_conditions_needed / unresolved |
| issues / issue_resolutions | 新发现的未解决问题使用本节问题 Schema；处置为 [{issue_id,resolution:resolved/unresolved,reason:非空}] |
| teacher_id / checked_at / explanation | 当前认证且有课程管理权限的真实教师 UUID、服务记录的真实 UTC ISO-8601、非空核对/修正说明；客户端不能指定身份/时间 |

- 必须查看可读的真实原图及本题上下文；核对可以在技术失败后或未使用 Vision 时由教师独立完成，明确保存“人工核对”的来源，原机器错误/未调用事实仍保留。这不是 §12.4 对 technical_error 伪作内容处置或语义通过。
- conditions_confirmed 至少有一条该图的实际确认条件；no_conditions_needed 要说明该图为何不提供额外必要条件，不能用空条件数组代表核对成功。任一图 finding=unresolved 或仍有未解决问题，status 必须 unresolved；confirmed 要求整组逐图有依据、必要条件完整。
- 新发现问题以真实未解决事实追加；后续核对必须对核对时已登记的当前修订机器问题及尚未解决的教师问题逐项保存 issue_resolutions，不因换一条核对记录而丢掉未解决问题。confirmed 不允许新未解决 issues，且所有上述问题须有 resolved 和具体理由；教师可明确说明模型误判，但不能删除原问题或改机器结论。
- 人工请求使用严格非负整数 expected_context_revision / expected_run_no / expected_check_no，并提交 status、实际条件、逐图判断、问题与说明；在锁内比对当前计数，不符返回 IMAGE_ASSESSMENT_STALE；影响上下文的字段/资产修改先独立保存，再提交核对最终输入的命令，不能同次静默改套修订号。当前修订的最新轮次仍 running 时不能确认未结束输出；如需取消，先记录实际取消结果，不能仅为放行写虚假结束时间。
- 资产引用须属于当前整组输入；source_condition_id/issue_id 须来自本题真实已登记记录，条件对应同图同上下文，问题处置不接受陌生/重复 id。每次接受核对只追加真实事件，不覆盖先前教师/UTC/条件/问题。pending 是没有当前已结束核对的读取投影，不存合成教师事件。最新 unresolved 不能回退使用旧 confirmed；模型 true 可在教师逐项解决后由服务投影已核对，但原模型布尔值保持原样。

### 15.5 当前判定、并发与语义核验联动

1. 当前机器轮次取本题最大 run_no，且其 context_revision 必须等于当前值；新轮次 running/technical_error 不回退旧成功。当前教师核对取最大 check_no，要求修订号和 run_no 均匹配当前值；无匹配核对时为 pending，历史核对保留并标记 stale，不改写其原 status。confirmed 还要求输入图真实可读、整组对应及问题已按 §15.4 处理。
2. 图像上下文变化、启动新轮次、追加人工核对及校正/入库/批准共同锁定所属题。暂存操作沿 PaperImport -> ExtractedQuestion 顺序，正式操作沿 §9 既有锁序锁 Question；分配轮次、保存修订/事件与相关内容同事务。外部调用前提交释放锁，结束后重取锁，仅补齐原 id 的实际结果；迟到/被替代结果不改当前条件或题目状态。
3. 仅当本题尚无人工事件和正式图像调用且目标 context_revision 未变时，合法 imported_review 可以提供当前已确认条件（见 §15.6）；其后出现的修订、调用或人工事件不能使旧绑定重新成为当前。is_current/stale、pending/confirmed/unresolved、requires_manual_review 为读取投影，不另存可独立修改的“已核对”布尔值。
4. 图像输入变化按 §12 同事务递增 Question.validation_revision；确认条件、有效核对引用或核对资格实际变化（包括开始新轮次使原核对待重核对）也推进一次。单纯补齐历史迟到结果、迁移定位或 trace 清理不推进。人工确认条件不再递增 context_revision，避免确认自身使其立刻失效；两个计数职责分开，不要求数值相等。
5. G02 QuestionValidationResult 的题图 Evidence.source_data.image_review_ref = {owner_kind:question/extracted_question,owner_id:UUID,check_id:UUID,binding_id:UUID|null}；原生核对引用正式题的实际事件，导入引用原暂存事件且 binding_id 指向本题 imported_review.id。服务在核验输入边界校验真实归属/当前适用性，保存本轮实际使用的确认条件及来源，不只保存短期 Trace。
6. G02 语义核验只读消费当前图片核对，其自身模型调用保存于 QuestionValidationResult；预览/考试/阅卷只读使用冻结题图与核对，阅卷调用沿原 GradingResult/Trace 规则记录。上述消费不新建 ImageUnderstandingRun 或推进图像轮次，只有显式重新理解条件才按本节启动新轮。
7. 必要图像条件未经当前确认时，不批准、不用旧 passed、caption、Provider 布尔值放行。图片核对不代替答案/解析/Rubric 的四项语义核验，正式题仍必须有当前 validation_revision 的最新 passed 报告和教师批准。阅卷必须使用既有冻结题图/核对条件；需要图像而 Provider 不可用或文件缺失时明确待处理，不用 OCR-only 或零分制造成功。

### 15.6 校正确认与正式题关联

- commit 在原批次事务内创建 Draft、沿用资产 id/file_id 与顺序，并逐字段比对实际图像上下文（question_type 映射 type、其他文字/金额按其业务值、真实原来源保持）；只有当前 confirmed 核对、可靠同图同序同定位且未改变实际上下文才建立 ImportedImageReview。
- ImportedImageReview = {id:服务生成UUID,context_revision:正式题初始图像修订号0,input_refs:正式题ImageInputRefs,source_ref:{owner_kind:extracted_question,owner_id:真实暂存题UUID,check_id:当前真实核对UUID,binding_id:null},bound_by:真实确认入库教师UUID,bound_at:实际UTC}。正式 ImageAssessment 初始 runs/manual_checks=[]，imported_review 指向原不可改写事件；不复制或重标其教师/核对时间，不伪装成正式题新 Vision 调用。bound_at 是关联时间，不是重新核对时间。
- 原调用/问题、技术失败、所有教师历史保留在 ExtractedQuestion.image_assessment，经既有来源关系可回溯；转入关联只继承当前确有依据的条件。任一输入不等、stale/pending/unresolved 或无核对时 imported_review=null，正式题保留可靠题图及原来源，completion_status=needs_completion、Draft；不自动补条件或自动 Approved。无题图按既有 [] 规则，不强行建立空图片报告。
- image_assessment、正式资产/文件关系、question_id/Corrected 与整批 commit 同事务；失败全回滚并保留原卷/校正记录。重复 commit 返回同一正式结果，不覆盖后来正式核对或重新复制旧证据。
- Corrected/Rejected 后禁止新图片调用、人工核对或改写原校正内容/已结束证据。终态前已登记的 running 轮次仍可补齐真实最终执行事实，属于历史完成记录，不能改写冻结核对、推进正式题条件或冒充入库时已有证据；数据库失败照实报告，不伪造已保存结果。此窄范围完成规则补充 §13 的终态边界，§14 的物理迁移规则继续适用。
- 后续正式题修订只更新正式 ImageAssessment，历史 binding/source_ref 保留并因计数不匹配失效，不反写原暂存题；改编候选即使复用原字节也按新业务资产身份重新核对，不把父题确认自动当成本题批准依据。

### 15.7 冻结、旧数据与实施边界

- 新调用/人工核对及条件/图像关联变更都是业务修改，继承 Approved 和已发布/历史引用保护；须按原流程合法退回修订，受发布保护时禁止原地修改核对依据，改编另建候选。仅保存终态前已启动调用的迟到历史结束事实不改变既有考试/结果依据。
- 历史两列保持 NULL；旧 caption、OCR、模型自信、布尔标记、校正备注或临时 UI 状态不能回填成已确认条件、教师身份/时间或成功调用。已 Approved/发布题状态及历史结果不因此改写，展示“历史图片核对未知”；未批准旧题按 v2.0 新审核路径建立当前真实核对与语义报告。
- JSON 引用原事件的源暂存题/核对记录不得独立清理；合法清理所属导入/题目时沿既有来源/发布历史保护枚举本节引用，不能留下无从核实的 binding。原文件迁移/备份复用 §14，不增加独立清理/备份系统。
- 后续验证聚焦多图对应、无能力/非法输出/缺图、独立人工接管、教师身份/UTC 与问题处置、A→B→A、迟到轮次、核对条件变化使语义报告失效、同输入转入/差异不继承、批次回滚与发布冻结；修改测试前按仓库要求形成 TCR。本任务只做文档一致性检查，不宣称数据库或服务已经实施。

## 16. T141：组卷意图持久设计（G08）

本节补齐 FR-048/049、G08 和 CHK008；契约见 [exam-assembly.md](contracts/exam-assembly.md)。只定义 v2.0 Exam 扩展，保留第 1–15 节原文；不新增独立表、题目版本、业务代码或迁移，模型/迁移由 T170 承接。

### 16.1 Exam.assembly_constraints

| 字段 | 类型 / 空值 / 默认 | 含义与约束 |
| :--- | :--- | :--- |
| assembly_constraints | JSONB 对象；可空；默认 SQL NULL | 最近一次通过准入校验并成功持久保存的教师组卷意图；组卷成功和组卷失败均保存本次要求，失败保留原卷事实。结构为 AssemblyConstraints，经 Pydantic Schema 校验。 |

这是意图记录，不是选题/题序/实际分值或执行成功的事实源；不保存满意标志、当前总分缓存或可独立改写的关系副本。只保存最近一次意图，不另存成功要求和失败要求两套记录、不新增尝试历史表。数据库提供 NULL 或 jsonb_typeof='object' 的基本形状 CHECK；完整 Schema 及课程/权限/状态由服务校验，按 Exam.id 读取，不新增 JSON 检索索引。

| AssemblyConstraints 属性 | 类型 / 校验 | 语义 |
| :--- | :--- | :--- |
| request | 非空 AssemblyRequest 对象 | 本次教师明确提交的完整要求，复用组卷输入 Schema，不由当前原卷反推 |
| request.course_id | UUID 字符串，等于 Exam.course_id | 原请求课程，不构成第二个可改写课程归属 |
| request.question_count | 严格正整数，1–200 | 要求题数；100 题仍仅为性能评测规模 |
| request.type_distribution | [{question_type,count}] | SINGLE_CHOICE/TRUE_FALSE/SHORT_ANSWER；题型去重，count 为严格正整数，合计等于 question_count |
| request.knowledge_coverage | [{knowledge_point,min_questions}]，允许 [] | 规范知识点标签非空、去重、精确匹配；min_questions 为严格正整数且不超过 question_count；[] 表示无额外知识点覆盖要求 |
| request.total_score | 正数有限十进制字符串；输入最多两位小数，存储为两位 | 教师要求总分，不套用单题 Numeric(8,2) 上限；不是原卷实际总分 |
| request.score_overrides | [{question_id,score}]；省略时保存 [] | question_id 为去重 UUID，准入时核对同课程/已审核；score 为正数有限两位金额、不超 999999.99；未选中题的合法 override 不表示必须选该题 |
| recorded_by | 非空 UUID 字符串 | 来自当前有权限教师的真实 User.id，由服务填写；JSON 内身份不是 SQL FK |
| recorded_at | 非空 UTC 日期时间字符串 | 服务记录本次意图写入时间；不是组卷成功时间或教师发布确认，不在重启/预览/PATCH 时刷新 |

DTO 拒绝未知字段、布尔冒充整数、重复条目、非法题型/空标签/金额；写入前核对考试存在、教师权限、课程一致、Draft 且无答卷/历史保护，以及请求引用的 override 身份/归属/资格。不合法请求（422）、越权/不存在（403/404）、发布/历史不可编辑拒绝不写入意图；这些请求未获准执行组卷，不能为了“记录失败”绕过 Schema 或冻结。候选缺题、条件冲突、策略未找到或准入后的真实技术失败属于应保存要求的组卷失败。

客户端不能直接 PATCH 存储 JSON、伪填教师/时间或将非空要求置 NULL 来绕过发布检查。持久对象加载也按同一 Schema 校验；历史 override 中未选中题后来不可用不使原请求结构变非法，重试时再按当前状态做完整准入和选题。SQL NULL 才是无额外意图；JSON null、空对象、缺字段/非法结构不得静默降级。

### 16.2 成功与失败的写入边界

1. 同场组卷按契约串行处理，事务锁定 Exam 并确认可编辑性；通过准入后建立当前 request/recorded_by/recorded_at。组卷成功在同一事务保存本次意图及全部 ExamQuestion 选题、连续题序和显式分值，提交成功才返回 200。
2. 组卷失败仍提交本次意图，但原 ExamQuestion 身份、选题、题序、score/base_score/发布依据保持不变；不能落入半成组合、重放新 override 或改写 Question.score。可用事务保存点隔离事实写入：事实写入失败回滚事实部分，再在仍有效的同场事务只提交意图；不能将整个事务回滚后误称失败要求已保存。业务不满足在意图提交后返回真实 409。
3. 技术失败保留真实错误及可用诊断；若数据库/事务故障同时导致意图无法保存，明确返回原失败和意图保存错误，不声称已满足本条。无法确认提交结果时显示保存状态未知，不填假成功，也不盲目覆盖或无限重试；教师重新加载读取数据库实际记录。
4. 原卷事实与意图提交必须遵守同场锁/固定顺序；不得在释放锁后由迟到失败回调覆盖较新意图，或在并发发布/历史保护成立后继续写失败要求。失败响应送出前已知保存结果；不同考试的意图、分值和题序独立。
5. 每次合法新组卷成功或失败都替换最近意图；仅人工替换/移位/改分不替换意图或 recorded_by/recorded_at。保留失败要求便于教师重新加载、调整输入再重试，不引入第二个“当前有效成功要求”。

### 16.3 当前原卷重算与事实源

- 预览/重启加载从 Exam 读取最新保存意图，按当前原卷关系重算 conditions、实际 total_score 与 publication_checks。例如原卷 2 题，本次要求 3 题且失败：保留 2 题原卷、保存 3 题意图，当前数量指标显示 target=3、actual=2、satisfied=false；不能显示旧目标为已满足。
- 题数来自当前 ExamQuestion；题序来自 order_index；实际分值来自 ExamQuestion.score（仅草稿 NULL 时按 §7.6/§9.2 取当前 Question.score），用 Decimal 精确求和。JSON total_score 只作目标比较，不能用来替代实际求和。
- 知识点按当前草稿关联题的规范标签精确计数，同题可覆盖多个知识点但不重复累计整卷题数/分值；合法题库元数据维护后重算草稿，不复用旧满意缓存。
- request.score_overrides 记录本次尝试的意图。成功组卷才将所选题对应值落入 ExamQuestion.score；失败不应用。以后预览、人工调整或发布不从 JSON 重放 override；替换保留位置原有本场 score，除非本次 PATCH 显式改 score/null。null 恢复题库默认，不恢复旧 override。
- 人工调整可暂存不满足最新要求的草稿，展示真实缺口；不得静默降低目标或把目标改成当前结果。再次成功/失败组卷才更新意图。
- 失败诊断的候选不足/搜索未找到是本次尝试结果，与保留原卷对最新意图的实际满足情况分开。原卷可能已满足最新意图；不得仅因上次执行失败永久禁止发布，也不能仅凭要求已保存或上次组卷成功允许发布。

### 16.4 发布、旧数据与实施边界

发布事务重新核对最新意图与当前实际原卷，全部约束及候选/图片/语义/评分依据检查通过才发布；冻结 assembly_constraints（含合法 NULL）及 §7.6 的本场关联/分值/知识点/评分依据。Published/Closed/Archived 或已有答卷/评分/复核历史的考试不写入新意图，失败请求也不能绕过冻结。历史核对使用冻结的 ExamQuestion.score/published_knowledge_points，不受题库元数据或请求 override 漂移影响。

v1.0 旧考试，以及沿原创建/手动选题接口新建且未保存合法组卷意图的考试，合法缺省为 SQL NULL；条件指标为 []，表示无额外数量/题型/知识点/总分目标，仍显示实际组合总分并执行完整发布资格检查。不得从现有选题推造要求、教师身份/时间；首次合法组卷即使失败也会将 NULL 改为本次真实意图。

非空坏数据应显式报告并阻止按未知要求发布，不因会话丢失、条件不满足或解析错误重置 NULL。E4 只新增可空字段并保留旧 NULL；历史题序/分值/评分依据按 §10/T144/T170 的真实证据处理。本节及契约中“失败保留原卷/不写入”专指不写原卷事实，允许且要求独立于成功与否保存本次合法意图；后续 T179/T145 验证须按此已确认语义核对，不修改其他任务状态或 v1.0 原规则。
