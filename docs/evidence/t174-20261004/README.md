# T174 本场评分准备、尾差和教师确认

本批完成的是结构化标准准备/确认及发布必要门禁；T175完整生命周期、T176–T177评分输入和消费、T178完整界面联动、T179性能/系统验收仍待办。T146继续使用AI辅助+开发者审查，独立教师标注0；T168整体质量not_met结论不变。

## 证据边界

- RED/GREEN原始日志、JUnit XML及外层命令收据均保留；同一测试可能出现在多个聚焦批次，不能加总为独立样本。
- 第一轮完整快照：2357 passed、3 failed、2 errors、2 skipped，2364项；原始运行873.223秒，pytest861.51秒。两项skip为缺显式独占M0 Compose配置和Windows不允许测试软链接，不计通过。
- 两个旧阅卷合同夹具直接插入Approved且缺客观Rubric，旧教师完整流程缺本场准备确认；按TCR29补真实合成数据与现有命令，保留原业务断言。另两项资产失败收据回归源于失效UPDATE意外autoflush早于收据交付，局部用no_autoflush保留原flush/commit归属；原测试完全未改。
- 第一次临时index误用关闭autocrlf，造成668个仅行尾变化路径，未改真实index或用户文件；其原快照和日志保留，仅作定位证据。第二轮用实际Git过滤器生成精确源码快照，tree `7551c2a35dfb7493b0bfb2d2b212bf8be6816c5d`、27个真实变更路径、1860文件。第二轮全量实际结果为 **2362 passed / 2 skipped / 107 warnings**（pytest861.74秒，外层872.771秒），exit0；首轮5个失败/错误节点全部通过，原始证据并存。
- 精确快照排除用户已有两份README和阅卷样本差异，取该三文件的HEAD内容；工作区和.env原字节未动，.env不复制到快照。`submission-source.json`保留逐文件校验、实际parent/tree和保护收据。该tree反映测试时源码，不包含之后追加的测试结果和完成状态。
- 真实浏览器步骤和持久读回见 `browser-verification.md`、`t174_ui_database_readback.log`；合成教师由AI实际操作，不冒充独立教师标注。

## 复现范围

`run_isolated.py`及`run_snapshot.py`复用当前配置并仅以环境变量覆盖本批独占DB/Redis/文件根，连接密钥不进入报告。运行完整pytest需预先建立相应隔离资源并配置独占记录；示例批次标识不能用于写入现有业务数据库。静态检查及Alembic check另有原始日志。测试不替代Docker M0、云模型质量或EXE验收。

## 最终静态与一致性检查

Ruff覆盖本批22个Python变更文件，全backend Mypy207个源文件通过；Alembic check无新增迁移操作。runtime-verification-r2.json确认当前backend/tests/scripts按正常Git过滤后与最终运行tree一致，真实index未改。

最终1860个运行快照文件SHA全部未变；零测试进程退出后，本批零连接数据库与带T170-174所有权标签的Redis已清理，缓存和业务测试文件保留。原库仍0012_audit_logs，原1考试/2关联/1答卷/2答案及三类评分结果0不变，四个用户文件原字节保持；详见t174_cleanup.json。
