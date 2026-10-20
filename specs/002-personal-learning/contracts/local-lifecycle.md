# Local Runtime, Maintenance and Retirement — Approved Boundaries / Proposed Execution Detail

对应 [spec.md](../spec.md) FR-002、FR-007、FR-031—FR-033、TV-009—TV-011。本文件不单独提供执行授权；本轮仅允许T008—T014独立Test基础迁移与资源验证，不授权用户数据迁移/销毁或真实模型调用。任务已拆分，G-A/G-Q/G-C/G-P分别见 [tasks.md](../tasks.md) T007/T008/T073/T082；未通过门禁的依赖操作保持阻塞。

**Status**：T007架构职责与行为边界已按2026-10-19用户批准回填；本文件具体路径、DTO、字段、状态名称及实现示例仍为Proposed细节。T008十项硬性质量原则见[research §6](../research.md#6-t008-已批准质量原则--approved)；T008开发临时保护/逐例验收方法现已批准，见research §7；仅T008—T014获本轮实施授权。复杂渲染依赖、真实效果、G-C/G-P不随此批准。

## 1. Personal and Settings Boundaries

已批准一个数据根一个个人空间、取消业务角色/登录、Setup与凭据安全独立；以下请求及资源检查是该方向的设计映射，不授权移除仍使用的共享保护：

- 通过已有环境解析得到当前空间，不接受客户端构造的教师/学生身份；不引入虚拟用户自动登录。
- Windows 保持本地监听，Docker 宿主发布保持 loopback。实际允许 Host 从部署配置取得，不将容器内部0.0.0.0当用户可访问 Host。
- 复用精确 Host/Origin 判定；个人写操作检查 Origin，上传 multipart 与 JSON 按实际协议处理，不能照搬设置中对 multipart 的拒绝。
- GET 图片/原页可能不带 Origin；校验合法 Host、当前空间、资源关系和业务阶段，不强迫所有 GET 带 Origin。
- Setup token、一次码交换、浏览器授权交接及配置修改保护继续独立；个人页面正常使用不需要维持管理 token。
- 文件读取沿现有 file_id→资源绑定→受管路径链；保留根身份、路径越界/reparse point、实际存在性、文件内容完整性和阶段可见性。不得将 storage 整目录公开为静态资源。

不增加远程多用户登录或全局 token 体系。若现有 Docker 代理/发布方式不能证明允许 Host/Origin，提交明确部署取舍，不以关闭检查解决。

## 2. Operations, Credentials and Recovery

RuntimeSnapshot 固定运行使用的设置修订、Provider/模型、Prompt/Schema版本、检索和 Embedding 身份以及凭据引用版本；密钥不进入业务记录、日志或引用响应。旧快照缺失或依赖不可用保持显式失败，不回落当前配置。

新操作使用现有 ProviderRegistry/客户端 Lease 生命周期。收费确认、用户重试、超时未知和防重复发送见 [personal-learning-api.md](personal-learning-api.md)。Embedding/Rerank/Vision/核验也可能收费，不能作为隐式“免费辅助步骤”。

维护备份/恢复必须从旧 Admin/JWT 依赖转为已有本地管理授权的明确入口；不得删掉权限检查后公开维护能力。备份清单与恢复校验需认识 Subject/Practice/QuestionRevision/GradeAttempt/Conversation 及新文件归属，检查真实 FK、文件、版本引用、索引身份与运行依赖；失败继续停写/明确不可用，不部分恢复后假装业务就绪。

新增业务历史的备份恢复仍需保护。用户“不留副本”针对已遗弃旧业务，不禁止保护新版本在用数据。旧业务销毁不是可逆迁移，不承诺 downgrade 恢复已删数据。维护与新运行恢复的确切兼容策略须在迁移设计 Review 后冻结。

## 3. Proposed Retirement Inventory

下列是源码支持的候选清单，不是删除指令；实施前还要逐对象核对消费者、FK、调用链和文件归属。只能在必需替代功能已迁入且独立验证后退役。

| 删除/替换候选 | 必须先处理的依赖 | 保留边界 |
| --- | --- | --- |
| 业务 auth/admin、User/Role/user_roles、教师/学生菜单 | Course/Submission/Review等FK、文件授权、审计/维护消费者 | Settings/Setup/Host-Origin、配置/凭据保护 |
| Course/Exam/ExamParticipant 与发布/考试时段/分配流程 | Subject/Practice替代、固定输入、并发事务、恢复owner映射 | 新练习、题目身份与历史事实 |
| 旧 Submission/Review 角色约束、整卷 final 投影 | 逐轮评分/争议接受、逐题诊断 | 评分结构、低置信/失败状态与幂等保护 |
| QuestionSourcePaper 派生题专用链（无新消费者时） | 确认rewrite/translate/extend是否全部退出 | QuestionOrigin及真实教学来源关系不能误删 |
| Gradio app/views/callback与旧BusinessEntry跳转 | React完整业务、API、必要loaders/纯逻辑迁出 | 仍有服务价值的逻辑，不按ui目录整包删除 |
| Gradio冻结脚本、打包hook及专属依赖 | app import、构建 spec、测试装配、间接依赖用途 | Windows Launcher/Bootstrap/前端产物与管道机制 |
| 废弃 JWT 必填/就绪配置和界面 | 确认所有业务认证消费者已退出、维护已迁移 | Provider/DB/Redis凭据与在用版本引用，不直接删除整库凭据 |
| 教师/学生seed、演示入口、旧路由与专属文档 | 交付脚本/示例消费者与新用户流程 | 已存在Development/Demo资源本轮不接管 |
| 明确旧业务行、导出、缓存、已无引用的旧运行对象 | 逐项身份与所有权，先停旧运行、核对引用 | 模型/凭据/部署配置、索引身份、新在用历史 |
| 废弃业务测试断言/夹具 | 最低必要TCR：原规则退出、新规则与安全断言去向 | 不删除安全保护或失败断言制造通过 |

.specify 旧指针、001 Feature、历史迁移链与评审记录不是本轮可清理对象。旧源码迁移脚本是初始化链的一部分，不因其建过旧表就直接删除；可能的整理需另行证明不破坏 fresh install/升级/恢复并获批准。

## 4. Migration / Cutover — Proposed

1. 枚举受影响表、FK、资产owner、配置及运行引用；将“遗弃”与“共享/在用”分开，清单经 Review。
2. 在身份明确的独立 Test PG Schema 建立旧头与新头转换，验证新空空间初始化及配置/凭据/索引身份保留；不迁移旧材料或旧成绩。
3. 以事务内数据库变更和可核验的文件处置边界设计失败状态；不声称跨数据库/文件动作天然原子。失败保持维护状态，不开放部分完成空间。
4. 真正实施时先停止属于目标应用的旧业务工作，再执行已授权清理；不为遗弃对象创建快照、备份、归档或副本，也不扫描私人目录。
5. 新业务可用、恢复映射验证后，删除已被替代且无消费者的旧代码/UI/路由/配置/文档；当前规划不执行任何步骤。

不假设清理共享 Docker 卷即可完成产品重构。Development/Demo资源及未知所有权对象始终排除；资源名字包含test不足以授权数据操作。

## 5. Deployment and Validation

Windows 沿用独立 onedir、前端产物和数据根分离、自有 worker及进程清理。受控浏览器只能使用本轮拥有的 profile；匿名管道由支持该协议的控制器接收，不能把 Chromium 当任意 stdin JSON 消费者。一次码不进 argv、环境、日志或临时明文文件。无需新增10秒硬门槛、跨用户DPAPI或三次首启强制重复。

Docker 使用独立项目、随机本地端口和自有卷/网络；沿现有 DockerRun 夹具核对PG系统标识、角色、数据库/Schema及Redis身份，清理前核对容器ID和label。禁止接管固定旧M0容器/卷；等价独立资源证据需如实标记。

两平台都需真正体验新React首页、资料/图文、练习、结果、诊断/对话和引用；同时复核初始化、配置恢复、挂载Secret（Docker）、数据持久化及退出。编译通过、HTTP就绪和历史v3验收不能替代这些新体验。

## 6. T006 具体消费者与维护兼容建议 — Proposed

| 实际责任方 | 当前事实 | Proposed修改范围及保留边界 |
| --- | --- | --- |
| file_resources.py、storage_migration_service.storage_references | 资源仅document/source_page/staged_asset/question_asset/export；owner含course、knowledge_base、paper_import、extracted_question、source_page、question及exam/submission | 新文件仍复用同一枚举/登记链；owner换为当前空间内subject及真实材料版本/导入/修订引用。export无新需求时退役，不因此新增导出功能 |
| QuestionAsset.imported_asset、asset_origin/image_owner | 正式导入图从唯一暂存题投影文件位置；依赖Question.imported_extracted_question单来源及同导入页 | 支持多来源同题后明确选择原始资产关系；固定修订引用受管资产，不能从canonical题猜原页或复制第二份路径；图文块资产不遗漏于备份清单 |
| FileStorageService._actor/_manages/_authorize、question_asset_access | 实际读取仍要求User/Teacher所有权或Student考试/作答关系；student_visible不足以独立授权 | 改当前空间、真实资源关联及练习阶段依赖；保留合法Host/Origin、原页答案泄露保护及受管路径完整性，不直接删鉴权分支 |
| core/maintenance.py、BackupRestoreService._check_actor、scripts/backup_restore.py | 管理依赖Admin/JWT；恢复后检查FK、同集owner/字节/收据，保留停写marker | 从既有本地管理授权进入；身份核对后在目标隔离根恢复，新域owner枚举与FK同步更新，失败不开放写入 |
| backup_restore_service._runtime_snapshot_ids | 当前只收集非终局grading Workflow绑定；manifest_version=1记录现有资源，尚不认识新GradeAttempt/Conversation/Operation | 新备份收集所有仍被新历史或运行引用的快照及真实配置/凭据版本依赖；不能只沿用非终局工作流扫描，也不复制真实密钥到业务快照 |
| React router/BusinessEntryPage、core/app.py、backend/app/ui | React业务入口仍跳/gradio；app import并挂载Gradio；UI loaders含AuthService、角色、课程访问，results_analysis还含纯格式逻辑 | 必需业务迁至React/API后删角色菜单和callbacks；可复用纯逻辑先迁出，核对所有loaders消费者；本轮没有删除任何UI |
| packaging/EduAgent.spec、scripts/freeze_gradio.py、pyproject/Windows构建依赖 | 构建收集gradio/gradio_client并注入冻结补丁 | 无消费者后去掉专属依赖/hooks；保留Launcher、Bootstrap、浏览器管道、前端产物及OCR/PDF运行依赖，双平台另验 |

恢复兼容的推荐是显式升级新业务备份格式和owner目录；以实际格式/表关系/引用判兼容，不以Git SHA要求组件锁步。旧manifest v1不冒充新域备份；旧工具遇新格式拒绝，新工具不自动导入旧业务备份。迁移到空个人域后，只保护新版在用数据及仍在用配置；历史设置文件/凭据版本按原服务保留。

维护失败继续保持目标停写；成功恢复仍需独立激活。数据库与文件变更不承诺跨介质天然原子。旧业务销毁后，downgrade只能讨论结构，不能声称找回旧行/原稿；不为此先生成废弃业务副本。基础DDL已在T009—T014独立真实PG验证；cutover、完整恢复格式/owner兼容及Windows/Docker产品交付仍留后续对应任务。
