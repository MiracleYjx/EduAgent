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

## P3.1.1 持久化复核轮次

日期：2026-09-21。范围：两个模型、`0010_review_round_ids` 迁移、复核 API/服务及对应测试；不改结果仓储、诊断、Workflow 图或 UI。

### 变更前事实与必要性

- 同一运行/答案重新 Pending 时，同内容的上一轮迟到请求与新决定无法仅凭 P3.1 字段区分。
- Pending 状态实际由结果仓储的 ORM 赋值写入；在模型状态变更事件上维护轮次，可覆盖原有仓储路径而不改 P1 事务逻辑。初次 Pending、从非 Pending 转入时生成 UUID4；重复保存同一 Pending 保留轮次，离开 Pending 清空。
- 复核服务使用条件 UPDATE，必须显式清空轮次、持锁核对轮次，并将轮次加入原子条件，不能只在 API 预检，否则 Pending→Accepted→Pending 的并发变化仍可绕过。

### 测试变更计划

| 测试范围 | 必要性与覆盖 |
| --- | --- |
| 模型结构及状态转换 | 精确更新列/索引断言；验证首次/再次 Pending 的 UUID4、重复保存稳定、非 Pending 清空、回滚后轮次不变 |
| API/真实服务幂等 | 同轮次同内容返回原记录；新轮次同内容写新记录；过期/随机轮次返回 409；审计记录保留已消费轮次 |
| 核心反例 | 第一轮 Modified→结果被覆盖→第二轮 Pending→相同内容 Modified；旧轮次请求不影响新 Pending，新轮次请求确实落库 |
| 事务内轮次守卫 | 请求预检后发生 Pending→Accepted→Pending，原状态相同而 round 不同仍须拒绝，不产生额外记录 |
| 旧客户端/历史行 | None 保留 P3.1 兼容语义，回执明确 idempotency_degraded；历史 NULL 不伪造回填，严格轮次不能匹配历史 NULL |
| PostgreSQL 迁移 | 隔离 schema 上执行真实 Alembic upgrade head / downgrade -1 / 再 upgrade，验证列类型/可空/普通索引、历史数据 NULL、业务数据不变；更新迁移链 head 断言 |

### 兼容策略

- `expected_review_round_id=None` 是明确兼容模式：按当前状态受理，仍核对 P3.1 的动作、score/reason、expected 状态及最新记录；不能提供跨轮次迟到请求保护，响应 `idempotency_degraded=True`。现有 UI 暂不传轮次，因此继续兼容模式，本批不改 UI。
- 迁移不给历史行生成 UUID；已有 Pending 且轮次 NULL 的行维持 legacy，只有下一次真实进入 Pending 才生成新轮次。
- 复核队列/详情暴露当前轮次，审计与决定回执返回记录轮次，供新客户端原样回传。严格模式下，已被新一轮覆盖的旧轮次请求返回 409，而非声称新一轮已处理。

### 验证记录

- 模型生命周期：`test_pending_round_is_stable_until_a_new_pending_transition` 验证 UUID4、同轮次稳定、离开 Pending 清空、再次进入换号及事务回滚；`test_legacy_pending_round_stays_null_until_a_new_transition` 验证迁移 NULL 不因加载/重复保存被回填。
- 核心反例：`test_same_content_in_new_round_creates_record_and_rejects_late_retry` 使用真实复核事务与图，第一轮 Modified 保存后注入恢复失败，再由 Re-grade 覆盖并产生第二轮 Pending；第一轮请求被拒绝，第二轮相同 score/reason 产生独立 Modified 记录，两条审计分别保存各自 round_id。两轮同轮次重试均返回原记录。
- `test_locked_round_check_rejects_pending_aba` 同时覆盖严格及旧客户端模式：预检之后发生 Pending→Confirmed→Pending，事务持锁时拒绝旧轮次，无额外审计记录。API 错误轮次用例返回 `409/REVIEW_SERVICE_STALE_ROUND`。
- 兼容降级与历史 NULL：`test_missing_round_explicitly_reports_degraded_mode`、`test_legacy_null_round_is_recorded_without_fake_backfill` 验证 `idempotency_degraded=True`、真实历史审计仍 NULL，以及显式 UUID 不能消费历史 NULL。
- PostgreSQL 正式 API 的既有 confirm/modify 跨实例重试测试改为传递详情给出的 round_id，验证原记录/轮次保持一致、严格模式不降级、待复核数量仍随权威结果变化。
- 真实 Alembic CLI 在随机 `test_review_round_*` schema 执行 `upgrade 0009_agent_runs`，准备旧数据后执行 `upgrade head`、`downgrade -1`、再次 `upgrade head`，全部成功；断言 UUID/nullable/普通索引、历史两列 NULL、降级后原业务行仍在。测试结束删除自身 schema，默认业务 schema 未迁移。
- 最终门禁：`pytest tests/ -q` 为 **1385 通过、1 跳过、8 条警告**，净增 11 项；唯一跳过项仍为缺少独立 Compose 参数的 M0 冒烟。`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 及新增迁移文件检查通过。
- 生产改动限于授权的两个模型、复核 API/服务和新增迁移；结果仓储、诊断顺序、Workflow 图及 P2 UI 均未修改。
