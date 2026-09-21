# P3 测试变更记录（TCR）

## P3.1 复核决定幂等区分重试与新决定

日期：2026-09-21。生产范围：`api/reviews.py`、`services/review_service.py`；保留现有复核短事务、结果持久化及诊断顺序，不新增列或持久化幂等键。

### 变更前事实与必要性

- API 在检查当前 Pending Review 之前，遍历历史记录，仅按 `Modified/Confirmed` 动作返回旧记录；不同分数、理由、新运行以及已被后续决定覆盖的请求都可能被误报为成功。
- 重试回执的 `pending_review_count` 固定为 0；恢复失败的服务回执还可能使用旧的待处理集合，必须从最新持久化评分状态读取答卷待复核数。
- 既有幂等契约测试仅写入历史记录、仍保留 Pending Review，便要求返回旧记录；该准备过程没有体现真实决定已生效的事实，需要改为当前评分与记录一致，并增加反例，保留单记录断言。

### 测试变更

| 测试 | 必要性及覆盖 |
| --- | --- |
| 相同确认/修改重试 | 最新记录、当前评分、有效运行及规范化 expected 状态一致时返回原记录，不调用新决定写入 |
| 旧 Modified + 新分数/理由 | 当前 Pending Review 必须进入真实新决定路径，不能复用旧记录（H04 反向测试） |
| 新 Workflow 与同运行再次 Pending Review | 合法新轮次不能因历史同动作被跳过；旧运行声明返回 409 |
| 已被覆盖或期望状态不匹配 | 只认可最新仍有效的决定；旧决定、内容不一致、错误 expected 状态均返回 stale/conflict |
| 最新待复核数 | 相同决定重试与恢复失败均核对真实数据库 Pending Review 行数，包含答卷中另一待复核答案 |
| 服务恢复守卫 | 恢复入口不能仅因动作相同便跳过内容校验，防止不同修改借重试路径覆盖 |

测试采用现有 API 契约框架，结合真实数据库、实际复核服务/Workflow 的集成验证；保留既有跨 Session 恢复、并发冲突及事务回滚测试。

### 无迁移版本的身份边界

- 请求身份为 `workflow_id + answer_id + action + score/reason（Modified）+ expected_review_status`；未传 expected 时按 API 既有合同规范为 Pending Review，并核对教师身份、最新记录及当前评分事实。
- `ReviewRecord` 没有运行外键和原始 expected 状态。运行创建时间可排除更早历史记录，但不是持久化运行关联；不把这种时间范围检查声明为跨重叠运行的严格身份保证。
- 同一 Workflow/answer 再次进入 Pending Review 时，同内容新决定与之前请求的迟到重发具有相同组合，现有数据无法区分。本版按当前 Pending Review 受理新决定。
- 后续迁移建议（待用户决策，未实施）：持久化每次 Pending Review 的 `review_round_id`，请求携带该轮次；复核记录关联 `workflow_id/answer_id/review_round_id` 并保存规范化动作、score/reason、expected 状态。同轮次重复返回既有回执、不同内容冲突、新轮次允许新记录；历史无法可靠回填的记录标记为 legacy，不伪造轮次。

### 验证证据

- `test_duplicate_decision_is_idempotent`（confirm/modify）保留“不写第二条记录”的断言，并将准备状态纠正为真实的已决定评分状态。
- `test_old_modified_does_not_swallow_new_pending_decision` 分别覆盖新分数、新理由、新 Workflow、同 Workflow 再次 Pending；`test_old_modified_new_round_creates_new_record` 用真实复核事务验证历史 Modified 不会吞掉当前新修改，最终确有两条不同 Modified 记录和新的评分。新 Workflow 用检查点 fixture 准备；本批不扩展启动 API 对已有运行答卷的重评策略。
- `test_modified_retry_rejects_changed_or_superseded_identity` 覆盖不同 score/reason、错误 expected、被新决定覆盖、记录早于当前运行；`test_resume_modified_rejects_different_content` 验证服务恢复入口也拒绝不同修改。
- PostgreSQL 容器健康；新增 `test_decision_retry_returns_original_record_and_live_pending_count`（confirm/modify）使用随机隔离 schema，从启动/复核 API 真实落库，跨应用和 Session 重试后仍是原记录，另一题完成后计数从 1 变为 0。
- `test_saved_decision_counts_database_pending_after_resume_failure` 在图恢复调用注入异常，真实决定事务仍提交；首次回执及重试均返回数据库计数 0，没有因旧图 Pending 返回 1，也未生成第二条审计记录。
- 反向验证：在独立 Python 进程中只用内存替换 `ReviewDecisionService.submit` 为基线 `54c760d` 的方法，4 个 H04 反例均以“返回旧 review_record_id”失败；未改动工作区生产文件。
- 最终门禁：`pytest tests/ -q` 为 **1374 通过、1 跳过、7 条既有警告**，相对 P2.3 净增 17 项；全部新增/扩展验收及既有 PostgreSQL 并发、跨 Session 恢复、事务回滚测试通过。唯一跳过项为 M0 冒烟缺少独立 Compose 项目/端口配置。
- `mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过；生产差异仅限两个授权文件，复核短事务 `_apply_decision` 未修改。
