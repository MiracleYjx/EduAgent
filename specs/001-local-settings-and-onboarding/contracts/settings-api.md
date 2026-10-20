# Settings API / DTO Contract

目标接口，不是现有实现。基础路径 `/api/settings`；FastAPI/Pydantic定义唯一OpenAPI来源，前端生成类型。旧/api业务路径和JWT/RBAC不改变。

## 1. 本机授权

公开：GET /health、GET /setup-ready、GET /ready以及React静态资源，仅返回安全概览，不含路径/端点/secret引用详情。GET设置页本身不授予权限。

POST /api/settings/session/exchange：{one_time_code} → {management_token,expires_at,environment_id,instance_id}。一次码32随机字节、5分钟、一用即废；只有本机启动器/受限Docker exec生成，公开HTTP不能申请。响应Cache-Control:no-store。浏览器立即清fragment，token只存内存。

其余Settings API均要求 `Authorization: Setup <token>`；令牌闲置10分钟/绝对30分钟过期，绑定environment/instance；重启撤销。操作始终检查精确Host；安全GET/HEAD允许缺Origin但仍须管理Token，Origin存在必须精确匹配；敏感操作必须有合法Origin，拒绝null/未知Origin及非JSON写请求，详见§1.2；不启用通配CORS。开发ViteOrigin需显式仅development允许。Windows peer+listener loopback；Docker经验证的主机loopback发布+token+来源，不能信任X-Forwarded-For。

该权限属于启动OS用户/宿主管理者，不包含账号管理、成绩和课程权限。秘密提交、Probe、诊断实际访问、初始化/重启同样受保护。限流：exchange每进程每分钟最多5次失败，超限429；这是小范围管理边界，不新增账号风控系统。

### 1.1 再次进入设置

Windows通过仍在运行的Launcher控制窗口“打开设置/重新授权”领取新一次码，管道登记确认后开浏览器；复用其自有worker控制通道，现有一次SERVE管道须由T221扩展，不能假定已支持。第二个EXE遇占用只提示返回原Launcher，不接管实例。无公开HTTP领码/续期权限接口。

| 场景 | 用户路径 | 服务端/浏览器规则 |
|---|---|---|
| 一次码过期、已消费或兑换失败 | 返回Launcher重新打开设置；Docker宿主重新执行受限管理命令 | 401 CODE_EXPIRED/CODE_USED；清fragment，不能重用旧码 |
| 刷新、关闭后重开设置页 | 页面显示需要重新授权，返回同一管理入口 | Token仅内存，丢失后GET静态页不自动获权 |
| 长期运行后重新进入、闲置10分钟或绝对30分钟 | Launcher重新授权，成功后GET已保存草稿/向导 | 401 SESSION_EXPIRED；清秘密输入，不自动重放Probe/初始化 |
| 浏览器关闭但Launcher仍运行 | 从任务栏打开Launcher，点击打开设置 | 不必重启业务；新发码作废此前未兑换码，已有会话独立到期 |
| Launcher退出或控制通道失效 | 按所有者停止规则退出并重新启动应用 | 新instance使旧码/token失效；不强杀忙任务或外部进程 |

注册新码的内部操作只能来自本机所有者控制通道；主体及instance/environment由该通道确立，exchange不能自报管理员身份。Docker同理从宿主受限exec进入，不从容器服务日志取码。非敏感已保存进度可恢复，未保存表单与秘密不保证恢复。

### 1.2 安全读取与敏感操作的来源规则（T193确认）

同源浏览器GET/HEAD可能不携带Origin，缺失不等于字面值 `Origin: null`，见[Fetch Standard §3.2](https://fetch.spec.whatwg.org/#origin-header)。下表为本项目策略，来源头不授予管理身份：

| 请求类别 | 授权 | 来源校验 |
|---|---|---|
| 公开概览/静态GET/HEAD | 仅原公开范围，无管理详情 | 精确Host及既定本机监听/发布边界；Origin若存在仍拒绝null/畸形/非允许来源 |
| Settings安全GET/HEAD | 必须有效Setup token，校验environment/instance和期限，不接受cookie或业务JWT替代 | 精确Host；Origin存在必须精确匹配scheme/host/port，缺失可接受 |
| POST/PATCH/PUT/DELETE管理操作，含诊断、Probe、凭据、初始化、重启 | Setup token；唯一exchange改验一次码 | 精确Host且必须合法允许Origin；缺失、null、未知、畸形或多值均403；继续要求JSON合同 |
| 开发跨源OPTIONS预检 | 不获取管理权限、不执行管理动作 | 精确Host/Origin、允许method/header后只返回CORS元数据，实际调用仍鉴权 |

安全GET/HEAD不得触发配置/凭据写入、Probe、初始化、重启或新建诊断任务。未支持HEAD可返回405；若支持，使用GET相同鉴权/来源规则且无正文。缺Origin仅对安全读取适用，不能延伸到exchange和敏感操作；不以Referer或Forwarded头补造写请求来源。

生产只允许实际本机服务origin；development可额外显式允许该次Vite origin；无通配CORS，不凭“同站点”放宽端口。非法Origin即使Token有效仍拒绝。管理Token不作为环境Cookie自动附带，跨源浏览器去掉Origin不构成豁免；非浏览器敏感操作客户端也须显式发送合法Origin，知道Origin不等于有权限。

未来T216/T217验证：有效Token/Host下GET/HEAD无Origin可读；无/过期Token、错误Host拒绝；读写提供null/非法Origin拒绝；写请求缺Origin/跨站拒绝；exchange同样拒绝；development精确Vite预检及实际调用、production拒绝开发来源。本轮未修改或执行测试。

## 2. Endpoint 表

| Method/Path | 输入 | 输出/行为 |
|---|---|---|
| GET / | 无 | SettingsView：active/pending revision、非敏感字段、FieldSource、CredentialStatus、restart_required |
| GET /providers | 无 | 实际Registry列表、能力/参数schema、任务适用性；不返回Key |
| POST /drafts | {base_revision,changes,acknowledge_override} | 201 DraftView；未知/秘密字段422，冲突409 |
| PATCH /drafts/{id} | {expected_revision,changes} | 更新草稿并产生新revision；原Probe失效 |
| POST /drafts/{id}/validate | {expected_revision} | ValidationReport；不调用付费模型 |
| POST /credentials | {purpose,secret_value} | Windows 201 {credential_ref,version,status}；Docker405 EXTERNALLY_MANAGED |
| POST /credentials/{id}/versions | {secret_value} | 新候选版本；不替换正在用的版本 |
| DELETE /credentials/{id}/versions/{v} | 无 | 204；仍被有效/待重启/非终态引用409 IN_USE；Docker405 |
| POST /drafts/{id}/credential-bindings | {profile_id,credential_ref,version} | 绑定安全引用；Docker只允许已配置secret_id |
| POST /probes | {draft_revision,profile_id,capability,allow_billable,timeout_seconds} | 202 ProbeView；外部检测须allow_billable=true，否则422 |
| GET /probes/{id} | 无 | ProbeResult，无原始SDK body |
| DELETE /probes/{id} | 无 | 202请求取消/200已结束；保留可能已计费提示，取消不是退款承诺 |
| POST /drafts/{id}/activate | {expected_active_revision,probe_ids,acknowledge_override} | 200 active 或202 pending_restart；能力/Probe/索引不满足409 |
| GET /readiness | 无 | ReadinessReport（Setup、业务前置、装配/恢复及Business、逐任务AI）；查询不触发模型请求 |
| POST /diagnostics | {checks:[postgres,redis,storage,migrations]} | 短时依赖诊断，禁止任意URL或命令 |
| GET /onboarding | 无 | OnboardingProgress与当前真实诊断 |
| PATCH /onboarding | {expected_revision,current_step,draft_revision} | 仅保存流程，不直接标依赖通过 |
| POST /initialization/preview | {draft_revision} | 非破坏性变更预览，目标identity/current_head/target_head及一次确认id |
| POST /initialization/apply | {confirmation_id,expected_revision} | 202 OperationView；仅执行刚预览的已有允许迁移，变更目标则409 |
| GET /operations/{id} | 无 | 迁移/重启步骤及安全错误；进程中断则interrupted，重新检查实际DB版本 |
| POST /restart | {expected_revision} | Windows受控停止自有worker；忙409；Python/Docker返回manual_restart_required，不操作宿主 |
| POST /diagnostics/export | {include:[configuration,readiness,versions]} | 脱敏下载，无业务内容或秘密 |
| POST /legacy-import/preview | {source_id} | 后端允许的本机选择来源的脱敏预览；不接受任意远端路径 |
| POST /legacy-import/apply | {preview_id,confirm:true} | 新安全修订；不改/删旧 .env，显示遗留明文风险提示 |
| GET /index-identities | 无 | 当前环境KB标识及非敏感索引身份摘要；不返回课程/题目/chunk正文 |
| GET /index-identities/{kb_id} | 无 | 旧身份、实际维度、修订、接纳证据/审计及兼容限制 |
| POST /index-identities/{kb_id}/adoption-preview | {expected_identity_revision,proposed_identity,evidence,operator_attestation} | 200 AdoptionPreview；缺证据422，已知身份/冲突409 |
| POST /index-identities/{kb_id}/adoption-confirm | {preview_id,expected_identity_revision,confirm:true} | 200 IndexIdentityView含adoption；仅接纳身份及同事务审计，不改向量 |

初始化不生成账号、不删除表、不执行任意SQL/命令；迁移必须匹配本批已审查许可集合。诊断不是自动修复。
全部产生状态的请求返回operation_id或revision；失联后先GET查事实，客户端不自动重放付费Probe/迁移。

## 3. DTO 边界

- SettingsView：environment、active_revision、pending_revision、business（脱敏locator）、profiles、bindings、sources、credential_statuses、warnings、restart_required。
- FieldSource：field、source_kind、overridden、effective_safe_value、editable、restart_required；密钥字段不提供value或掩码尾号。
- SecretWrite：secret_value为write-only SecretStr，错误不附input；禁止配置changes嵌入secret。
- ValidationReport：valid、errors[{field,code,message}]、affected_tasks、restart_required；无异常原文。
- ProbeView/Result：见data-model；status为queued/running/succeeded/failed/cancelled，result_code表达原因。
- IndexIdentityView/AdoptionPreview：字段和事务见data-model §7；认证主体由服务端记录，不能由请求体覆盖；无秘密或业务内容。
- ReadinessReport：见data-model §8；前置条件不含装配，Business含装配及恢复；候选诊断与effective状态分列。
- ErrorEnvelope：{code,message,stage,field?,retryable,correlation_id}。retryable是用户可重试提示，不触发自动重试。

## 4. Probe 结果码与HTTP

创建异步任务202；非法请求422、未经授权401、来源/权限403、资源不存在404、修订/并发409、并发限额429。供应商的HTTP码不原样当本服务权限码，放入安全结果分类。

| result_code | 判定 |
|---|---|
| KEY_MISSING | 目标auth_mode=api_key但无凭据；auth_mode=none不报此码 |
| KEY_INVALID | 供应商明确认证失败；403若是配额/权限无法确认则归UNKNOWN_PROVIDER_ERROR |
| SERVICE_UNREACHABLE | DNS/TLS/连接失败或TIMEOUT子原因 |
| MODEL_NOT_FOUND | 供应商结构化错误明确模型不存在；普通404路由错误不猜 |
| RATE_LIMITED | 明确429/限流，可给受控retry_after |
| CAPABILITY_UNSUPPORTED | Registry/远端明确不支持目标能力 |
| STRUCTURED_OUTPUT_INVALID | JSON解析或目标Pydantic校验失败；无正文回传 |
| SUCCESS | 对本次能力与候选的最小检测成功 |
| UNKNOWN_PROVIDER_ERROR | 无法可靠分类的上游异常，保留correlation_id与安全码 |
| CANCELLED | 主动取消；可能已产生费用 |
| RESOURCE_UNAVAILABLE | 本地模型/运行库缺失、不可加载；不自动下载 |
| EMBEDDING_INCOMPATIBLE | 实际维度或语义身份与目标索引不兼容 |

检测固定合成输入：LLM最小目标Schema、Embedding一条短句、LLM Rerank两条固定候选、Vision包内无用户信息小图、OCR固定小测试图。每次只检测一个能力；组合验证分别明确列出调用次数/费用，不能隐藏多次调用。

云网络请求只到用户配置的http/https服务，禁止URL嵌入凭据、禁止自动跨主机redirect携带Authorization、禁SDK/业务自动重试与fallback；本机无鉴权模型允许loopback HTTP，远端携密钥使用HTTPS。端点响应不是指令，不把响应字段当作本地文件路径。每环境最多1个付费probe在途、默认15s/硬上限30s；完成/取消后关闭客户端。

### 4.1 激活验证与持续使用

10分钟只用于新增/变更/启用的目标能力在activate时提交新鲜Probe；必须匹配candidate/profile/credential/capability。失败或过期返回409 PROBE_REQUIRED/PROBE_EXPIRED，不覆盖active。完全未变的已接纳绑定沿用activation_validation，无关字段保存不迫使再次付费。pending_restart接纳后重启不重新计费。
成功激活后保留接纳事实；last_probe到期只显示检测时间，不撤销active、快照或AI Ready。轮询、打开页面和普通重启均不得自动Probe。凭据明确无效/缺失或实际依赖失败按任务报告；临时调用网络错误不伪装成功，也不被转换为定期付费验证义务。

## 5. 健康状态响应

/health：200 {status:"ok",service:"backend"} 仅存活。
/setup-ready：静态资源、最小配置诊断及设置授权服务可用200，否则503；不要求操作者已获得Token或已填完整配置。
/ready：只有业务前置条件通过、业务路由/鉴权/Gradio装配且本进程原恢复启动阶段完成后才200；失败503 {status:"not_ready",service:"backend",failed_check,detail} 保留旧安全响应含义，增加检查项不把AI缺失混为PG故障。
详细AI状态仅在授权readiness API。后端未装配业务时，旧业务入口返回503 BUSINESS_NOT_READY，不用SPA HTML冒充API成功；装配后继续原鉴权，设置token不能通过。


## 6. 历史Embedding接纳操作

设置中心“索引兼容性”依次调用身份列表/详情→adoption-preview→确认→详情读回。Setup权限只授予非敏感索引维护元数据；不借此读取课程/答案或调用业务API。该入口依赖目标PG/schema可用（否则503 DEPENDENCY_UNAVAILABLE），不要求Business Ready，避免修复入口依赖业务装配。

操作者先看旧身份unknown与实际维度，再从原部署、摄取或可关联该KB的评测记录提供证据。UI要求来源定位、时间、KB覆盖范围、脱敏摘录及各身份字段依据，人工确认已核对原材料；服务不自动读取任意文件/远端URL，不接受当前配置或本次Probe代替历史证据。model_version原来没有则明确unknown，其余必需语义字段不足返回422 EVIDENCE_INSUFFICIENT。

预览显示原unknown、拟接纳身份、服务实际核对的维度/字段一致性、仍未知的事实及“不会重建/修改已有向量”。相冲突证据返回422 EVIDENCE_CONFLICT；维度不符409 EMBEDDING_INCOMPATIBLE。预览10分钟内仅同会话/实例可confirm；身份变化、摄取冲突返回409 IDENTITY_CONFLICT/INDEX_BUSY，过期返回409 PREVIEW_EXPIRED。会话过期先重新授权再重新预览。

confirm在同一KB行锁事务中保存legacy_adopted和data-model §7审计；写失败保留legacy_unknown，不许身份已改而审计缺失。重复同confirmation_id读取已保存结果；响应丢失先GET详情核对。前后chunk/vector保持不变；无自动下载模型、Probe、重建或删除。T214负责领域事务，T220负责这些API，T228负责页面，T211/T238证明失败/并发/备份行为。

## 7. T223 首次向导恢复与最小接线增量

本节复用既有端点与状态承载，不新增 endpoint、Workflow 状态或浏览器持久秘密存储。

- `GET /onboarding` 的 `OnboardingReport` 在 `progress/readiness` 之外增加 `draft: DraftView|null`、`probes: ProbeView[]`、`initialization: OperationView|null`。它们只投影 `progress.draft_revision` 关联的已保存草稿、当前进程已有检测和最近持久化初始化操作；没有关联/记录时分别为 null/空数组/null。GET 不创建新草稿、不开凭据库、不恢复执行、不发起 Probe；重新授权后可恢复非敏感表单与已有操作事实，秘密输入仍须重新录入。未接纳 Probe 跨进程丢失，不自动重测；操作记录跨实例的 queued/running 显示 interrupted。
- `PATCH /drafts/{id}` 与凭据绑定返回新的草稿 UUID，旧 Probe 不适用于新 candidate；PATCH/validate 的 `expected_revision` 为该草稿 `parent_revision`（当前 manifest head，取 pending 或 active），不是草稿自身 UUID。`POST /drafts` 的 changes 从默认值构造候选而非隐式读取 base 的值；更新已有候选使用 PATCH。激活响应的 `revision_id` 是发布修订 UUID，可能不同于草稿 UUID。`ActivationView` 明确列入实际 OpenAPI 的 200/202 响应。
- `business.database_locator` 对外统一输出 `schema`；输入兼容 `schema` 与原 Python 字段名 `schema_name`，支持真实 HTTP DTO 往返，其他未知字段继续拒绝。设置路由的非法秘密/额外字段/非法 JSON 校验失败返回安全 422 错误体，不含原始 input、秘密或 JSON 正文。
- `SettingsView.credential_statuses` 可同时列出服务端已配置的 mounted catalog 引用，`CredentialStatusView` 增加非敏感可空 `external_secret_id`，供客户端构造完整 `CredentialReference`。响应 `credential_ref` 对应引用中的 `credential_id`。首次 GET 尚无已打开后端时只显示保守 unavailable，不读取 Secret 或写入完整性登记；选择后由显式凭据/初始化/Probe/激活操作按可信 catalog 核验。mounted 后端的既有 warnings 字段包含 `EXTERNALLY_MANAGED`，即使 catalog 为空也不显示明文录入/生成按钮；空 catalog 仅提示宿主准备挂载。
- `POST /probes` 先完成本地前置检查并记录 running，再以 202 返回可轮询/取消的 ID，不等待网络检测终态。查询只读已有记录。`DELETE /probes/{id}` 在 running 时返回 202，终态返回 200；`may_have_been_billed` 始终保留，取消不是退款。每个能力分别明确确认；默认 LLM 的 text 与 json_object 是两个独立能力，不以一次按钮隐藏两次调用。显式 POST 可以按需打开后端，GET 查询不得为此首次登记凭据。
- “跳过 AI”可在 changes 中显式保存 `bindings.default_llm={task_kind:"default_llm",profile_id:null,profile_revision:null,inherit_default:false,required_capabilities:[]}`，清除深合并候选中的旧默认绑定；该形状表示未配置，不是新增 Provider/能力或成功检测。业务配置/JWT 仍须通过原门禁，AI readiness 继续如实为 not_configured。
- 新应用启动仅在未对外挂载阶段处理 pending：按该候选自身值解析本地 schema/凭据及持久 activation_validation，经前置依赖检查与工厂装配成功后原子提升已有 pending 为 active；不重新调用 Probe，也不把过期检测当作新授权。失败不消费 pending，普通 GET/诊断及当前已挂载实例不会热切换。首次激活若原进程尚未装配业务，即使响应 active，也须重启后才可能 Business Ready；恢复阶段未完成不能宣称 ready。
- 初始化预览 `allowed=false`、缺少可确定迁移 head、目标身份/当前版本/目标 head 变化或过期均不能启动执行。持久操作文件在既有承载中关联草稿与 confirmation ID，同一确认的重复/并发 apply 返回原 operation，不再次迁移；其他初始化在已有操作执行中返回 409 `INITIALIZATION_BUSY`。中断后先 GET 操作并重新预览实际版本，不自动重放 apply。初始化成功不自动激活草稿、不生成账号，不接受任意 SQL/命令。


## 8. 设置维护视图与恢复接线

- `SettingsView.values` 保留已保存 active（无 active 时为 pending）的非敏感值；`pending_values` 独立显示待重启值。`effective_values` 使用现有 resolver 从当前进程的非敏感覆盖名计算；无效覆盖返回 null 与 `effective_errors`，不回落旧值。`sources` 为本次解析来源，不以保存时元数据冒充当前来源。它们均不是业务进程已热加载的证明。
- `GET /recovery` 只读列出所属环境的修订、有效性和非敏感值；损坏 manifest 的 `manifest_fingerprint` 仅作本次恢复并发控制，缺失为 `MISSING`，不作为组件版本门禁。
- `POST /recovery/restore` 输入 `revision_id/expected_revision/expected_manifest_fingerprint?/confirm`。明确确认后复用 ConfigService 的凭据、业务、非终态运行与恢复校验；只设置 pending_restart，保留原始损坏证据，不热切当前业务或重放 Probe。
- `GET /legacy-import/sources` 仅返回受信来源名称，不读取旧文件；预览与 apply 复用同一运行时 importer，来源集合变化使旧预览不可用。确认 apply 才按现有安全后端打开凭据库，源文件保持不变。
- 索引接纳预览由设置运行时持有，数据库会话仍按请求释放；确认继续核验管理会话、期限、身份修订、证据和向量指纹，原子保存接纳与审计。
