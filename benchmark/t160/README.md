# T160 专用验收工具

这两个入口执行固定批次，读取独立隔离数据库和持久目录配置。已有质量证据在 `benchmark/results/v2/t160-assisted-20261003/quality/`，其冻结运行代码、真实请求/响应及参考来源保持原记录；可提交工具副本经过 CLI 整理和格式化，源码摘要与冻结脚本不同。

`quality_runner.py` 固定 IMP-TEXT、IMP-SCAN、IMP-IMAGE、IMP-MIXED、IMP-CROSS，各 3 轮。默认文字模型来自当前配置并须为本批已批准的 deepseek-chat，不切换 Provider；真实服务、RapidOCR、HTTP 尝试和 Trace 均保留。请求模型与响应报告模型分别保存。参考是用户确认的 AI 辅助标签，独立教师真值数为 0；按题号与真实来源页一对一比较，保留选项键序、失败计划分母和未知字段排除数。

先准备工作目录中的 `isolation.json`（database、pg_container、redis_container、redis_port）及已迁移的隔离 PostgreSQL、Redis和持久文件目录。工具不更改 .env，不创建生产资源，不执行生产迁移。代码快照通过 `--source-root` 选择，预置 OCR 模型通过 `--ocr-model-dir` 或现有 OCR_MODEL_DIR 指定。

```powershell
python benchmark/t160/quality_runner.py --plan-only --run-root <隔离工作目录> --repo-root <仓库目录> --source-root <源码快照目录> --annotations benchmark/corpus/t160-assisted-20261003/annotations.draft.json --input-root benchmark/corpus/v2-draft-20261001 --ocr-model-dir <PP-OCRv5模型目录>
```

`--plan-only` 仅核对输入、标签、枚举、模型文件并写计划，不调用模型、不写业务数据库。实际调用在明确授权及固定并发 1 的窗口使用 `--execute-confirmed`；新目录和执行标记保留失败，不覆盖或挑选旧结果。费用未由 Provider 返回时保留 null。失败后的未执行校正保留计划分母，不能作为已完成的人工作业。

`crop_assist.py` 是零云调用的后置步骤。它按本批实际原页阅读的新 AI 测量矩形，在真实 SourcePage 上调用资产服务生成裁图与稳定服务 ID，保存真实字节/类型/来源页/顺序。图像是否含有参考要求的区域需查看裁图证据；它不把新操作边界写回原参考未知 source_regions，不新增教师核对记录，不自动整卷 commit。学生可见保持 false。已知有图却未登记的情形继续计入关联分母。

```powershell
python benchmark/t160/crop_assist.py --plan-only --run-root <隔离工作目录> --repo-root <仓库目录> --source-root <源码快照目录> --quality-output <已完成的质量输出目录>
```

核对计划与原页后再用相同参数及 `--execute-confirmed` 执行本地资产写入；性能测量期间不执行此步骤。其 `after_image_assistance` 快照和关联计数与自动、纯字段辅助校正阶段分开保存。

资源采样使用同目录的 resource_sampler.py；操作日志中缺采样保留 null，并说明共享 PostgreSQL容器的进程 RSS范围，不将不同时间峰值相加或宣称独立教师质量达标。

## 导入性能与固定故障工具

`performance_runner.py` 固定执行 1/10/50 页各 3 冷、同规模 1 次预热和 5 暖，合计 27 次新导入；预热单列，生产 OCR 实例不跨导入额外复用。Provider 与重试维持生产路径，逻辑调用、Provider 尝试、真实 HTTP 请求钩子和响应分别计数。配置模型、真实请求体 `model`、响应 `model` 分开记录；请求钩子表示传输尝试，不证明服务器收到字节。每个冷进程和暖组拒绝覆盖已有结果，不能把失败后补跑替换原计划分母。

提交副本已修正 Windows venv 进程归属：worker 在业务阶段前写 `actual-worker.json`，controller 通过 Windows 父链确认来自精确启动的子树，再采实际 worker 工作集。原冻结脚本采的是 venv redirector，12 组均与实际业务 PID 不同。历史原始记录保持不变，`import-performance/resource-scope-audit.json` 明确应用内存和同时合计缺测、预算结论为空；原 `complete=true` 不能证明全部组件完整。提交副本结束窗口只纳入终态前完成的采样，缺测、短窗口或间隔过大均不能称完整。

`fault_cases.py` 仅验收固定损坏、51 页超限、OCR 禁用和显式 OCR 故障。前三者走自然错误，第四例在 OCR Provider 接口抛 `OCR_CALL_FAILED`，必须实际触发注入。真实原卷、已保存页图及失败状态保留；LLM 工厂意外进入时在 SDK 创建前阻止。这是管线业务故障验收，不是识别质量或教师真值。

复跑先独立准备已迁移的私有数据库、专属 Redis 与 `isolation.json`；不由脚本创建/清理资源，不使用业务库。数据库名限 `eduagent_e2_acceptance_<12位hex>` 或 `eduagent_t160_performance_<12位hex>`，Redis 名与 token 对应。`run-root` 和私有配置在仓库 `.cache` 下。显式提供真实源码位置、追溯值、预置模型和**新的结果目录**；追溯值未提供时记为 `unrecorded`，不会伪称历史提交。固定硬件值只标为历史验收环境，不能充当复跑机器的实测信息。

```powershell
# 参数示意：先填入本次独立资源与新结果目录，plan-only 不调用模型。
python benchmark/t160/performance_runner.py --plan-only --repo-root <仓库> --source-root <源码快照> --source-commit <真实追溯值> --run-root <仓库/.cache/新私有目录> --isolation-file <私有目录/isolation.json> --model-dir <预置PP-OCRv5模型> --output-root <仓库/benchmark/results/v2/新批次/import-performance>
python benchmark/t160/fault_cases.py --plan-only --repo-root <仓库> --source-root <源码快照> --source-commit <真实追溯值> --run-root <新私有目录> --isolation-file <私有目录/isolation.json> --output-root <仓库/benchmark/results/v2/新批次/technical/fault-cases>
```

实际执行需明确授权，性能会产生真实云调用费用；将相同参数的 `--plan-only` 改为 `--execute`。与其他模型任务、数据库写入和 UI 计时串行，保持已批准的完整次数；不新增补跑。性能的直接 `--worker` 入口同样校验数据库、Redis、文件根与私有配置一致后才执行。

`SCRIPT_PROVENANCE.json` 保留冻结执行脚本、实际计划和提交副本 SHA256，以及差异说明。提交副本另修默认路径、未完成原始记录的缺测统计并经 Black/Ruff 整理；历史证据属于冻结脚本，不能用提交副本更正后的行为反推已实测成功。本批导入性能与应用资源验收未通过，工具准备完成不代表 T160 已通过。

实际执行的质量/裁图/性能/资源/故障脚本及原计划逐字节归档于 `benchmark/results/v2/t160-assisted-20261003/frozen-execution/`；对应 copy-receipt 保存原始路径及 SHA；原字节封存于该目录 actual-execution-sources.zip，archive-receipt 逐成员核对，避免 Git 行尾转换改变历史字节。它们是历史测量材料，携带当时 cache 路径，不作便携入口；新执行请使用本目录参数化副本。真实页面测量、回放与观测器见 `ui-correction/README.md`，原 UI 冻结脚本另存其 frozen-runtime-sources/actual-ui-sources.zip，并保留成员摘要。
