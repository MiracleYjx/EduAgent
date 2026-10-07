# T189 启动性能优化与真实冻结包复验（2026-10-07）

**最终包达标，T189 完成。** 本次仅优化启动及验证直接影响，T169 不重复执行。首次 <30 秒、后续 <10 秒目标不变，未筛除慢样本。主机、配置边界见 [environment.json](environment.json)，精确机器汇总见 [summary.json](summary.json)。不打开浏览器；计时包含实际 EXE 创建、必要预检/迁移、真实服务到 `/ready` 和 `/gradio/` 双 200，不包括浏览器渲染。

| 最终完整协议 | 真实结果 | 结论 |
|---|---|---|
| 首次，3 个独立空数据库和持久根 | 9.670 / 9.032 / 9.005 秒 | 3/3 <30 秒 |
| 后续，同一已迁移库/根，前次正常退出后新建 EXE | 8.865 / 8.353 / 8.836 / 8.307 / 8.805 秒 | 5/5 <10 秒 |
| 就绪、正常退出/所属进程退出 | 各 8/8 | 通过 |
| 1Hz 完整资源窗口和预算 | 各 8/8 | 通过 |
| 配置/数据库/Redis/模型网络/权重/迁移故障 | 8/8 明确正确失败步骤，无业务端口，所属预加载进程退出 | 通过 |
| OCR 开关、PNG 权限、持久重启、真实 SDK 受控失败 | 4/4；两次正常退出 0 | 通过 |

应用峰值 **401.45 MiB**，共享 PostgreSQL 全进程 RSS **258.54 MiB**，Redis RSS **8.52 MiB**，同周期合计峰值 **656.78 MiB**。合计不是各独立峰值相加。各预算保持应用 4GB、PG 1.5GB、Redis 512MB、总计 6GB（十进制）；启动资源窗口不代表长时模型负载。实际入口采样包括自有启动器及子树；逐次资源通用 scope 内“排除父重定向进程”的旧措辞不适用于本入口，以顶层 summary 和观察者源码为准。共享 PG 页可能重复计算，1 秒采样不是连续物理内存最高值，浏览器和 OS 不计。

## 根因和最小改动

1. 重试模块为类型识别在导入阶段提前加载完整 OpenAI SDK。将类型导入延迟到真实异常分类，内部已有错误仍优先处理；SDK 各错误类别、重试、Retry-After、脱敏保持。
2. Gradio 6.26.0 组件创建时解析/写入 IDE `.pyi`。只在冻结构建副本禁用 `create_or_modify_pyi` 的函数体；事件验证、监听器注册和全部其他 AST 保持。安装库、源码/Docker 入口不改。
3. 首次 HTML 生成的 `Blocks.get_api_info` 为每个输入/输出重复扫描所有组件。只在冻结构建副本用本次调用的 ID 索引，保留重复 ID 首项和缺失组件跳过行为；API 描述、可见性、返回值逐值差分一致。原工作台两种 API 信息模式实测从 0.749/0.740 秒降为 0.285/0.173 秒；此源码微测仅作定位，最终目标以 EXE 实测为准。

转换范围固定为两个已剖析的方法；升级锁定 Gradio 时需重新审查 AST 和真实差分，不新增运行时版本锁步门槛。[实际 EXE 编译模块检查](final-frozen-module-inspection.json) 从 CArchive/PYZ 读取，证明代码确实进入最终包。所有源码追溯及实际哈希见 [构建收据](build-api-receipt.json)。`source_dirty=true` 如实保留，构建收据记录的源码字节与当前代码全部一致，用户已有修改未带入业务变更。

最终包为 `.cache/t189-optimize-20261007/build-api/dist/EduAgent`，onedir，本地模型依赖版本；9643 文件、969472546 字节，EXE SHA-256 `a34a141ad94c4c8125f62fe49162c5c820f6532096097a74066d44ab3530c88d`，追溯提交 `fcba66dd99c4d8eb6e061dde266f143d79b34ad4`。配置、PG/Redis、业务文件和模型权重在包外。源码验证、构建成功与真实包验收分别保存；没有测纯净 Windows 主机或发布正式 release。

## 验证与失败保留

- [最终启动逐次记录](startup-final/summary.json)、每次原日志/资源 JSONL、[隔离库清理](startup-final/cleanup.json)；完整 3 首次＋5 后续，不清 OS 缓存。
- [最终严格故障](faults-final/summary.json)：8/8。前一轮误用已删除的实验 DB，模型网络及 Redis 实际早停在 PostgreSQL；旧子串观察者误报 Redis。[原回执](faults/summary.json) 7/8 不改，[严格审计](faults-observer-audit.json) 明确为 6/8，修复观察者后使用新有效隔离库完整重验。业务失败事实、端口和退出门槛不放宽。
- [运行技术回执](runtime-local/receipt.json)：真实冻结 PDF→PNG、教师 200/学生 403、禁 OCR 真实失败、重启状态/图片 SHA 一致、启 OCR 得到真实非空文字；冻结 SDK 对自有回环受控 503 的 3 次请求真实传播失败。**云推理 0 次**，合成 API key，不用受控失败证明成功提取或质量；真实模型/BGE/完整业务闭环沿用此前 T189/T191 原证据，本批未再调用。
- [最终兼容回归](final-frozen-ui-regression.xml)：同时加载两个实际构建转换的模块，原 UI/启动/SDK/Embedding/导入生命周期回归 **302 passed，0 failed/skip，37.17 秒**。[生产工作台差分](api-index-differential.xml) **6 passed，25.57 秒**。两批覆盖目的不同且有重叠，不相加，也不称当前全量回归。T168 v8 原完整快照 2662 passed/0 failed/1 skipped 保留为此前结果。
- backend mypy **216 文件通过**；构建转换 mypy 1 文件通过；当前活动 Ruff/Black 通过。测试变更事前登记 TCR §§49–52，既有业务断言不降低。
- 第一批 SDK 延迟后的 [startup-sdk](startup-sdk/summary.json) 后续 1/5 达标；第二批再禁 IDE 写入的 [startup-gradio](startup-gradio/summary.json) 后续 4/5，一次 10.457 秒超标。每次都先新增定位与修复再重验，原慢样本完整保留，[三批 CSV](startup-comparison.csv)。首次外部原生 py-spy 捕获失败的诊断日志和运行观察者一次 import 错误均保留，未宣称这些失败是成功 profile/验收。

## 可复现入口与保护边界

构建使用 `packaging/EduAgent.spec` 与既有锁定运行时，设置 `EDUAGENT_BUILD_LOCAL_MODELS=1`，每版独立 `--workpath/--distpath`，不覆盖旧包。真实启动入口 `benchmark/t189/windows_acceptance.py` 传 `--exe`、`--output`、`--private-root`、`--no-browser`；严格故障入口 `benchmark/t189/fault_acceptance.py` 使用有效独占库的 `--source-config`，所有密码留在私有配置。可公开的实际观察者脚本及 SHA 归档在 [executed-source/manifest.json](executed-source/manifest.json)，含隔离认证配置常量的运行脚本仅登记私有路径和 SHA；辅助脚本依原 .cache 路径定位，归档用于审计，不从归档目录直接运行。重新运行应创建新结果目录，不覆盖本批。

[前](preservation-before.json)/[后](preservation-after.json)核对：原库仍 `0012_audit_logs`、24 张公共表、关键行数不变；两份 README、用户阅卷标注文件及 .env 字节一致。全部新独占 DB 零连接后删除，自有 EXE 退出，共享 PG/Redis 健康；配置、JWT、业务文件和模型权重留在私有 .cache，不公开。AI 辅助＋开发者审查、独立教师 0、未知项及标注替换路径保持。

T168 已按用户逐字段口径通过，T169 已完成；语义条件误报16.67%、语义 Rubric 覆盖77.78%、图片条件完整性72.73%和待定图像提取仍作为原已知限制。历史启动15–16秒限制被本批当前结果解除，原10秒目标未降低。建议下一步由用户复核交付清单，并在独立 Windows 环境演练部署；正式发布或恢复激活需对应授权。
