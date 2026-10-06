# 文件归属、读取与一致备份

业务根由 STORAGE_ROOT 配置，uploads/papers/assets/exports 保存持久材料，安装目录、临时缓存及EXE内部资源不作为业务根。Document、SourcePage、QuestionAsset和ExportFile保持稳定file_id/真实归属，共享字节保留每个真实引用和操作收据。路径不是公开授权凭证。

教师按课程授权读取；学生按当前考试或本人最终答卷上下文读取明确开放的题图。源卷、来源整页及含答案整页永久私有。新元数据关联仅接受持久根内已登记且有权读取的文件，外部资料经上传；历史定位必须显式迁移，缺失/未知不可当作已有文件。

导出服务先登记真实归属，再写入字节、元数据和收据；失败保留 writing/failed事实。当前业务没有新建导出UI，Benchmark仍沿用原方式。T191用实际最终成绩CSV调用既有FileStorageService并验证教师字节一致、学生403，不能称所有业务导出入口已实现。

一致备份需要维护窗口：

1. 停止后端、Worker及所有写入脚本，确认所属进程退出。管理员JWT只从环境变量读，不写命令参数/日志。
2. 执行原 `scripts/backup_restore.py --pg-container <明确的现有PG容器> backup --backup-root <与业务根独立的新目录> --writers-stopped`。也可使用已安装标准客户端目录。工具检查实际实例、排空连接、持有表锁/文件维护标记，备份数据库和四目录及收据。
3. 只把 manifest.outcome=complete 的同集材料用于正式隔离恢复；incomplete/failed保留诊断，不能改清单制造完整。
4. 原CLI restore指定全新数据库、全新独立目录和独立报告。工具核对文件SHA/归属/资源关系/外键；失败保留原环境和调查材料，verified后仍停写，不自动切换.env。
5. 另行核对题目来源、原图、选项顺序、考试本场分值、答卷成绩、复核和诊断。激活恢复目标、修改配置或清理旧材料须按用户授权执行。

T191实际同集恢复32张表全部行一致，20文件字节一致，4 Document/3SourcePage/3QuestionAsset/1ExportFile身份保持；两场总分10/5、最终2/10、原C/A/B/D顺序及缺答案/失败状态保持。恢复后的教师下载、学生开放裁图和源页403均由生产文件授权服务核对；未重新启动恢复环境或切换原配置。原库仍0012，原PG/Redis健康。

证据见 [storage-verification](../benchmark/results/v2/t191-20261006/storage-verification/receipt.json) 与 [说明](../benchmark/results/v2/t191-20261006/README.md)。哈希用于内容完整性和追溯，不能代替业务语义、授权或运行验收。
