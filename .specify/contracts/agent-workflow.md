# Agent and LangGraph Workflow Contract

## Agent Responsibilities

| Agent | Contract |
|---|---|
| Supervisor Agent | 可独立调用的确定性离线路由组件；根据显式任务种类、输入与只读状态返回工具/Agent 路由或暂停/完成建议，不接入生产 Workflow |
| Question Agent | 接收出题条件和检索上下文，返回结构化候选题目 |
| Grading Agent | 接收主观题评分输入和 Final Context，返回结构化评分候选 |
| Reviewer Agent | 接收评分结果、理由和知识点，返回接受、修订或重新评分决策 |

Supervisor 的 `decide` 不调用模型，也不执行 Agent 或修改状态。生产阅卷入口只接收阅卷任务；
`Classify Question` 和后续图节点、Workflow 服务及教师决策承担实际分流、暂停、恢复和结束。
独立调用得到的 `route`/`pause`/`finish` 是建议而非生产执行事实；不能把它当作图节点或
生产 Workflow 的前置条件。未来若接入跨任务生产路由，须另行验证决策在实际路径中被消费。

## Grading Workflow States

```text
START
  -> Load Submission
  -> Classify Question
  -> Rule Grade or Retrieve Context
  -> Grade
  -> Structured Validation
  -> Confidence Check
  -> Accept or Pending Review
  -> Reviewer/Re-grade when required
  -> Unified Result
  -> Generate Diagnosis
  -> END
```

## Required State

```text
workflow_id
submission_id
current_answer_id
question_type
retrieved_context_ids
grading_result
validation_status
confidence
review_status
final_results
diagnosis
error
```

## Contract Rules

- Question classification must route Objective and Subjective answers separately.
- Objective grading must be deterministic and must not call an LLM.
- Subjective grading must include question, reference answer, rubric, student answer and course
  retrieval context.
- AI-generated and AI-graded results must pass structured validation before business consumption.
- Confidence below the configured threshold must set `Pending Review` and pause the Workflow.
- Only Teacher can confirm or modify a low-confidence result.
- After review, the Workflow must resume and preserve the original run and review trace.
- Diagnosis must consume only accepted or manually reviewed final results.

## v2.0 图像与核验扩展

本节对应 [spec.md](../spec.md) FR-041–FR-043、FR-046、FR-047 和 FR-049，补充出题/原题导入路径；上文四类 Agent 职责、生产 LangGraph 阅卷拓扑、低置信度 Pending Review/教师复核流程保持不变。
下述 DTO、节点和状态消费是目标设计，不表示当前代码已实现，不把 Supervisor 路由建议升级为生产执行事实。

### 图片输入边界

- Agent 输入可包含 QuestionAsset 引用：question_asset_id、question_id、file_id，以及真实来源页/region；暂存题使用 ExtractedQuestion 的原图引用，不伪造尚未创建的 QuestionAsset。
- 服务先核对题目/暂存题、课程/考试授权及文件可读性，按 [vision-capability.md](vision-capability.md) 构造结构化图片输入；客户端引用不能直接成为任意本机路径或 URL。
- 图片理解调用 BaseLLMProvider.supports_vision()；不支持返回 VISION_NOT_SUPPORTED，依赖未就绪、调用失败、文件缺失分别保留原码/原因，不静默改成文本调用或更换供应商。
- 通过 generate_structured 返回 VisionResult，经 Pydantic 及来源/坐标对应校验后才能写入 Agent 状态；OCR 文本、caption 或未校验自由文本不能冒充图像条件。
- 状态中的 image_inputs、vision_result、verified_image_conditions、image_review_status 均为校验过的逻辑 DTO；原图保留，不可靠条件标记待人工核对，不能用模型自报“已理解”替代教师确认。
- 无能力/不可靠时仍保留导入校正入口；已有可靠人工确认条件可以继续审核，缺必要条件则阻止批准。修改图片/相关条件后重新核对题干、答案、解析及 Rubric。
- 阅卷引用同一本场冻结图片/已核对条件及 [exam-scoring.md](exam-scoring.md) 的有效分值；客观题仍走规则，不因有图调用 LLM。

### 语义核验节点

节点针对出题/改编/补全后的候选，报告问题与依据，不自动改写题干、选项、答案、解析或 Rubric。

| DTO | 输入/输出与约束 |
| :--- | :--- |
| SemanticValidationInput | 候选题、同课程来源 Chunk/真实引用快照、参考答案、Rubric、解析、图片引用及已核对条件；答案/Rubric 为候选字段的同源投影，不维护另一份独立可变答案 |
| SemanticValidationResult.checks | 分项检查：答案正确性、条件充分性、选项歧义、评分标准明确性；每项含 verdict（pass/fail/insufficient_evidence/needs_review）、reason、evidence_chunk_ids/原图证据 |
| SemanticValidationResult.issues | [{code, field, severity, message, evidence_refs}]；severity 为 info/warning/error，未知依据不能伪造 chunk_id |
| SemanticValidationResult.can_review | 是否已满足进入教师批准阶段的核验前提；由服务根据完整字段/未处置问题/关键依据/图片核对计算，不直接信任模型布尔值，也不等于自动 Approved |
| SemanticValidationResult.requires_manual_review | 是否仍需教师明确核对/处置；问题及依据可展示，false 不能代替批准 |
| SemanticValidationResult.error | 技术失败的真实 code/message 或 null；不把调用故障包装成“答案错误” |

- 核验输入/输出均经 Pydantic 校验，保留实际来源和分项证据；形式合法只证明结构，不保证答案正确。
- 未解决问题、关键依据不足、必要字段待补全或图像条件未确认时 can_review=false；教师仍可查看/校正，不能因不可批准隐藏待处理问题。
- 核验是辅助意见；教师有依据地修正/处置问题并重新核验后才可批准，不能只删除问题列表或忽略严重度完成审核。

~~~text
现有候选生成/人工建题 -> 按既有流程送审 Pending Review
  -> 图片理解/必要人工核对
  -> 语义核验
      -> 通过：保持 Pending Review，等待教师批准
      -> 不通过：Needs Revision，保存问题与依据
Needs Revision -> 教师修订/明确处置 -> 再提交 Pending Review -> 重核验
~~~

- 核验不通过由 Question 服务在合法 Pending Review -> Needs Revision 转换中保存状态和结构化报告；Agent 只返回报告，不直接执行数据库修改或 Approved。
- 既有 QuestionCandidate.status 保持 Candidate Generation 的输出语义；它不是持久 Question 的审核状态。尚未持久化的候选仅返回问题/修订建议，不伪造已完成状态转换。
- 调用/Schema 失败保留真实技术错误并阻止批准，不伪造成功报告或内容错误；不能自动降级、无限重试或用旧核验通过结果放行。
- 题干/选项/答案/解析/Rubric/图片条件及关键依据变化后旧核验失效；重新提交/批准必须消费与当前内容对应的报告。
- 核验报告和人工处置应持久化，不以 UI 内存或自由文本覆盖原结果；QuestionValidationResult 的存储承载实施前补齐数据模型，本次不修改模型或代码。
- Approved 内容修改先退回修订；受发布/历史保护题不能原地退回或改变内容，需创建派生候选，遵守 [exam-assembly.md](exam-assembly.md)。

### 试卷导入 Agent（若引入）

- 不新增必需 Agent，也不改四类 Agent 的既有责任；可选辅助组件只协助拆题/归一化、提示缺失字段或转换候选 DTO，人工校正主导。
- ExtractedQuestion -> Question 的实际持久化由导入服务执行 [paper-import.md](paper-import.md) 的 commit；须先获得有权限教师确认，不由 Agent 自行入库/批准。
- 正式 Question、来源关系/题图及 ExtractedQuestion.question_id/Corrected 同事务提交；失败回滚，重复确认返回既有正式题。
- 原题可缺答案入库为 Draft/待补全，不能由 Agent 凭空补标准答案，不能因 PaperImport.Ready 自动变为 Approved。
- 原始页图、跨页/图像关系和校正依据保持可追溯；之后修订正式题走既有审核与发布保护，不反写已确认导入记录。

### 扩展验证边界

验证有图/无图/不支持 Provider、结构化失败、语义问题/缺依据、修订后重核验、教师确认入库及发布引用保护；保留 v1.0 原拓扑和验证证据。
本步骤只追加契约，不修改实现、迁移或测试；任何后续测试修改先形成 TCR。
