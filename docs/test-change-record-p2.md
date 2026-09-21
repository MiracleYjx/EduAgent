# P2 测试变更记录（TCR）

## P2.1 Gradio 认证包装保留异步语义

日期：2026-09-21。范围：修复 Gradio 业务视图认证守卫包装异步回调后丢失协程函数标识的问题；不改变认证判定、错误文案、视图函数、Gradio 版本或其他 wrapper。

### 变更前事实与必要性

- `create_gradio_app()` 会把所有含共享 `session_state` 的已注册回调替换为 `_guard_view_callback()` 的返回值。
- 该守卫当前始终返回同步函数，因此实际注册在 `Blocks.fns` 中的 `generate_candidates`、`confirm_review`、`save_review_changes` 均不再满足 `inspect.iscoroutinefunction()`；Gradio 据此不会等待其返回的 coroutine，异步正文没有执行。
- 既有 UI 测试只验证应用可构建，未验证注册后回调的同步/异步语义与认证失效路径，因此需要补充回归测试。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| 已注册异步回调语义测试 | 防止守卫再次把协程函数包装成同步函数 | 从 `create_gradio_app().fns` 读取三个真实注册回调，断言均保留协程函数标识；等待 `generate_candidates` 后断言其正文返回预期 UI 结果 |
| 已注册同步回调回归测试 | 防止异步分派影响现有同步路径 | 从 `Blocks.fns` 读取 `next_review_item`，断言仍为同步函数且正文结果不变 |
| 已注册回调认证失效测试 | 确认两类 wrapper 保留相同认证边界 | 分别调用真实注册的异步与同步回调，断言失效会话都抛出原有“登录状态已失效，请退出后重新登录。”UI 错误 |

不调用裸视图函数，不替换 Gradio 的调度判断；仅替换认证状态读取以隔离数据库，并通过注册表中的 callback 验证守卫后的最终形态。

### 验证记录

- 变更前从完整 `create_gradio_app().fns` 读取实际注册项：`generate_candidates`、`confirm_review`、`save_review_changes` 的 `inspect.iscoroutinefunction()` 均为 `False`。
- 聚焦回归：`pytest tests/unit/ui/test_gradio_app.py -q` 为 **8 通过**；三个异步注册项均恢复为协程函数，等待生成回调后得到正文 UI 结果，同步注册回调仍为同步函数，异步/同步失效会话均返回原错误文案。
- 全量门禁：`pytest tests/ -q` 为 **1345 通过、1 跳过、19 条既有警告**；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。
- 唯一跳过项仍为 M0 容器冒烟所需的独立 Compose 项目与端口配置，和本次 UI 回调修复无关。
