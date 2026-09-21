# Agent and LangGraph Workflow Contract

## Agent Responsibilities

| Agent | Contract |
|---|---|
| Supervisor Agent | 接收任务上下文，返回工具/Agent 路由和 Workflow 控制信号 |
| Question Agent | 接收出题条件和检索上下文，返回结构化候选题目 |
| Grading Agent | 接收主观题评分输入和 Final Context，返回结构化评分候选 |
| Reviewer Agent | 接收评分结果、理由和知识点，返回接受、修订或重新评分决策 |

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
