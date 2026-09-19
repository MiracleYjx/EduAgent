# S5 置信度决策复用测试变更说明（TCR）

日期：2026-09-19。范围：仅默认置信度策略及 M4 默认主观题评分链路；不修改 Workflow、DTO 字段、数据库列或注入路径语义。

本记录在新增测试前形成，沿用项目 pytest、真实评分服务与外部依赖替身的测试方式。保留既有断言，不用源码文本匹配或复制业务算法证明正确。

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| 在 `tests/unit/grading/test_grading_task_service.py` 增加默认记录器回归 | 原测试能证明有决策记录，但不能证明记录就是实际回填使用的那次决策，也不能发现重复阈值计算 | 对真实 `ConfidencePolicy.evaluate` 作观察包装，覆盖低于、等于、高于阈值；每次仅判定一次，记录保留该决策对象，返回状态一致且评分内容未变 |
| 在同一文件增加显式注入策略的兼容回归 | 自定义策略可能覆盖 `evaluate` 或 `apply`；默认路径优化不能跳过其逻辑或改变调用顺序 | 自定义策略的原有 `evaluate -> apply` 调用顺序、返回修改、决策记录及拒绝结果时不产生记录的行为保持 |
| 在 `tests/unit/agents/test_grading_agent.py` 增加真实默认评分器链路回归 | 现有默认评分器测试只计数检索、重排和 Provider，未发现评分器完成判定后 Agent 再次判定和回填 | 使用真实 `SubjectiveGrader`、真实策略与记录器，仅替换外部检索/模型依赖；观察评分器返回结果及决策，验证单次判定、低置信度暂停、边界接受、六字段快照和 Agent 不重复回填；逐次设置覆盖构造设置且不访问全局配置 |
| 在同一文件增加自定义策略与评分器组合的兼容回归 | 默认评分器和自定义评分器对策略的调用责任不同，不能统一删除 Agent 末端处理 | 分别覆盖默认评分器配显式自定义策略、自定义评分器配显式自定义策略；原有策略执行顺序、策略优先级、结果修改和 Agent 末端确认保持 |

复用现有测试验证：未校验结果拒绝、人工 Confirmed/Modified/Final/Re-grade 不被自动覆盖、错误码、客观题不调用策略、配置优先级、历史快照不按新配置重算、Workflow 暂停与恢复。

执行顺序：先运行新增的聚焦测试，确认默认路径用例能暴露当前重复判定；实施后运行相关评分/Agent 测试；最后执行 `mypy backend/app/`、`ruff check backend/ tests/`、`pytest tests/ -q`。所有提交门禁通过后才提交和推送；环境导致的跳过单独记录。

## 验证记录

- 改动前新增回归：6 个默认路径用例失败，分别观察到 M3 判定 2 次、M4 判定 3 次；4 个注入兼容用例通过。
- 改动后聚焦回归：`pytest tests/unit/grading/test_confidence_policy.py tests/unit/grading/test_grading_task_service.py tests/unit/grading/test_subjective_pipeline.py tests/unit/agents/test_grading_agent.py -q --tb=short`，112 通过。
- 提交门禁：`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过；`pytest tests/ -q` 为 1323 通过、1 跳过、19 条既有警告。
- 唯一跳过项为 M0 容器冒烟，原因是没有设置该用例要求的独立 Compose 项目与隔离端口；本批没有修改 M0 环境或放宽测试条件。
