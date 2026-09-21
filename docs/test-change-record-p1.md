# P1.1 执行归属分离测试变更说明（TCR）

日期：2026-09-19。范围：只处理 M3 后台任务与 M4 LangGraph 工作流共用 `workflow_runs` 表时的读写归属判别（复盘 H05 的归属部分）。不修改评分、事务与诊断逻辑，不新增数据库列（复用 `WorkflowRun.checkpoint` JSON），不迁移测试路径。

本记录在新增测试前形成，沿用项目 pytest、真实仓储/检查点存储与 SQLite 隔离数据库的测试方式；保留既有断言，不用源码文本匹配证明归属语义。

## 事实基线

- M3 后台任务检查点：`grading_repository.CHECKPOINT_KIND = "background-task-checkpoint"`。
- M4 业务状态载荷：`state.WORKFLOW_STATE_PAYLOAD_KIND = "langgraph-grading-state-payload"`，由 `workflow_state_to_checkpoint_payload()` 写入顶层 `kind`，并被 `workflow_state_from_checkpoint_payload()` 严格校验。
- 冲突点：`api/workflow.py` 的最近运行、`api/reviews.py` 复核详情的运行查找都不带 kind 过滤，可能读到 M3 行；`grading_repository.find_task_for_submission`/`get_task` 也不带过滤，可能把 M4 行当成后台任务。
- 方案（经用户裁决为方案 A）：沿用上述两个既有 kind 值做归属判别，不新增键、不改 T065 载荷契约。

## 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/services/test_workflow_checkpoint.py` 新增 `test_saved_checkpoint_carries_workflow_owner_kind` | 固定“M4 写什么 kind”，并证明归属过滤条件确实能选中本执行器的行 | 保存后载荷 `kind == WORKFLOW_RUN_KIND == WORKFLOW_STATE_PAYLOAD_KIND`、版本一致且与 M3 kind 不同；`run_kind_criteria()` 只选中该行 |
| 同文件新增 `test_save_checkpoint_rejects_foreign_executor_row` | 原实现会在同一运行记录上覆盖 M3 行，需要反向证据 | M3 kind 行上写入业务状态抛 `WorkflowCheckpointOwnershipError`（`WORKFLOW_CHECKPOINT_OWNERSHIP_MISMATCH`），且原载荷、节点与状态字段不被改写 |
| 同文件新增 `test_list_resumable_ignores_foreign_executor_rows` | 恢复列表原先会列出 M3 行 | 标记 `resumable=True` 的 M3 行不出现在全局与按 `workflow_id` 的恢复列表中 |
| `tests/unit/grading/test_grading_repository.py` 新增 `test_task_queries_ignore_other_executor_runs` | M3 查询原先会把 M4 行当后台任务返回 | M4 kind 行下 `get_task`/`find_task_for_submission` 返回 `None`、中断收敛返回 0；随后 M3 任务仍可正常创建与查询，且 M4 行保持原状态 |
| `tests/contract/test_workflow_api_contract.py` 新增 `test_start_ignores_background_task_run_for_same_submission` | 启动幂等必须有正式入口的反向证据（H05 前向冲突） | 同答卷存在 M3 待复核行时，`POST /api/workflow/submissions/{id}/runs` 仍创建 M4 运行（两行并存），M3 行原样保留；重复启动复用 M4 运行而不新增行 |
| `tests/contract/test_reviews_api_contract.py` 新增 `test_detail_ignores_background_task_run_for_same_submission` | 复核详情按“最近运行”取线程身份，更新的 M3 行会顶替 M4 身份 | 追加更新的 M3 行后，详情仍返回 M4 的 `workflow_id`、`thread_id` 与 `WorkflowStatus.PAUSED` |

复用现有测试验证、未放宽的既有断言：异类检查点隔离（`mark_interrupted_tasks_failed`）、`workflow_runs` 幂等启动与冲突语义、`resumable` 必须由持久 runtime 检查点支撑、检查点身份与所有权校验、M3 任务状态映射。

## 验证记录

- 正向（改动后）：`pytest tests/unit/grading/test_grading_repository.py tests/unit/services/test_workflow_checkpoint.py tests/contract/test_workflow_api_contract.py tests/contract/test_reviews_api_contract.py -q -k "other_executor or workflow_owner_kind or foreign_executor or background_task_run"` 为 **6 通过**。
- 反向对照（临时 `git stash push` 掉 5 个后端归属改动后重跑同一选择）：`test_task_queries_ignore_other_executor_runs`、`test_start_ignores_background_task_run_for_same_submission`、`test_detail_ignores_background_task_run_for_same_submission` **3 个失败**；检查点存储文件因 `WORKFLOW_RUN_KIND` 不存在而收集失败。反向验证后已 `git stash pop`，5 个后端文件按 blob 哈希核对还原一致（`files-restored=True`，`git stash list` 为空）。
- 聚焦回归：`pytest tests/unit/grading/test_grading_repository.py tests/unit/services/test_workflow_checkpoint.py tests/unit/grading/test_grading_task_service.py tests/contract/test_workflow_api_contract.py tests/contract/test_reviews_api_contract.py -q` 为 **140 通过**。
- 提交门禁：`pytest tests/ -q` 为 **1329 通过、1 跳过、19 条既有警告**（基线 1323 通过 + 本次 6 个新用例）；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。
- 唯一跳过项仍为 M0 容器冒烟（缺少该用例要求的独立 Compose 项目与隔离端口配置），本次未修改 M0 环境或放宽条件。

## 未覆盖与边界

- 本步只做“谁能读到/写到哪一行”的归属判别；M4 正式结果落库（H01）与诊断顺序留待 P1.2/P1.3，因此本次没有新增端到端的成绩落库断言。
- 归属判别在 SQLite 与真实 PostgreSQL 上用同一 SQL 表达式（JSON 列 `kind`）表达；本步没有新增 PostgreSQL 专属并发用例，真实库并发边界由既有 M3 隔离与后续 P1.2 事务验收覆盖。

## P1.2 Workflow 结果事务写入

日期：2026-09-21。范围：只处理 H01 的“结果持久化责任”——把 M4 一次运行/恢复的结论（单题结果、决策快照、整卷结果、答题进度、工作流业务状态）在**一个事务**内写入；不修改评分算法、图内节点顺序与诊断入口（诊断顺序留 P1.3），不新增数据库列或迁移。

### 事实基线（勘察结论）

- M4 图的 `Unified Result` 节点产出 `exam_result`（`ExamResultDTO`，带 `is_final`）；`Generate Diagnosis` 节点在其后执行（顺序修正在 P1.3）。
- 暂停在人工复核时，返回状态里**没有** `exam_result`，只有逐题 `grading_results` 与 `confidence_decisions`，必须由 `ResultAggregator` 现场汇总为待复核整卷结果。这段逻辑原先只存在于 T079 集成测试的 `_persist_paused` 辅助函数里（测试补做持久化），本步把它移入正式入口。
- 失败路径由图写入 `exam_result=None`，因此“状态是否携带 `exam_result`”可以干净地区分【最终/待复核】与【评分或结构化失败】两类事实。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/grading/test_grading_repository.py` 新增 `test_save_workflow_outcome_writes_results_and_state_in_one_transaction` | 需要证明结果与状态确实一次提交，而不是两次提交 | 单事务写入两条 `GradingResult`、最终整卷结果（`is_final=True`、`ExamResultStatus.FINAL`、总分 16.00）、答案与答卷 `Graded`/`graded_at`，且 `WorkflowRun.exam_result_id` 指向同一行 |
| 同文件新增 `test_save_workflow_outcome_rolls_back_when_state_write_fails` | 状态写入失败时不得留下半成品成绩 | `state_writer` 在写入运行行后抛 `WorkflowCheckpointError`：评分行、整卷结果、运行行全部不存在，答案状态与 `graded_at` 与调用前一致（按调用前后快照比对） |
| 同文件新增 `test_save_workflow_outcome_aggregates_pending_exam_result` | 暂停分支需要证明“待复核整卷结果”由仓储汇总而不靠测试补做 | 只给逐题结果与决策时写出非 final 整卷结果（`ExamResultStatus.PENDING_REVIEW`、`final_total_score=None`）与单题决策快照（`decision_requires_review=True`、阈值 0.8、`review_status=Pending Review`） |
| 同文件新增 `test_save_workflow_outcome_marks_answers_failed_without_grades` | 失败分支必须与“写成绩”区分开 | 未完成答案被标为 `Failed`，且不写任何 `GradingResult`/`ExamResult`，运行行不关联整卷结果 |
| `tests/contract/test_workflow_api_contract.py` 新增 `test_start_persists_pending_review_outcome` | 需要正式启动入口的端到端证据（H01：复核队列原先看不到新答卷） | `POST /api/workflow/submissions/{id}/runs` 之后：待复核题评分行在库且 `review_status=Pending Review`、`decision_review_status=Pending Review`；整卷结果非 final 且为 `Pending Review`；运行保持 `Paused`（含暂停原因、`resumable=True`、已关联整卷结果）；答卷状态为 `Graded` |
| 同文件新增 `test_start_persists_final_result_for_accepted_run`（配合新增替身 `_AcceptedSubjectiveAgent`） | 需要高置信度路径的正式入口证据 | 全部自动接受时运行 `Completed`，整卷结果 `is_final=True`、`ExamResultStatus.FINAL`、总分 16.00，答案/答卷为 `Graded`，运行关联最终整卷结果 |
| 同文件修正 `_record_decision` 辅助函数 | P1.2 之后正式入口已写入同一评分行，原实现无条件再插一行会撞唯一约束 | 按 `answer_id` 复用既有评分行；教师结论语义不变 |

复用现有测试验证、未放宽的既有断言：M3 `save_outcome` 事务、任务状态映射、中断收敛、检查点身份与所有权、幂等启动与冲突语义、复核恢复的部分成功回执。

### 验证记录

- 新增仓储用例：4 通过；正式入口用例：2 通过。
- 聚焦回归：`pytest tests/contract/test_workflow_api_contract.py tests/unit/grading/test_grading_repository.py -q` 44 通过。
- 提交门禁：`pytest tests/ -q` 为 **1335 通过、1 跳过、19 条既有警告**（基线 1323 + P1.1 六个新用例 + P1.2 六个新用例）；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。
- 唯一跳过项仍为 M0 容器冒烟（缺少该用例要求的独立 Compose 项目与隔离端口配置），本次未修改 M0 环境或放宽条件。

### 未覆盖与边界

- **图内诊断顺序未改（属 P1.3）**：生产高置信度路径仍会在 `Generate Diagnosis` 阶段因“整卷结果尚未落库”而失败，P1.2 不承诺该路径的 `Completed`。本次验收只以“三种事实按正式入口落库并可读回”为准。
- **发现（预存在，不在本步范围）**：M4 图返回并写入检查点的 `grading_results` **偶发缺失已评完题目**。同一契约场景连续 4 次运行中有 2 次只保留待复核题（客观题结果缺失），且持久检查点与落库行同步缺失，说明丢失发生在图/状态合并阶段而不是写入阶段。影响：暂停时的待复核整卷结果可能少计一题；恢复时 `Unified Result` 的“缺题显式失败”门槛可能触发。建议在 P1.3 或后续单独定位通道合并语义并补反向用例；本次未修改图内节点，也未据此放宽任何断言（仅将契约用例的行数断言改为“待复核题必在库且无多余答卷”）。

> 该现象已在 **P1.2.5** 定位并修复：根因不是图/通道丢数据，而是题序不确定，详见下节。

## P1.2.5 题序确定性修复

日期：2026-09-21。范围：修 `Exam.questions` 题序不确定问题；不改评分算法、不改 T073 Checkpointer 接口、不新增数据库列或迁移。

### 根因（已复现并证实）

`exam_questions` 关联表只有复合主键 `(exam_id, question_id)`，**组卷题序从未被持久化**；`Exam.questions` 关系没有 `order_by`；而阅卷快照用 `enumerate(exam.questions, start=1)` 生成 1 基题序。当数据库按主键索引返回关联行时，行序即 `question_id` 升序（随机 UUID），于是：

- 10 次同一契约场景连续运行：5 次题序为 `[客观题, 主观题]`（暂停在题序 2，落库 2 条结果），5 次题序为 `[主观题, 客观题]`（低置信度主观题成为题序 1，运行在题序 1 暂停，只评完一题、只落库 1 条结果）；
- 10/10 次的“题序 1”都等于两道题中 `question_id` 字典序较小者（相关性 100%）；
- 与并发、题目数量、上下文规模无关；状态、持久检查点与落库行三者始终一致 —— **不是通道合并或 Checkpointer 丢数据**，而是题序随随机 UUID 翻转。

影响：暂停时的待复核整卷结果会缺题（题目与题序错配）、恢复时 `Unified Result` 的“缺题显式失败”门槛可能被触发。

### 修复

| 位置 | 改动 |
| --- | --- |
| `backend/app/models/exam.py` | `Exam.questions` 增加 `order_by="Question.created_at, Question.id"`，使组卷、阅卷快照、成绩读模型看到同一顺序 |
| `backend/app/services/grading/grading_task_service.py` | 新增 `_question_order_key` 并在 `_build_snapshot` 里显式排序后再生成题序，不依赖数据库返回顺序 |

边界：`exam_questions` 没有顺序列，所以“题序 = 题目创建顺序，并列时按题目标识”；如需显式组卷顺序，应单独新增顺序列与迁移（本次不做）。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/grading/test_grading_task_service.py` 新增 `test_real_reader_question_order_is_deterministic` | 需要确定性反向例：旧实现直接使用数据库返回顺序 | 把“创建时间顺序”与“题目标识字典序”显式设成相反，断言关系列表与快照题序都按创建时间；旧实现必然失败 |
| `tests/integration/test_langgraph_grading_workflow.py` 在暂停用例中新增不变量 | 需要图级证据：暂停时题序 ≤ 当前题序的题目逐题结果必须齐全 | 断言 `set(state["grading_results"]) == {题序 ≤ current_answer_order 的答案}`；旧实现会因题序翻转只剩一题 |
| `tests/contract/test_workflow_api_contract.py` 收紧待复核用例 | 需要正式入口证据：题目数与题序确定 | 断言落库恰好两题；待复核整卷结果的 `items` 题目数等于提交答案数，题序 1 为客观题、题序 2 为主观题（此前只断言“待复核题在库且无多余行”） |

### 验证记录

- 复现（旧实现）：10 次独立运行，5 次题序翻转 ⇒ 5 次只有 1 条逐题结果；题序 1 == `question_id` 最小者 10/10。
- 修复后连续 20 次独立运行：**20 通过 / 0 失败**（同一契约待复核用例）。
- 反向对照：临时 `git stash` 掉两处生产改动后，新题序用例确定性失败（关系列表按 id 升序）、契约待复核用例失败；`stash pop` 后按 blob 哈希核对还原一致。
- 聚焦回归：`pytest tests/unit/grading/test_grading_task_service.py -k reader -q` 5 通过；`pytest tests/integration/test_langgraph_grading_workflow.py -q` 5 通过。
- 提交门禁：`pytest tests/ -q` 为 **1336 通过、1 跳过、19 条既有警告**；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。

## P1.3 诊断生成顺序（结果事务提交后再生成诊断）

日期：2026-09-21。范围：生产诊断从图内执行改为结果事务提交之后执行，并加 `is_final=True` 门槛；不改 `save_workflow_outcome` 事务逻辑、不改其他图节点（Load/Classify/Retrieve/Grade/Validate/Confidence）、不改 T073 Checkpointer 接口、不新增列与 Worker、不改 M3 API。

### 方案与理由

选**方案 B（标记语义，且不新增状态字段）**：图内 `Generate Diagnosis` 只标记“诊断待生成”，生成移到 `WorkflowService` 的事务提交之后。理由：①`DiagnosisReportStore.save` 硬性要求“整卷结果行已存在且 `is_final=True`（否则直接拒绝）”，而结果事务在图结束后才提交，所以图内诊断在生产必然失败——这是 H01 的最后一环；②用节点自身的 `current_node=generate_diagnosis` 就是可验证的标记，无需新增 T065 状态字段（避开方案 B 列出的缺点）；③保留图拓扑与节点顺序（`WORKFLOW_NODE_ORDER`/`NODE_LABELS`/拓扑用例不变），比方案 A 的“移除节点”改动面更小；④不需要“图依赖外部事务状态”（方案 C 的缺点）。

### 关键设计

- 图侧：`_node_generate_diagnosis` 只返回 `{current_node, diagnosis: None}`；`GradingWorkflowDeps` 移除 `diagnosis_service`（图不再承担诊断职责）；失败分类抽为模块级 `diagnosis_failure_patch(error, *, recovery_supported)` 供服务层复用。
- 服务侧：`_persist_outcome` 在 P1.2 事务提交后调 `_finalize_diagnosis`；门槛为 `exam_result.is_final=True` 且状态中没有 Ready 诊断；成功写 `Completed`+报告；失败走 `_write_diagnosis_state`：可重试 Provider 错误且有持久 runtime 检查点 → `Paused`+`resumable=True`+暂停原因，否则 → `Failed`+脱敏错误；两者都保留已提交成绩、`diagnosis` 保持 `None`、不写空报告。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/workflows/test_grading_workflow.py` 更新 helper 与完成态断言 | 图不再调用诊断，旧断言（`COMPLETED`+诊断非空+依赖被调）已不成立 | 图跑完后 `status=RUNNING`、`current_node=generate_diagnosis`、`diagnosis is None`，而 `exam_result.is_final` 与 `final_results` 仍齐全 |
| 同文件把两个“诊断失败”用例换为 `diagnosis_failure_patch` 单元用例 | 失败分类已移出图，需保留 H03 的单元证据 | 可重试+有恢复支撑 → `Paused`+`resumable`+暂停原因；不可重试或无支撑 → `Failed`+`resumable=False`；两者都保留结果、不带假诊断 |
| `tests/integration/test_langgraph_grading_workflow.py` 更新 helper 与图级完成态断言 | 同上（真实图同样不再生成诊断） | 真实图混合答卷跑完：题序与逐题结果齐全、结果 final、图状态尚未完成；教师复核路径仍由 `ReviewService` 记录诊断（该路径断言保留） |
| `tests/contract/test_workflow_api_contract.py` 新增诊断顺序用例 | 需要正式入口证据（P1.3 核心） | 高置信度：事务提交后生成 Ready 诊断且真实 `DiagnosisReportStore` 可读、运行 `Completed`；诊断抛可重试错误（有持久检查点）→ 成绩保留+`Paused`/`resumable`；抛不可重试错误 → 成绩保留+`Failed`；两种情况都无 Ready 报告；低置信度暂停 → 不调用诊断且无诊断 |

### 验证记录

- 修正后全量门禁：`pytest tests/ -q` 为 **1338 通过、1 跳过**（M0 冒烟因未启用容器参数而跳过）；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 全部通过。
- 新增/修改测试：图级用例改为标记语义（`Running` + `current_node=generate_diagnosis` + `diagnosis is None`，`exam_result.is_final` 与 `final_results` 仍齐全）；两个旧“图内诊断失败”用例换为 `diagnosis_failure_patch` 单元用例与真实存储的服务级用例；教师复核路径的用例改为“诊断由记录器补齐且运行 `Completed`”。新契约用例共 4 条：`test_start_generates_diagnosis_only_after_result_commit`（正向：`Completed` + 真实诊断表 `Ready` 行 + 只在 `is_final` 上调用）、`test_diagnosis_retryable_failure_keeps_committed_grades`、`test_diagnosis_hard_failure_keeps_committed_grades_and_fails`、`test_low_confidence_pause_does_not_generate_diagnosis`（非最终成绩零调用且无诊断行）。
- 反向验证：把三份生产文件（`api/workflow.py`、`ai/workflows/grading_workflow.py`、`services/review_service.py`）暂存回到旧实现后，新增正向用例确定性失败：`AssertionError: assert 'Failed' == 'Completed'`——旧实现下诊断在结果事务提交前执行，`DiagnosisReportStore` 因“尚无整卷结果/未最终确认”拒绝落库，正是 H01 的失败形态；弹出暂存后按行尾归一化的 SHA-256 核对三份文件与修改稿一致。
- 失败处理验证：可重试 Provider 错误且有持久 runtime 检查点 → `Paused` + `resumable=True` + 暂停原因；不可重试或无恢复支撑 → `Failed` + 脱敏错误码；两种情况都保留已提交成绩且诊断表无行（不伪造 `Ready`、不写空报告）。

## P1.4.1 启动收敛：结果已提交但诊断未生成的遗留运行

日期：2026-09-21。范围：M4 执行器侧新增启动收敛，把“已有最终整卷结果、状态里无 Ready 诊断”的遗留 `Running` 运行改为可恢复的诊断待生成态，并在应用启动钩子（M3 收敛之后）接入；不改评分算法、不改 T072 图节点、不改 T073 Checkpointer 接口、不改 P1.2 事务写入与 P1.3 诊断顺序、不新增列与 Worker。

### 背景与必要性

P1.3 之后诊断在结果事务提交后生成：进程若恰好在两者之间中断，`WorkflowRun` 会停在 `Running` 且 `resumable=False`，恢复入口直接 409，诊断再也不会被补上。既有启动收敛 `GradingRepository.mark_interrupted_tasks_failed()` 只处理 `checkpoint['kind']=background-task-checkpoint` 的 M3 行（其 docstring 明确“M4 LangGraph 运行由所属执行器恢复”），而 M4 侧没有对应入口，所以这一形态此前无人收敛。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| 新增 `test_startup_recovery_turns_diagnosis_pending_run_resumable` | 需要证明“遗留 Running → 可恢复 → 只补诊断”的完整链路 | 用 monkeypatch 让 `_finalize_diagnosis` 跳过（模拟进程在诊断前中断，不手写业务结果行），得 `Running` + `is_final=True` + 无诊断的遗留态；收敛后 `Paused`/`resumable`/暂停原因，整卷结果不变；走正式恢复端点后 `Completed`、诊断表一条 `Ready`、逐题结果与 `retry_count` 不变（不重评），再收敛返回 0（幂等） |
| 新增 `test_startup_recovery_leaves_runs_without_final_result_untouched` | 防止收敛误动其他遗留运行 | 无最终成绩的 `Running` M4 行保持 `Running`、`resumable=False`，不产生诊断行 |

不放宽断言的措施：正向用例同时断言“收敛前确实是 Running+无诊断”“收敛后成绩不变”“恢复后诊断行真实存在且 `Ready`”“恢复后不重评（`retry_count` 不变）”，任一环缺失都会失败；反向用例断言行状态与可恢复标记都不变。

不新增镜像实现：用例只调用公开入口（正式启动/恢复端点）与公开方法（`recover_diagnosis_pending_runs`、`restore_state`、仓储读接口），没有为测试重写一份收敛逻辑，也不手写 `grading_results`/`exam_results`/`diagnosis_reports` 行。

### 验证记录

- 全量门禁：`pytest tests/ -q` 为 **1340 通过、1 跳过**（M0 冒烟因未启用容器参数而跳过，与 P1.3 基线相比新增 2 条用例）；`mypy backend/app/` 通过（125 个源文件）；`ruff check backend/ tests/` 全部通过。
- 正向链路已实测：正式启动端点把整卷结果提交后运行停在 `Running`（模拟在诊断前中断），`recover_diagnosis_pending_runs()` 返回 1 并把该行改为 `Paused` + `resumable=True` + 暂停原因，整卷结果仍为 `is_final=True`；随后走正式恢复端点返回 200/`Completed`，诊断表出现且仅出现一条 `Ready` 行，`_finalize_diagnosis` 的替代仅用于模拟中断（恢复前已 `undo`），恢复后逐题得分不变、`retry_count` 仍为 0（未重评）；再收敛一次返回 0（幂等，已完成行不再被触碰）。
- 反向链路已实测：无最终成绩的遗留 `Running` M4 行收敛前后均为 `Running`、`resumable=False`、无诊断行。
- 既有 M3 收敛未受影响：`mark_interrupted_tasks_failed()` 仍只处理 `background-task-checkpoint` 行，本步未修改它；启动钩子只是在其后追加 M4 收敛，存储未就绪时返回 0、不阻断启动。

## P1.4.2 T079 从正式入口验证闭环

日期：2026-09-21。范围：只重写 `tests/integration/test_langgraph_grading_workflow.py` 的 T079 集成验收，使结果、复核与恢复均从正式 HTTP 入口验证；不修改评分算法、图节点结构、Checkpointer 接口、结果事务、诊断顺序或启动收敛，不新增列与 Worker。

### 变更前事实与必要性

- 既有 T079 先直接运行 `GradingWorkflow`，再由 `_persist_paused()` 手工保存业务检查点、调用 `ResultAggregator` 并写 `exam_results`；测试替代了 P1.2 已归还生产入口的持久化职责，不能证明低置信度结果由正式启动进入复核队列。
- 完成态用例在图返回后手工调用 `save_exam_result()`、`mark_completed()`；复核、跨实例恢复与并发用例直接调用 `ReviewService`，绕过了 workflow/results/reviews API 的身份、读模型与恢复编排。
- 既有事务回滚证据主要位于仓储单元测试；T079 尚未在真实 PostgreSQL 隔离 schema 上证明正式启动的结果事务提交失败不会留下部分业务结果。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| 移除 `_seed_checkpoint`、`_persist_paused`、`_review_service` 及完成后手工写结果/状态的代码 | 测试不得承担生产持久化与恢复职责 | 所有运行由 `POST /api/workflow/submissions/{submission_id}/runs` 创建，结果只经 GET API 观察 |
| 高置信度正式入口用例 | 证明最终成绩不是测试补写 | 启动后由学生 `GET /api/results/me/submissions/{submission_id}` 读到 final 成绩，并从诊断 GET 读到真实 `diagnosis_reports` 的 Ready 报告 |
| 低置信度正式入口用例 | 证明 H01 的复核可达性 | 启动后教师 `GET /api/reviews/queue` 与详情端点能看到真实待复核评分及原 Workflow 身份 |
| 复核、结果重读与跨实例恢复用例 | 同时覆盖正式复核入口、两阶段恢复和新 Session | `POST /api/reviews/decisions` 先保存教师结论；在真实诊断存储的 `save` 边界注入一次可重试写入失败以保留可恢复态；销毁客户端后由新服务/Session 调 `POST /api/workflow/runs/{run_id}/resume`，再从结果与诊断 GET 重读最终事实 |
| 并发正式复核用例 | 保留既有 PostgreSQL 行锁冲突证据且不绕过 API | 两个独立客户端同时提交同一决定，仅一条 `ReviewRecord` 生效，另一请求得到冲突 |
| 正式启动事务失败用例 | 满足 P1.4.2 第 5 项 | 在结果事务提交前让该仓储 Session 的 `commit` 抛异常；验证 `grading_results`/`exam_results` 无新增、运行不为 Completed、Answer/Submission 状态未变 |
| 结构化结果非法用例改走启动 API | 保留既有失败路径且移除手工状态写入 | Provider 返回越界分数时正式运行进入 Failed，结果 API 不伪造成绩 |

### 不放宽与边界

- 保留跨实例恢复、并发冲突和事务回滚；断言仍落在 PostgreSQL 行、API 回执和新 Session 重读事实三层。
- 允许的替身仅限评分/诊断 LLM Provider、Embedding、Reranker 与检索外部边界；数据库表、Workflow、Checkpointer、结果仓储、复核服务、诊断服务/存储及 API 路由均使用真实实现。跨实例用例只在真实诊断存储的 `save` 调用上注入一次异常，不伪造或手写报告。
- fixture 继续复用 `tests/postgres_helpers.py::isolated_postgres_engine` 创建随机 schema 并在退出时级联删除；不接触默认 schema 或现有业务数据。

### 验证记录

- 聚焦：`pytest tests/integration/test_langgraph_grading_workflow.py -q` 为 **6 通过**；低置信度队列、高置信度结果读取、复核后结果/诊断重读、新 Session 正式恢复、提交失败无部分结果均通过。
- 并发用例由两个独立 `TestClient` 同时调用 `POST /api/reviews/decisions`，回执严格为 200/409，冲突码为 `REVIEW_SERVICE_CONFLICT`；PostgreSQL 中仅一条 `ReviewRecord`，评分行状态为 `Confirmed`。
- 事务失败用例在结果仓储 Session 的 `before_commit` 事件中调用已由 `monkeypatch` 替换的 `session.commit` 并抛 `SQLAlchemyError`；正式启动返回 500 后，`grading_results`/`exam_results` 均无行，运行未变为 `Completed`，Answer 状态、Submission 状态及 `graded_at` 保持原值。
- 全量门禁：`pytest tests/ -q` 为 **1341 通过、1 跳过、19 条既有警告**；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。
- 唯一跳过项仍为 M0 容器冒烟（缺少该用例要求的独立 Compose 项目与隔离端口配置）；本步真实 PostgreSQL 容器健康，T079 每个用例均使用随机隔离 schema 并在退出时删除。
