# Quickstart：安全路径、资源与验证操作

接口与验收场景见 [contracts/settings-api.md](contracts/settings-api.md)；当前任务进度以 tasks.md 为准，实际执行结果仅在 Codex 会话汇报。

## 0. 准备与停止条件

先完成每批具体TCR，使用专属Test数据库/角色、Redis实例、文件根和FakeProvider；任何连接指向development/demo立即停止，不能为跑通验证改用用户库。保留两套非Test哨兵。DB/Redis安装、模型下载和迁移不能在未审查目标时自动执行。

记录：代码/包标识、环境profile、实际连接身份、数据/Prompt/模型版本、输入来源、命令、UTC时间、退出码、失败/skip、清理回执；秘密不进记录。实际包保留 docs/evaluation.md 的计时起止点，当前 MVP 次数与非阻断性能目标以 [plan §9.1](plan.md#mvp-acceptance) 为准；人工输入/服务安装另计。

### 0.1 每次进入SpecKit前的活动路径确认（只读）

安装版本为1.0.3，旧.specify/feature.json仍指向.specify。仅设置SPECIFY_FEATURE_DIRECTORY并不保证不写指针：common.ps1中Get-FeaturePathsEnv默认持久化；setup-plan/setup-tasks及不带PathsOnly的check-prerequisites会调用它。本轮不得运行这些会写状态的helper，也不得覆盖旧指针。

在仓库根执行下列只读预检，记录FEATURE_DIR/三个主文档/旧指针前后指纹；失败立即停止依赖该路径的技能。指纹只用于证明本轮未改指针，不作为产品运行版本门禁。

```powershell
$reviewRoot = (Resolve-Path -LiteralPath '.').Path
$expectedFeature = (Resolve-Path -LiteralPath (Join-Path $reviewRoot 'specs/001-local-settings-and-onboarding')).Path
$pointerPath = Join-Path $reviewRoot '.specify/feature.json'
$pointerBefore = (Get-FileHash -LiteralPath $pointerPath -Algorithm SHA256).Hash
$priorInit = $env:SPECIFY_INIT_DIR
$priorFeature = $env:SPECIFY_FEATURE_DIRECTORY
try {
    $env:SPECIFY_INIT_DIR = $reviewRoot
    $env:SPECIFY_FEATURE_DIRECTORY = $expectedFeature
    $paths = & (Join-Path $reviewRoot '.specify/scripts/powershell/check-prerequisites.ps1') -Json -PathsOnly | ConvertFrom-Json
    if ((Resolve-Path -LiteralPath $paths.FEATURE_DIR).Path -ne $expectedFeature) {
        throw 'Feature path mismatch'
    }
    foreach ($name in @('spec.md', 'plan.md', 'tasks.md')) {
        $file = Join-Path $expectedFeature $name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing $name" }
    }
    $paths | Select-Object FEATURE_DIR, FEATURE_SPEC, IMPL_PLAN, TASKS
}
finally {
    $env:SPECIFY_INIT_DIR = $priorInit
    $env:SPECIFY_FEATURE_DIRECTORY = $priorFeature
    if ((Get-FileHash -LiteralPath $pointerPath -Algorithm SHA256).Hash -ne $pointerBefore) {
        throw 'Feature pointer changed unexpectedly; stop and review'
    }
}
```

PathsOnly仅解析，不验证文档内容；上面的存在检查和后续Spec/Plan/Tasks一致性评审缺一不可。之后读取/修改均使用已核对绝对路径，不能退回旧指针；新进程不保证继承本次env，必须重新执行预检。

未来实施优先继续显式路径和NoPersist等价前置检查，随后按技能规则处理此Feature，避免调用持久化helper；不得声称已完整执行未运行的标准脚本。若后续必须使用会写指针的标准流程，须把旧/新值及恢复方式单列为元数据变更取得授权后再做。本轮不作该切换。Git实际分支仍单独以git核对，不把Feature目录名当分支。

### 0.2 三层取证

T192在源码修改前保存B0静态身份/差异/既有运行回执来源；无法安全运行旧测试就标动态证据not_run。T203的B1是T196–T202护栏及配置修改后的当前基线，不能追认为修改前。后续B2回归须引用可比B1及本次差异，原失败归因证据不足时标unknown。详见[统一TCR §4](../../docs/test-change-record-local-settings.md)和[analysis-report](analysis-report.md#b0-baseline)；不因要补B0而冒险运行旧清理逻辑。


每层记录source_commit、working_diff/file_manifest、test_revision、配置/资源脱敏身份、UTC、命令、退出码和回执来源。B0只证明修改前静态身份与可比旧结果；B1必须披露已包含的T196–T202护栏/配置/测试变更，B2引用最近可比回执。
无安全B0动态比较时保留归因未定；不能因失败首见B1便称既有或新增。补比较只在安全隔离副本、披露附加护栏/测试修订，不回切用户工作区或关闭护栏；直接影响后续的失败先定位，无关旧问题单列。

<a id="resource-plan"></a>
### 0.3 Development / Test / Demo资源计划与历史状态（T194）

以下是历史准备记录，不代表当前占用或操作授权。当前 MVP 取消固定历史项目名/端口/旧卷的强制要求，允许独立同构 Test 等价验收，见 [plan §9.1](plan.md#mvp-acceptance)；原固定 M0 入口保持 not_run，脚本护栏不改，不接管旧容器或卷。

长期环境结构见[plan §7](plan.md)，这里是实际准备/核验的唯一资源状态表。S0只读观察UTC 2026-10-10T06:29:28.5812984Z，不把旧观察宣称当前运行验收；下表另记2026-10-10的T199新资源回执。M0 Backend容器构建及就绪仍未通过。
B0原始清单见[evidence/s0-b0-manifest.json](evidence/s0-b0-manifest.json)，事实边界见[analysis-report §B0](analysis-report.md#b0-baseline)；每批具体测试变更见[统一TCR](../../docs/test-change-record-local-settings.md)。

#### 资源矩阵与当前状态

| 资源 | Development保护对象/计划 | Test拟用目标 | Demo拟用目标 | Test/Demo状态 |
|---|---|---|---|---|
| PostgreSQL | 当前默认eduagent项目已有PG，不当Test | 专属实例中的eduagent_test库、eduagent_test_runner角色；pgvector(1024) | 独立eduagent_demo库/角色 | Test实例ID `7694989589121413158`不同于Development `7682002080870305831`；runner非超级用户/无CREATEDB/CREATEROLE，拒绝连接维护库及建库；vector 0.8.6、Alembic head `0023_grading_exam_question`已核验 |
| Schema | 原业务schema不创建/清理 | 每run随机test_<run_id_hex>；创建返回owner与实际目标写manifest | demo业务schema独立 | 真实helper创建、owner核对及本run删除通过；无test_残留schema |
| 备份恢复 | 原库保持、不恢复覆盖 | 同专属Test实例的每run新目标库；单独维护角色仅用于该隔离实例，不借dev凭据 | 使用隔离副本验收 | 维护角色非超级用户且具CREATEDB；原12项备份集成通过，无test_e1_残留库 |
| Redis | 当前eduagent-redis-1保持 | eduagent-test独立Redis实例；key含environment_id/run_id | eduagent-demo独立Redis | Test实例ID `cd31946612eb1cd54b73e673ac704404ad98e9f1`；错误实例拒绝和本run哨兵key删除通过 |
| Windows根 | 未来development根需显式接纳旧位置，旧配置/storage不移动 | C:/Users/10169/AppData/Local/EduAgent/test/runs/<run_id>/ | C:/Users/10169/AppData/Local/EduAgent/demo/ | 已创建本次独立根 `8750cb73793e400ca80f237139d0ba46`；原用户根未修改 |
| 文件子目录 | 原用户文件保持 | config/storage/cache/logs/temp/results/credentials均在该run根；合成秘密独立 | 相同子目录在demo根 | 已创建；credentials目录及3个合成env文件ACL仅当前用户/SYSTEM/Administrators；T202业务路径贯通未验收 |
| Docker项目 | 当前eduagent保留；未来eduagent-dev只是已规划目标，禁止抢占旧实例 | eduagent-test（保持M0固定名） | eduagent-demo | Test PG/Redis容器及网络标签均为eduagent-test，原Development容器仍healthy；Backend容器待M0构建 |
| Docker端口 | 当前发布5432/6379，原业务通常8000；实际worker未确认 | host loopback15432/16379/18000 | 拟25432/26379/28000 | PG/Redis仅loopback 15432/16379运行；Backend 18000尚未发布 |
| Docker卷 | 当前postgres_data/redis_data保护 | 项目专属卷；如已有卷，先查归属/内容，不自动清空或接纳 | 独立demo卷 | Test PG/Redis卷标签均为eduagent-test；Backend storage卷待M0，原卷未清理 |
| Provider | 原真实凭据保护 | 默认Fake/Mock，固定合成样本；不加载父进程真实Key、不走外网 | 实调用显式授权，默认合成验证 | 普通回归为Fake；真实模型授权未取得 |

M0固定值来源：tests/integration/test_m0_smoke.py:21–27、scripts/smoke_m0.ps1:22–27。历史M0使用过eduagent_t190_m0_fixed及既有测试卷，不等于当前新Test准备完毕；不覆盖旧固定库。普通Test库eduagent_test与旧M0固定库分别记录实际locator；后续M0若复用历史库，必须先证明它为专属测试目标，不能因名字含test放行。

S0取证时docker ps只见eduagent-postgres-1、eduagent-redis-1（healthy）；这是默认项目运行摘要，非数据库/模型可用性验收。Get-NetTCPConnection选定端口没有返回记录，与Docker发布摘要不能推导“端口空闲”；以执行前目标归属检查为准，当前不预留端口。

#### Test权限与所有权

eduagent_test_runner只连接专属Test库、创建/拥有本run schema，不使用superuser、CREATEROLE或开发账号；拒绝对dev/demo库写入。扩展/建库由隔离实例的受控维护步骤准备，普通测试不静默安装。备份测试确需新建库时，只在专属Test实例经单独资源计划及TCR使用维护角色。

ResourceManifest记录environment_id、run_id、实例/host/port、实际current_database/current_user/current_schema、已创建schema/库、创建者/owner、已解析文件路径、Redis实例/key范围、创建时间与清理结果；秘密不写manifest。本轮已对新Test实例执行身份、owner与清理核验；历史S0无DB查询的证据边界保持不变。
清理授权来自该次创建事实及目标复核，不能靠test前缀、端口或哈希推断。Test runner仅存在于隔离Test实例，Development实例无同名角色；对Development PG的Test runner身份认证尝试被拒，未执行SQL。Test实例内亦拒绝该角色连接维护库和建库。未对Development/Demo业务库做写入探测，跨环境保护还以独立Docker网络/卷/实例ID和错误目标拒绝为证。

#### 哨兵和失败清理边界

后续在独立dev/demo验收副本放合成哨兵（行、文件字节、合成凭据引用），记录前后值/指纹；不为哨兵修改用户实际教学库。Test只创建本run合成资料，清理后非Test哨兵变化应为0。本轮只在Test Redis创建并删除本run哨兵key；没有创建非Test业务哨兵。

只清理manifest列出的本run资源：拒绝public/原业务schema、其他run、父/根路径、重解析点逃逸；禁FLUSHALL、共享卷down -v、删整个用户目录。每次实际清理前重新核对绝对目标/owner；清理失败记录残留并停止扩大删除，不生成成功回执。

#### 真实调用与准入

父进程观察到DEEPSEEK_API_KEY存在，只记录存在性。测试入口必须显式选择test与Fake，不继承该Key去构造真实客户端；S0及本轮付费/普通模型请求均0。Real调用要有目标能力、样本、最大次数/超时和费用授权，不能靠Key存在启用。

真实PG/Redis及备份的上述核验已完成；M0三容器构建、就绪与容器迁移回执仍pending。T199未标完成时不自动进入T202/T203。禁止用当前eduagent项目、原Windows配置或Development/Demo资源替代。


## 1. 空配置与状态分层（US3；G4）

Windows真实包选择全新Development验收副本（不可用用户默认目录），不提供数据库/模型/JWT配置；双击后记录Setup Ready及设置页面截图。/ready应503。缺数据库与Redis分别重做诊断，页面仍可用。缺前端dist时Setup Ready也不能200。

提供独立Test依赖与目录，通过初始化预览核对数据库身份/head，显式确认允许的迁移。诊断即使Business Prerequisites通过，未装配进程仍/ready503并提示重启；重启装配原业务、调用原三个恢复钩子一次后/ready才200；未配AI仍标具体任务missing/unverified。已装配进程短暂断连再恢复不重复装配/钩子；初始化请求和健康轮询不调用钩子。旧业务保持登录。来源/密码不在浏览器读取响应。

## 2. 配置保存及权限（US1/US4；G1/G2）

先从本机launcher获得一次授权，确认第二次兑换、过期token、错误Host、跨源及无token Probe均拒绝。分别覆盖一次码过期、刷新、关页重开、闲置10分钟及绝对30分钟：回到Launcher控制窗口重新授权，GET已保存进度，不自动重放旧请求；关闭浏览器不退出Launcher，重复EXE不接管已有实例。两个浏览器同时编辑同修订，第二次保存409。
保存本地值再增加受控进程覆盖，检查来源、覆盖提示和effective=false。坏覆盖不能偷用原值。分别注入只读目录、磁盘写失败、损坏配置、未来schema，原active不变，恢复须显式选择。重启不能丢失已保存草稿。

## 3. Provider与费用（US2；G3）

先Fake/Mock逐一证明八类结果、unknown、取消、本地缺资源，不发真实外网请求。确认Reviewer为规则、不显示可选模型；未注册Provider不可保存为可用。LLM text成功不把Vision/Embedding标成功。
经明确真实调用授权后，用固定无业务数据样本分别检测所选能力；UI展示每次可能收费，单次外部请求<=1，无隐式retry/fallback。记录实际耗时/usage（未知保持null）。失败/超时/取消不换active或旧密钥引用。更换profile/credential后旧检测不能授权新候选。用Fake时钟分别验证未激活候选超过10分钟被拒、已激活绑定超过10分钟仍可使用、pending普通重启不重测；状态轮询/开页/等待带来的外部模型请求数均为0。保留实际网络失败，不能让计时器制造离线。

## 4. 凭据生命周期（G2）

Windows同用户保存后退出重启读取成功；密文损坏/凭据丢失时显示reentry_required，不落回.env。跨用户动态隔离 deferred/not_run，不创建账户或修改安全策略；解密失败的现有保护逻辑保持。对测试秘密原文检查普通配置、响应、错误、Trace、Audit、benchmark、浏览器持久存储和诊断输出，出现次数必须为零。检查DPAPI输出内存/客户端释放路径，真实冻结包单独证明。

Docker只读Secret缺失/被撤销/版本轮换，UI给出外部管理步骤；Windows写入API在Docker405。检查localhost发布后的合法请求不会因NAT误拒，同bridge无token请求仍拒绝。服务日志不含一次授权码/秘密。
删除被active/pending/暂停运行引用的凭据应409；解除引用后的删除必须真实成功后才更新状态。业务备份恢复不带秘密，需重录/重新挂载后再检测。

## 5. 运行快照与索引（US5；G6）

准备客观题+主观题、低置信度暂停的实际测试Workflow。绑定A启动，激活B后创建新任务；旧运行仍A、新运行B。重启后恢复旧checkpoint，actual model/prompt/config引用保持。删除旧秘密被拒；模拟外部撤销时恢复明确失败且不切B。人工复核、规则评分、考试分值、最终化与诊断原断言继续成立。

旧版本payload无snapshot应保留legacy_unknown；冻结其已接纳基线，未终结前相关配置激活拒绝，原恢复路径仍可用。不可将新snapshot写成历史已知。

有向量库分别尝试非1024维及同维不同模型，均拒绝，不改向量。旧无身份库显示unknown；没有接纳证据不新增混合向量。进入设置“索引兼容性”，经身份详情→脱敏历史证据→预览→确认→读回，核对legacy_adopted及操作者/前后身份/证据审计；测试缺证据、冲突、过期及审计写失败均不改变旧身份或向量，备份恢复保留审计。空知识库首次原子登记，两个不同模型并发不能混入。无需运行索引删除/重建。

## 6. 隔离与回归命令（US1/US6；G1/G7）

未来profile入口实现后，先输出并检查ResourceManifest，再执行下列既有框架命令；不要在当前缺统一Test护栏的仓库直接执行全量测试。

```powershell
# 前提：本进程已通过显式Test目标预检，凭据以受控方式注入。
python -m pytest tests/unit/settings tests/contract/test_settings_api.py tests/integration/test_settings_lifecycle.py
python -m pytest
python -m mypy backend/app
python -m ruff check backend/ tests/
```

实际命令以仓库已安装工具为准，不因缺工具自动安装。前端未来锁定npm脚本：类型检查、单元测试、production build和生成DTO漂移检查；应由T224/T231写入真实入口，当前不伪造可执行脚本。

全量中M0/真实DB/Windows权限等skip必须明确原因；不能把skip算通过。运行后检查dev/demo哨兵未变，本次Test资源按manifest清理；清理失败记录残留而不扩大删除范围。普通回归请求计数证明付费模型0次，真实评测单独启用并保存结果。

## 7. 实际交付（US6；G5）

- Windows：构建后onedir在独立路径启动，无源码目录/Node依赖，React资源和/gradio真实打开；首启、中断、重启、端口冲突、DPAPI和仅退出自有进程各有回执。
- Docker：独立项目/卷、模型Secret缺失和数据库故障时HTTP设置可达；/ready保持503；修复初始化后核心ready。不能把healthcheck的unhealthy当理由重启循环杀死向导。
- UI：1366×768和1920×1080，键盘、错误焦点、来源、只读secret、费用、待重启、旧登录跳转；离线无CDN。
- 原业务端到端：教学资料、导入/校正、生成/审核、组卷/考试、阅卷/复核/诊断、文件备份恢复，使用受控输入及当前独立资源。新配置系统不重新判定旧业务语义。

### Docker 构建与外部 Secret

`Dockerfile` 用锁定的 npm 依赖构建 React，并在构建中校验 OpenAPI/TypeScript；最终 Python 镜像只包含 `settings-ui` 静态资源，不包含 Node 运行时。Compose 的三服务端口仅发布到宿主回环；HTTP Bootstrap不等待数据库或模型，`/ready` 仍用于业务healthcheck。

显式选择独立项目和非秘密插值文件启动。`docker-compose.yml` 不把整个 dotenv 传入应用，也不复制宿主模型Key。持久卷仍挂到 `/app/storage`，环境子目录位于该根；既有文件不自动迁移或删除，旧数据必须经原接纳流程核对。

需要秘密时叠加 `docker-compose.secrets.yml`，设置 `EDUAGENT_SECRETS_DIRECTORY` 为操作者准备的目录，并在 `EDUAGENT_SECRET_MOUNTS` 提供非秘密JSON目录，例如 `{"postgres-v1":{"path":"/run/secrets/postgres-v1","purpose":"database"},"jwt-v1":{"path":"/run/secrets/jwt-v1","purpose":"jwt"}}`。目录整体只读挂载，缺失文件由设置诊断报告，程序不创建宿主来源目录。Provider可按需单独登记，空模型配置不阻断设置页。

宿主管理者通过同一显式 Compose 项目执行 `docker compose … exec -T backend python -m backend.app.deployment.container_control --port <宿主发布端口>`，取得一次性 fragment URL 后在本机浏览器打开；重新授权可加 `--reauthorize`。命令连接仅容器同 UID 可用的私有 Unix socket，不增加 HTTP 发码端点；URL 只返回给该 exec 调用，不写服务日志。不要把返回值保存到共享日志。

全新持久卷需由操作者明确创建所选 storage 根，再由向导初始化数据库；应用仅自动准备 cache/logs/temp，不因旧 storage 不可用而创建替代空目录。Test 必须额外提供已核验实例、角色、数据库与 Redis 的专属 profile；Development/Demo 保留各自接纳与身份流程。

轮换使用新文件名/secret_id，保留仍被active、pending或运行快照引用的旧版本；不原地改写旧版本后宣称引用未变。UI只选引用，不写Docker秘密。Demo仍核验原创建回执、基础依赖和迁移目标；Setup可用但业务未就绪时返回设置入口，不Seed、不清卷、不把unhealthy当作自动重启理由。

## 8. 交付判断

必须区分：文档一致→源码实现→测试通过→构建成功→运行加载→系统验收→发布回执。仅本批既定门禁通过后才能声明第一批交付；真实模型未跑则列未验证能力，不猜测效果。规划完成后本轮停止，不执行上述步骤。
