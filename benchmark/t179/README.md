# T179 组卷、考试分值与发布冻结验收

本目录是真实 PostgreSQL/服务/Gradio 的技术与性能评测入口。沿用 T146 固定 300 题、12 个组卷场景和原教学依据/题图；基准为 **AI 辅助 + 开发者审查**，独立教师标注数量为 0。运行夹具在真实启用的合成教师账号下，通过受控语义 Provider 和真实批准命令建立当前可入卷资格；不调用云模型，不宣称模型质量或独立教师核对。原标注和未知项不改写。

## 复现准备

使用已安装项目依赖的 Windows Python，PostgreSQL/pgvector 和专用 Redis 已就绪。由本机操作者创建 **全新** `eduagent_t179_` 前缀的隔离数据库，配置 `DATABASE_URL`、`REDIS_URL`、`STORAGE_ROOT` 到该数据库、独占 Redis 和新文件目录。连接凭据只通过环境/本机配置提供。不要对业务库运行本评测或迁移。先执行 `python -m alembic upgrade head`。

建立工作目录，例如 `.cache/t179-reproduction/`。`isolation.json` 保存实际 `database`、`pg_container`、`redis_container`、`redis_port`（不含密码）；`environment.json` 保存实际 OS/CPU/核心/物理内存/磁盘、Python/包版本、PostgreSQL/pgvector/Redis版本、运行方式及网络。容器名字必须是真实观测对象。

```powershell
python benchmark/t179/fixtures.py --output .cache/t179-reproduction/fixtures.json
python benchmark/t179/business_acceptance.py --fixtures .cache/t179-reproduction/fixtures.json --output .cache/t179-reproduction/business/acceptance.json
python benchmark/t179/performance.py --fixtures .cache/t179-reproduction/fixtures.json --isolation .cache/t179-reproduction/isolation.json --output .cache/t179-reproduction/performance --node <已安装的Node.exe> --playwright <已安装的playwright模块绝对路径>
```

需要本机已安装 Microsoft Edge；不下载新浏览器或依赖。`--pilot` 只用于独立诊断批，不能代替正式协议。`T179_PROFILE=1` 可在独立诊断批记录真实回调 cProfile，正式批必须关闭。输出目录已存在时拒绝覆盖；修复后另开批次。

## 锁定执行与边界

- 正式输入：1/25/100 题，各有满足与明确缺失知识点的不可满足请求；每种 3 次新应用 worker 冷态、同 worker 1 次预热 + 5 次暖态，共 **54 个动作、48 次正式计时**。每次新建同样两题的草稿，保存完整前后事实后，仅清理该次自有草稿、复核数据库规模恢复；清理在计时之外，候选总库 300 题、每题型 100，默认分值均为 1.00；1 题请求实际类型候选 100，25/100 请求实际类型候选 300。不清理 OS 磁盘缓存，数据库/Redis复用且预先就绪，并发为1。
- 真实界面操作：预填真实合成教师登录状态，使用生产 `create_exam_view`、实际服务和文件授权；列表选择/输入条件不计时。点击提交至完整题目/选项/答案/解析/原图预览，或已保存失败要求且原卷保持的真实缺口诊断，包含事务和渲染；所有图片实际解码，两个动画帧后结束。使用同一浏览器 `performance.now()` 差，不以 API 延迟替代，不生成新题。
- PID由实际 uvicorn worker 回传并核对控制器启动的进程树；资源从提交 IPC 即刻观测，三个独立组件并行，之后按动作起点每秒采样。仅采用浏览器真实动作时间窗内完成的采样周期；首周期延迟≤50ms、周期/尾间隔≤1s+50ms、每周期≤1s且无组件缺失才记完整。小于1秒的操作允许一次真实观测，不伪造第二次或连续峰值；无完整周期仍为缺测。UTC仅用于同主机资源窗口定位，业务耗时始终使用浏览器单调时钟。浏览器/控制器不混入应用 working set；PG为共享容器全部postgres进程RSS，有共享页重复计算的可能。
- 每次记录真实数据库前后事实、最新失败意图、输入/源码指纹、全部 browser/PID/资源/失败信息及截图。所有计划失败占原编号。3秒目标及各资源预算逐次判断，不能用均值、诊断或预热代替失败运行。200题只核对原请求边界，不纳入≤100题性能或改变准入。
- 退出只停止该控制器仍在运行的自有 worker 树。原始回执已落盘的本批自有草稿可在严格归属/可编辑检查后清理以恢复固定初态；批次后由操作者核对独占数据库零连接、Redis容器身份，再清理该批资源；业务数据库、共享PG容器、用户浏览器和文件不清理。原始输入、结果和诊断失败均保留。
