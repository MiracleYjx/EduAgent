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

## P2.2 复核队列选中事件与索引兼容

日期：2026-09-21。范围：只修复复核队列 Dataframe 的选择事件装配、行索引解析与无效选择空态；不修改 P2.1 认证 wrapper、复核加载器接口、复核服务或业务详情展示。

### 变更前事实与必要性

- 完整应用中实际注册的 `select_review_item` 有 `queue`、`queue_items`、`state` 三个普通输入，同时 Gradio 又在参数 0 自动注入 `SelectData`；注册时已产生“函数最多需要 2 个普通参数、却收到 3 个”的警告。
- P2.1 守卫按普通输入列表记录 `state` 索引。事件在参数 0 注入时，现有声明会使运行期实参整体右移，守卫按旧索引读取到 `queue_items`，无法完成认证。
- `evt.index` 可能是 Dataframe 的二维索引；直接 `int((row, column))` 会抛 `TypeError`，负值还会被 Python 当作尾部索引，均不符合“无效选择返回空态”的要求。

### 测试变更

| 测试变更 | 必要性 | 覆盖内容 |
| --- | --- | --- |
| 实际 Gradio 事件装配测试 | 裸函数调用不能证明组件输入数、事件注入位置和认证守卫协同正确 | 从完整 `create_gradio_app().fns` 取得已注册回调，经 `Blocks.process_api` 注入 `SelectData`；断言普通输入仅为 `queue_items`、`state`，二维索引只取 row，一维整数索引保持可用 |
| 无效选择参数化测试 | 防止空索引、取消选择、负值或越界触发异常/错误选中 | 经同一已注册回调触发各形态，断言不读取详情、选中状态为空、返回既有明确选择提示 |

测试仅替换认证状态读取和 `review_loaders` 外部边界，不直接调用本步骤待修复函数来证明新增行为。

### 验证记录

- 修复后实际注册回调仅有 `queue_items`、`state` 两个普通输入；`state` 保持在索引 1，Gradio 在索引 2 注入 `SelectData`，完整应用构建不再报告该回调参数数量不匹配。
- 聚焦回归：`pytest tests/unit/ui/test_review_view.py tests/unit/ui/test_gradio_app.py -q` 为 **31 通过**；二维 `(row, column)`、一维整数、空索引、取消选择、负值、越界及空队列均经实际 `Blocks.process_api` 事件装配验证。
- 全量门禁：`pytest tests/ -q` 为 **1352 通过、1 跳过、7 条既有警告**；`mypy backend/app/` 通过（125 个源码文件）；`ruff check backend/ tests/` 通过。
- 唯一跳过项仍为 M0 容器冒烟所需的独立 Compose 项目与端口配置，与本次 UI 事件修复无关。
