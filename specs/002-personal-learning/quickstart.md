# Quickstart and Validation Guide — Approved Architecture / Development Guardrails

最新状态（2026-10-23）：T001–T087均已勾选（87/87）。2026-10-23用户明确批准T083、T084、T085、T087按“先验收、先使用、发现问题再优化”的带边界口径勾选，T086保持已勾选；勾选不等于全部pass。4条历史outcome_unknown保留，完整浏览器闭环未完整验证，DOCX两平台未重测，详见下列已知边界。G1既有真实样例仍有oracle_pass=false（ANSWER_ENCODING_INVALID等），不因勾选改记为pass。账本记录末值recorded=39/40、proven=32/35、disputed=6、A3=6/10、A4=9/10；本轮真实发送0，无commit/push/正式发布。
P1 b29ab843后续只读DB确认paper c5fa5c0f Pending Review、候选d7beccc3 Pending Correction，DB映射后的现有_validate_p1范围通过；候选尚未进入正式questions/练习。G2、R1、T1、Probe、V1与T2独立补测的成功记录不覆盖G1未通过事实。


本指南供方案获批、功能实施后验证使用；当前已执行本地/Fake Spike，证据及能力边界见research P02/P03/P04/P08。T008—T014的基础验证与后续完整产品场景分开报告。契约详见 [API/UI](contracts/personal-learning-api.md)、[内容与评分](contracts/content-and-grading.md)、[本地生命周期](contracts/local-lifecycle.md)。

## 2026-10-23 用户裁决：带边界验收

本节为T083–T087当前验收口径；下文旧日期的批次数字、失败和未勾选描述仅是历史时点记录，不覆盖本次裁决。任务勾选表示用户接受现有结果及边界，不代表所有质量项、浏览器或格式实测均为pass；not_run/not_claimed及失败结果保留原义，不改冻结oracle、账本历史或operation/争议/评分。requirements checklist仍为12/16，属于历史评审项；finish_reason未持久化仍为账本schema边界。本次裁决不补造或追认历史A2/A3授权，也不授予后续真实发送权限。

### 边界1：4条历史outcome_unknown

最近一次只读DB查询：running=0、active_slots=0、outcome_unknown=4；不是unknown清零。以下创建时间均为北京时间（UTC+08:00）。

| 原operation ID | 创建时间 | 场景 | 后续独立补测operation（成功） |
| --- | --- | --- | --- |
| 9468d072-af85-4913-9652-9d183d04f9af | 2026-10-21 18:08:34.831909 | A1 补答案 | d3da82e9-1619-4f79-81c2-20080ed1258a |
| da8b2db0-d550-402e-b7e4-3223762514af | 2026-10-21 18:08:35.229788 | S2 主观评分 | 4ff1d966-b0f4-42ce-b278-409bf6b13b88 |
| 3df36a78-e386-4483-97da-3c2e8ea37eca | 2026-10-21 18:08:35.620184 | S4 主观评分 | d27e7965-1ab3-40ca-ace8-84ad07cf654d |
| ccd8030b-f59b-4c83-beff-f7855973458b | 2026-10-21 18:11:13.475645 | T2 多轮教师对话 | 81a2a1d8-d42e-4763-a351-a8f202bdafee |

原4条均保留outcome_unknown，actual_requests=1、retry_of_id=NULL；结果及实际费用仍不确定。后续补测使用新对象成功，不等于原请求重试、补偿或结果确认。历史证据为.cache/t083-batch1/evidence/requests.jsonl、.cache/t083-batch1/stop-loss-state.json及.cache/t083-batch1/evidence/stop-loss-result.json。用户接受为非阻塞历史审计边界；后续优化入口归属T083运行恢复/unknown补偿：仅在可取得原请求结果或补偿依据且另获授权后处理，不原地重执行，不以独立样例成功改写历史unknown。

### 边界2：浏览器闭环未完整验证

T084与T085的journey-result.json均为api_passed_browser_not_claimed（API-only），路径分别为.cache/t082-t086-20261021/windows-runtime/journey-result.json与.cache/t082-t086-20261021/docker-t085-b4711a5f53084e97a39b1af6bd6458d1/journey-result.json。Windows局部截图/UI回读为.cache/t082-t086-20261021/windows-browser-teacher.jpg及.cache/t082-t086-20261021/windows-runtime/ui-readback.json；Docker局部证据为.cache/t082-t086-20261021/docker-browser-settings.jpg、.cache/t082-t086-20261021/docker-browser-unknown.jpg及.cache/t082-t086-20261021/docker-t085-b4711a5f53084e97a39b1af6bd6458d1/root-ui-readonly-observation.json。以上支持局部UI、Fake回答/引用及持久化核对；完整浏览器闭环未完整验证，按not_claimed保留；T083真实AI业务从浏览器入口贯通至结果/引用仍为not_run，不记为pass。用户接受带边界验收；后续优化入口归属T084/T085双平台完整浏览器闭环，真实AI部分归属T083，涉及任何Provider/Embedding/Probe/Vision发送均须另获明确授权，本轮不执行。

### 边界3：DOCX两平台未重测

T033历史有限DOCX材料支持路径存在：.cache/review002-t032-t058/browser-fixtures/docx-clear.docx；用户接受其用于SC-002的有限格式覆盖。T084/T085的journey样例不含DOCX，Windows与Docker均未重测（not_run），不能把T033历史覆盖写成两平台DOCX实测pass。用户接受此边界；后续优化入口归属T084/T085的DOCX样例重测，并回对T033/SC-002的上传、原内容/OOXML锚点和图文顺序，使用隔离Test资源，涉及真实发送时须另获授权。

## 1. Preconditions

- 使用项目隔离 Python >=3.12 环境及现有前端依赖锁，不全局安装/升级。解释器、工具版本与新增依赖先核验；pyproject 当前未声明 Ruff/Mypy，先确定补齐方案。
- 单元/契约测试使用合成题目和 Fake Provider，不能加载根目录 .env、真实凭据或产生付费请求。AppSettings 当前默认 .env，需要通过已核对 fixture 注入显式配置；不能假设设置测试的 conftest 自动覆盖其他目录。
- 真实数据库测试须由现有 Test resource profile核对host/port、database、role、schema、PG system_identifier，确认独立所有权后通过 tests/postgres_helpers.py 创建本次schema；Redis和文件根同样核验。
- 配置/凭据管理和运行时依赖使用正式服务及合成Secret，不能手写假manifest冒充授权资源。独立真实模型验证另需明确授权，不属于本规划。
- 新增/修改测试先按仓库规则留最低必要 TCR，说明旧断言改变原因和新覆盖；不新建验收流水报告。

## 2. Existing Commands for Later Use

以下是在仓库确认存在的入口。变量必须指向经过核验的项目隔离资源；命令存在不表示当前已经满足新业务断言或会自动完成环境隔离。

前端在 frontend 目录执行；EDUAGENT_PYTHON 指向已核验解释器，API类型生成需扩展到新的个人接口后再检验：

~~~powershell
npm run test
npm run typecheck
npm run build
~~~

后端在仓库根目录按修改范围选择既有聚焦入口。运行前核对每个夹具，不把以下列表当未经检查即可安全运行的全量命令：

~~~powershell
& $testPython -m pytest tests/unit/grading/test_objective_grader.py
& $testPython -m pytest tests/contract/test_settings_security_api.py tests/contract/test_settings_secrets.py
& $testPython -m pytest tests/integration/test_database_initialization_postgres.py tests/integration/test_runtime_settings.py tests/integration/test_reference_lifecycle.py tests/integration/test_backup_restore.py
& $testPython -m pytest tests/integration/test_launcher_control_process.py
~~~

真实PG分区只在已核验的Test配置下执行。isolated_postgres_schema用于真实迁移链，isolated_postgres_engine用于元数据型测试，两者不能互相冒充迁移证据。backup/restore的旧Admin/JWT依赖需按新维护合同改造后验收。

工具环境确认并补齐获批声明后，沿用静态检查入口，按实际受影响代码范围扩展：

~~~powershell
& $testPython -m ruff check backend/ tests/
& $testPython -m mypy backend/app
~~~

Windows构建参数在现有脚本中存在；ownedBuildRoot必须是本批拥有的仓库内独立目录，不重用未知历史产物：

~~~powershell
.\scripts\build_exe.ps1 -Python $testPython -BuildRoot $ownedBuildRoot
~~~

Docker测试入口要求显式测试镜像及仓库.cache下自有root，依现有夹具创建新UUID项目、随机本地端口及资源：

~~~powershell
& $testPython -m pytest tests/integration/test_docker_settings.py
~~~

其中 EDUAGENT_DOCKER_SETTINGS_IMAGE 与 EDUAGENT_DOCKER_SETTINGS_ROOT 由独立测试准备提供。不要直接对固定 docker-compose.test.yml 资源执行接管或清理。禁止为了运行上列命令读取未知.env/密码、修改系统权限或降低既有检查。

## 3. End-to-End Scenarios

以下是未来验证场景，不是测试结果或新测试文件。对应实现与验收位置已映射到 [tasks.md](tasks.md)；T007八方向已批准，T008开发保护/验收方法已批准；真实模型验证另须T082授权。本轮只执行T008—T014对应基础验证，其他完整业务场景留对应任务。

| 编号 | 前提/动作 | 可观察的通过条件 |
| --- | --- | --- |
| Q01 个人入口 | 干净独立Test根完成配置，进入React首页，创建科目 | 无教师/学生登录；正常学习不依赖持有Setup管理token，设置修改仍独立保护 |
| Q02 学习资料 | 合成PDF文本/扫描、DOCX、TXT/MD、PNG/JPEG导入，选择学习资料并确认组织 | 顺序/图文/公式/定位可对照；可选元数据缺失不阻塞；试卷用途不混入教材检索 |
| Q03 真题校正 | 含单选/多选/主观、跨页图文和缺答案的试卷 | 原题/原图/字段可核对、校正、批量确认；无答案不静默补；同次AI输出依据后服务核验，疑点暂缓且不自动追加模型复核；保存与准入分离 |
| Q04 同题与变式 | 导入同题不同来源、关键图片变化题、已练过重复题 | 疑似项需确认；同题共享身份保留所有来源和历史；变式不误合并 |
| Q05 组题 | 三来源×三模式及资料/通用依据，分别制造真题和未做不足 | 实际覆盖可见；不静默AI补题/重复/放宽；同次练习无重复题 |
| Q06 评分 | 不同分值单选/多选/主观及关键图像题，整组提交 | 新计分规则、结构/范围/实际模型置信约束；客观零模型；空主观题提示后可提交、本地零分且零模型，区分未答/已答零分/失败；完成率、含未答本组均值、仅有效已答选择题正确率及部分成绩可核对；图像依赖不可用保持明确失败；无教材通用路径不造chunk |
| Q07 重评 | 部分题评分失败或争议，纠正依据后重评，先不接受再接受 | 原新评分可对照；未接受仍排除；其他有效题独立计入；旧轮次/重复请求不误解争议 |
| Q08 诊断 | 已知答案序列、20题边界、4/5题、恰好60%、不同满分、多标签、重练、未答及已答零分 | 仅有效已答样本逐题简单平均；未答数单列，不占窗口/门槛/首次身份，仍按未做；有效已答零分保留；首次/重练分开，无样本与0%区分，阈值/窗口准确；不宣称精确掌握度 |
| Q09 AI教师与费用 | Fake HTTP先覆盖新会话/继续/换模型/超时/派生请求，使用真实来源白名单 | 确认前零收费发送、费用变化重新确认、失败不自动重发；引用有效、聊天不改分、删除聊天不删成绩 |
| Q10 新域恢复 | 真实独立PG/Redis、已批准迁移、材料新旧版本、Workflow及凭据引用 | 默认与历史互不覆盖；恢复FK/文件/索引身份/快照依赖正确；缺失时安全闭锁，维护授权仍有效 |
| Q11 退役清理 | 隔离Test中按批准清单转换旧业务，新域功能可用后核对删除 | 遗弃数据不迁移不建副本；共享/在用配置和凭据保留；旧角色入口/路由确实退出 |
| Q12 双平台体验 | 新Windows onedir与独立Docker完成Q01到练习/诊断，真实浏览器查看图文及引用，退出重启 | 真实渲染/操作/持久化可用；退出清理自有进程端口资源；Docker挂载Secret及恢复边界仍有效 |

代表样例必须包含容易失败的浮动图/公式、跨页题、矛盾答案和不足证据。质量阈值、容量上限及超时数值在技术验证后提出并由用户确认，不在本指南编造。

## 4. Evidence and Stop Rules

区分 unit/contract、Fake HTTP、真实PG、真实模型、浏览器、EXE、Docker结果；如实输出 passed/failed/errors/skipped/not_run，不将不同批次计数相加冒充单次全量。旧B1/B2/G6条目在新旧合同变化下逐项映射，保留相关安全断言，不用“旧Feature完成”代替新验收。

有明确产品缺陷则在授权实施阶段修复并聚焦复测；需要真实模型授权、系统权限、未知资源操作或需求取舍时停止对应操作。不存在的依赖、失败响应、缺失来源不伪造成通过。

结果直接在当前聊天报告；正常工具机器产物可按既有机制产生，不新建人工验收报告。只清理本轮清单确认拥有的资源，禁止清理Development/Demo或未知卷/数据库。

## 5. T008 已批准的后续测量方法 — 能力结果待实测

T008硬性质量行为已批准，已批准开发临时包/验收方法集中于[research §6—§7](research.md#6-t008-已批准质量原则--approved)。以下为已批准的开发阶段测量方法；14份只作工程起步。仅执行本轮T008—T014所需测试，其余场景留对应任务；不授权真实付费调用。

1. 代表输入建议从7条路径起步：文字PDF、扫描PDF、DOCX、TXT、MD、PNG、JPEG，各清晰/压力1份，共14份起始样例；复杂DOCX、跨页题、选项图/公式/表格另按必需场景补足。采用合法合成/授权样例和人工固定真值，不用同页转格式当独立准确率样本。
2. 对原字节、解包字节、像素、页数、图像出现次数、JSON/模型输入输出分别记实值；按L01—L06边界内及超一档检验明确拒绝、原内容保留与未完成状态。原生PDF对象/解压开销须单列；不能只测空白页或tracemalloc。
3. 初次各取冷/热1次，记录阶段耗时、进程/原生/浏览器RSS和在途资源数；只作为工程观察，不称p95或统计容量。依据资源峰值复审临时预算，不能为通过测试自动抬高上限。
4. 原生线程取消需验证“调用方超时后仍在执行”的真实状态、占用不提前释放、不重发及最终退出。60秒软等待不是硬终止证据；硬执行时限和释放策略须补验证，必要新独占worker方案先给出影响供评审。
5. 质量按批准样例核对关键内容正确或明确标疑点且阻断自动评分；分别报告正文/公式/图归属/引用/误合并候选/评分误差，不由一个混合CER冻结总体百分比。首次/重练、空白/有效零分和争议按既定业务规则验。
6. 真实模型另经T082/G-P：先指定Provider/端点/模型、Test凭据来源、样例、逐步骤及总请求/token/超时/金额上限、实际发送前约束和停止条件，再运行T083。未配置必要资源边界不发送；所有可能收费的Embedding/Rerank/Vision和复核计入预算，禁止继承T237。
7. Windows/Docker各用同组资料完成Q01—Q12，报告实际性能和资源释放；不先要求尚未实现的新闭环测量结果来形成循环依赖。T008临时限制和验证/复审办法已获批准；仅本轮基础范围取得实施授权；最终效果与交付验收仍在后续任务完成，不由门禁审批冒充已通过。

## 6. 基础任务验证入口与隔离

T014入口为tests/integration/test_personal_learning_foundation.py、tests/contract/test_learning_operations.py、tests/integration/test_runtime_settings.py，并按共享消费者覆盖runtime_consumers、settings_management、模型和Provider、应用装配/Bootstrap、设置安全与前端。真实PG迁移和Fake协议分开解释，按现有Test回执核对后仅创建随机schema。

迁移测试须避免Alembic fileConfig替换Pytest处理器/禁用SDK日志，旧迁移回归可单独进程运行。历史设置夹具使用其自建profile时，不继承另一个Test根；仅允许本轮自有临时目录中的合成.env，仓库.env、真实凭据及Development/Demo根仍禁止访问。OpenAPI导出可在禁止网络、真实配置和凭据读取的运行时审计下验证。

## 7. T082 离线冻结与 G-P 授权记录

固定样例和人工oracle位于`tests/fixtures/personal_ai_acceptance/samples.json`，版本`002-acceptance-v1`。12个主请求覆盖通用/资料出题、补答案、跨页抽题、满分/部分分/已答零分、同输入最低两次比较、修订重评、关键图像评分和教师追问。两片资料各一次索引Embedding，加一次语义查询，建议业务总数15。已有同模型、端点、凭据版本且可复用的text验收证据时，另加json_object、vision、embedding三次Probe为18；全新配置依现有Registry/激活门禁还须独立text Probe，即四次远程Probe、总数19。G-P-20261021已批准全新配置的19次方案（12业务+3 Embedding+4 Probe）；15/18变体不得自动替代，须重新冻结并另行确认。改为远程hybrid_rerank时另有重排及其必要Probe，必须重新冻结，不能自动多发。

当前Registry的文字候选是`openai_compatible`或`deepseek`；Embedding使用现有适配器且维度符合当前索引；Vision目前只有`deepseek`接线，代码还要求其明确支持的模型与官方端点，不能称普通OpenAI-compatible已具备Vision。G-P-20261021已选定`deepseek`/`deepseek-flash`（官方端点`https://api.deepseek.com`，同一模型覆盖text、json_object、vision）与`openai_compatible`/`BAAI/bge-m3`（1024维，`https://api.siliconflow.cn/v1`）；不启用远程hybrid_rerank，不读取真实配置。OCR继续使用已预置的本地RapidOCR，不下载权重、不收费Vision替代。

G-P-20261021已按下列字段填实（批准值见本节授权记录）：各task的Provider/profile修订、模型、完整端点、Test凭据引用及版本；逐请求输入/输出Token、图像计数办法、Embedding维度/响应字节、有限B、30秒HTTP等待及操作总期限；Probe清单；币种、当时单价、逐项估算状态、总请求/Token/金额授权及停止条件。模型和价格未知时不编造金额，不将估算当供应商账单硬上限。样例JSON的授权字段已按G-P-20261021填实，实际发送仍为0。

生产`LearningOperations`支持显式注入有限`PaidValidationPolicy`，固定每个operation的purpose、计划摘要、Snapshot及模型/端点/凭据版本，按已持久发送事实和全部计划最坏预算核对。Probe仅在显式validation policy存在时纳入同一有限清单，发送前持久登记，未知和失败不退额度。没有policy仍默认拒绝个人付费业务，不能以MockTransport绕过门禁作为唯一证明。

正式产物的无付费Fake验收仅允许已核验Test根中的`paid-validation-test.json`：必须同environment_id、loopback_only、literal loopback端点和`synthetic-`模型；使用合成协议完整JSON字节的保守预算，不冒充真实模型Token计数。空grants只允许生成预览，不授权发送；本地测试操作者须逐项冻结精确operation grants。此加载器不在Development/Demo读取，不用于开放真实供应商。没有HTTP接口可写授权文件。

### G-P-20261021 授权记录（已批准）

**批准来源**：2026-10-21 用户确认T082推荐草案。仅授权T083的真实模型验收，不授权正式发布、不继承历史T237；实际发送**0**，T083**未执行**。

| 能力 | kind / adapter | 模型 | 端点 | Test凭据引用 |
| --- | --- | --- | --- | --- |
| text、json_object | `llm` / `deepseek` | `deepseek-flash`（DeepSeek-V4.1-Flash-1017） | `https://api.deepseek.com` | `d9993954-08c5-4f8a-a5ab-f534a60a2ee9` / `2cf8bc5c-7d1a-4821-8ff4-8553d6b1c63e` |
| vision | `vision` / `deepseek` | `deepseek-flash` | `https://api.deepseek.com` | `d9993954-08c5-4f8a-a5ab-f534a60a2ee9` / `2cf8bc5c-7d1a-4821-8ff4-8553d6b1c63e` |
| embedding | `embedding` / `openai_compatible` | `BAAI/bge-m3`，dimension 1024 | `https://api.siliconflow.cn/v1` | `8afc04ca-1020-40c9-81d1-cf997b9496a9` / `14a3e04c-1cb9-4e13-b753-1bf1eed27b9d` |
| OCR | `ocr` / `rapidocr` | 本地预置 | 无 | 无 |
| rerank | 不启用远程重排 | — | — | — |

凭据只以实际引用出现，Key由本地Test操作者在“安全凭据”页写入既有DPAPI存储。`POST /api/settings/credentials`只接受`purpose`（provider/database/redis/jwt），返回服务端生成的`credential_ref`与`version`UUID，不接受自定义名称或序号版本；同一purpose只能靠UUID区分。2026-10-21共写入三个`purpose=provider`凭据：DeepSeek主用`d9993954-08c5-4f8a-a5ab-f534a60a2ee9`/`2cf8bc5c-7d1a-4821-8ff4-8553d6b1c63e`（14:25:51）、DeepSeek备用`8aac0618-b63a-4e1d-a520-528d384988d3`/`57fcc8f4-280c-4b6f-ac94-75a9c1586c96`（14:26:55）、远程Embedding`8afc04ca-1020-40c9-81d1-cf997b9496a9`/`14a3e04c-1cb9-4e13-b753-1bf1eed27b9d`（14:42:32）。三者均未绑定Profile、未运行Probe/激活，实际发送0；“可用”仅表示DPAPI可解密，不等于供应商已验证Key有效。DeepSeek不提供Embedding API，备用Key不能充当Embedding；第三个凭据按选定方案映射到`https://api.siliconflow.cn/v1`/`BAAI/bge-m3`，若实际供应商或端点不同，必须先更新本表再进入T083。操作者已于2026-10-21在SiliconFlow控制台确认`BAAI/bge-m3`仍为免费层；本地Qwen路线在冻结EXE中不可用（`packaging/EduAgent.spec`明确排除`sentence_transformers`/`torch`），改本地需重建EXE或改运行环境并重新授权。引用缺失、版本不符或端点变化即拒绝发送。真实验收使用进程内经评审的有限`PaidValidationPolicy`，不得用`paid-validation-test.json`的loopback/synthetic门禁冒充。

**请求计划（19次）**：12个业务主请求（G1、G2、A1、P1、S1—S4、R1、V1、T1、T2）+2次资料索引+1次语义查询Embedding+4个Probe（text、json_object、vision、embedding）。15（本地BGE）与18（可复用text证据）变体未获批，不得自动替代；改用本地Embedding为15次，但需重建EXE/改运行环境，未获批准，不得自动切换。2026-10-21用户批准G-P-20261021-A1：总上限增至21次、输入106,000、输出27,000；新增2次为vision Probe重试与1200×1000 vision诊断，金额上限仍≤¥1.01。

| 范围 | 输入Token上限 | 输出Token上限 |
| --- | --- | --- |
| 出题G1/G2（各1次） | 6,000 | 2,000 |
| 补答案A1、抽题P1（各1次，json_object） | 8,000 | 2,000 |
| 评分S1—S4、重评R1（各1次） | 4,000 | 1,200 |
| 图像评分V1（1次，含1图） | 6,000 | 1,500 |
| 教师T1/T2（各1次） | 6,000 | 1,500 |
| Embedding索引2+查询1（各1次） | 2,000 | —（dimension 1024） |
| Probe text/json_object/vision/embedding | 6,000 / 8,000 / 6,000 / 2,000 | 2,000 / 2,000 / 1,500 / — |
| **合计19次** | **94,000** | **24,000** |

**金额上限**：DeepSeek≤**¥1.00**，Embedding≤**¥0.01**，合计≤**¥1.01**。按2026-10-21官方单价（`deepseek-flash`峰时输入¥2/百万、输出¥8/百万；谷时减半），本计划峰时最坏约¥0.364、谷时¥0.182；Embedding经操作者2026-10-21在SiliconFlow控制台确认为免费层；若后续计费变化则停止并重新确认。估算不是供应商账单硬上限；免费层或单价变化先停止并重新确认。

**时间上限**：单请求HTTP等待30秒；单操作≤300秒；整批验收≤3,600秒；无自动重试。

**Token/图像计数**：预发送计数取`max(tiktoken cl100k_base, ceil(UTF-8字节/2))`，每张图片另预留1,024输入Token（官方V4.1页未给单图上限）；仅为保守预算，实际以响应`usage`记录。`response_bytes`≤65,536。任一请求超过计划上限、模型/端点/凭据版本/样例/价格变化、Probe失败或unknown，均停止且不补发；未知保持已用计数、不退额度。

### G-P-20261021-A1 修订与 Probe 结果（2026-10-21）

用户批准将总请求由19次增至**21次**，输入/输出上限由94,000/24,000增至**106,000/27,000**；金额上限仍**≤¥1.01**（峰时最坏约¥0.412）。新增2次：vision Probe重试1次（6,000/1,500）与1200×1000冻结图vision诊断1次（6,000/1,500）。

| 请求 | 结果 | 请求数 | usage（输入/输出） |
| --- | --- | --- | --- |
| text Probe | SUCCESS | 1 | 64 / 30 |
| json_object Probe | SUCCESS | 1 | 64 / 26 |
| vision Probe（1×1样例） | UNKNOWN_PROVIDER_ERROR | 1 | unknown |
| vision诊断（1200×1000，生产适配器+JSON） | SUCCESS `{"ok":true}` | 1 | unknown |
| vision Probe重试（128×128 RGB） | SUCCESS | 1 | 248 / 43 |
| embedding Probe（BAAI/bge-m3） | SUCCESS | 1 | 11 / 0 |

vision首次失败定位为ProbeService的1×1样例缺陷；已改为独立128×128 RGB PNG（OCR样例不变）并加回归测试，150项离线通过。实际发送6次，剩余15次（12业务+3 Embedding）。业务与浏览器证据尚未执行，T083保持未完成。

### G-P-A2/A3 授权链与账本口径复核（2026-10-21）

- **A2**：`tasks.md` 与 `.cache/t083-a2/evidence/STATUS.md` 保留 25 次/120,000/30,000/≤¥1.01 的项目记录；该记录不等同于已找到用户批准原文，A2 批准原文仍待核实。
- **A3**：仅见 `.cache/t083-batch1/evidence/a3-preflight.json` 的 `authorization: G-P-A3`、`.cache/t083-batch1/live_env.py` 常量及结果文件标签；**授权链待核实**。其标注上限为新增 10 次/120,000/30,000/≤¥0.50、累计 35 次/240,000/60,000/≤¥1.51；用户确认前不得作为已授权依据。
- **已发生 A3 发送**：保留全部原始证据，标记为“实际发送已证实、事前授权待核实”；不补造历史批准记录，不删除或改写历史证据。
- **账本并列口径**：`recorded_total=29`、`proven_total=23`、`disputed_overcount=6`。6 次差异为“强推断重复计数（A1 的 6 次 Probe），仍有未记录发送的理论可能”；历史账本与原始 JSON 保留，不把正式账本直接改为 23。后续统计必须区分逐请求证实值、历史记录值、A3 批次计数和累计授权上限。
- **A3 新增 10 次**：在获得明确相反授权证据前按硬上限处理；不得使用累计 35 次余额绕过，不自动追加第 11 次；P1/V1/R1/T1 的缺口另行申请授权。
- **`max_tokens=5000`**：此前未获批准；2026-10-21 用户另行明确授权 1 次 G2 generation（7000 输入/5000 输出，费用目标≤¥0.10），不追认 A2/A3、不构成后续授权。该次已执行并通过 G2（operation `fa276122-a69f-4b1b-b755-e4e593a9a254`，usage 3651/2320，`finish_reason=stop`）；A3 记 6/10，本次运行后 `recorded_total=30`、`proven_total=24`、A3 余 4；T083 仍因 P1/V1/R1/T1 未完成保持 `[ ]`。

### 2026-10-22 T083 剩余请求的离线准备与待批准增补

本节为下一轮提案，不是 G-P 批准记录。用户本轮要求按上一轮诊断推进，已完成
离线准备；本轮真实发送 **0**。截至已有 A4 结果，`recorded_total=32`、
`proven_total=26`、`disputed_overcount=6`、A3 `6/10`、A4 `2/6`。
此前 A4 的 T1 资料索引成功，P1 在输出上限 2,000 时空正文失败；失败保留计次。
历史 A2/A3 授权链疑点仍保留，本提案不追认历史授权。

#### 已完成的零外发检查

- `.cache/t083-batch1/next_preflight.py` 仅通过只读 ConfigService 读取指定 Test
  修订的非秘密元数据；不打开凭据库，不读取 `.env`，不连接 PG/Redis。
- 在内存只新增独立 `vision` 绑定，复用当前 DeepSeek Profile 的原 ID/修订。
  真实配置投影由 14 个 target 变为 15 个，仅新增 `vision:vision`；原 14 个
  identity 和已激活接纳记录可沿用。候选修订未持久化、未激活。
- RuntimeSnapshot 的内存投影可解析 vision，原任务绑定及 Embedding identity
  保持一致。这不等于真实业务 preview 或 V1 视觉效果已通过。
- `test_adding_vision_binding_only_requires_its_new_candidate_probe` 使用合成凭据与
  MockTransport，验证生产激活流程仍拒绝缺失/旧候选 Probe，新候选一次 vision
  Probe 后可沿用旧接纳记录，且历史快照不变。TCR 已先记入既有文档。

#### 一次批准、分阶段执行的六次请求提案

模型、端点、凭据版本、思考模式、冻结样例和 oracle 沿用现状；不修改共享 Profile
的 `reasoning_effort`，不切换模型。`max_tokens` 明确指 **reasoning + content
总生成预算**，不保证正文最低额度；下列值是工程初值，尚无成功率保证。

| 顺序 | 场景 | 请求数 | 输入上限 | 总生成输出上限 |
| --- | --- | --- | --- | --- |
| 1 | P1 重新预览并显式再次抽取 | 1 | 8,000 | 5,000 |
| 2 | R1 修订重评 | 1 | 4,000 | 4,000 |
| 3 | T1 教师答疑 | 1 | 6,000 | 4,000 |
| 4 | 新候选修订 vision Probe | 1 | 6,000 | 1,500 |
| 5 | V1 语义查询 Embedding | 1 | 1,000 | 无，1024 维 |
| 6 | V1 vision 评分 | 1 | 6,000 | 5,000 |
| **新增合计** | | **6** | **31,000** | **19,500** |

vision Probe 的 1,500 仅适用于既有 128×128 最小探针，不代表业务评分预算。
单响应字节上限仍为 65,536；请求 30 秒、操作 300 秒、整批 3,600 秒，零自动重试。
沿用历史冻结 DeepSeek 输入 ¥2/百万、输出 ¥8/百万计算，新增 DeepSeek 最坏预算
约 ¥0.216；这是旧价条件估算，当前单价及 Embedding 免费状态尚未重新核实。
提议新增金额授权上限 **¥0.25**；实际账单不由本地估算保证，历史 unknown 不退额度。

请求额度采用保守增补提案：**recorded 累计上限 35→38、A4 总上限 6→8**，
proven 累计上限仍为 35，A3 已用 6 不变。六次全发送后的投影是 recorded 38、
proven 32、A4 8；保留 disputed 6，不把疑似重复直接删掉，也不借用 A3 余额。
这替代“只改 proven 口径即可继续”的做法，须由用户明确批准。

新增六次使用独立的 31,000/19,500 前瞻预算，不能直接复用 A4 原 6,200 总输出限制。
若核对完整 A4 的计划最大预留，已发 T1 索引 1,000/0 与失败 P1 8,000/2,000
仍占用，因此新旧合计应为 **8 次、40,000 输入、21,500 输出**。未发送的旧
R1/T1/V1 计划不能与新计划同时获发放权限；已失败/旧快照不得原地改预算或重置计次。
历史 token/费用总账保留，新增预算须在下一轮授权中明确追加，不能靠重新建 manifest
获得额外授权或清零已用请求。当前提案 `grants=[]`、`probe_grants=[]`，未安装到运行时。

#### 批准后的执行与停止条件

1. 先核对当前单价、指定 Test PG/Redis 身份和 T1 已持久索引。仅复用有效索引，
   不重复发送成功的 T1 索引；失效则停止并报告六次计划已不成立。
2. 重新创建 P1/R1/T1 preview，以新预算冻结 operation/snapshot/plan_digest，
   核对完整实际输入、token/图片计数及独立 grants。按 P1→R1→T1 串行执行，
   每次成功并通过冻结 oracle 后才继续。
3. 只新增 vision 绑定，形成持久候选；发放绑定该候选的单次 Probe grant。
   Probe 成功后在有效期内激活；旧 14 个接纳记录须仍可沿用，否则停止。
4. 在新修订重新创建 V1 preview，核对冻结图片、文本/检索材料无 oracle 泄露，
   计划恰为 Embedding 1 + vision 1；再串行执行。配置激活和预览本身不发送模型请求。
5. 任何空正文、`length`、超时/unknown、schema/oracle 失败、身份或预算变化，
   立即停止剩余付费发送、保留真实结果并计次；不自动重试、提上限或换模型。
   P1 在 5,000 失败只证明当前配置未通过，先分类再决定后续独立授权。
6. 汇总已有真实浏览器入口及其他冻结验收证据后，才判断 T083；六次成功不自动
   等于全部验收完成。T083 未满足时，T087 保持未执行/未勾选。

**待用户明确批准**：以上六次与逐项上限、追加 31,000/19,500 token 和 ¥0.25、
recorded 38/A4 8 的额度增补、vision-only 候选/一次 Probe/激活，以及失败即停止。
未批准前只做离线工作，不发放真实 grants、不激活真实配置；不得把本节当批准来源。

### G-P-20261022-A4-EXT-6 用户批准记录

2026-10-22 用户明确批准上一节六次请求及逐项预算：P1 8000/5000、R1 4000/4000、
T1 6000/4000、vision Probe 6000/1500、V1 Embedding 1000/1024维、V1 vision
6000/5000；新增总输入31,000/输出19,500，新增金额上限¥0.25。批准recorded上限
35→38、A4上限6→8、disputed 6保留，以及仅新增vision绑定的新修订、一次vision Probe及激活。
发送前核对当前价格与索引；任何失败即停止余下发送，零自动重试，失败不退计次；
P1 5000仍失败或Probe失败停止，不自动提额/换模型；V1隔离断言失败不发送。
不读`.env`、不改冻结判定条款、不commit。实际发送与usage以本轮逐请求证据为准。

## 8. 显式执行者恢复

每次个人操作持久保存execution_id与执行者身份，并持有同一数据根内的内核执行锁直到最终数据库写完成。显式恢复必须取得原始文件对象的空闲锁并核对持久身份，复制/缺失/替换的witness不是原执行者停止证明；Windows PID复用和Docker PID命名空间变化不能代替此证明。事务内轮换execution_id进行fencing，旧执行者迟到reserve/response/release写入拒绝。

本地操作员可通过`scripts/recover_learning_operations.py --operation-id <id> --execution-id <原执行id>`复用Setup本地管理授权进行协调。此命令不发送模型请求，不在启动或GET中自动运行；保留请求、计数、成功片段和unknown，确认旧执行停止后才释放遗留槽位。原未知操作不可原地重执行；后续操作仍需独立预览、确认和预算。资料OCR同样提供显式恢复，不能在仍执行或缺少原witness时假称恢复完成。

隔离恢复到新根后，如需协调原运行，可由本地操作者显式传入`--executor-proof-root <原始根>`，只核验原文件对象与内核锁，持锁直到目标事务提交。新根中的复制文件不能代替原始证据；没有原始证明时继续闭锁，不自动搜索其他路径。
