
# T147–T149 文件持久化实现记录

本批实现基于 deepcode / 9912ece，按 speckit-implement 执行。任务完成范围是文件服务、已有资料上传接线、模型/迁移与授权 HTTP 读取；后续历史文件迁移、备份恢复、E2 原页/资产和 E4 发布冻结接入仍按任务依赖实施。

## 配置与迁移

- STORAGE_ROOT 是唯一运行根，可显式配置；开发默认仓库 storage，Windows EXE 默认 LOCALAPPDATA/EduAgent/storage，Docker 为 /app/storage，挂载 storage_data 命名卷。
- 业务文件与私有操作收据排除 Git / Docker 构建上下文。重建 Backend 后使用同一卷，不能把容器临时层作为唯一副本。
- 0013_file_storage 增加可空 Document.file_metadata JSONB 和 ExportFile；旧文件路径/业务状态不回填。只有实际文件核对后的登记才能保存长度/摘要/类型。运行更新源码前须通过既有启动迁移流程升级到该版本；本批仅在隔离库/schema 应用迁移，开发业务库未升级。
- ExportFile 课程/考试/答卷恰一归属、RESTRICT 外键、audience、writing/ready/failed、真实错误和 UTC 起止时间由模型/迁移约束；只有可靠写入且 ready 提交后可以下载。

## 服务与公开入口

- FileStorageService 的 d_/e_ file_id 投影实际 Document/ExportFile UUID；DocumentSummary 提供 file_id，客户端回传服务提供的标识。
- API multipart 和知识库 UI 上传共用 KnowledgeBaseService。课程权限/格式先检查，再保存 uploads 原稿及 prepared→written 收据；引用提交后才更新 committed。解析读取持久原稿，正文不能替换该身份的原稿。
- 新内容须新资源身份；同名文件不会覆盖。数据库失败保留原稿/拟建身份/真实归属/失败收据；提交后收据更新失败返回已提交状态和错误，不假称没有引用。
- 公开元数据登记可无定位；有定位时仅关联当前教师有权读取的已登记持久材料。每个共享身份有独立关联收据，保护元数据解除后的题目来源快照。旧可信内部登记及历史绝对/相对定位按原值读取，不隐式查找别的根。
- GET /api/files/{file_id} 使用真实登录与资源关系：教师限本人管理课程，submission_owner 导出仅该答卷学生本人及课程教师；其他学生/教师拒绝。未认证 401、越权 403、不存在/缺失/未知 404，未就绪导出 409；缺失文件不改写原业务成功历史。
- 内部清理没有新增 HTTP 入口。共享定位关联/清理使用 PostgreSQL 事务级 advisory lock；有效 Document/ExportFile、题目来源快照或未完成收据拒绝 FILE_IN_USE；无法核对归属的材料保留。仅测试临时原稿执行清理。
- create_export 接收真实业务生产者的 bytes、发起者、唯一归属和 audience，写入 exports 并返回授权投影。本批没有现成业务导出生产者，保留 Benchmark 原有结果格式/写入方式，不添加导出 UI。
- 原页/题图真实模型在 E2 接入；当前不伪造 p_/a_ 映射或学生复习权限，不挂公开 storage 静态目录。E4 的发布/历史保护继续使用文件服务触点。

## 验证与交付边界

测试变更先追加 docs/test-change-record-v2.md 的本批 TCR；结果见 docs/validation-report.md。Windows 符号链接用例受系统权限限制跳过，Docker M0/真实 EXE/历史文件迁移/备份恢复不能由本批源码或单元测试替代。

原 8 个用户 UI/README/测试改动保留，不纳入本批提交；T146 仍待真实教师标注，不勾选。仅在最终检查通过后勾选 T147/T148/T149。
