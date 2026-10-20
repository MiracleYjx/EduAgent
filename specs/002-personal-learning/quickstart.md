# Quickstart and Validation Guide — Approved Architecture / Development Guardrails

本指南供方案获批、功能实施后验证使用；当前已执行本地/Fake Spike，证据及能力边界见research P02/P03/P04/P08。T008—T014的基础验证与后续完整产品场景分开报告。契约详见 [API/UI](contracts/personal-learning-api.md)、[内容与评分](contracts/content-and-grading.md)、[本地生命周期](contracts/local-lifecycle.md)。

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
