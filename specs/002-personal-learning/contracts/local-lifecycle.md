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

## 7. T073 当前对象核对与已批准执行范围（2026-10-20）

本节核对现有源码和实际迁移头。用户在当前会话明确批准本节对象级范围：先替代并回归，后退役16张表；旧行/文件处置仅限§7.1核验的独立Test及本轮合成对象；禁止扩大范围、未核实对象/依赖的DROP CASCADE及未知资源操作，workflow_runs整表排除。T073据此通过；T074—T081仍须按依赖独立实施验收。12/16前置清单仅流程覆盖，原内容和勾选不变；前文较早批次的授权文字仅为历史背景。

### 7.1 实际结构与目标身份

- 分支`deepcode`，HEAD `9ebcf55cf6b311f0d6f6b60d8faf0674931157bf`；开始时已有56个未提交文件（29个已跟踪修改、27个未跟踪），均为保护对象。
- 当前完整迁移头为`0032_personal_conversations`。在本轮新建的独立Test PG实际升级到该头并读取50张表及外键；没有查询用户既有业务数据库，也未将T006静态候选视为当前结构证明。
- T073首次批准的数据库资源（该历史目标现已不存在）：新容器`b371184e9cbde7179725d3a9319651a719044c496e76d06d7c79c9ead61eebbf`（`eduagent-t073-t081-70758f8f`），loopback `127.0.0.1:46057`，PG系统标识`7698662914478903334`，数据库`eduagent_test`，角色`eduagent_test_runner`；资源回执位于本轮自有`.cache/t073-t081/`。结构核对使用的随机schema已由身份核验夹具释放。
- T079–T081接续资源已获用户明确对象级批准：仅将相同§7范围应用于Test PG `127.0.0.1:46061`、`eduagent_test`、系统标识`7698775259907203110`、容器`301d965b42c3`，以及Redis `127.0.0.1:45913`、容器`beb8d9987aba`；文件仅`.cache/t079-t081`本轮新建、归属已证实且解析路径无联接越界的合成对象。操作前仍逐次核验身份、16表清单、引用与共享消费者；不扩展清理对象，不作用于其他实例或未知记录。准确资源回执在本轮自有缓存中，凭据不写入合同。
- 待批准的实际数据执行范围仅限上述本轮自建Test实例中再次登记的隔离测试数据库/schema和本轮自有Test根内的合成旧业务对象；运行前仍须核验数据库/角色/schema/实例与文件根。当前没有任何Development/Demo、用户既有数据库、容器、卷或历史文件的对象级授权。本轮产出的cutover工具将来也不能凭源码清单自行接管这些资源。

### 7.2 A类：仅服务于遗弃业务，用户已批准按前置条件退役

| 对象 | 当前删除依据 | 执行前约束 |
| --- | --- | --- |
| `users`、`roles`、`user_roles` | 旧业务账号、角色与关联；新科目及Setup管理主体独立 | 先解除下列C类User FK及维护/文件消费者；不删除任何凭据库文件 |
| `courses` | 旧教师课程；新域使用`subjects` | 先处理C类共享表的旧行及course FK，不能依靠级联删除共享数据 |
| `exams`、`exam_participants`、`exam_questions` | 旧考试、学生分配及发布题面；新域固定输入在`practice_items` | 先处理Submission/Answer/旧Workflow/导出及题引用；保留个人固定题面 |
| `submissions`、`answers` | 旧学生整卷及答题；个人使用`practice_answers` | 先处理旧评分/聚合/Workflow，禁止混淆同名逻辑Answer |
| `grading_results`、`review_records` | 旧单结果/教师审核；新域使用append-only `grade_attempts`及`score_disputes` | 不触碰新轮次、争议、接受关系 |
| `exam_results`、`diagnosis_reports` | 旧整卷final及学生聚合诊断；个人诊断为有效作答投影 | 不修改个人作答/得分事实 |
| `export_files` | 旧课程/考试/答卷导出登记，新版未提出导出需求 | 对登记路径逐文件验证所有权；共享/未知路径排除 |
| `question_revision_comments` | 旧教师审核评论，必须引用User；新修订历史独立 | 先核对没有个人修订/来源隐式引用 |
| `question_source_papers` | 旧rewrite/translate/extend派生关系，不是新同题多来源 | 仅删除旧派生链；`question_origins`及规范身份关系完整保留 |

上表共16张专属表；提交审批的是逐表退役及其已确认旧业务行，不是无条件`CASCADE`或任意数据库整库清空。历史Alembic迁移文件及`alembic_version`保留，未来fresh install/升级须完整执行并验证。

### 7.3 B类：新版仍在使用，必须保留

- 个人域：`subjects`、`knowledge_points`、`chapter_knowledge_points`、`material_series`、`material_versions`、`question_revisions`、`question_revision_knowledge_points`、`question_revision_assets`、`question_origins`、`duplicate_candidates`、`practice_sessions`、`practice_items`、`practice_answers`、`grade_attempts`、`score_disputes`、`operation_runs`、`conversations`、`conversation_messages`。所有来源、固定版本、轮次、争议和计费操作事实保留。
- 共享表的个人行及受管字节：`chapters`、`knowledge_bases`、`documents`、`document_chunks`、`questions`、`paper_imports`、`source_pages`、`extracted_questions`、`question_assets`。以真实Subject/Import/Question/Revision关系判断，不能按表、目录、文件年代或文件名整体清空。
- ProviderRegistry、RuntimeSnapshot及其原设置修订/凭据版本、Lease、LearningOperations、Embedding Identity、检索范围保护、解析/OCR/PDF/DOCX及有序图文、SubjectiveGrader/客观评分等共享纯逻辑；仍用的Workflow/检查点和脱敏追踪基础设施。
- Setup/管理会话、Host/Origin、受管路径/文件完整性、数据库及Redis配置、Provider及API凭据、部署设置和现有凭据版本。停止使用JWT不授权删除整个凭据库或其历史引用。
- React个人入口及设置/初始化/诊断面；Windows Launcher/Bootstrap、受控浏览器管道、worker清理、前端资源；Docker loopback、挂载Secret、独立卷/网络装配。构建清单只有被证明Gradio专用的项进入候选。
- `.specify`旧指针、Feature 001、requirements全部内容及16个复选框、已批准架构/质量合同、历史迁移链、用户其他文件。

### 7.4 C类：必须先替代接线，再按批准对象处理

| 对象/责任方 | 当前可达依赖及所需替代 | 提出处理的精确边界 |
| --- | --- | --- |
| `backup_restore_service.py`、`core/maintenance.py`、`scripts/backup_restore.py`及`migrate_storage_paths.py` | 备份入口/CLI仍用旧Admin JWT；`for_local_management`已有但尚未贯通备份；收据仍要求User且仅识别course owner | T074改接已有Setup管理授权；不得删除检查后公开维护，也不创建虚拟管理员 |
| `BackupRestoreService._runtime_snapshot_ids`及`settings/management.py`运行索引 | 备份只收集非终态旧Workflow；设置索引虽有Operation/GradeAttempt仍先查询旧Workflow表 | 收集个人操作、全部评分及对话引用的真实快照/配置/凭据依赖；缺失闭锁；旧运行退出前先迁移消费者 |
| `workflow_runs` | `submission_id`非空，FK还指向旧Answer/ExamResult；旧启动恢复及运行索引仍查询 | 先退出旧启动恢复/业务Store并保留通用Workflow/检查点能力；仅提出清除已证实旧业务运行行。是否删除整个表/结构尚不纳入本次16表批准包 |
| `agent_runs`、`audit_logs` | 追踪/审计能力可复用；AgentRun有User FK、可选旧Workflow FK；AuditLog没有FK但含actor_role和resource隐式引用 | 保留共享结构/能力；只有能追溯到批准旧业务对象的行才可纳入后续显式处置，独立/用途不明记录排除；不得全表清空 |
| `chapters.course_id/confirmed_by`、`knowledge_bases.course_id`、`documents.course_id/uploaded_by`、`document_chunks.course_id`、`questions.course_id/created_by`、`paper_imports.course_id/uploaded_by` | 实际0032 FK仍指向Course/User；个人分支这些字段为空，新Subject关联独立 | 先按确切旧Course归属选旧行，并排查新历史、JSON及文件的反向引用；无混合/跨域引用才可删旧行，再解除无消费者旧列/FK。个人行保持原ID和字节 |
| `question_generation_metadata`、`question_source_chunks`、`question_validation_results.requested_by` | 旧出题/核验/推荐使用；共享题模型及纯核验逻辑仍有导入；新生成使用Revision/Origin/Operation | 旧题从属行随其明确处置；不得据名称删除共享纯逻辑或未知来源快照；User FK在消费者迁出后解除 |
| 文件登记和目录：`file_resources.py`、`storage_references`、`FileStorageService`、`question_asset_access.py` | 新版已有subject owner；旧分支仍加载Course/Export/User；暂存/正式资产可能共享同一物理文件 | 以file_id、owner、规范路径、摘要及全部在用反向引用生成确切处置项；仅删无新引用的根内旧字节及对应旧收据。根外、reparse point、未登记文件不删除；不清空uploads/papers/assets/exports整个分区 |
| `api/paper_import.py`、`api/question_generation.py`、`services/diagnosis_service.py`、`question_validator.py`等混合模块 | 分别含新个人router/服务/准入逻辑及旧教师入口；`subjects.py`实际导入个人router | 只退出旧router/角色分支；保留或迁出所有新消费者，不按文件名整体删除 |
| `core/app.py`、旧auth/admin/courses/exams/submissions/grading/reviews/results/workflow/traces等API及旧业务服务 | 完整工厂仍装配旧路由、AuthenticationMiddleware、Gradio和旧启动恢复；个人首页已接React | 在T076验证后退出旧装配、业务路由及专用服务；保留个人API/异常传播/资源关闭。`/api/files`等旧入口先迁完共享读取能力 |
| `backend/app/ui/`及`BusinessEntryPage.tsx` | Gradio页面/loaders依赖Auth/角色/旧课程；实际React路由`/business`已重定向个人首页，但未使用旧组件仍含`/gradio`链接；UI纯格式化逻辑有单独测试 | 核对纯逻辑新消费者后退出被替代页面、callbacks/loaders；不删除仍使用的纯逻辑或安全断言 |
| JWT必填、就绪及设置界面 | `AppSettings`、`SettingsRuntime.app_settings_for`、readiness和部分React设置仍认识JWT；个人数据库投影已不要求JWT | 最后解除旧业务前置/入口；历史凭据版本不被自动抹除，Setup独立授权不变 |
| `pyproject.toml`、`packaging/EduAgent.spec`、`requirements-windows.txt`、`build_receipt.py`、`scripts/freeze_gradio.py` | 显式Gradio/gradio_client收集、冻结补丁及构建回执仍存在 | 无消费者后删除Gradio专属项；其传递库逐个核对，不凭来源删共享库；Windows/Docker重新构建验证 |
| `scripts/demo_seed.py`、`run_demo.ps1`、`demo_materials/python_basics.md`、旧考试审计/benchmark及相关fixtures/文档 | 教师/学生seed和`/gradio`演示入口；部分benchmark复用共享模型/解析能力 | 仅退役专属源码/合成夹具及已被替代断言，最低TCR映射到新合同；不执行脚本接触既有Demo，不动Feature 001 |

### 7.5 D类：所有权/用途未确认，本轮执行排除

未知业务数据库/schema、历史Docker容器/卷/网络、Development/Demo、私人目录、根外历史原稿、未登记或新旧混用文件、无法证明只属旧业务的Audit/Agent记录均不在申请删除包。仅靠`subject_id IS NULL`不足以将异常行认定旧业务：必须还能证明有效旧Course/Import归属且无个人引用。遇到混合归属或个人历史引用旧文件，停止对应对象，不自动复制/迁移、删引用或放宽约束；将具体事实交用户裁决。

### 7.6 建议执行顺序与审批状态

1. 用户按A/C/D边界确认逐对象范围和本轮独立Test目标；没有此确认，T073保持`[ ]`，不执行cutover或旧对象删除。
2. T074先适配新域备份恢复、所有快照/配置/凭据版本依赖及本地管理授权，校验格式兼容与缺失闭锁。新域备份保护不成为遗弃旧域的备份手段。
3. T075在实际核验的自有Test中登记确切旧行/文件；写入持久维护状态、停止目标自身旧写入、排查全部反向引用，再按依赖处理已批准旧对象。数据库事务与文件删除分阶段保存事实；失败保持停写，只对同一确认计划安全重入，不假装跨介质原子。
4. T076独立验证旧对象无副本、新域历史/配置/凭据/索引/运行引用保留、失败不激活及恢复依赖完整；通过后T077/T078退出角色/旧入口并验证安全。
5. T079最后清理无消费者的源码、Gradio依赖/冻结配置、专属seed/文档/夹具；不删除历史迁移或有效安全断言。T080/T081再运行退出后的真实回归；本轮不进入T082或真实模型调用。

**实际审批状态：用户已在当前会话正式批准§7的16表及受控旧行/合成文件范围，允许继续T073—T081；禁止扩大到未列明对象，workflow_runs整表不删除。** 破坏性操作前必须再次核验指定实例/数据库及根内实际路径、外键和全部在用引用；发现混合/不明归属停止对应处置并报告。T075/T076现已仅在该指定Test实例的本轮自有Schema/合成文件验证退役；T077后续源码/角色入口退出尚未完成。资源身份不代替批准，后续真实部署不得自动执行清理。

## 8. T074 个人备份恢复接线

沿用同一BackupRestoreService和标准PostgreSQL客户端，新增进程内`for_local_management`入口；原CLI的`--local-management`由既有LocalManagementAuthority发行/兑换Setup授权，不公开HTTP发行或维护入口，不使用虚拟User或业务JWT。环境/实例绑定及每次操作授权仍校验；旧角色入口的最终退役留T077，不能由本项替代。

个人备份使用manifest v2并记录environment_id及实际数据库schema，旧manifest v1不能作为个人备份导入；旧入口不能把v2当旧格式恢复。新增materials原件/解析媒体及subject owner/无User收据的同一资源清单，个人目录不要求旧Export表或查询旧Course/Export。遗弃业务行或材料仍存在时，在dump前拒绝为其建副本；尚无个人owner映射的Agent/Audit行保留源记录并阻断备份，不能擅自归为个人历史。未登记文件在复制前拒绝，不先复制再标incomplete。

OperationRun全部状态、GradeAttempt全部轮次及Conversation Message的原快照均进入引用集合，同时保存其原非敏感设置修订元数据；不复制凭据正文。恢复目标必须显式指定同一空间的全新隔离根/数据库，并先以原修订及原凭据版本完成依赖核验；缺失不回落当前配置、不生成替代身份。凭据和配置的独立恢复沿用原服务，业务恢复不自动激活配置。文件/FK/实际schema和同集字节核对后也保留目标维护标记，`verified`不是业务已启用。

个人备份发生异常、进入窗口失败或结果incomplete时持续保留停写标记；成功完整备份正常释放。恢复保留未知收费结果、实际发送数及占位，不运行模型或自动补发。T074本项没有删除任何旧业务表/行/文件或改写历史迁移头；T075实现与限定执行事实见下节。

## 9. T075/T076 显式Test退役与安全重入

`0033_personal_cutover`只创建`personal_cutovers`持久维护记录，保留0001—0032全部迁移。普通`alembic upgrade head`不会清除旧行、旧表或文件，迁移头到达0033也不代表退役完成；实际状态以维护记录和停写标记为准。downgrade明确拒绝，不能找回已销毁的遗弃对象。

沿用`backup_restore.py --local-management`的Setup本地管理授权，增加`cutover-prepare`与`cutover-execute`，显式传入已批准的database_identity（包含实际Schema）及file_root。实现仅接受身份核验一致的独立Test；不从生产配置或历史授权推断新的清理权限。预检保留停写、返回真实operation_id和计划摘要；执行必须提交同一身份与摘要。该凭据不会公开到HTTP业务路由，未停止写入不得执行。

按有效旧Course/Import/Submission链证明共享旧行归属，并检查保留行的FK、标量、冻结JSON及实际文件别名。未知Agent/Audit行保留；无法证明旧归属或发现个人引用时，在删除前闭锁。仅解除§7已列明的旧FK，不删除共享表/列、未知约束、枚举、函数或迁移历史；16张专属表使用逐表DROP，不使用CASCADE，`workflow_runs`整表保留。个人文件写入改用已有个人资源目录，不再要求旧Export表；所有个人HTTP写入在维护期间于收费占用前返回503。

日志只保存对象身份、数量、摘要和阶段，不保存旧行或文件内容。文件逐项先持久登记deleting，再删除并登记deleted；DB内共享旧行、16表DDL及database_retired阶段在一个本地事务提交。文件删除与DB事务不具有跨介质原子性；DB回滚不能恢复已删除字节。失败、中断或释放标记失败保持停写；重新授权本地管理会话后只可显式重入原operation/摘要，不自动执行、复制或恢复旧业务。预检未形成计划时，将失败事实持久写入维护标记，可显式按原operation重新预检；已有计划不能被覆盖。

会话级PG维护锁阻止并发重入；独占维护连接退出后实际关闭，不留下池中空闲客户端或会话锁。再次核对保留行摘要、退役对象缺失、计划文件已删除后才完成并释放标记；释放失败可显式重入继续。退役后的真实学习旅程和标准pg_dump/pg_restore已验证，恢复目标仍按T074合同停写，不自动激活配置或重发未知收费请求。
