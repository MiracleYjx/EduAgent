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
