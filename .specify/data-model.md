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
| source_page_id | UUID；可空；FK SourcePage.id | 可追溯原页；原题导入必须有真实原页，不伪填历史未知来源 |
| region | JSONB；可空 | 形如 {bbox:[x0,y0,x1,y1]}，在 source_page 原图坐标系表示裁图区域 |
| created_at | TIMESTAMPTZ；非空；当前 UTC | 资产关系创建时间 |

索引/约束：索引 question_id、source_page_id；CHECK asset_type 枚举、width/height>0、region 为对象或 NULL。有 bbox 时需 source_page_id 非 NULL，四个坐标满足 0<=x0<x1<=页宽、0<=y0<y1<=页高；同课程和页尺寸由服务校验。引用整页图可 region=NULL，不能捏造裁图框。

无独立状态枚举：是否可修改继承 Question 的 Approved/发布保护；文件是否缺失由文件服务显式报告，图像条件是否可靠由结构化核验/人工校正表达，不靠 caption 或创建成功判断理解完成。新增/替换/删除资产、修改 bbox/图示条件须重核验题干、答案、解析和评分标准；Approved 内容守卫扩展到题图，受发布保护时禁止变更题图集合、图像内容、关联或覆盖原文件。每题最多 5 张由事务内锁定题目后确认计数，不能用单行 CHECK 伪称已限制集合大小。

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
