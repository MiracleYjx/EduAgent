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
| SemanticValidationInput | 候选核验字段投影（不含仅作题库分类维护的 difficulty/knowledge_points）、同课程来源 Chunk/真实引用快照、参考答案、Rubric、解析、图片引用及已核对条件；由服务绑定 input_revision 和轮次，答案/Rubric 为候选字段的同源投影，不维护另一份独立可变答案 |
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
- 核验报告和人工处置应持久化，不以 UI 内存或自由文本覆盖原结果；QuestionValidationResult 的存储、输入修订号/轮次、真实执行来源及教师处置由 [data-model.md](../data-model.md) §12 明确，消费遵循下文 T135 边界，后续代码与迁移另行实施。
- Approved 内容修改先退回修订；受发布/历史保护题不能原地退回或改变内容，需创建派生候选，遵守 [exam-assembly.md](exam-assembly.md)。

### T135：报告持久化、当前输入与人工处置

持久字段、JSON Schema 与责任以 [data-model.md](../data-model.md) §12 为唯一模型定义；本节规定核验节点与 Question 审核服务如何共同消费，不新增独立版本系统或修改原阅卷图。

- 每个已持久 Question 对应多轮 QuestionValidationResult，outcome 为 running/passed/failed/technical_error；报告保存当轮 checks/issues、实际输入/证据、真实 Agent/Provider 来源、错误与时间。QuestionRevisionComment 继续只表达真实教师文字意见，机器报告不得借教师身份写入。
- Question.validation_revision 从 0 开始，在相关题目内容、解析、基准分值、图像条件或关键依据变化时由责任写入方同事务递增；Needs Revision -> Pending Review 重新送审也递增。元数据 difficulty/knowledge_points 的单纯分类维护保留，路径/追溯版本变化不成为相等门禁；影响真实依据的操作按依据变化处理。
- 启动时锁定 Question，捕获 input_revision、实际证据及 MAX(run_no)+1，保存 running 后释放事务锁再调用。完成时重新锁定，只对修订号一致、run_no 为最新且 Question 仍 Pending Review 的报告执行审核状态副作用；即使修改后改回原文，旧修订号也不能匹配。报告不能将启动修订号换成完成时当前值。
- input_refs 指向当轮真正使用的 Chunk/来源快照及原图/已确认条件；片段证据保存原身份、位置与实际 content_snapshot，不用当前资料重建历史。检查/问题引用仅允许本报告内真实 evidence_id；图像核对的具体持久映射由 G05 承接。AgentRun 链接可按 Trace 生命周期清空，报告实际执行来源仍保留；配置模型与短期 Trace 不能冒充已完成语义核验。
- 合法输出完整保留四项 checks 和问题；未获得合法业务输出时 checks/issues 为 null，与合法无问题 [] 区分。Provider/Schema/取消/超时失败保留原错误分类和真实阶段，outcome=technical_error，不能填 pass 或“答案错误”。
- 最新有效报告由最大 run_no 且 input_revision 等于当前修订号派生；无报告、stale、running、failed、technical_error 阻止批准，不能在最新失败/执行中时回退到旧 passed。当前 passed 仍须核对完整字段、关键依据、图像核对及未解决问题；can_review/is_current 等由服务计算，不持久保存可独立改写的成功布尔值。
- 当前 failed 报告完成与合法 Pending Review -> Needs Revision 在同一事务；passed 保持 Pending Review，等待教师。technical_error 只保存错误并阻止批准，保持 Pending Review；数据库提交失败保留真实错误，不能宣称报告/状态已落库。迟到或被替代结果保留历史，不改变当前状态。
- manual_dispositions 在报告内采用受 Pydantic 校验的追加数组，关联具体 issue/check、当前 input_revision、动作、真实依据、教师 UUID/UTC 时间/说明及可选真实文字意见 id。写入时验证报告为当前最新且已结束的 passed/failed；running/technical_error 只显示真实进度/错误，不伪造内容处置。历史只读，不能覆盖原 checks/issues/error、删除问题或改写 outcome。
- 人工 provide_evidence/resolve_issue 后按既有流程重新核验；接受这类处置后，即使原报告为 passed，也须从处置数组派生待重核验并阻止批准，处置不直接变成 Approved/成功报告；下轮显式使用的处置记录进 input_refs.manual_context，内容变化后不自动继承已解决结论。人工 request_revision 与实际文字意见/状态同事务；机器自动退回不创建虚假教师意见。
- 批准门禁同时应用于 Question 状态更新/approve、候选审核及后续导入/改编路径，不能只放在一个 UI/API；服务在批准事务内重核对当前报告及发布保护，Agent 不写 Approved。
- 尚无正式 Question 的 Candidate Generation 只返回 DTO/建议，不虚构 report.question_id 或完成数据库转换。历史题无报告显示历史核验未知，已有 Approved/发布状态与结果保持；进入 v2.0 新批准/修订重审时建立真正的当前报告，不用既有字段校验状态伪造语义通过。

### 试卷导入 Agent（若引入）

- 不新增必需 Agent，也不改四类 Agent 的既有责任；可选辅助组件只协助拆题/归一化、提示缺失字段或转换候选 DTO，人工校正主导。
- ExtractedQuestion -> Question 的实际持久化由导入服务执行 [paper-import.md](paper-import.md) 的 commit；须先获得有权限教师确认，不由 Agent 自行入库/批准。
- 正式 Question、来源关系/题图及 ExtractedQuestion.question_id/Corrected 同事务提交；失败回滚，重复确认返回既有正式题。
- 原题可缺答案入库为 Draft/待补全，不能由 Agent 凭空补标准答案，不能因 PaperImport.Ready 自动变为 Approved。
- 原始页图、跨页/图像关系和校正依据保持可追溯；之后修订正式题走既有审核与发布保护，不反写已确认导入记录。

### 扩展验证边界

后续验证覆盖有图/无图/不支持 Provider、结构化失败、语义问题/缺依据、修订后重核验、教师确认入库及发布引用保护，并覆盖内容改回/元数据维护、最新失败不回退、迟到结果、报告与退回事务、真实人工处置和多个批准入口共用门禁；保留 v1.0 原拓扑和验证证据。
T135 仅补齐数据模型与本契约，并标记本任务；不修改业务实现、迁移、测试或其他任务状态。后续测试修改先形成 TCR，文档定义不作为运行验收证据。
