# T160 修复重验证据（2026-10-03）

最终派生结论见 acceptance-summary.json：按用户批准门槛通过；原导入全部通过门槛仍失败。源码9c9e79f，修复782784d/678bcd5/9c9e79f。

- quality：15次真实导入、逐字段自动/AI辅助校正对照、33次真实裁图；独立教师0，质量阈值未宣称。
- import-performance：27导入、24计时，50页7/8，1/10页16/16；每个真实worker与逐秒原始内存样本，保留cold-3实际失败。
- ui-correction / ui-correction-summary：27原生动作与截图，24计时全部<500ms。首轮真实导入字段重放仅用于性能，不计为新提取。
- technical：先行红绿测试、诊断、真实64MiB进程探针、全量必验、故障/重启、清理及工作区保护。aggregation-initial-missing-restart-path.json为第一次派生汇总路径诊断，最终汇总已显式引用实际重启回执；不是重跑验收。
- frozen-execution：计划、实际脚本ZIP、参数与回执。acceptance-helpers.zip保留较早缓存辅助版本；最终实际汇总/停止助手另归档在final-helpers.zip及哈希回执。

质量OCR最高观察485.867MiB与正式文字性能303.184MiB分开解释；质量3个短窗/UI动作资源未知不填补。PG含共享容器RSS重复页，不宣称独占物理内存或EXE验收。

完整中文解释见 docs/evaluation.md 与 docs/validation-report.md 的“2026-10-03修复重验”节。用户此前已有文件字节保留，T146/T168口径未改。
