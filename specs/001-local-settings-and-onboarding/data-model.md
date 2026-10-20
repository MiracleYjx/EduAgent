# Data Model: 配置、凭据与运行身份

本文件定义未来数据结构，当前未创建任何业务表或迁移。约束来源：[spec.md](spec.md)、[plan.md](plan.md)。

## 1. 环境与目录

EnvironmentProfile：environment=development|test|demo，environment_id（持久UUID），runtime_kind=windows|python|docker，config_root、storage_root、cache_root、log_root、temp_root、results_root、database_locator、redis_locator、source_policy、schema_version。

environment_id 与部署目录绑定，不由客户端随意改写。database_locator 不含密码，包含host/port/database/user/schema；redis_locator 不含密码。Test还需要运行时 ResourceManifest：run_id、实际连接身份、创建资源及owner、已解析绝对路径、清理状态；此manifest不是对任意目标的删除授权。

目录内 `config/manifest.json` 指向 active_revision 和 pending_restart_revision；`config/revisions/<id>.json` 不可变；`config/drafts/<id>.json` 可修订；`config/snapshots/<id>.json` 保存非敏感运行快照。Windows `credentials/` 保存受ACL约束的DPAPI密文；Docker只允许已挂载Secret引用。目录图为设计，不在本轮创建运行数据。

## 2. SettingsRevision

| 字段 | 类型/约束 |
|---|---|
| schema_version | 正整数，初版1；未来版本拒绝写入 |
| revision_id / parent_revision | UUID / 可空；用于并发、追溯，不是源码版本 |
| environment_id | 必须匹配当前启动环境 |
| business | DB/Redis非敏感定位、password_refs、storage_root、jwt_secret_ref、旧JWT非敏感参数 |
| providers | ProviderProfile ID → 不可变profile修订 |
| bindings | TaskBinding；必需任务和不适用状态明确 |
| policies | confidence_threshold、hybrid_weight、rerank上限/timeout等实际业务参数 |
| created_at / activated_at | UTC，可空activated_at；按真实动作写入 |
| activation_validation | 按绑定/能力记录不可变目标、credential_version、probe_id、tested_at、accepted_at；已接纳事实不因Probe期限过去失效 |
| source_metadata | 每字段default/saved/dotenv/process_env/mounted_config；不包含secret值 |
| status | draft/validated/active/pending_restart/superseded/invalid；active指针是唯一当前事实源 |

新schema升级：只读旧配置→创建新版本副本→结构校验→显式激活；失败原文件保持。不创建数据库依赖。空字符串不是沿用原密钥指令；秘密新增/删除是专门动作。

## 3. ProviderProfile / TaskBinding

ProviderProfile：profile_id、profile_revision、display_name、adapter_id、model_id、base_url（http/https；无userinfo/query/fragment秘密）、credential_ref（无鉴权本地服务可空）、auth_mode=api_key|none、capabilities、parameters、resource_directory（仅本地预设）、enabled。

capabilities 按实际 Adapter 支持的 text、json_object、json_schema、vision、embedding、llm_rerank、local_rerank、ocr 枚举；声明=declared、已探测=verified，不能混为一列。structured输出仍经本地Pydantic。高级参数按Adapter白名单及模型约束校验，未知参数拒绝；不接受任意SDK kwargs或网络header。

TaskBinding：task_kind、profile_id/revision或inherit_default、retrieval_mode、required_capabilities。question_generation/subjective_grading有可选覆盖；diagnosis/content_validation/paper_extraction明确继承default；reviewer固定rule_based（profile_ref为空，不默认LLM）；Vision/Embedding/OCR独立。

公开视图 CredentialStatus 与 FieldSource 单独DTO，绝不序列化内部秘密对象。

## 4. CredentialReference

字段：credential_id、version、environment_id、backend=dpapi|mounted_secret|process_env、external_secret_id（仅mounted）、purpose、created_at、status=absent|available|invalid|unavailable|revoked|reentry_required。

Windows：每个新秘密生成独立id/version，密文文件原子写，CurrentUser DPAPI负责完整性；状态metadata不得含原文。Docker：服务端配置允许的secret_id映射只读挂载，不接受API自带路径；版本化挂载不得原地轮换。检测 Secret 是否被原地替换所需完整性记录仅存在受权限保护的凭据元数据，绝不进入公开响应/普通配置/业务备份；不能用源码哈希替代这个生命周期校验。

生命周期：new candidate → available/unavailable → probe accepted/invalid → 被配置引用 → superseded但在途可用 → 无active/pending/非终态引用时可delete。外部撤销影响可用性但不会自动改绑定。旧版本删除返回409 IN_USE；已完成记录可保留已删除的引用作追溯。

密钥提交只写此边界；输入不得进入配置revision。DB/Redis密码/JWT与模型Key同规则。业务备份不包含该库或外部秘密。

## 5. ProbeResult

probe_id、candidate_revision、profile_revision、credential_version、capability、status、safe_error_code、tested_at、expires_at（成功后10分钟，仅用于候选激活）、elapsed_ms、external_requests（0或1）、usage（服务有返回才记录，未知null）、cancelled。

状态码见 contracts/settings-api。记录固定合成sample_id，不记录请求Authorization、原始响应体或秘密。config/credential/model/capability任一变更均使该结果不再能用于激活。能力判断失败在网络前返回，外部状态无法确认时用UNKNOWN_PROVIDER_ERROR，不将所有404猜成模型不存在。

ProbeResult记录单次检测；SettingsRevision.activation_validation记录已激活/待重启修订的接纳。过期不能接纳新目标，但不撤销已有接纳。目标/凭据变更重新检测，未变绑定可沿用接纳事实；保存无关字段或普通重启无需再次付费。就绪查询只读这些事实及本地依赖，不发模型请求。

## 6. RuntimeSnapshot

snapshot_id、environment_id、settings_revision、resolved_task_bindings、provider_identity、requested_model/returned_model（后者未知可空）、prompt_versions、schema_versions、retrieval_policy、confidence_policy、embedding_identity、credential_refs/versions、created_at、legacy_origin。

快照不可变且无secret原文。配置变更不会更新快照，恢复不重新merge当前配置源；process_env秘密引用仅原进程有效，重启后明确不可用。结果生产者记录本次实际requested/returned identity。没有返回版本不能用当前文档版本或猜值替代。

GradingWorkflowState新增snapshot_id；WORKFLOW_STATE_PAYLOAD_VERSION升级且解码支持旧版本。现有 WorkflowRun.checkpoint（JSON）与LangGraph持久检查点是原状态承载，不新增第二张workflow状态表。恢复在构建Provider前解析snapshot；不存在则报 SNAPSHOT_UNAVAILABLE。非敏感快照在一致业务备份内保留；凭据另行重录。

旧载荷缺snapshot保留legacy_unknown，不能回填伪造身份；接纳后的legacy baseline限制模型激活直到这些运行终结。运行原本缺失的Prompt/凭据不由当前默认填补。

## 7. EmbeddingIndexIdentity（最小业务增量）

未来在 KnowledgeBase 增加可空 `embedding_identity` JSONB，身份和审计在同一对象、同一事务更新，不新增泛化审计平台：
provider、adapter、endpoint_identity（不含秘密）、model_id、model_version（可空且明确unknown）、dimension=1024、semantic_parameters（归一化/查询及文档前缀等）、status=native|legacy_unknown|legacy_adopted、identity_revision、adoption（可空）。

历史NULL在读取DTO映射为legacy_unknown，不写库回填。原始历史事实仍为unknown；legacy_adopted表示接纳为兼容基线，不证明每条旧chunk的模型版本。model_version未知保持null，其余影响空间的必需字段证据缺失则不可接纳。

adoption包含：
- adoption_id、confirmation_id、accepted_at（服务端UTC）、operator {principal_kind:local_os|container_operator, principal_id, environment_id, instance_id, management_session_id}。主体由本机授权通道建立，不信任网页自由填写为认证身份；无真实OS身份时标container_operator，不猜用户名。会话ID不是Token/一次码。
- previous_status=legacy_unknown、previous_identity（原NULL/未知对象）、accepted_identity；旧事实与新确认分列。
- evidence[] {kind:deployment_record|ingestion_record|benchmark_record, source_reference, source_date, kb_scope, safe_excerpt, supported_fields}。保存来源定位、脱敏摘要及覆盖的身份字段；“当前设置如此”和新Probe不能证明历史。benchmark只有关联此KB/其摄取来源才可作为证据。
- verified_facts {stored_dimension, schema_head, evidence_field_consistency}、operator_attestation（人工已核对原材料及KB覆盖范围）、limitations（如未记录model_version）。不保存凭据、业务题目/答案或完整原始日志。

服务不自动下载URL或读取网页指定任意文件。操作者在本机查阅原材料，经UI提交脱敏摘要和准确定位；服务检查字段完整、相互一致、目标KB及实际vector维度，不能自动证明自由文本真实性。预览明确“人工证据接纳”及可核对/无法证明的部分；缺证据返回缺项，无“按当前配置填充”按钮。

AdoptionPreview为短期内存对象：preview_id、environment_id、kb_id、expected_identity_revision（NULL用legacy-null前置值）、evidence/accepted_identity、verified_facts、expires_at（10分钟）、management_session_id。不调用模型；只在同会话/实例确认，过期/重启重做预览。身份变化或新增摄取使预览失效。confirm带confirm:true，在KB行锁下重查身份/维度和摄取互斥，原子写identity+adoption。重复相同confirmation_id返回已保存adoption，不重复写；失联后GET事实，不自动重放。

新库首次成功摄取锁行，在同一事务写native身份与向量；并发不同模型不能混写。历史有向量库不能被首次摄取逻辑覆盖unknown。接纳与摄取/身份修改共用KB锁，忙返回409。native/legacy_adopted不能经该入口重新声明模型；纠错/换空间另行决策。查询和新增摄取按身份兼容检查，API Key轮换不改变语义。

已有未知向量保留既有配置读取模式并标未验证，禁止换模型/新增混合摄取；接纳后才允许兼容的新增摄取。迁移只加可空列；接纳不改chunk/vector、不调用模型或重建、不伪造生成时间。审计随原KB备份保留，保存失败事务回滚。

## 8. OnboardingProgress / ReadinessReport

OnboardingProgress：environment_id、current_step、completed_steps、draft_revision、last_error_code、updated_at、business_initialized_at。只表示用户流程，不证明依赖当前ready；重启重新检测。

ReadinessReport：
setup {ready,reason}；
business_prerequisites {ready,configuration_revision,checks: business_config/jwt_reference/postgres/vector/migration/schema/redis/storage}；
business {ready,assembly: not_mounted|mounted|failed,recovery: not_started|completed|failed,reason}；
ai {evaluated,tasks:{task_kind:{configured,capability,credential,activation_validation,last_probe:{status,tested_at,activation_expires_at},last_runtime_result,ready,reason,dependencies}}}；
restart_required、pending_revision、candidate_diagnostics（可空，明确不是effective就绪）。
早期E1尚未接AI计算时，ai返回evaluated=false/reason=not_evaluated，不能默认true或伪造具体Probe；T220接完整真实计算。
Business Ready由前置依赖+装配+恢复阶段投影；recovery=completed只说明原钩子启动阶段已完成，不说明每个历史Workflow成功。配置损坏但可诊断不妨碍Setup服务本身ready。单纯Probe超过10分钟不改变已激活绑定ready。
内部状态来自实时检测与配置事实，不持久保存单一“已安装=true”覆盖健康事实。

## 9. 事务与失败责任

文件配置与凭据无法构成同一DB事务：先安全写候选凭据/修订，最后原子切manifest；失败保留原active，孤立候选可显示和人工清理。业务任务在登记运行/保存首个checkpoint前持久快照；失败不开始模型调用。GC只删除无active/pending/非终态引用且用户明确要求清理的候选，不自动扫盘删除。数据库迁移、业务备份一致性仍复用原服务责任。
