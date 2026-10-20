# Tasks: Local Settings and Onboarding

**Input**: 本Feature的spec/plan/data-model/contracts/quickstart、research决策索引及统一TCR。
**Status**: T192–T239 已按当前本地 MVP 条件完成，当前48/48。按 2026-10-19 本地 MVP 范围验收，执行结果在对应 Codex 会话汇报。
**ID**: 从T192开始，避免与历史T001–T191重名；不修改旧状态，T125–T132不在范围。

## 执行约束

- 每个Task只记录完成状态和必要简短证据；测试/验收/失败分析只在Codex会话汇报。设计变化更新既有设计或契约；测试变更先登记精简TCR。禁止新建替代格式验收报告。

- 每项测试变更必须先在T195建立的记录中追加该批具体TCR；后续条目写“先TCR”是强制前置，不代表T195一份总表永久授权所有测试修改。
- T192–T194只读取证；T195仅记录具体TCR。T192先固化修改前B0；T203产出护栏建立后的当前B1；后续回归为B2，见quickstart §0.2及统一TCR §4。不能为了取得修改前运行证据而使用当前开发库。
- 每次进入技能先执行quickstart §0.1只读Feature路径核对；显式路径不是防写措施，禁止未经单独授权运行会持久化旧指针的常规helper。
- 文件均相对仓库根；contracts/等仅出现在S0规划任务时相对当前Feature；执行证据在当前会话汇报。未来新增文件明确在plan中定义，迁移revision由实施时实际head分配。
- [P]表示其列出的依赖完成后可与同层不触碰同文件的任务并行；不表示可跳过依赖。每项优先级见详情，US标签与spec对应。S0/S1为共享基础，其余按用户故事分组。
- 一项任务完成要求实现、对应聚焦验证及真实记录，不能因旧模块存在自动勾选。源码未变时不重复跑无关测试。

当前 MVP 的验收调整以 [plan §9.1](plan.md#mvp-acceptance) 为准：启动 SLO、跨用户 DPAPI 及额外首启重复为 deferred；固定 M0 原入口仍 not_run，可复用独立同构 Test 等价证据。保留48项及历史完成任务内容，不将调整等同历史测试通过。

## 阶段汇总

|阶段|任务|数量|优先级|入口依赖|主要门禁|
|---|---|---:|---|---|---|
|S0|T192–T195|4|P1|无|G0范围、TCR、资源计划|
|S1|T196–T203|8|P1|S0|G1隔离、来源、原子保存；安全基线|
|S2|T204–T215|12|P1|S1|G2/G3秘密与能力；G6快照/索引|
|S3|T216–T223|8|P1|S1后先E1；完整装配/向导汇合S2|G2权限、G4设置先启动/真实状态|
|S4|T224–T230|7|P2|S2/S3|PC维护、来源/费用/错误/旧业务入口|
|S5|T231–T239|9|P1为主，T237 P2|S2–S4|G5/G7双交付、全量质量、恢复|

## S0：审计与基线确认

- [X] T192 确认当前基线、未提交修改和审计证据 — `specs/001-local-settings-and-onboarding/analysis-report.md#b0-baseline`

  - 优先级/覆盖：P1；FR-033 NFR-008。
  - 依赖：无。
  - 输入/输出：输入显式Feature路径核对、当前HEAD/工作区和审计；在源码修改前输出B0证据清单（HEAD、差异/文件指纹、环境和已有回执来源）；不重新勾旧任务。
  - 测试覆盖：只读核对源码与回执，不运行潜在数据写测试。
  - 验收：Feature解析绝对路径准确、旧指针前后不变；B0注明哪些只是静态/历史证据，未跑记not_run，旧T001–T191不变。
  - 风险/回退：审计结论过期则补证据；不回滚用户改动。

- [X] T193 确认本批合同和旧规格替代边界 — `specs/001-local-settings-and-onboarding/plan.md`；`contracts/settings-api.md`；`specs/001-local-settings-and-onboarding/plan.md#future-roadmap`

  - 优先级/覆盖：P1；FR-009 FR-024 FR-028。
  - 依赖：T192。
  - 输入/输出：输入两项用户回答与D01–D08；输出本轮F01–F08与24项评审结论、实施条件和DTO确认；不得用旧自检代替独立评审。
  - 测试覆盖：文档一致性及宪章检查。
  - 验收：不混入AI Reviewer、无登录迁移、全React或Prefix Cache。
  - 风险/回退：重大合同变化停止依赖工作，保持原合同直到决策。

- [X] T194 [P] 确定隔离验收资源计划 — `specs/001-local-settings-and-onboarding/quickstart.md`；`docs/test-change-record-local-settings.md`

  - 优先级/覆盖：P1；FR-001 FR-002 FR-029 SC-004。
  - 依赖：T192。
  - 输入/输出：输入可用专属Test资源；输出DB/Redis/目录/端口/秘密来源计划，无凭据原文。
  - 测试覆盖：只读检查目标命名和权限责任，不清理资源。
  - 验收：Test不复用dev/demo库与文件，真实模型默认关闭。
  - 风险/回退：无专属资源只暂停集成验证，不使用业务库替代。

- [X] T195 建立每批具体TCR与断言保护记录 — `docs/test-change-record-local-settings.md`

  - 优先级/覆盖：P1；FR-030 FR-033。
  - 依赖：T193、T194。
  - 输入/输出：输入既有TCR框架；输出首批变更必要性/文件/保留断言/预期记录及B0/B1/B2取证方式，其后每批追加；只写TCR不改测试。
  - 测试覆盖：审核测试设计非镜像实现、无放宽断言。
  - 验收：任何测试源码变动前有对应具体TCR，未跑标not_run。
  - 风险/回退：合同未决不改该批测试；保留已存在断言。

## S1：配置与环境隔离基础（US1）

- [X] T196 定义环境身份和分层配置类型 — `backend/app/core/environment.py`；`backend/app/schemas/settings.py`

  - 优先级/覆盖：P1；FR-001 FR-003 FR-004。
  - 依赖：T195。
  - 输入/输出：输入环境矩阵；输出Bootstrap/Business/Provider schema、resource manifest与安全DTO。
  - 测试覆盖：tests/unit/settings/test_environment.py覆盖默认/显式Test/非法环境。
  - 验收：DEV_MODE不当环境身份，DTO不含secret原文。
  - 风险/回退：不识别身份则拒写，不自动接纳旧数据。

- [X] T197 [P] 实现统一非敏感配置解析与来源 — `backend/app/core/config.py`；`backend/app/services/settings/config_service.py`

  - 优先级/覆盖：P1；FR-003 FR-004 FR-005 FR-008 SC-005。
  - 依赖：T196。
  - 输入/输出：输入defaults/saved/显式dotenv/process-env；输出逐字段resolved/source、段级错误。
  - 测试覆盖：tests/unit/settings/test_sources.py验证优先级/非法覆盖/无隐式.env。
  - 验收：所有入口同优先级，非法高优先值不被低值掩盖。
  - 风险/回退：旧EXE优先差异显式记录，保留旧文件供导入。

- [X] T198 [P] 先定义隔离与清理行为验证 — `tests/unit/settings/test_cleanup_guard.py`；`tests/contract/test_environment_contract.py`

  - 优先级/覆盖：P1；FR-001 FR-002 FR-029 SC-004 SC-010。
  - 依赖：T196。
  - 输入/输出：输入TCR及哨兵资源计划；输出Fake清理和错误目标用例。
  - 测试覆盖：覆盖实际DB身份、路径越界、public/schema/Redis全清拒绝。
  - 验收：实现前行为缺口可复现，非Test哨兵断言固定。
  - 风险/回退：环境错误不算目标红灯，拒接真实开发库。

- [X] T199 接入Test目标护栏和资源所有权清理 — `tests/postgres_helpers.py`；`scripts/smoke_m0.ps1`；`tests/integration/test_backup_restore.py`；`backend/app/core/environment.py`

  - 真实资源增量（2026-10-10）：新`eduagent-test` PG/Redis容器、网络和专属卷归属已核验；Test实例ID `7694989589121413158` 与Development不同，runner非超级用户且无CREATEDB/CREATEROLE，维护身份非超级用户且具CREATEDB。runner不能连接维护库或建库，以Test身份登录Development PG亦被拒且未执行SQL；真实schema创建/owner/本run删除、Redis错误实例拒绝及本run key删除通过，Dev容器保持healthy。Test库Alembic到head，备份原集成12/12、Fake护栏31/31、Ruff与聚焦Mypy通过，备份run库/schema无残留。M0错误Compose文件副作用前拒绝；正确Test配置构建因依赖下载缓慢有界取消，三容器/ready/容器迁移及原M0集成未验收。保持`[ ]`，T202/T203不放行；资源详情见quickstart §0.3、统一TCR-003续批。
  - 优先级/覆盖：P1；FR-001 FR-002 FR-029 SC-004。
  - 依赖：T198。
  - 输入/输出：输入显式Test profile；输出经过连接核对的resource manifest和受限cleanup。
  - 测试覆盖：运行T198及既有M0隔离断言，模拟清理失败保留残留。
  - 验收：只删本run资源、测试角色不能写dev/demo、保留M0项目端口要求。
  - 风险/回退：验证失败中止写入，不flushall/down-v或扩大删除。

- [X] T200 [P] 定义配置原子持久与故障恢复用例 — `tests/unit/settings/test_config_persistence.py`

  - 优先级/覆盖：P1；FR-004 FR-005 FR-014 NFR-003。
  - 依赖：T196。
  - 输入/输出：输入TCR/版本状态机；输出损坏、未来版本、并发、磁盘失败、待重启用例。
  - 测试覆盖：pytest tmp_path与故障注入，先定义行为再实现。
  - 验收：各故障点旧active不被覆盖，恢复必须显式。
  - 风险/回退：不复制存储算法作oracle，不写真实配置根。

- [X] T201 实现修订仓库、manifest和激活原子性 — `backend/app/services/settings/config_service.py`；`backend/app/schemas/settings.py`

  - 优先级/覆盖：P1；FR-004 FR-005 FR-014 NFR-003 SC-003。
  - 依赖：T197、T200。
  - 输入/输出：输入校验后草稿；输出immutable revisions/active/pending及冲突响应。
  - 测试覆盖：T200红绿验证，含第二进程写锁与过期修订。
  - 验收：原子指针唯一事实源，保存/检测/激活明确区分。
  - 风险/回退：写失败保留旧active和候选证据，不自动回退伪成功。

- [X] T202 贯通缓存、日志、文件和评测输出的环境路径 — `backend/app/core/environment.py`；`backend/app/services/file_storage_service.py`；`backend/app/services/evaluation_service.py`；`scripts/run_demo.ps1`

  - 优先级/覆盖：P1；FR-001 FR-031 FR-032。
  - 依赖：T199、T201。
  - 输入/输出：输入各环境根与旧位置；输出所有受控路径及旧资源接纳预览。
  - 测试覆盖：tests/integration/test_environment_paths.py和现有文件/评测路径回归。
  - 验收：文件/缓存/temp/log/results均隔离，旧库不得自动配空新storage。
  - 风险/回退：接纳不移动/删除旧文件；冲突只给诊断。

- [X] T203 在护栏下建立当前可复现业务回归基线 — 当前 Codex 会话

  - 追溯边界：原始 B0/B1 机器产物保留；重建的基线不追认为修改前运行结果。历史资源身份不代替当前 Test 核验。

  - 优先级/覆盖：P1；FR-033 SC-004 SC-010。
  - 依赖：T199、T201、T202。
  - 输入/输出：输入T192的B0、T196–T202变更清单及专属Test资源；输出护栏后的当前B1（精确代码/测试/配置身份、命令、结果、skip/失败、哨兵和清理回执），后续B2引用该B1。
  - 测试覆盖：先聚焦后按现有要求pytest/mypy/ruff，真实模型关闭。
  - 验收：B1明确已含本批护栏/配置修改，不能称修改前基线；有可比B0运行证据才判既有/新增，否则标归因未定并列差异。可用安全隔离副本作定点比较，不回切/改写用户工作区。
  - 风险/回退：不能安全取得B0动态证据则保留not_run，不强跑旧危险helper；直接影响后续任务的未归因失败须先定位，无关既有问题单列。

## S2：凭据与Provider服务（US2）/业务保护（US5）

- [X] T204 [US2] 登记并建立凭据/保密合同用例 — `tests/unit/settings/test_credentials.py`；`tests/contract/test_settings_secrets.py`；`docs/test-change-record-local-settings.md`

  - 优先级/覆盖：P1；FR-006 FR-007 FR-008 FR-031 SC-005。
  - 依赖：T203。
  - 输入/输出：输入TCR与Credential生命周期；输出Fake存储、响应/异常扫描用例。
  - 测试覆盖：覆盖空/失效/损坏/替换失败/引用删除，零原文断言。
  - 验收：旧active引用保留，错误安全且区分缺失和不可解密。
  - 风险/回退：不使用真实用户Key作测试标记。

- [X] T205 [P] [US2] 实现Windows用户范围DPAPI凭据库 — `backend/app/services/settings/credential_store.py`；`backend/app/services/settings/windows_credentials.py`

  - 优先级/覆盖：P1；FR-006 FR-008 FR-031 NFR-007。
  - 依赖：T204。
  - 输入/输出：输入SecretWrite；输出ACL密文及opaque版本引用。
  - 测试覆盖：T204、ABI/LocalFree/错误路径单元；真实Windows另T234。
  - 验收：无LOCAL_MACHINE或明文fallback，DB/Redis/JWT同边界。
  - 风险/回退：解密失败reentry_required；不删旧密文或写env。

- [X] T206 [P] [US2] 实现Docker外部Secret只读解析 — `backend/app/services/settings/mounted_secrets.py`

  - 优先级/覆盖：P1；FR-007 FR-008 FR-031。
  - 依赖：T204。
  - 输入/输出：输入允许secret_id/版本映射；输出可用性和受控内存值。
  - 测试覆盖：T204与挂载缺失/轮换/无权限/任意路径拒绝合同。
  - 验收：写操作EXTERNALLY_MANAGED，旧版本不可静默替换。
  - 风险/回退：缺Secret阻止依赖任务，不在volume保存明文。

- [X] T207 [P] [US2] 定义Registry、能力和Probe合同测试 — `tests/contract/test_provider_registry.py`；`tests/unit/settings/test_probe.py`

  - 优先级/覆盖：P1；FR-009 FR-010 FR-011 FR-012 FR-013 FR-034 SC-003 SC-012。
  - 依赖：T203。
  - 输入/输出：输入TCR及8类错误/能力矩阵；输出Mock目标行为用例。
  - 测试覆盖：覆盖unknown、404非模型、auth_none、无隐式下载、费用确认、超时/取消/次数；候选10分钟过期拒激活，active跨过期限/普通重启/读取状态零自动请求。
  - 验收：不按OpenAI兼容名推能力，不因Reviewer调用模型。
  - 风险/回退：禁止真实模型默认启用，失败保留原active。

- [X] T208 [US2] 统一注册表并补通用文本Adapter和任务绑定 — `backend/app/services/settings/provider_registry.py`；`backend/app/ai/llm/openai_compatible.py`；`backend/app/ai/llm/factory.py`；`backend/app/ai/embedding/factory.py`；`backend/app/ai/retrieval/reranker.py`；`backend/app/ai/vision/provider.py`；`backend/app/ai/ingestion/ocr/factory.py`

  - 优先级/覆盖：P1；FR-009 FR-010 FR-011 FR-026。
  - 依赖：T205、T206、T207。
  - 输入/输出：输入profiles/bindings；输出真实工厂、能力Schema、默认/任务覆盖与规则Reviewer状态。
  - 测试覆盖：T207及既有Provider/JSON/Pydantic合同，验证LLM/Embedding枚举不分叉。
  - 验收：通用Adapter真实身份，Rerank别名不冒充/rerank，原抽象必需参数兼容。
  - 风险/回退：保留既有DeepSeek路径；不支持能力明确拒绝。

- [X] T209 [US2] 实现单次受控连接检测与安全错误 — `backend/app/services/settings/probe_service.py`；`backend/app/core/retry_policy.py`

  - 优先级/覆盖：P1；FR-012 FR-013 FR-034 NFR-002 NFR-004 NFR-007 SC-003 SC-012。
  - 依赖：T208。
  - 输入/输出：输入候选/能力/费用确认；输出异步ProbeResult与修订绑定。
  - 测试覆盖：T207，Mock记录0/1外部请求、30秒上限与取消释放。
  - 验收：没有自动重试/fallback，结果无SDK正文，过期不授权新候选激活；不撤销已有接纳，绝不创建定时付费重测。
  - 风险/回退：异常不改配置，取消可能已计费须如实显示。

- [X] T210 [US2] 完成配置与凭据引用事务及旧配置接纳服务 — `backend/app/services/settings/config_service.py`；`backend/app/services/settings/credential_store.py`；`backend/app/deployment/configuration_editor.py`

  - 优先级/覆盖：P1；FR-005 FR-006 FR-008 FR-014 FR-032 SC-003 SC-005。
  - 依赖：T201、T205、T206、T209。
  - 输入/输出：输入有效候选/Probe与显式导入预览；输出active/pending、持久activation_validation及引用生命周期；本任务建立激活校验服务，T220在G6后才接可写管理入口。
  - 测试覆盖：配置/凭据两阶段故障注入、IN_USE删除、旧env只读导入、未变绑定沿用接纳/变更需新Probe、pending跨重启不重测。
  - 验收：失败不覆盖旧密钥；新写只进安全库；旧明文事实可见。
  - 风险/回退：不清理旧.env；导入冲突保持未激活并给修复路径。

- [X] T211 [US5] 定义配置快照恢复与索引兼容验证 — `tests/unit/workflows/test_workflow_state.py`；`tests/integration/test_runtime_settings.py`；`tests/integration/test_embedding_identity.py`

  - 优先级/覆盖：P1；FR-015 FR-016 FR-017 FR-030 NFR-008 SC-006 SC-007。
  - 依赖：T203。
  - 输入/输出：输入TCR与旧/新版payload样本；输出跨进程恢复及旧unknown用例。
  - 测试覆盖：覆盖A运行/B激活、凭据丢失、实际model覆盖、同维不同模型、客观题/复核；旧索引证据缺失/冲突、预览过期、并发接纳与摄取互斥、身份及审计同事务、原向量零变化。
  - 验收：原轮次/分值/最终化断言保留，旧unknown不回填。
  - 风险/回退：真实PG只用Test，向量不重建。

- [X] T212 [US5] 实现不可变快照、客户端所有权与备份承载 — `backend/app/services/settings/runtime_snapshot.py`；`backend/app/ai/workflows/state.py`；`backend/app/services/workflow_checkpoint.py`；`backend/app/services/backup_restore_service.py`

  - 优先级/覆盖：P1；FR-014 FR-015 FR-016 FR-031 NFR-007。
  - 依赖：T210、T211。
  - 输入/输出：输入resolved配置；输出snapshot和新版可兼容checkpoint payload、非敏感备份清单。
  - 测试覆盖：T211状态往返/崩溃/取消释放/备份缺秘密用例。
  - 验收：旧运行持同snapshot且恢复不叠加新环境变量，新运行新配置；process_env秘密引用重启后明确不可用；没有第二workflow状态表。
  - 风险/回退：缺snapshot明确失败，保留原checkpoint，不用当前默认补造。

- [X] T213 [US5] 贯通所有实际AI消费者与来源记录 — `backend/app/api/workflow.py`；`backend/app/api/grading.py`；`backend/app/ai/workflows/grading_workflow.py`；`backend/app/ai/agents/question_agent.py`；`backend/app/services/grading/subjective_grader.py`；`backend/app/ai/retrieval/reranker.py`；`backend/app/services/diagnosis_service.py`；`backend/app/services/content_validation_service.py`；`backend/app/services/paper_import_service.py`；`backend/app/services/image_understanding_service.py`

  - 优先级/覆盖：P1；FR-010 FR-014 FR-015 FR-016 FR-030 NFR-008 SC-006。
  - 依赖：T212。
  - 输入/输出：输入snapshot；输出统一消费者绑定与实际请求/返回模型、Prompt来源。
  - 测试覆盖：T211及直接调用方RAG/Workflow/出题/诊断/导入回归。
  - 验收：不能中途get_settings取新active；规则Reviewer不新增LLM，客观题零调用。
  - 风险/回退：旧无snapshot运行冻结legacy基线，完成前拒影响绑定激活。

- [X] T214 [US5] 登记并执行索引语义兼容检查 — `backend/app/models/knowledge_base.py`；`backend/app/services/settings/index_identity.py`；`backend/app/ai/ingestion/service.py`；`backend/app/ai/retrieval/vector_search.py`；`migrations/versions/（实施时分配新增可空身份列迁移）`

  - 优先级/覆盖：P1；FR-017 SC-007。
  - 依赖：T208、T211。
  - 输入/输出：输入KB与Embedding身份；输出原子native登记、legacy_unknown读取、证据预览/确认领域服务和不兼容拒绝；data-model §7规定identity+adoption同一JSONB事务。T220/T228分别接API/UI。
  - 测试覆盖：T211新旧库、同维换模型、并发首次摄取；证据来源/必需字段、操作者事实、失联幂等读回与审计故障回滚；既有迁移/向量回归。
  - 验收：保留vector1024，未知不按当前配置补造；只有可核验证据+人工确认才legacy_adopted，审计完整且已有向量零改动。
  - 风险/回退：迁移只增可空列；不drop/改维度/自动重建，回退不删列。

- [X] T215 [US5] 验证模型配置变更不破坏业务闭环 — `tests/integration/test_runtime_settings.py`；当前 Codex 会话

  - 优先级/覆盖：P1；FR-015 FR-016 FR-017 FR-030 SC-006 SC-007。
  - 依赖：T213、T214。
  - 输入/输出：输入Test场景与A/B配置；输出重启恢复、旧payload、客观/主观/复核证据。
  - 测试覆盖：T211全场景及旧考试有效分值/审核/最终化聚焦回归。
  - 验收：进行中运行不混配置，待复核不进成绩，历史未知保持。
  - 风险/回退：失败禁发布新配置，保留原业务记录和错误证据。

## S3：Bootstrap与首次向导（US3）

- [X] T216 [US3] 先建立Bootstrap权限/健康合同用例 — `tests/contract/test_settings_api.py`；`tests/unit/settings/test_bootstrap.py`；`docs/test-change-record-local-settings.md`

  - 优先级/覆盖：P1；FR-018 FR-019 FR-020 FR-022 FR-023 FR-024 SC-002 SC-009。
  - 依赖：T203。
  - 输入/输出：输入TCR、无DB/Key/JWT场景；输出公开/管理/业务权限矩阵、E1与完整装配分别归属的用例；TCR先于测试，设计任务记录有效行为红灯，不能把尚未实现的完整场景算通过。
  - 测试覆盖：Mock依赖覆盖无token、跨源、错误Host、过期/重放/重新授权、ready503；settings-api §1.2的GET/HEAD无Origin可读但有Token、非法Origin拒绝、写请求缺Origin/跨站拒绝、exchange与Vite预检矩阵；启动前置→装配→恢复顺序及失败、Probe过期不撤销active（归T220/T223整体验证）。
  - 验收：静态可用不等于业务健康，旧业务JWT断言保留。
  - 风险/回退：测试只用合成一次码，不打印真实token。

- [X] T217 [US3] 实现独立设置授权与请求来源校验 — `backend/app/core/settings_security.py`；`backend/app/deployment/local_management.py`

  - 优先级/覆盖：P1；FR-023 FR-024 NFR-001 SC-009。
  - 依赖：T216。
  - 输入/输出：输入本机一次码；输出进程/环境绑定短期Setup token、可信操作者会话元数据及可重复本机领码协议；公开HTTP无领码通道。
  - 测试覆盖：T216来源方法矩阵；Windows loopback与Docker NAT边界Mock合同。
  - 验收：公开HTTP不能领管理权，Probe也鉴权，无通配CORS。
  - 风险/回退：过期/错误来源明确拒绝；不退回业务JWT或无认证写入。

- [X] T218 [US3] 拆分应用启动与业务配置强校验 — `backend/app/core/bootstrap.py`；`backend/app/core/app.py`；`backend/main.py`；`backend/app/core/config.py`

  - 优先级/覆盖：P1；FR-018 FR-022 FR-026。
  - 依赖：T201、T217。
  - 输入/输出：输入最小引导段；输出最小根应用、公开健康路由、静态资源挂载和设置授权；隔离业务模块的过早导入/强校验。业务装配协调接线归T220，不在本任务启用配置激活或历史恢复。
  - 测试覆盖：T216缺依赖导入/启动、原应用工厂和安全异常回归。
  - 验收：无假Key/假连接，缺配置设置仍可用，未装配业务503，真实/假模型均不用于填充启动必填项；完成只代表Bootstrap边界可用。
  - 风险/回退：保留业务入口路径；不在失败时执行恢复副作用。

- [X] T219 [US3] 实现分层就绪和核心依赖诊断 — `backend/app/services/settings/readiness.py`；`backend/app/core/app.py`；`backend/app/core/database.py`；`backend/app/core/redis.py`；`backend/app/api/settings.py`；`backend/app/schemas/settings.py`

  - 优先级/覆盖：P1；FR-019 FR-022 FR-026 FR-034 SC-002。
  - 依赖：T218。
  - 输入/输出：输入Business配置及实际依赖；输出Setup、Business Prerequisites、装配/恢复状态和基础诊断；接GET设置安全视图、GET/readiness、POST/diagnostics的授权API/DTO。AI段明确evaluated=false/not_evaluated；缺凭据后端的检查报告credential_backend_unavailable，不降级匿名连接。完整凭据解析/AI计算归T220。
  - 测试覆盖：T216迁移版本与表结构偏差、PG/Redis/目录故障、JWT引用缺失；真实资源仅在T199护栏下使用。
  - 验收：前置依赖通过不把未装配业务标ready；/ready仍503，诊断无AI/索引服务依赖、无隐式模型下载。
  - 风险/回退：依赖超时保持设置导航，错误安全不伪成功。

- [X] T220 [US3] 接完整设置API、初始化及业务装配协调 — `backend/app/api/settings.py`；`backend/app/schemas/settings.py`；`backend/app/services/settings/config_service.py`；`backend/app/core/bootstrap.py`；`backend/app/core/app.py`；`backend/app/services/settings/readiness.py`

  - 优先级/覆盖：P1；FR-017 FR-019 FR-020 FR-021 FR-026 FR-028 FR-032 SC-007。
  - 依赖：T219、T210、T215。
  - 输入/输出：输入草稿/目标预览/确认；输出完整settings-api（含索引身份GET/预览/确认）、向导进度及操作状态；接逐任务真实AI计算、前置检查→装配→原恢复一次的启动协调。依赖T215后才开放模型激活及业务入口，不把E1发布为完整业务。
  - 测试覆盖：T216 API字段/初始化目标变化409/重入/中断/秘密write-only；索引接纳鉴权/证据/并发/审计，未装配修复须重启、已装配短暂断连不重跑恢复。
  - 验收：一次预览只授权所示允许迁移；不生成账号或执行任意SQL；Business Ready无循环依赖，候选诊断不冒充effective；接纳不改向量。
  - 风险/回退：未知/破坏性迁移暂停，已有数据不删除；初始化失败不标完成。

- [X] T221 [US3] 接通Windows/源码本机入口与受控重启 — `backend/app/deployment/windows_launcher.py`；`backend/app/deployment/local_management.py`；`scripts/launch_windows.py`；`backend/app/deployment/configuration_editor.py`

  - 优先级/覆盖：P1；FR-018 FR-021 FR-027 FR-028。
  - 依赖：T219、T217。
  - 输入/输出：输入引导profile；输出先Setup服务/授权浏览器、持续控制窗口“打开设置/重新授权”、存续的自有worker匿名控制管道（注册确认后开页）及停止/重启所有权；不依赖完整初始化API。后续T220复用此控制合同。
  - 测试覆盖：部署unit+T216，管道交付/失效、码过期、刷新/关页重开/长期会话过期、重复双击/端口占用、busy重启及仅自有进程退出；不新增后台服务。
  - 验收：DB/模型预检不阻断HTTP；原Tk明文默认路径退役且无明文fallback。
  - 风险/回退：旧env仅显式导入；初始化中断保存真实状态，不杀外部服务。

- [X] T222 [US3] 交付最小React静态外壳与基础诊断（E1） — `frontend/package.json`；`frontend/src/app/router.tsx`；`frontend/src/api/client.ts`；`frontend/src/api/generated.ts`；`frontend/src/features/onboarding/OnboardingPage.tsx`

  - 优先级/覆盖：P1；FR-018 FR-023 FR-025 FR-026 NFR-005 NFR-006。
  - 依赖：T219、T221。
  - 输入/输出：输入T219的真实OpenAPI和授权合同；输出React+TS+Vite+AntD+Router、类型生成、构建/聚焦测试入口及无DB静态页面；显示授权恢复指引、环境/基础依赖诊断，完整向导交T223。
  - 测试覆盖：先TCR创建frontend/src/features/onboarding/OnboardingPage.test.tsx；外壳/鉴权/错误/重新授权、依赖诊断和无DB资源加载；类型检查+构建验证，未实现初始化/AI步骤显示未提供，不伪造成功。
  - 验收：不复制后端校验；一次码立即清fragment、秘密只在内存；不迁业务页；E1无配置激活/迁移/业务装配，不依赖T214和完整AI快照。
  - 风险/回退：资源丢失明确Setup失败；仅恢复服务端已有非敏感事实；本任务不宣称完整向导或双交付验收。

- [X] T223 [US3] 接通完整首次向导并验证中断恢复 — `frontend/src/features/onboarding/OnboardingPage.tsx`；`tests/integration/test_settings_lifecycle.py`；`frontend/tests/onboarding.spec.ts`；当前 Codex 会话

  - 优先级/覆盖：P1；FR-018 FR-019 FR-020 FR-021 FR-022 SC-001 SC-002 SC-009。
  - 依赖：T222、T220。
  - 输入/输出：输入空配置/故障/修复的独立资源；输出完整向导（目录/依赖/模型/检测/初始化/跳过AI/完成）、T216全部场景和浏览器+API回执；复用T222外壳，不重复生成工程。
  - 测试覆盖：无DB/Key/JWT、改配置重启、旧登录业务入口、授权过期恢复、恢复钩子每进程一次、前置通过但待重启、active Probe超过10分钟零自动付费调用。
  - 验收：用户无需手改.env，设置成功不冒充业务和AI成功。
  - 风险/回退：失败只保留本次副本和诊断，不切用户资源。

## S4：图形化设置中心（US4）

- [X] T224 [US4] 补齐设置UI契约、生成类型和测试入口 — `frontend/src/api/generated.ts`；`frontend/src/features/settings/SettingsPage.test.tsx`；`frontend/package.json`

  - 优先级/覆盖：P2；FR-025 FR-026 FR-033 NFR-005。
  - 依赖：T222、T220。
  - 输入/输出：输入OpenAPI与TCR；输出类型漂移门禁、组件行为测试与构建脚本。
  - 测试覆盖：契约字段变更应使类型/编译失败；错误/取消/费用/并发场景。
  - 验收：无手写第二份DTO语义，模型秘密不可入持久前端状态。
  - 风险/回退：类型生成不启动真实DB/模型，失败不手改生成文件骗过。

- [X] T225 [US4] 实现默认和任务模型配置页面 — `frontend/src/features/settings/ModelSettings.tsx`

  - 优先级/覆盖：P2；FR-009 FR-010 FR-011 FR-025 SC-003。
  - 依赖：T224、T208、T209。
  - 输入/输出：输入Registry与bindings；输出默认/覆盖、高级参数及能力不适用视图。
  - 测试覆盖：T224组件测试，Reviewer规则、独立Embedding、Vision能力和Rerank别名。
  - 验收：没有未实现选项，默认继承可解释，Probe与保存/激活分开。
  - 风险/回退：测试失败保留草稿与原active，不能自动换模型。

- [X] T226 [P] [US4] 实现凭据状态、替换与外部Secret指引 — `frontend/src/features/settings/CredentialSettings.tsx`

  - 优先级/覆盖：P2；FR-006 FR-007 FR-008 FR-031 SC-005。
  - 依赖：T224、T210。
  - 输入/输出：输入credential statuses；输出Windows录入/删除和Docker只读来源说明。
  - 测试覆盖：T224缺失/失效/不可解密/IN_USE、提交后清输入及无浏览器存储。
  - 验收：不读回秘密，业务备份不冒充密钥备份。
  - 风险/回退：无法保存不清原引用，外部管理不显示假可写按钮。

- [X] T227 [P] [US4] 实现依赖和逐任务诊断页面 — `frontend/src/features/diagnostics/DiagnosticsPage.tsx`

  - 优先级/覆盖：P2；FR-012 FR-013 FR-019 FR-020 FR-025 FR-034 SC-012。
  - 依赖：T224、T219。
  - 输入/输出：输入readiness/Probe；输出分类原因、费用确认、取消、检测时间和安全导出。
  - 测试覆盖：T224状态/费用/超时/unknown，键盘及错误可见。
  - 验收：不以全局绿灯替代任务状态，unknown不猜测为密码错误。
  - 风险/回退：错误只展示安全DTO，禁止原始SDK响应导出。

- [X] T228 [US4] 实现配置来源、并发维护和旧配置接纳UI — `frontend/src/features/settings/SettingsPage.tsx`；`frontend/src/features/settings/EnvironmentSettings.tsx`；`frontend/src/features/settings/IndexIdentitySettings.tsx`

  - 优先级/覆盖：P2；FR-003 FR-004 FR-005 FR-014 FR-017 FR-032 SC-007。
  - 依赖：T225、T226、T227。
  - 输入/输出：输入FieldSource/revisions/导入预览；输出覆盖说明、409冲突、待重启、显式恢复；索引兼容性页支持查看旧身份→填脱敏证据/定位→预览限制→明确确认→读回接纳审计。
  - 测试覆盖：T224双窗口/外部覆盖/损坏恢复/导入确认；历史身份未知、证据不足/冲突、会话过期重做预览、接纳失败保持旧值；不自动以当前配置填身份。
  - 验收：显示实际生效与保存差异，不搬旧storage或改.env。
  - 风险/回退：冲突要求重新加载/合并，不覆盖他人修订。

- [X] T229 [US4] 连通旧业务入口并收敛PC信息导航 — `frontend/src/app/router.tsx`；`backend/app/ui/gradio_app.py`

  - 优先级/覆盖：P2；FR-024 FR-025 NFR-005 NFR-006。
  - 依赖：T228。
  - 输入/输出：输入Setup/Business状态；输出进入/gradio入口与返回设置路径。
  - 测试覆盖：原Gradio登录/权限回归及两UI导航浏览器验证。
  - 验收：只增加可达导航，不复制七类业务页面，不共享长期秘密存储。
  - 风险/回退：业务未ready明确原因，旧UI保留可用退路。

- [X] T230 [US4] 完成PC设置中心可用性与秘密输入验收 — `frontend/tests/settings.spec.ts`；当前 Codex 会话

  - 优先级/覆盖：P2；FR-025 NFR-005 NFR-006 SC-011 SC-005。
  - 依赖：T229。
  - 输入/输出：输入两种PC尺寸和故障场景；输出截图/操作与网络证据。
  - 测试覆盖：1366x768/1920x1080、键盘、非颜色提示、无CDN、浏览器持久存储扫描。
  - 验收：全设置流程清晰可用，密钥离开后清空，未启用手机验收。
  - 风险/回退：问题局部修正后只重验受影响流程。

## S5：测试、打包与交付验收（US6）

- [X] T231 [US6] 执行完整自动化与静态质量门禁 — 当前 Codex 会话；`tests/contract/test_settings_api.py`；`frontend/package.json`

  - 优先级/覆盖：P1；FR-030 FR-033 SC-004 SC-009 SC-010。
  - 依赖：T215、T223、T230。
  - 输入/输出：输入实现后的隔离工作区；输出pytest/mypy/ruff、前端类型/测试/build结果。
  - 测试覆盖：既有必验+聚焦合同与集成；Fake外部请求计数。
  - 验收：真实失败/skip/warnings完整，非Test哨兵未变。
  - 证据：隔离分区及修复复测、前端三门禁、Ruff/Mypy 已执行；M0 not_run、原权限 skip 及各批原始结果见当前会话。
  - 风险/回退：失败先定位，不改断言/用旧包替代。

- [X] T232 [US6] 适配Docker静态资源、Secret和启动顺序 — `Dockerfile`；`docker-compose.yml`；`scripts/run_demo.ps1`

  - 优先级/覆盖：P1；FR-001 FR-007 FR-018 FR-019 FR-027。
  - 依赖：T223、T230。
  - 输入/输出：输入新构建资源与环境挂载；输出三容器独立项目交付路径。
  - 测试覆盖：Docker配置/构建聚焦，缺模型/DB启动HTTP的合同。
  - 验收：不再模型预检阻断HTTP，healthcheck仍业务ready，宿主loopback。
  - 证据：真实镜像构建及隔离容器 Bootstrap/静态页/业务503/只读挂载验证通过；合同修复复测与类型检查见当前会话。
  - 风险/回退：保留已有卷，失败不down-v；不增加第四常驻服务。

- [X] T233 [P] [US6] 适配Windowsonedir资源与构建回执 — `packaging/EduAgent.spec`；`packaging/requirements-windows.txt`；`scripts/build_exe.ps1`；`packaging/README.md`

  - 优先级/覆盖：P1；FR-027 NFR-006 NFR-008 SC-008。
  - 依赖：T223、T230。
  - 输入/输出：输入前端构建及后端；输出包内静态资源/DPAPI依赖与构建身份。
  - 测试覆盖：真实build、包静态路由与无源码目录运行准备。
  - 验收：不需Node运行时，配置/密文外置，版本仅追溯。
  - 证据：真实 onedir/追溯回执完成，无源码工作目录及无 Python/Node PATH 下设置资源可达；退出码缺陷转 T234 修复。
  - 风险/回退：失败保留前一包不覆盖用户配置；无自动明文回退。

- [X] T234 [US6] 完成真实Windows凭据与交付验收 — `benchmark/t189/windows_acceptance.py`；当前 Codex 会话；`docs/test-change-record-local-settings.md`
  - MVP 证据：真实 onedir 干净配置浏览器闭环、初始化/激活/受控重启、恢复及业务交互通过；匿名管道交接与非持久浏览器 Context，无授权码落盘，退出码0/端口释放；修复目标 Schema 误读 public 迁移版本。

  - 优先级/覆盖：P1；FR-006 FR-008 FR-027 FR-028 FR-031 SC-001 SC-005 SC-008。
  - 依赖：T231、T233。
  - 输入/输出：先TCR移除临时明文env生产；输入真实包/隔离凭据；输出首启/重启/DPAPI/退出回执。
  - 测试覆盖：同用户重启、密文损坏/丢Key重录；至少一次干净配置的真实 EXE 浏览器向导、初始化、激活、受控重启、业务交互及退出；保持旧计时起止点并单列 Setup/人工配置/依赖安装。跨用户动态测试及额外首启重复 deferred。
  - 验收：真实包证据不由源码代替；无临时明文秘密，外部安装耗时不隐藏。
  - 历史性能证据：真实浏览器后续启动 11.58–11.86 秒，<10 秒未达并已 deferred；跨用户 DPAPI deferred/not_run，同用户及本轮干净首启功能闭环有效。
  - 风险/回退：凭据失败不触碰原用户库；回退不导出密钥成env。

- [X] T235 [P] [US6] 完成DockerSecret、NAT与Bootstrap验收 — `tests/integration/test_docker_settings.py`；当前 Codex 会话

  - 优先级/覆盖：P1；FR-007 FR-018 FR-019 FR-023 FR-027 SC-002 SC-008 SC-009。
  - 依赖：T231、T232。
  - 输入/输出：先TCR；输入独立Compose资源；输出缺Secret/DB/正常迁移/退出/权限回执。
  - 测试覆盖：主机浏览器合法、同bridge无token拒绝、跨源、只读挂载轮换。
  - 验收：设置可达与ready503同时成立，Docker无GUI秘密写入。
  - 证据：独立真实 Compose 生命周期、NAT/权限、迁移、Secret 缺失恢复及新 ID 轮换通过；仅本轮容器/卷清理完成，详细结果见当前会话。
  - 风险/回退：不改宿主Secret原文，不清共享卷，清理只本次资源。

- [X] T236 [US6] 统一Benchmark默认调用、来源和输出脱敏 — `scripts/run_retrieval_benchmark.py`；`scripts/run_grading_benchmark.py`；`scripts/setup_benchmark_corpus.py`；`benchmark/t160/quality_runner.py`；`backend/app/services/evaluation_service.py`

  - 优先级/覆盖：P1；FR-008 FR-015 FR-029 NFR-008 SC-005 SC-010。
  - 依赖：T199、T213、T231。
  - 输入/输出：输入TCR及现有结果合同；输出默认Fake、显式real、实际模型/配置来源、安全结果。
  - 测试覆盖：入口contract与原历史结果读取回归，非正常退出cleanup测试。
  - 验收：默认真实调用0，unknown仍unknown，无完整可回显秘密的响应落盘。
  - 证据：默认 Fake、来源/历史读取、脱敏及取消/中断清理回归和真实 Test PG 集成通过；真实付费未调用，详细结果见当前会话。
  - 风险/回退：保留历史产物不重写，不默改模型或人工真值。

- [X] T237 [US6] 按授权执行最小真实Provider与业务保护验证 — 当前 Codex 会话；`benchmark/results/（环境内run目录）`

  - 优先级/覆盖：P2；FR-009 FR-012 FR-029 FR-030 SC-003 SC-006 SC-010。
  - 依赖：T215、T231、T236。
  - 输入/输出：输入显式付费授权/合成样本/选定Provider；输出实际Probe和最小业务记录。
  - 测试覆盖：仅已启用能力实调，检查结构化/模型来源/额度；无授权标not_run。
  - 验收：不推断未测能力，真实与Fake证据分离，无质量提升宣称。
  - 证据：独立 DPAPI Test 凭据、持久三请求预算及 Fake HTTP 安全回归通过；真实 DeepSeek text/json_object/生产 SubjectiveGrader 各一次成功，真实 PG 关键词检索与 RuntimeSnapshot/ProviderRegistry 生效，三次额度已耗尽。
  - 风险/回退：外部失败保留原分类，不无限重试或切服务。

- [X] T238 [US6] 完成一致备份、升级接纳与安全回退演练 — `tests/integration/test_backup_restore.py`；当前 Codex 会话

  - 优先级/覆盖：P1；FR-015 FR-016 FR-017 FR-031 FR-032 SC-006 SC-007 SC-008。
  - 依赖：T205、T212、T214、T215、T231、T233、T235、T236；保留实际 Windows 同用户凭据/包兼容证据，不再依赖 T234 的性能、跨用户矩阵或首启重复（plan §9.1）。
  - 输入/输出：先TCR；输入隔离旧/新业务副本；输出快照随业务、秘密另管、legacy接纳与回退限制证据。
  - 测试覆盖：缺文件/快照/旧凭据、旧payload、可空索引列兼容及接纳审计随KB恢复、非终态任务保护；接纳前后向量内容不变。
  - 验收：不伪造旧模型，不承诺旧包能读v3秘密，恢复不自动覆盖用户库。
  - 当前证据：真实 PG 迁移/非破坏性回退/重复升级、快照及索引审计恢复、原生 pg_restore 失败闭锁通过；两个兼容 v3 包升级/回退和显式配置恢复通过，数据与密文保持。修订后的独立验收满足。
  - 风险/回退：原副本保持，禁止自动downgrade/drop或明文密钥导出。

- [X] T239 [US6] 汇总交付证据和剩余限制并停止本批 — 当前 Codex 会话；`specs/001-local-settings-and-onboarding/analysis-report.md`

  - 优先级/覆盖：P1；FR-033 NFR-008 SC-001 SC-002 SC-003 SC-004 SC-005 SC-006 SC-007 SC-008 SC-009 SC-010 SC-011 SC-012。
  - 依赖：T234、T235、T237、T238。
  - 输入/输出：输入所有门禁和not_run项；输出可审查交付结果及当前限制。
  - 测试覆盖：核对G0–G7、需求映射、任务状态与实际回执一致。
  - 验收：按 plan §9.1 的本地 MVP 门禁复核双交付、完整首次设置、真实 Provider 最小业务、核心评分/复核和数据保护；未验证真实能力及 deferred 明确列出，不把构建等同验收或自动发布。
  - MVP 证据：真实 Windows 干净首启和 DeepSeek 三请求闭环通过；新版 Docker 生命周期通过，复用有效 B2/G6 分区基线并补本轮预算、Launcher、迁移/API 回归。Test 运行资源已清理；固定 M0 原入口 not_run，性能/跨用户强化仍 deferred，不代表企业级或自动发布。
  - 风险/回退：达标即停止；提交/发布/后续无登录需独立授权。

## 依赖、并行机会与独立验收

阶段编号用于分组，不要求把整个S2完成后才进入S3。保持48项及原ID，以显式依赖调度；较早编号T220可排在T221/T222之后执行，无环不要求数字严格顺序。

- 共享基础：T192→T193/T194→T195→T196；T197/T198/T200分工；T199/T201汇合T202/T203。T203先建立B1后再进入任一面向运行的分支。
- E1首次可见增量：T203→T216→T217→T218→T219→T221→T222（T201为T218已有前置）。输出授权后的最小React外壳和基础依赖诊断；没有T204–T215作为祖先。
- 完整AI/业务保护分支：T204→T205/T206，T207→T208→T209→T210→T212→T213；T211定义保护用例，T214在T208/T211后完成索引事务，T213/T214汇合T215。
- 完整向导汇合：T219/T210/T215→T220，T220/T222→T223。T220才接完整配置写入、初始化、业务装配及AI状态。T223证明完整无DB/Key→配置→重启的用户路径。
- 设置维护：T222/T220→T224，再T225/T226/T227（共享文件冲突时串行）→T228→T229→T230。T228补受控索引接纳UI，不以仅有后端服务算完成。
- 双交付：T231保留完整质量门禁；T232/T233可按文件并行，T234/T235必须真实包/容器验收。T236不共用打包输出目录；T237无付费授权仅记not_run，T238保护备份/历史，T239汇总。

[P]只代表满足依赖后文件边界可分工，不授权共享环境并写。测试设计任务的有效红灯与实现任务的聚焦通过分开记录；E1权限、来源、业务拒绝和静态资源测试必须通过。T216中完整装配/AI场景明确留给T220/T223，不能记作早期已通过。

## 实施策略与停止点

E1在T222可审查，完整向导在T223验收，整批交付仍要T231–T239；三者不可混称。提前的只是设置外壳和基础诊断，不删除T214/T215或Workflow/Embedding安全保护。先G1再真实资源，先G2设置权限子集再开放管理诊断；秘密API和模型激活仍需完整G2/G3/G6，E1不接受秘密写入、迁移或模型激活。
真实付费T237无授权记not_run及能力未验证，不阻塞独立Fake/本地工作；T239不得计为通过。达成本批即停止，不自动推进plan §11后续路线、不提交或发布。

<a id="requirement-traceability"></a>
## 需求追踪

| 要求 | 任务 |
|---|---|
| FR-001 | T194、T196、T198、T199、T202、T232 |
| FR-002 | T194、T198、T199 |
| FR-003 | T196、T197、T228 |
| FR-004 | T196、T197、T200、T201、T228 |
| FR-005 | T197、T200、T201、T210、T228 |
| FR-006 | T204、T205、T210、T226、T234 |
| FR-007 | T204、T206、T226、T232、T235 |
| FR-008 | T197、T204、T205、T206、T210、T226、T234、T236 |
| FR-009 | T193、T207、T208、T225、T237 |
| FR-010 | T207、T208、T213、T225 |
| FR-011 | T207、T208、T225 |
| FR-012 | T207、T209、T227、T237 |
| FR-013 | T207、T209、T227 |
| FR-014 | T200、T201、T210、T212、T213、T228 |
| FR-015 | T211、T212、T213、T215、T236、T238 |
| FR-016 | T211、T212、T213、T215、T238 |
| FR-017 | T211、T214、T215、T220、T228、T238 |
| FR-018 | T216、T218、T221、T222、T223、T232、T235 |
| FR-019 | T216、T219、T220、T223、T227、T232、T235 |
| FR-020 | T216、T220、T223、T227 |
| FR-021 | T220、T221、T223 |
| FR-022 | T216、T218、T219、T223 |
| FR-023 | T216、T217、T222、T235 |
| FR-024 | T193、T216、T217、T229 |
| FR-025 | T222、T224、T225、T227、T229、T230 |
| FR-026 | T208、T218、T219、T220、T222、T224 |
| FR-027 | T221、T232、T233、T234、T235 |
| FR-028 | T193、T220、T221、T234 |
| FR-029 | T194、T198、T199、T236、T237 |
| FR-030 | T195、T211、T213、T215、T231、T237 |
| FR-031 | T202、T204、T205、T206、T212、T226、T234、T238 |
| FR-032 | T202、T210、T220、T228、T238 |
| FR-033 | T192、T195、T203、T224、T231、T239 |
| FR-034 | T207、T209、T219、T227 |
| NFR-001 | T217 |
| NFR-002 | T209 |
| NFR-003 | T200、T201 |
| NFR-004 | T209 |
| NFR-005 | T222、T224、T229、T230 |
| NFR-006 | T222、T229、T230、T233 |
| NFR-007 | T205、T209、T212 |
| NFR-008 | T192、T211、T213、T233、T236、T239 |
| SC-001 | T223、T234、T239 |
| SC-002 | T216、T219、T223、T235、T239 |
| SC-003 | T201、T207、T209、T210、T225、T237、T239 |
| SC-004 | T194、T198、T199、T203、T231、T239 |
| SC-005 | T197、T204、T210、T226、T230、T234、T236、T239 |
| SC-006 | T211、T213、T215、T237、T238、T239 |
| SC-007 | T211、T214、T215、T220、T228、T238、T239 |
| SC-008 | T233、T234、T235、T238、T239 |
| SC-009 | T216、T217、T223、T231、T235、T239 |
| SC-010 | T198、T203、T231、T236、T237、T239 |
| SC-011 | T230、T239 |
| SC-012 | T207、T209、T227、T239 |
