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
