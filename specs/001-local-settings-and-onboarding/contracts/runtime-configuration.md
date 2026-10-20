# Runtime Configuration Contract

## 1. 唯一来源与解析

ConfigService.resolve(environment, revision?) → ResolvedConfiguration；每字段来源由服务端生产。非敏感优先级：defaults < saved/mounted-config < explicitly-selected nonsecret-dotenv < process-env。秘密解析按CredentialStore与显式development/test进程覆盖；不在普通配置合并明文。

任何覆盖非法都返回具体配置段不可用，不偷用较低优先级。环境身份不能由网页修改；旧EXE文件覆盖环境规则在v3接纳后退役，导入预览显示行为差异。测试禁止隐式读开发 .env。

无鉴权 `openai_compatible` Profile 的既有 `auth_mode=none` 必须随内部 AppSettings 投影传到 Adapter，禁止伪造 Key 以穿过旧校验。默认仍为 `api_key`，缺 Key 和 DeepSeek 的 `none` 继续拒绝；此投影不新增持久状态或设置端点。

## 2. ConfigService 行为

create_draft / validate / activate / restore_revision 统一预期修订检查。保存不等于生效，检测不等于激活。新增/变更/启用的绑定激活须引用相同候选/凭据/能力且成功后10分钟内的Probe；未变绑定沿用activation_validation，无关可选任务不要求Probe。接纳事实在active/pending_restart中保留，不因原Probe过期撤销AI Ready；查询/重启不得自动付费检测，见settings-api §4.1。
错误返回安全阶段、旧有效修订和修复建议，不能把旧配置回退作为成功响应。已保存草稿可以不完整；active配置按可用业务段和enabled绑定判定。

文件层仅写环境内受控路径；原子manifest为唯一有效指针。两个进程争用同环境配置根时第二个拒绝写服务并提示原实例；不能靠进程内锁声称跨进程安全。

## 3. ProviderRegistry 与 CredentialStore

Registry entry：adapter_id、factory、config_schema、supported_capabilities、probe_factory、lifecycle。只注册真实实现。DeepSeek和openai_compatible分开真实身份；Embedding不提供未实现deepseek项；Rerank openai_compatible历史别名显示为LLM重排，不能伪装/rerank协议。

CredentialStore.get(ref,version)仅供受控Provider/数据库客户端构造；公共API只能status。Docker put/delete明确EXTERNALLY_MANAGED；Windows缺DPAPI返回不可用而非plaintext fallback。SDK原始异常在Provider/Probe边界转为安全DTO，原error类型/分类保留，秘密正文不持久。

client acquire(snapshot_id,binding) / release：每运行或Probe拥有客户端；可复用同snapshot实例但不能跨修订重配。关闭/取消时释放；Embedding本地模型缓存仅由资源管理者在重启或无引用后释放，不无限堆多个模型。

## 4. RuntimeSnapshot 与消费者

应用服务在第一次AI调用前解析全部实际任务依赖并创建快照，任务全过程传递相同snapshot；QuestionAgent、SubjectiveGrader、Rerank、Vision、Embedding、诊断、语义核验、OCR导入等不得中途读取新active。
Reviewer按原规则执行，客观题评分无模型依赖。即使AI未配，纯客观流程仍遵循原最终化约束；诊断是独立AI能力，不能使已完成的规则评分伪装失败或生成假诊断。

Workflow恢复使用原snapshot+原Prompt/Schema版本，不能拿当前默认重新解释旧结果，也不能重新叠加当前process-env、配置文件或bindings。process_env秘密引用绑定原进程实例；重启后该引用不可用，需要可恢复运行应预先使用DPAPI或版本化挂载Secret。缺少旧组件返回SNAPSHOT_DEPENDENCY_UNAVAILABLE，不执行替换模型；待人工复核队列仍可查看。旧payload按plan §6兼容，新增版本读写往返必须覆盖。

一致备份在原维护窗口内从非终态 Workflow 的既有 checkpoint 读取快照引用，并从同环境只读配置服务复制原快照；缺失不得发布完整备份。隔离恢复将原字节复制到与目标 storage 匹配的环境 `config/snapshots` 全新目录，已有目录拒绝覆盖，环境身份/模型/凭据引用不重写。恢复仍保持停写，verified 仅证明数据/字节完整；凭据未复制，重新诊断、依赖提供及激活是独立步骤，旧包不保证读取 v3 状态。

数据库/文件根变更仅待重启，不能让同一运行写到另一个数据库或新空storage。重启前不强杀在途任务；暂停运行可持久恢复。

## 5. 索引兼容合同

KnowledgeBase是索引语义身份责任方；先比较dimension和provider/model/version/影响语义的参数，再执行向量查询/摄取。URI中无秘密的endpoint identity可参与比较，API Key轮换不改变向量语义。
本批vector仍1024；即使新模型输出1024也不能自动等同旧空间。native首次登记与摄取同事务，legacy_unknown不回填新默认。显式证据接纳产生legacy_adopted而非伪造native。操作入口为settings-api §6，数据与审计同data-model §7；Setup授权读取非敏感KB身份并预览/确认，在KB锁与同事务中保存旧unknown、证据、服务核验事实、操作者确认和接纳身份。当前配置/新Probe不构成历史证据；冲突/审计写失败不改变身份或向量。
不提供自动重建、删除chunk或改列维度API；若任务必须换空间，独立索引重建计划获批后再实施。

## 6. 隔离和清理

Test必须有显式profile和ResourceManifest；预检同时核对实际数据库/user/schema、文件root、Redis实例和environment_id。创建和清理仅本次资源，失败同样执行有界清理；失败清理报告残留，不扩大范围删全库/卷。
普通回归FakeProvider默认不能解析真实secret/外网端点；真实检测/Benchmark需显式real标记及授权记录，不因环境里有Key自动启用。
ENV名、schema名、哈希本身均不是授权证明；资源所有权来自创建记录和目标检查。

### 6.1 T202运行路径接线与当前验收边界

- `EnvironmentProfile`在同一环境根内固定config/storage/cache/logs/temp/results/credentials。解析本身不创建目录；操作边界复核规范绝对路径、跨根输入和后来出现的重解析点。Test只能来自显式T199资源回执，不回落Development；环境名/确定性旧身份不能授权数据库或卷。
- 未接纳Development仍引用原源码storage或冻结包旧用户storage，不搬迁、不覆盖旧配置/文件。已有受管相对定位且在目标storage完全无法核对时，新写入在建目录前返回`FILE_STORAGE_ADOPTION_REQUIRED`，不能将旧库自动配空新storage。未知历史的精确定位/原显式迁移合同保留；非Development新Profile不得借绝对旧定位读取外部文件。旧资源预览不接纳、不移动、不删除，只返回确认需求和冲突。
- 源进程在应用/Gradio导入之前准备环境cache/logs/temp，设置HF、SentenceTransformers、Torch/XDG、Gradio与进程临时目录；Windows实际启动日志写环境logs/application.log。独立legacy `check_storage` helper保留显式root/logs兼容，但启动main不双写旧日志。
- Evaluation读取和三个直接结果生产者的默认值在调用时解析环境results，不在导入时固定共享benchmark/results；原授权、脱敏、unknown、质量证据和JSON/CSV格式不变。历史路径常量仅供显式调用；已选环境/Profile不能通过output-dir/results-dir越界，run_id在目录/输入/Provider副作用前校验。三个写入器还须在任何输出前核对JSON及CSV实际目标的规范路径，拒绝文件级重解析目标；普通既有文件的原覆盖/追加规则不变。旧结果不迁移、不删除。
- Demo保留零参数、`-EnvFile`、`-WaitTimeoutSeconds`，默认只选择`%LOCALAPPDATA%/EduAgent/demo/config/demo.env`；缺文件安全拒绝，不读取仓库/CWD的Development配置，不复制Key。独立`eduagent-demo`与内部创建回执/非敏感override用于实际ID/时间/标签/挂载/网络及PG/Redis/storage身份核对，未知旧资源拒绝而不接管；端口来自解析配置，不锁定某组建议值。
- Demo保留独立PG/Redis健康→一次性只读PG/Redis/Storage身份探针→同配置迁移并核对head→创建且核验Backend的顺序。探针和迁移只解析基础依赖，不要求模型或JWT。Backend先等待真实 `/setup-ready`（含静态资源），随后仅在 `/ready` 为200时Seed；设置可用而业务503时保留实例与创建回执，提示完成设置和受控重启。Dockerfile及普通Compose直接启动Bootstrap，不以基础服务健康或模型配置作为HTTP前置。原恢复函数与 Test 实例/角色/库/schema/Redis/卷身份门禁保持；当前 MVP 的独立同构环境及固定 M0 入口边界见 [plan §9.1](../plan.md#mvp-acceptance)，不依赖历史容器名称授权。
- 选择后的有效Compose模型只保存在内存，通过stdin及空环境输入传给后续Compose，不重读原env/Compose，不把秘密放入argv、日志或内部回执；原配置的字面美元符号必须保留。镜像经显式构建/解析后固定实际ID，后续禁止隐式build/pull。探针、迁移和实际Backend的完整业务env、镜像、工作目录/命令及实际卷/网络目标逐一核验，不以同文件名或哈希替代资源授权。临时阶段覆盖Dockerfile业务CMD，文件系统和Storage只读、数据库观察事务只读；迁移在同一镜像内重新查身份后才执行Alembic并复查head，不导入业务应用。临时阶段禁止自动重启。
- 创建容器不等于启动：一次性容器及Backend均在执行入口前核对实际inspect；up返回0不代替真实Setup/Business HTTP状态。业务healthcheck仍只查 `/ready`；Backend处于starting/unhealthy但设置可达时不误报业务成功，也不执行Seed。已运行、到head且配置一致的实例复用，未业务就绪保持设置入口；运行中需升级/变更仍拒绝并提示操作者受控停止。明确停止的已登记Backend可在身份/head核验后按精确ID非强制移除并重建，不删卷或业务文件。仅成功且已核验的临时精确ID可清理；未知、失败及不完整回执保留，schema2回执缺身份/镜像/资源字段时不授权复用。

### 6.2 Docker 本机授权通道

Docker 入口将当前应用的 SetupSecurityService 接到环境 temp 下私有 Unix socket（目录0700、socket0600），Linux peer UID 必须与服务一致。受限 exec 只接受固定 open_settings/reauthorize 和精确回环 URL，复用同一授权事实源；不提供 HTTP 发码端点或第四常驻服务。返回给 exec 操作者的一次性 fragment URL 不进入服务日志，停止时释放线程和自有 socket。宿主发布端口由操作者明确传入，Host/Origin/Token 门禁仍由原设置安全服务负责。

## 7. 历史合同替代关系

| 旧约束 | 本批适用 | 退役条件 |
|---|---|---|
| JWT/Admin/User | 全部旧业务保留；Bootstrap独立设置权限 | 后续StudentProfile与数据归属迁移单独批准、回归/恢复通过 |
| Gradio | 旧业务入口保留；React只做设置 | React逐业务等价验收后逐页退役 |
| 配置缺失禁止业务ready | 继续有效；允许Setup先启动 | 不退役，不将其降格为健康成功 |
| EXE明文.env与文件优先 | 显式接纳后统一优先级/DPAPI；旧文件不自动修改 | 用户确认接纳且新凭据与包验证通过；清理旧明文另由操作者明确执行 |
| Docker云Embedding预检阻断HTTP | 变为逐AI任务诊断；核心业务ready严格 | 新Bootstrap双交付验收通过 |
| Benchmark字段 | 保留历史读取格式，新增可选snapshot/实际模型字段 | 无需重写旧记录或做版本相等门禁 |
| 旧迁移/测试 | 原断言保留；新增变化先TCR | 仅明确改变的配置启动合同更新对应断言，不放宽评分/授权 |
