# T190 兼容回归与测量汇总（2026-10-06）

执行与证据汇总完成，v2.0 总体门禁未通过。基线 deepcode/d2b1a4d；当前工作区包含原用户两 README、开发者阅卷样本，全部保护。只修正 M0 编排，不改既有业务、迁移、测试或断言。

- checks：真实独占 PostgreSQL，全量 2617 passed / 1 failed / 15 skipped / 126 warnings，1144.75s；mypy 215 文件通过。唯一失败为受保护默认样本已有4个 developer_review 分数，与旧“默认未标注”断言冲突。
- supplement：显式标准 PostgreSQL 客户端的备份/题图15/15；合法隔离配置和 git archive 的原提交样本阅卷契约30/30。不能拼接成一次全量通过。committed-input 首轮缺配置的18失败/12通过保留。
- 原 broad Ruff 扫描遇到 T160 历史 frozen-execution 的导入排序失败，原证据未格式化；排除 benchmark/results 数据后全活动源码通过。最终 M0 单元/Compose 契约14项通过。
- Docker：原入口先等 Backend 就绪、后迁移，空库恢复检查点时报 WORKFLOW_SERVICE_NOT_READY。修复为构建→依赖健康→一次性容器迁移→Backend→三服务健康/readiness；保留固定项目/端口及原断言。首次重验因 Docker info 超时跳过，随后的观察连接失败保留；诊断引擎恢复后原集成测试1/1、34.883s，三容器 healthy、/ready 200、0023 head、Redis PONG。项目已 down，卷保留。M0 的 Embedding 配置只过 constructor，不声明 Docker AI 推理通过；真实 BGE/LLM 另列。
- benchmarks：首轮缺本地权重的摄取失败保留；v2使用既有完整 BGE 权重，默认12查询×4模式、默认4合成阅卷样本×3策略，真实 DeepSeek LLM重排/评分。关键词召回0、重排P95 4548.837ms及所有原输出保留；grading 的历史 teacher_score 字段来自 developer_review，独立教师数0。语料/模型/调用事实见各 manifest、summary 和 JSON/CSV。
- aggregate.json 保存 T160/T168/T179/T189 同源记录及引用哈希，只用于追溯，不新增版本相等门禁。不同样本/协议/资源窗口不拼接为一次验收，不改旧失败/未知/任务勾选。

`*.log` 已脱敏并显式纳入本批证据；.cache 内原私有配置、密钥、JWT、完整构建日志和二进制留本地，不提交。JUnit 保留先行失败/跳过。所有临时检查/Benchmark库均零连接后删除，原 eduagent 保持0012。T190勾选表示上述执行、局部缺陷修复与记录完成，不表示T168质量、T189交付启动或工作区全量门禁已通过。
