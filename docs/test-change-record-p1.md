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
