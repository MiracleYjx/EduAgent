# v2.0 测试变更记录与验证映射（T145）

日期：2026-10-01；分支 `deepcode`；准备基线 `0bca025`；T144 成果 `d60a4b3` 已先行提交。依赖 T143 已完成复验，M0 未通过项仍保留。本文件沿用已有 TCR 的“必要性 → 变更边界/覆盖计划 → 验证计划 → 实际变更与验证”结构，先形成计划，不修改 tests/、夹具、业务代码或原 v1.0 断言，也不宣布新增测试已经失败或通过。

依据：spec FR/SC、plan Gate 1–19、data-model、contracts、[T144 盘点](v2.0-storage-inventory.md)、[v2 评测协议](evaluation.md)。T146 已获用户确认先备齐待教师复核样本，正式人工标签缺失；模型质量/教师统计验收不能用草稿真值代替。

## 1. 每批测试变更前的 TCR 模板

后续任务在新增/修改测试前追加一个具体 TCR 小节，并完成下列字段；本文件的模块计划不能代替该批具体记录。

| 字段 | 必填内容 |
| :--- | :--- |
| 标识 / 基线 / 日期 | Txxx、具体设计版/代码提交、真实 UTC 时间；源码版本用于追溯，不作为跨组件相等门禁。 |
| 必要性 | 当前可观察缺口、设计/合同引用、为什么现有用例不足；不因覆盖率数字或想当然增加测试。 |
| 已确认设计与未决项 | 当前字段/状态/事务/错误/历史语义；未决公共接口或 Provider 等决策先确认，仅暂停依赖部分。 |
| 变更内容与边界 | 具体文件和目标节点、新增/改动断言、原断言保留情况；若需改旧断言，说明合同实际变化与不可放宽部分。 |
| 输入与预期行为 | 固定 case_id、来源/标注版本、动作、预期输出/持久副作用与失败；多层只在各自实际责任边界证明必要事实。 |
| 隔离与替身 | 真实 PostgreSQL 临时 schema/单独库、本次文件目录、固定 M0 项目/端口；Provider 受控失败替身只证明业务分流，不证明 OCR/模型准确率。 |
| 先行失败证据 | 实现前执行目标节点，记录命令、实际数量/退出码及首个错误；失败必须来自目标缺口，不能把环境错误、缺插件或拼错节点当行为红灯。 |
| 实现后聚焦验证 | 同一目标节点/输入验证新行为及直接受影响的原入口；不复制算法成为测试 oracle。 |
| 实际执行 | 真实结果、失败/skip/warnings、隔离资源清理、模型/浏览器/包证据路径；未执行标 not_run，不预填通过。 |

新接口尚未存在时，可先形成针对预期行为的失败优先用例；不能把单纯 ImportError 当完整业务证明。实现入口可调用后，仍需证明正确动作、拒绝与事务结果。凡改测试，保留原 v1.0 回归；测试设计不重新裁定业务架构。

## 2. 分层与既有框架

- unit：现有 `tests/unit/models/`、`services/`、`ingestion/`、`retrieval/`、`grading/`、`ui/`、`workflows/`，验证实际边界、计算和状态；不逐行镜像实现。
- contract：沿用 `tests/contract/` 的 FastAPI/Provider/Parser 协议用例，核对生产者与直接消费者；Schema 通过不等于语义正确。
- integration：沿用 `tests/postgres_helpers.py`、既有迁移测试和真实装配模式；临时 schema 的 Inspector 查询必须显式限定 schema，迁移版本表也在本次隔离范围。文件、并发、重启/新 Session 检查实际落库事实。
- benchmark/真实验收：按 T142 固定输入/教师标签、3 轮模型质量、3 冷/5 暖性能与全量失败留存；人工校正、真实浏览器、真实 EXE 和一致恢复各有实际证据。
- 现有两类阅卷/检索 Benchmark 的读取格式保持；v2 模板位于独立输入目录，不把未运行模板写成 benchmark/results 的成功记录。

## 3. 必须保留的 v1.0 回归

| 原行为 | 现有模块（实际存在） | 增量实施不得破坏 |
| :--- | :--- | :--- |
| JWT/RBAC、课程归属 | test_auth_contract.py、test_auth_api_contract.py、test_admin_api_contract.py | Teacher/Student/Admin 边界及已有越权响应。 |
| 教学上传、摄取、Ready/Failed | test_document_parsers.py、test_knowledge_base_upload_contract.py、test_teacher_setup.py；unit/ingestion | 原 PDF/TXT/Markdown 主路径，不因 OCR 未安装阻断纯文本。 |
| Provider 与四检索模式 | test_embedding_provider.py、test_retrieval_contract.py、test_question_agent_retrieval_modes.py | 无新增范围时原默认语义、来源身份、检索模式和结构化结果。 |
| 候选与 I01 守卫 | test_question_generation_api_contract.py、test_question_update_api_contract.py、unit/services/test_question_service.py | 待审核、Approved 内容不可编辑、合法修订、元数据例外；发布保护需另扩展。 |
| 考试参加/重复提交 | test_exam_participation_contract.py、unit/services/test_exam_service.py、test_submission_service.py | 身份、课程、考试状态、重复提交，200 题请求边界不改成 100。 |
| 规则/主观评分与事务 | test_grading_api_contract.py、test_grading_pipeline.py；unit/grading | 客观题不调用 LLM、主观结构化结果、失败非零分、整批原子落库。 |
| 复核/恢复/诊断 | test_reviews_api_contract.py、test_workflow_api_contract.py、test_langgraph_grading_workflow.py、test_submission_finalization.py | 轮次身份、迟到拒绝、最终成绩、诊断只读与失效、重启真实记录。 |
| 来源与迁移 | test_question_source_migration.py、test_review_round_migration.py、test_audit_migration.py；unit/models | live_chunk_id SET NULL 与快照保留、原 FK、历史 NULL、不伪造身份/时间。 |
| 结果权限及 UI | test_results_api_contract.py、test_teacher_results_ui.py；unit/ui | 学生仅本人、教师课程范围、真实空态和失败信息；保护用户未提交 UI/测试改动。 |
| Docker/静态 | test_m0_smoke.py、test_docker_demo_config.py、test_run_demo.py；mypy/ruff | 固定隔离项目、配置/迁移/readiness/Redis，真实失败不忽略。 |

表内 test_*.py 位于 contract/integration；同名 unit 已注明相对层。后续按目标节点选择最小充分回归，不为文档/输入数据改动重新运行全部业务套件。

## 4. 增量模块的必要性、先行用例及完成证据

以下是设计已确认部分的测试计划，不一次性创建未来阶段全部测试。标为“拟新增”的模块仅是建议位置，具体批次 TCR 再锁定文件/节点；不在此冻结新公共接口名。

| 模块 / 必要性 | 先行验证目标行为 | 框架与原模块 / 拟新增 | 实施 → 证据任务 |
| :--- | :--- | :--- | :--- |
| 持久文件与共享引用 | 重名不覆盖；上传失败保留真实状态/材料；file_id 映射与课程/答卷授权；共享引用/发布保护阻止删字节；缺失与未知区分 | 现有上传 contract、来源 models；拟 unit/services/test_file_storage_service.py、contract/test_file_storage_contract.py、integration/test_storage_migration.py | T147–T151 → T152/T191 |
| 同集备份/隔离恢复 | 排空在途写入；DB+文件同集；隔离核对再切换；缺文件/校验失败保留原环境；故障点和操作收据真实可查 | 拟 integration/test_backup_restore.py；本次只准备计划，不对用户库执行恢复 | T151 → T152/T191 |
| 导入/校正/并发确认 | 文字/扫描/混合/跨页/错序/无答案，50/51 页边界；原卷不进 Chunk；Uploaded 不冒充入库；null 边界合法、真实页不可空；重复 commit/并发不重复题图或正式题 | 现有 parser/upload/teacher_setup；拟 contract/test_paper_import_contract.py、unit/services/test_paper_import_service.py、integration/test_paper_import.py | T153–T159 → T160 |
| OCR 可选适配器 | OCR 禁用/缺依赖/真实错误区分空白页；Schema、区域坐标、置信度合法；保持原文字路径 | 现有 parser；拟 contract/test_ocr_provider.py、unit/ingestion/test_paper_pipeline.py；选型 T155 未决前不写具体 PaddleOCR 断言 | T155/T156 → T160/T168 |
| 章节生产与范围检索 | 不跨已确认边界重叠；章/节同课程；真实教师确认；四模式均 Top-K 前过滤；知识点 JSONB 精确成员；旧未知/无条件默认回归，无不足时全课程回退 | 现有 chunking/retrieval/knowledge-base contract，真实 pgvector/JSON 查询 integration | T161/T162 → T169 |
| 父题改编与来源 | 同课程、无环、父题不覆盖；沿本次真实教学引用保存；来源/候选/意见事务；改图/条件后重核验、依据不足不得批准 | 原 Question Agent、生成 contract/source migration；拟 integration/test_question_adaptation.py | T165/T167 → T169 |
| 语义报告与修订 | 四类分项、真实证据；revision/轮次匹配；改回旧内容仍失效；迟到通过不能覆盖当前失败/运行中；教师处置留原问题/身份；旧数据不回填假通过 | 原 question update/workflow contract；拟 unit/services/test_content_validation_service.py、integration/test_question_validation.py | T163/T166 → T169/T168 |
| 图片理解/人工核对 | figure/table/diagram 原图/顺序/归属；不清晰/不支持/真实失败转人工且保留；图像修订、核对轮次及正式题继承绑定；共享字节不继承父题确认 | 拟 contract/test_vision_capability.py、integration/test_image_assessment.py；以真实 Provider/教师标签另评质量 | T154/T163/T164 → T160/T169/T168 |
| 历史关联/本场分值 | T144 关联数保留；排序依据核对；同题两场不同满分；Unknown 不能由当前题库回填或重评；数据库升级/回滚边界按正式迁移决定 | 原 models/exam/迁移；拟 integration/test_exam_question_migration.py | T170/T171 → T179 |
| 条件组卷/意图 | 成功满足数量/题型/覆盖/Decimal 总分；确无解与策略未找到分开；失败保留选题事实但保存合法本次意图；非法/越权/冻结不写意图；重启读意图 | 原 exam contract/service；拟 contract/test_exam_assembly_contract.py、unit/services/test_exam_assembly_service.py | T172 → T179 |
| Rubric 换算/尾差 | 28 位 Decimal、中间不量化；0.005；正/负尾差均需真实教师确认；未确认阻止发布；定性标准不猜权重；本场标准不二次缩放 | 原 objective/structured grading/result aggregator；拟 unit/grading/test_exam_scoring.py | T173/T174 → T179 |
| 发布冻结/并发 | 发布与编辑/修订/资产变更同场锁；Published/Closed/Archived 及答卷历史保护；内容/题序/题图/知识点、本场标准稳定，不建完整题目版本 | 原 I01/考试/并发 integration；拟 integration/test_exam_publication_freeze.py | T175–T178 → T179 |
| 教师统计与学生推荐 | 按有效最终样本统计，逐题/多知识点分母明确；失败/待复核不当零分；同题不同满分、零样本；本人/课程边界，资料/练习真实来源 | 原 results contract/loaders；拟 unit/services/test_exam_analysis.py、integration/test_v2_results_analysis.py、unit/ui 增量 | T180–T183 → T184 |
| UI 与 EXE | 三页样板后七类业务页；真实操作/错误空态；真实打包启动/子进程所有权；缺依赖/配置/网络/迁移失败；重启、退出不停止用户服务 | 原 unit/ui 与 app；拟 unit/packaging 启动器行为；真实 Windows 包/浏览器另验 | T185–T188 → T186/T189/T191 |

## 5. FR / SC / Gate 映射

| 需求 | 上表验证模块 | 证据 / 门禁 |
| :--- | :--- | :--- |
| FR-010 扩展、FR-044/045 | 文件/备份/导入 | T152/T160/T191；Gate 13；SC-010/014 |
| FR-018 扩展 | 父题改编、图片、语义报告 | T169；Gate 12/19；SC-011 |
| FR-020/028 扩展 | I01、语义报告、发布冻结 | T169/T179；Gate 15/19；SC-011/012 |
| FR-024/025 扩展 | 章节/四模式范围查询 | T169；Gate 19；SC-011 |
| FR-039/040 扩展 | 教师统计/学生推荐 | T184；Gate 19；SC-013 |
| FR-041/042 | 导入/校正、OCR | T160/T168；Gate 10/11/13；SC-010 |
| FR-043 | 图片核对/复用、发布/答题/评分显示 | T160/T169/T179；Gate 12；SC-010–012 |
| FR-046/047 | 父题来源、语义核验及失效 | T168/T169；Gate 19；SC-011 |
| FR-048 | 组卷/意图/预览/拒绝 | T179；Gate 14；SC-012 |
| FR-049 | 历史、本场分值、Rubric、冻结 | T171/T179；Gate 15/16；SC-012 |
| FR-050/051 | 独立统计、本人结果与来源推荐 | T184；Gate 19；SC-013 |
| FR-052 | EXE/依赖/配置/退出/恢复 | T189/T191；Gate 17/13；SC-014 |
| SC-010 | 已确认题原文件/页/题图可追溯、重启；逐字段教师核对 | T160/T191；未经确认不入正式题库，缺答案不批准 |
| SC-011 | 两条出题路径、四类错误及旧答案失效；未解决不得批准 | T169/T191；不以 Schema 通过代替内容质量 |
| SC-012 | 可满足/不可满足、同场依据一致、历史冻结 | T179/T191；不可满足不发布，不能自动放宽 |
| SC-013 | 最终/待复核/失败/缺依据及有效分母，与教师独立核算一致 | T184/T191；缺正式教师真值时不可声明通过 |
| SC-014 | 真实包、同集恢复、七类页操作/截图 | T189/T191；源码运行不替代 EXE |

| Gate | 最小充分证据 / 回归责任 |
| :--- | :--- |
| 1 | 原固定隔离 M0 三容器 healthcheck、迁移、Redis；T143 未通过完整入口，后续补验不能偷改标准。 |
| 2 | 原支持资料摄取来源；T152/T160 扩展不破坏知识库主路径。 |
| 3 | 四模式同一检索集、规范产物；T162/T169 范围前过滤，T190 真实回归。 |
| 4 | 候选不得绕审核；T165–T167/T169。 |
| 5 | 原客观/主观路由与本场评分；T176/T179/T190。 |
| 6 | 低置信度复核/恢复；T177/T179/T190。 |
| 7 | AI JSON→Pydantic→DTO；OCR/图片/语义均覆盖合法/非法/真实故障。 |
| 8 | 保留 v1.0 资源/P95 条件与历史欠缺；T190 如实测量，不以 v2 预算替换。 |
| 9 | 原 Benchmark 格式、失败留存；v2 独立目录及 T142 格式，不冒称现有看板已支持新格式。 |
| 10 / 11 | 导入状态、拆题/校正逐字段和重启；T160。 |
| 12 | 原图理解/人工核对与预览/答题/评分/结果一致；T164/T169/T179。 |
| 13 | 文件、迁移、同集恢复与失败；T152/T160/T191。 |
| 14 | 合法组卷意图、实际条件和失败保留；T179。 |
| 15 / 16 | 题序/本场分值、冻结、同比例标准/尾差、客观/主观/复核/汇总；T179。 |
| 17 | 真实 Windows 包首次/后续启动及完整资源采样；T189。 |
| 18 | 三页代表样板至七类逐页 UI 操作；T185/T186/T191。 |
| 19 | 语义铁律、范围/父题、教师独立统计、真实学生反馈及来源；T169/T184/T191。 |

## 6. 执行与停止规则

具体批次先追加 TCR、锁定已确认设计，再写能暴露该缺口的用例，跑目标节点并记录红灯，随后实现/聚焦回归。环境失败先定位，不能改断言、加测试特例或以 skip 制造成功。聚焦检查足以证明后，除新修改/失败/跨组件影响外不扩大重跑。

代码或测试实际变化后执行仓库所需 pytest、mypy、ruff；迁移、容器、Provider、包、浏览器按实际责任分别验证。计划自检/数据结构校验不能升级成模型质量、教师真值或系统验收。失败原样保存，不以旧报告、替代文件、无依据默认或无界重试兜底。

OCR 具体适配器 T155、质量阈值 T168、教师标注和独立统计基准尚未完成；选择/真值补齐前，仅准备独立于这些取舍的用例结构。Redis 继续沿既有依赖，不自行取消；服务端冻结、JSONB 语义核验/图像证据及 Exam.assembly_constraints 按已经确认的合同执行。

## 7. 本任务实际产出与验证

本次仅新增此记录，核对现有模块/合同引用、FR/SC/Gate 覆盖及任务依赖。未新增或修改 pytest 用例、未运行假红灯、未声明所有未来设计已锁定；原测试和用户工作区文件保持不变。T145 勾选表示 TCR 计划/映射已建立；各批具体 TCR、先行测试和真实验收仍随对应实施任务完成。

## 8. T147–T149：本批具体 TCR（实施基线 9912ece）

本批按用户授权实施持久文件服务、授权读取和教学上传接线。T146 仍未勾选，不依赖教师真值评测；原 8 个 UI/README/测试改动保护。无新 Provider、导出格式或 UI。真实 UTC、命令及结果在执行后追加，不预填通过。

### 必要性及已确认边界

现有 multipart API 在系统临时目录落盘；UI 直接登记 Gradio 路径；数据库文件没有持久元数据或 ExportFile。现有读取将相对路径当工作目录路径，重启/根目录切换无法可靠读取。文件 GET 尚不存在，字节与引用失败也没有归属收据。T137/T140 明确稳定资源投影、相对定位、JSONB 元数据、导出三选一归属、写入收据及事务级定位锁，需直接验证这些行为。

用户已确认 ExportFile 写入/登记/授权服务供后续入口调用；当前没有业务导出生产者，不改无课程/考试/答卷归属的 Benchmark 脚本。新建公开元数据路径本批已确认仅关联已授权持久文件，外部材料走上传；此前既有历史路径保留，T150 才执行显式迁移，不把新服务测试当历史迁移/恢复验收。

### 文件与覆盖

- 新增 tests/unit/services/test_file_storage_service.py：开发/EXE 根配置、路径越界、同名新身份不覆盖、真实摘要/类型及 prepared→written→committed 收据、写入/DB 失败留下真实归属；缺失与未知分开、共享引用/未完成收据拒绝删除、旧定位精确读取。
- 新增 tests/contract/test_file_storage_api.py：真实文档/导出读取，未认证 401、其他课程/学生 403、不存在/缺失 404、未知定位独立错误、writing/failed 409；下载名/类型正确，响应不泄露内部定位。原页/题图模型未实现部分留 E2 接入，不伪造资源。
- 新增 tests/integration/test_file_storage_migration.py：临时 PostgreSQL schema 从 0012 升级，Document JSONB 可空、旧路径/状态原值保留；ExportFile FK/恰一归属/状态约束、升级/降级，仅删除本次临时 schema。
- T149 在新服务/API用例验证 API 和 UI 共用服务边界：授权先于落盘、真实持久相对路径、新 Session/服务实例重读、解析失败留原稿、DB 失败收据保留；PDF/TXT/Markdown 主路径及元数据空登记保留。
- 如需修改 tests/contract/test_knowledge_base_upload_contract.py，只把 Path(document.storage_path) 改为服务解析持久根，并注入本次 tmp_path；保留真实字节、Ready/Failed、片段/向量及权限全部原断言。这是路径合同实际变化，不放宽可读性断言。
- 不改用户已修改 tests/unit/ui/test_gradio_app.py 或新增 test_question_bank_view.py；不改 Benchmark、旧数据、评分或真实模型评测。

### 隔离、先行与后续检查

SQLite 只用于请求/基础行为，不能证明 PostgreSQL 定位锁/JSONB；真实迁移和锁在既有 helper 创建的唯一临时 schema 检查，不升级 public。文件测试注入 pytest tmp_path；pytest 临时目录/缓存/结果放本批 .cache，不写默认业务根。无需新增或升级依赖、外发材料或调用模型。

先执行根配置的行为用例证明当前无持久配置；新增服务模块尚不存在的 ImportError 只记录入口缺失，不作完整业务红灯。T148 接口挂载前以有实际资源的请求证明 404 缺口，再跑新增授权/生命周期用例。每次实现后跑对应目标和受影响的既有知识库、认证/结果模块；本批影响持久模型/公共读取/上传边界，完成后执行既有 pytest、mypy、ruff 必验检查。Docker 配置检查和应用挂载需证据；T143 M0 构建/配置未通过仍保留，不凭源码/配置通过宣布真实三容器门禁成功。

实际结果：先行/首轮及最终结果见本节后续记录。

T147 首轮：13 passed / 1 failed / 1 skip（本机不允许建立测试符号链接）。失败用例把所有 fsync 都设为失败，却要求失败收据能够落盘，不能证明目标的单次原稿写入故障。拆成“原稿 fsync 一次失败、收据可保存”和“收据也持续无法更新”两种边界；前者保持 failed 收据断言，后者明确原收据与二次失败，不放宽业务成功断言。

T147 部署回归：旧 test_docker_demo_config.py 用“没有任何 Backend 卷”表达“不挂主机模型缓存”；与本批已确认的业务持久卷不兼容。仅改该条断言为唯一 storage_data:/app/storage 命名卷及 STORAGE_ROOT=/app/storage，保留无主机模型缓存、无构建密钥、云端参数/缺项失败的原断言。迁移行为已通过真实隔离升级/降级；T148 先行文件 GET 以真实字节/资源和 JWT 得到 404，记录入口确实缺失。

T149 路径决策补充（用户已确认）：公开元数据请求只可引用持久根内已登记且该教师可读取的原稿/就绪导出，外部材料走正文上传；历史已落库定位保持原值。服务保留原可信内部登记用法供旧脚本/旧夹具读取，API 改走明确的受限登记方法，不能从公开接口注入本机任意路径。新增 contract 用例验证本机外部路径、根内未登记文件、他人文件拒绝，以及本人的共享定位可读取、空元数据登记不伪造文件。共享新引用须取得同一 PostgreSQL 事务定位锁。

本次追加覆盖仍属原 TCR 范围：tests/unit/services/test_file_storage_ingestion.py 检查授权在落盘前、DB/收据失败事实、原稿内容不替换；服务单元用例补登记身份不可重写及未知主机定位；真实 PostgreSQL 用例补同一路径锁冲突及 rollback 后释放。Export GET 夹具改由实际导出服务写入 exports，保留实际字节/角色所有权断言，避免使用 Document 路径充当就绪导出。

身份检查追加后，新增共享引用用例尝试用同一已登记 Document 再写 pending 字节，被正确拒绝；夹具改建新 Document 身份，仍保持有效引用/未完成收据均拒删的原断言。另三个追加用例缺少 FileStorageError 导入，已修正导入；这轮为 20 passed / 4 failed / 1 skip，不能当作业务全部通过。

共享历史引用复查发现一个直接边界：A 原稿被登记为 B 共享资料后，题目来源快照可能只保留 B 的身份；A/B 元数据都解除后，只有 A 的收据无法证明 B 的历史引用。补一个实际共享登记→来源快照→解除资料→拒绝清理的 contract 用例；每个新共享身份也需记录真实关联操作收据。无任何登记/收据的根内材料不能证明可清理，补拒删未知材料与已提交且确无引用原稿可清理的服务用例。追加前仍不执行真实用户文件清理。

首轮全量必验：1616 项，1611 passed / 3 failed / 2 skipped。三项均是本批合同变化直接影响的旧测试前提：test_audit_migration.py 的 head 后 -1 现在只退到 0012，并未撤销 audit；test_m3_migrations.py 的固定 head 仍是 0012；test_teacher_setup.py 用不存在的上传路径登记元数据。修改前锁定：audit 用例显式退到原 audit 父版本 0011，保留原 audit 存在/不存在断言；迁移链仅追加 0013→0012 并移动 head，旧 revision 结构断言保留；教师准备流程沿用本来只登记元数据的语义，移除虚构路径并增加 null/未知文件断言，继续全部课程、审核、组卷及权限原断言。真实原稿上传/授权关联由本批 contract 和原上传测试覆盖，不把空元数据说成实际文件上传。

共享历史红灯：关联 B、保留 B 的题目来源快照、删除 A/B 后，原实现未拒绝物理清理；已追加每个共享身份的关联收据。修改后的新增模块与直接 API/UI/迁移回归 59 passed / 1 skip；全量需基于最终源码再运行，本段不是最终通过声明。

迁移 TCR 收尾：真实 schema 升降级已验证；在同一用例补直接数据库输入，核对 ExportFile 恰一归属、audience、ready 前提、JSONB 对象 CHECK 及课程 FK 的 RESTRICT。断言依据数据库 SQLSTATE/指定 FK，而非复写服务判断；仅本次临时 schema 插入/删除，原路径/Ready 状态保留断言继续执行。最终业务源码全量已通过，此项仅补已有迁移用例的数据库约束证据，运行对应聚焦验证。

### 本批实际结果（2026-10-02，UTC 记录）

先行红灯留在本批缓存：缺少 STORAGE_ROOT、文件 GET 404、导出/持久上传入口缺失、公开路径登记返回 201、共享身份历史来源未拒删。不是只以 ImportError 证明完成。首轮全量三项前提失败和夹具/转义错误均保留；修改上述前提后原流程与迁移聚焦 18 passed。

- 最终业务源码全量：1617 passed、0 failed、0 errors、2 skipped、19 warnings；pytest 347.78 s，外层 355.884 s；2026-10-01T17:26:54.946042+00:00 开始。跳过项为 M0 缺四项隔离变量及本机符号链接权限；未记为 M0 或符号链接验收成功。
- 新增模块和直接 API/UI/迁移聚焦：59 passed、1 skipped；共享定位/历史来源、提交后收据失败、重名/原稿身份、实物 bytes、权限、未知/缺失均核对。收尾补 PostgreSQL CHECK/FK/锁的同一迁移模块：2 passed，旧原稿路径与 Ready 状态原值保留。
- mypy backend/app：145 source files，无问题；2026-10-01T17:32:50.831446+00:00，2.803 s。Ruff backend/ tests/：通过，0.140 s；补迁移断言后再次通过。
- 隔离数据库 alembic upgrade head 与 check、最终 check 均退 0；实际 schema 升降级、JSONB 与导出约束、同定位 PostgreSQL 锁冲突和 rollback 释放通过。
- 实际 Docker Compose config 验证唯一业务持久卷 storage_data:/app/storage 和 STORAGE_ROOT=/app/storage；不等同容器 build/启动/三容器门禁。T143 的 M0 未通过结论保留，真实 EXE、文件迁移和备份恢复未执行。
- 本批独立测试数据库/Redis已清理；业务库及现有容器未升级/搬移/清理。8 个用户已有文件 SHA-256 全部与实施前一致，T146 仍待真实教师标注。
- 原始命令、时间、首轮/最终 JUnit 和日志：.cache/e1-t147-149-20261002（忽略目录，本机保留）；可提交摘要见 docs/validation-report.md。测试对象是当前工作区，包括保持原样的用户未提交成果，不宣称干净 checkout 已独立验证。

## 9. T150–T152 具体 TCR（2026-10-02）

基线：788599d；工作区另有 8 个用户 UI/README 文件，基线摘要保存在 .cache/e1-t150-152-20261002/baseline.json，禁止覆盖或提交这些文件。

必要性：既有文件服务已证明上传/授权/共享保护，但尚无历史迁移脚本、真实 PostgreSQL dump/隔离恢复和持续停写窗口，原用例不能证明复制后事务失败保留原定位、共享历史身份、同集数据库/文件恢复或维护标记拒绝写入。依据 data-model §14、file-storage 迁移/备份契约及 T144 盘点。

已确认方案：用户本批确认离线维护 CLI、停止写进程后核对数据库连接并持有阻写表锁、文件维护标记；仅恢复到新建隔离数据库/目录，验证报告后人工授权切换。工具复用现有管理员 JWT（环境变量传入），验证当前启用账户与管理员角色，收据记录真实用户；不改变课程文件 GET 权限，不新增备份表或依赖，不清理真实旧副本。

变更边界与覆盖：
- 新增 tests/unit/services/test_storage_migration_service.py：共享绝对/旧相对定位完整事务更新；保持 Document.status/file_id/来源；重复执行验证字节后跳过；真实缺失与未知分列；复制中断、摘要不符、提交失败保留旧引用/字节及失败收据；教师/停用账户不可执行维护。原文件服务断言全部保留。
- 新增 tests/contract/test_storage_maintenance_contract.py：实际 CLI 入口/参数、JWT 不出现在参数或报告、manifest 规范路径/阶段/complete 约束、隔离恢复报告及文件写入维护拒绝；不镜像算法。
- 新增 tests/integration/test_storage_migration.py、test_backup_restore.py：真实 PostgreSQL 共享定位事务/并发锁、数据库存在写连接或锁无法排空时真实失败；标准 pg_dump/pg_restore 在本批新建数据库验证同集课程/题目来源/考试/答卷/评分/复核与文件身份/授权归属，四目录实物及真实操作收据保留；缺失/未知/未归属/篡改/复制失败/恢复失败/已存在目标均不宣称 complete/verified，不改原环境。
- 直接回归：既有 test_file_storage_service、test_file_storage_ingestion、test_file_storage_api、test_file_storage_migration、test_knowledge_base_upload_contract；上传重启、同名不覆盖、资源越权/共享历史来源拒删及 PDF/TXT/Markdown 原入口继续有效。

隔离与替身：单元使用既有 SQLite 夹具但不声称 PG 锁通过；集成使用真实 PostgreSQL 临时 schema 或仅本批新建数据库、tmp_path 文件根、现有容器内 PostgreSQL 16 标准客户端。故障注入限复制/SQL commit/清单写入的真实边界；不修改实际开发数据库、原材料、.env 或用户服务。恢复目标只新建，失败材料保留；测试结束只清理自身命名资源。

先行验证：新增目标先执行记录实际失败；入口尚无时 ImportError 只说明入口缺失，后续须证明目标动作、拒绝和 SQL/实物结果。实现后同节点聚焦；项目必验 pytest、mypy backend/app、ruff backend/ tests/ 并记录 skips/warnings，不将 M0/原卷题图/EXE/模型质量视为已验收。

实际结果：待各任务运行后追加。T150 → T151 → T152 顺序推进；仅完成后勾选对应任务，T146、T160/T191 与就绪检查标记保持原值。

T150 先行：6 failed，迁移模块不存在；该入口失败不是完整行为证明。实现后新增单元与真实 PG 8 passed，原文件服务直接回归合计 30 passed / 1 skipped；修正 Literal 类型和导入顺序后静态检查通过。证据：.cache/e1-t150-152-20261002/t150-focused.xml。

T151 首轮合同：8 failed；包含维护标记存在时仍允许实际文件写入的行为红灯、CLI 缺失及清单 Schema 缺失。实现后与迁移回归 14 passed。真实集成首轮 8 errors，夹具错误提供 GradingResult 并不存在的 grading_status/status 字段；逐字段核对当前模型后移除虚构字段，保留所有真实分数、review_status 和复核断言；该轮不是业务红灯或通过证据。

第二轮仍为同一夹具错误（不存在的 status）；不改业务模型来迎合夹具，不将此错误作为实现失败证据。

T151 复查追加：SQL 提交后的收据确认必须持有同一物理定位锁/数据库连接，才能被离线窗口排空观察；新增真实 PG 用例在提交后收据阶段仍未关闭业务 Session 时拒绝备份，不只测试请求/SQL commit。另补已存在目标数据库、恢复报告可靠写入失败（保留隔离停写标记）、新旧格式版本用于追溯而非锁步，以及真实 CLI 从环境变量加载管理员 JWT。新增前记录本条，原评分/权限/来源断言不放宽。

最终聚焦首次命令误用不存在的 test_file_storage_contract.py，0 项执行；已按实际 test_file_storage_api.py 修正，不当作行为失败或通过。

扩展聚焦首轮 76 passed / 1 failed / 1 skipped，失败是新 CLI 用例在临时 User 对象上清空 roles 导致 SQLAlchemy 父对象被回收；改为持有真实 User 对象后再清空，保留“JWT 旧声明不能绕过当前管理员角色”的原断言。

T151 真实隔离首轮修正后 8 passed（19.57 s）；扩展聚焦除 CLI 夹具生命周期一项外 76 passed / 1 skipped，生命周期修正后该 CLI 节点 1 passed（5.27 s）。实际标准 pg_dump/pg_restore、同集关系、SHARE 写入拒绝、收据连接未排空拒绝和目标失败保留已证明；进入 T152 运行最终全量必验。

T152 复查必要补充：维护标记可能在原稿写入已失败之后出现，保存失败收据又被维护拒绝。新增故障用例必须仍返回最初 FILE_WRITE_FAILED、保留原 OSError 原因与原收据/字节，不能被第二个维护错误覆盖；只修正本次维护边界的错误传播。

T152 全量首轮：1645 passed / 1 failed / 2 skipped / 20 warnings，pytest 439.92 s，外层 450.034 s。失败为新 CLI help 用例使用 Windows 默认 GBK 解码子进程 UTF-8 中文，读取线程 UnicodeDecodeError 使 stdout=None；不是业务 CLI 失败。修改前确定仅在该新用例固定子进程 PYTHONIOENCODING=utf-8 和捕获 encoding=utf-8，保留 exit=0/--token-env 原断言，不吞解码错误或放宽内容检查。

T152 收尾复查：Doc/Export 可共享同一物理原稿；迁移按首个 Doc 选 uploads 会违反已确认的导出独立 exports 目录。追加跨资源共享迁移用例，预期一份字节/完整事务/稳定身份，同时落 exports；只调整已有目标目录选择，不修改原业务格式或权限。追加在测试前，先保留行为红灯，再聚焦全部迁移与 PG 锁验证。全量 1646 passed / 2 skipped / 19 warnings 是本次收尾目录修正前源码，不能标为收尾后的再次全量结果。

T152 实际收尾：UTF-8 help 合同 8 passed；修正后全量 1646 passed / 2 skipped / 19 warnings（392.41 s），mypy 150 source files 和 Ruff 通过。跨 Doc/Export 目录先行实际失败（落 uploads），仅目录分流修正后迁移/真实 PG 10 passed（3.69 s），mypy/Ruff 再次通过；全量结果明确为该收尾目录修正前，不将聚焦计数加成一轮全量。原始错误覆盖红灯已复现并修复，原 OSError/FILE_WRITE_FAILED 保留。只清理本批临时库/Redis，保留缓存、原库 0012 与原服务。


## 10. T153–T154 具体 TCR（2026-10-02，基线 baf0803）

### T153 必要性、边界及先行覆盖

现有模型缺少原卷/原页/暂存题，且 Question 尚无解析与真实来源字段。按已确认 G03/G06 实施，不建暂存资产表、不回填历史未知来源或批准时间，不实现 T157/T158 的解析和整批确认。

拟新增 tests/unit/models/test_paper_import_models.py、tests/contract/test_paper_import_schemas.py、tests/unit/services/test_paper_import_foundation.py、tests/integration/test_paper_import_migration.py。验证原路径关系投影、JSONB 校正记录重读、独立状态与唯一/FK/CHECK、页集合及像素坐标/金额/空值、知识资料条件非空及 paper_source 不能列表/摄取/检索、人工/AI 新题真实分类、解析省略/清空/Approved 拒改、真实批准/退修时间。迁移只在独立 schema/数据库测试，历史 Question 的三列保持 NULL，原考试/分值/来源快照不变；降级遇导入数据拒绝而非丢失原材料。

先写目标用例并记录实际失败，再实施和聚焦验证。T154 的学生展示承载待用户决策；其具体测试边界在决策后追加，再写测试。全批完成后执行既有 pytest、backend mypy、Ruff 与 Alembic 元数据一致检查，保持 v1.0 断言及已有 8 项工作区改动。源码/迁移测试不代表 OCR/真实教师质量/完整导入 UI 验收。


### T154 独立于展示决策的具体边界（写测试前）

拟新增 tests/unit/services/test_question_asset_service.py、tests/contract/test_question_assets_api.py、tests/integration/test_question_asset_persistence.py。覆盖真实 PNG/JPEG、原页像素坐标及裁切输出、同导入/真实来源页、稳定暂存 id/file_id 与无正式行事实、完整数组及最多五图、教师/课程授权、Approved/历史保护、原图关联重读、终态来源保留、G05 受校验记录保留及 A→B→A 修订失效。原页与源卷对学生拒绝；题图对学生的成功展示用例在展示持久决策确认后补充，不假定批准。

新模型改变文件枚举，必须同时补原页/暂存/正式资产在 GET、迁移、备份及删除中的实际引用。隔离 PostgreSQL 验证 JSONB、FK/唯一/形状约束，复用已建立的维护窗口/真实 dump 恢复；空 E2 表不得破坏既有 E1 行为。补共享文件的多个业务身份、暂存到正式定位投影、迁移仅更新内部定位、原校正/核对事件不反写、备份 manifest 真实归属及恢复读取。保留原 E1 断言，不清理业务材料或切换配置。G05 完整调用、人工语义处置、整批入库及 UI 仍分别属于 T163、T158/T159。


迁移图兼容用例调整（修改前）：tests/unit/models/test_m3_migrations.py 的链条和当前 head 随实际新增的 0014/0015 向后追加；保留所有旧 revision 对象、枚举、升降级边界断言，新对象真实行为由本批 PostgreSQL 用例覆盖。新增普通教师图像上传、图序重排、G05 JSONB 原错误重读和实际内容往返修改失效用例，均属于本节已锁定原图/校正/冻结范围。展示成功断言仍待持久承载决策，不放宽拒绝源卷/完整原页的要求。


补充同一生命周期覆盖（新增前）：即使教师解除当前题图关联，G05 的旧轮次/核对仍可引用原资产，清理前必须核对这些持久引用；新增删除关联后历史证据仍阻止字节清理及备份保留原材料的断言。解除关系不删除文件，技术失败/未知事实不被改写。


### 首轮全量发现的旧迁移夹具边界（修改前）

full_pytest 首轮：1677 collected，1673 passed、2 failed、2 skipped（432.471 s pytest，外层 441.892 s）。两项失败为 test_question_source_migration 与 test_review_round_migration；根因是在 0010/0009 的旧 schema 中用当前 Question ORM 插入，INSERT 带上不存在的 analysis/source_type/frozen_at/image_assessment。不能修改旧迁移补新列或让业务 ORM 猜旧库。

本次仅把这两个用例的历史题目生产者改为按当时实际反射表写入；保留用户、课程、客观/主观题、考试、答卷、答案和所有旧枚举/值、升级/降级/重升、索引/FK/来源未知/评分不变断言。新增 tests/integration/legacy_question_fixture.py 仅服务真实旧迁移夹具，不改通用 seed_submission 或业务代码。先聚焦重验两项失败，再运行最终全量。

### 实际验证与任务边界

T153 首轮缺模块/枚举产生三项 collection error；实现后聚焦覆盖真实字段、原路径关系投影、知识库隔离、解析审核保护及真实 PostgreSQL 0013→0014→0013。修复 UTC 导入遗漏和重复 enum CHECK 的迁移 DDL 后，通过目标检查，不改历史未知来源或时间。

T154 独立部分的先行用例暴露资产服务入口缺失，后续实际 PNG 裁切、同导入定位、最多五图、教师权限、稳定身份、终态保护及重读通过。0015 与 ORM 一致；原页、暂存资产和正式资产真实进入文件 GET/迁移/备份枚举。实际 dump/隔离恢复用例包含源卷、原页、暂存图、正式图、共享原页字节、导入正式资产定位投影和持久核对历史；解除关联后历史轮次仍阻止字节清理。所有物理删除只针对测试材料。

旧迁移夹具按当时反射表写入后，原两项失败用例 2 passed / 4 warnings（8.13 s pytest），原断言保留。基于收尾全部源码和测试再次全量：2026-10-02T04:27:27.412048+00:00 开始，1679 collected，1677 passed、0 failed、0 errors、2 skipped、23 warnings；pytest 431.89 s，外层 441.115 s。两项 skip 为 M0 缺隔离四变量及 Windows 符号链接权限，不将跳过记作验收成功。

最终 mypy backend/app：160 source files 通过（16.236 s）；Ruff backend/tests/本批两迁移通过（0.191 s）；独立库 Alembic check 通过（1.530 s）。所有命令、首轮失败和最终 JUnit 保存在 .cache/e2-t153-154-20261002。验证对象包含用户已有 UI/README 成果，9 项保护文件（含 .env）SHA-256 均与实施前一致。

本批数据库 eduagent_e2_import_3d186fa5073b 与独立 Redis 已经核对零连接/任务标签后清理，原业务库仍 0012_audit_logs，原 PostgreSQL/Redis 仍 healthy；没有应用业务迁移、切换配置或删除业务材料。Pillow 已由既有 Gradio 环境提供，仅显式登记本批直接依赖，没有安装/升级依赖或调用 OCR/Vision。

T153 达成并勾选。T154 只完成教师端基础，学生展示持久核对方案仍待用户确认；所有新题图默认仅教师可读，源卷/完整原页始终拒绝学生，本批不实现未批准的展示核对字段，也不勾选 T154。完整校正入库、OCR、Vision/人工处置调用、业务 UI 和发布生命周期分别仍属后续任务；不把这些基础用例冒充 T160、T163 或系统验收。

## 11. T154 学生展示开关具体 TCR（2026-10-02，基线 44d7560）

用户已确认方案 A：QuestionAsset.student_visible 为非空 Boolean，Python/数据库默认 false；教师显式选择是否允许学生展示。教师校正开关 UI 留 T159，本批提供受现有权限/内容冻结保护的 API/服务。沿用暂存 JSON 持久字段用于 T159 校正和 T158 转入，不建展示核对事件表、不替代 G05 图像语义核验。

必要性：现有资产只有教师读取，不能证明已开放且学生有权查看的题图成功读取；源卷/原页与同字节整页别名不可因开关绕过。需要覆盖公开 DTO、实际 GET 字节授权和数据库默认值升级旧资产后仍关闭，而不是只测试模型赋值。

新增 tests/contract/test_question_asset_visibility.py：真实 JWT/PNG，默认不显示且字节 GET 拒绝，教师显式开关及列表保持所有资产；学生考试详情和题图列表仅包含 true/允许区域，授权成功可取真实字节；当前考试分配/开放窗口、本人已提交答卷与其他学生/其他题访问区分；关闭立即撤销展示；源卷、完整原页、整页题图及共享字节别名即使标 true 仍拒绝；教师/异课程/学生写权限、Approved 冻结和非法 bool 拒绝。仅修改本批旧资产测试补显式 DTO false 默认断言，其他原断言保持。

新增 tests/integration/test_question_asset_visibility_migration.py：实际 PostgreSQL 0015→0016→0015→0016，旧资产默认 false、直接 INSERT 省略字段也 false、非空 Boolean 约束、数据/其他列原值和降级边界；不升级真实业务库。旧迁移图用例只追加 0016 前驱和 head，不改原 revision 对象断言。

源卷和已登记完整页的相同字节/定位视为原页别名，学生过滤与文件读取共用一个实际规则；不能将裁切当自动去答案判定，教师显式 true 表示已核对资产仅含允许展示内容。暂存没有正式 QuestionAsset 时仍仅教师可读。模型/公开 Schema/合同同步该语义，迁移不能反填 true。

先写目标用例暴露入口缺失，再实施并聚焦；最后执行原 pytest、mypy、Ruff 和隔离库 Alembic check。所有资源/日志位于本批忽略缓存，原 .env 和八个用户文件保持。M0 就绪检查、真实教师标注、OCR/Vision、T159 UI 和完整导入流程不在本批完成声明中。

首次实现聚焦为 12 passed / 2 failed：两项均为新增夹具问题。HTTP 上传使用独立 Session，测试在旧 Session 未刷新时把旧 NULL 核对记录当作快照；改为重新读取真实上传后的记录再验证开关不修改 G05。迁移断言用两次独立反射表构造同名 FROM，产生笛卡尔积/重复别名；改为同一反射表变量，保持原路径/default/非空/降级断言。上述修正先记录，不能修改业务逻辑迎合过期缓存或放宽断言。

聚焦修正后 18 passed（19.55 s）。为直接证明 true 开关随现有同集备份恢复而非只随 ORM 重读保留，在既有本批 test_real_backup_restores_original_pages_staged_and_formal_assets 中将可靠裁图明确标 true，正式映射承接该字段并核对恢复后仍 true；保留原路径投影、来源/核对历史、原页与源卷学生拒绝断言，不更改备份/恢复算法或通用夹具。该项先记录再修改。

### 本批最终结果

先行红灯 5 failed，缺少学生显示字段/开关接口/受校验 Schema；首次实现聚焦 12 passed / 2 failed 是上述新增夹具问题，修正后原节点及考试分配合同 18 passed。原资产用例保持原断言，新 true 恢复覆盖追加于本批既有实际备份用例。

最终全量 1685 collected，1683 passed / 0 failed / 0 errors / 2 skipped / 27 warnings；2026-10-02T05:12:45.152991+00:00，pytest 527.43 s、外层 536.960 s。真实 PostgreSQL bool 默认/非空/升降级、源卷/页/暂存/正式图 dump/恢复 true、唯一路径、核对历史及学生源卷隔离均包含通过。两项 skip 是 M0 缺隔离四变量和 Windows 无符号链接权限，不作为通过门禁。

最终 mypy 161 source files 无问题（5.281 s），Ruff backend/tests/0016 通过（0.176 s），独立库 Alembic check 通过（1.646 s）。报告与详细时间见 validation-report.md 最新 T154 节；日志/JUnit 在 .cache/e2-t154-visible-20261002，本批资源零连接/标签检查后已清理。原业务库仍 0012_audit_logs，原服务 healthy，九项受保护文件（含 .env）字节保持。只勾选 T154，不改就绪清单、T146 或后续任务；开关 UI 按用户决策留 T159。

## 12. T156 可选 OCR Provider 具体 TCR（2026-10-02，基线 980dd48）

登记时间 UTC：2026-10-02T07:21:59.043319+00:00

必要性：当前没有 OCR Provider，无法证明禁用/缺 SDK 不影响文字服务，或真实空页与调用失败、坏坐标/置信度正确区分。用户已确认 RapidOCR 3.9.2 + ONNX Runtime 1.30.0 CPU，显式 PP-OCRv5 mobile 检测/识别和预置模型，不再重新选型。本批不改 PDF 解析编排、数据库、教师标签或 UI。

先新增 tests/contract/test_ocr_provider.py 与 tests/unit/ingestion/test_rapidocr_provider.py，再实施。合同用例验证抽象接口、严格文本/置信度/像素框及页尺寸上下文、实际阅读顺序、无总体置信度时 null、禁用/错误选型明确失败和无 OCR SDK 的应用/文字路径。适配器用例以真实 Pillow 页图作输入，仅替换 SDK 加载边界，覆盖显式配置、固定模型文件身份、依赖/模型缺失与损坏、正常空结果、原生部分/非法结果、原始异常链、事件循环不阻塞和单实例串行；不以模拟结果声明 OCR 质量。

真实验证复用 T155 已核对模型和 paper_scan、paper_mixed、paper_cross_page 渲染页，另建白页/坏图，调用本次适配器；无教师标注，不计算准确率。预置文件校验用于真实权重身份，非无关源码/包版本锁步门禁。只用隔离环境补齐 OCR 依赖，不改主机全局安装及 .env，不触碰业务库。

先行失败、聚焦修正及最终 pytest tests/ -q、mypy backend/app/、Ruff backend/tests 的真实退出码/日志保存在 .cache/t156-ocr-20261002。全量复用专用 PostgreSQL/Redis 隔离流程，后按记录清理仅本批资源。原有测试不删断言；已有 8 项用户修改及 .env 用字节摘要保护。仅完成 T156 时勾选，不把 T146/T168 标签、T157 编排、EXE 完整交付或系统验收计作本批完成。

收尾审查补充（修改测试前）：原生输出中的空 dict/字符串不应因 len=0 被误当合法空数组。该项属于原有坏输出/空页边界，给现有参数化用例增加三个非法空容器；仅允许 SDK 原生 list/tuple/ndarray 转成数组后的结构，保留真正空数组与三项 None 的空识别结果。首次全量尚未完成，主动中断，修复后重新运行最终全量；中断不计通过。不修改旧测试或增加模型质量验收门槛。

### T156 聚焦及真实推理证据

实现前两项 collection error 为 OCR 包不存在；实现后最初 52 passed。静态首轮暴露导入/格式、异常类型和动态 SDK 属性的类型标注问题，已修正；Pydantic 校验器保留 ValueError 使非法数据仍转换为 ValidationError。非法空容器补充用例修复前 3 failed / 28 passed，修复后全部目标 55 passed（2026-10-02T07:39:23.642284+00:00，pytest 14.85 s，外层 15.956 s），未放宽旧断言。

真实适配器最终复验开始 2026-10-02T07:39:37.465772+00:00，外层 21.026 s。复用 T155 的七张页图：扫描 26 区域、混合两页各 26、跨页两页 6/9、10 页样本第 1 页及 50 页样本第 50 页各 7 区域。均返回非空文字、原图范围内框和未知总体置信度 null；真实白页为空，坏图和丢失文件分别为 OCR_INPUT_INVALID/FILE_MISSING。结果及独立原生置信度 JSON 只保留在忽略缓存，不作字段准确率、教师核验或页数性能验收。

仅在 T156 虚拟环境离线复用 uv 缓存安装已确认 rapidocr 3.9.2 / onnxruntime 1.30.0；没有改主机全局依赖、T155 环境或业务配置。验证脚本首次把 Windows asyncio 自唤醒 socket 一并阻止，发生验证工具错误，尚未调用 OCR；调整为事件循环建立后禁止 socket.connect/getaddrinfo，再运行实际 Provider 通过，未放宽 OCR 阶段网络限制。新容器检查修正后实际模型又复验通过，日志见 real_smoke_final.log。

### T156 最终检查与清理

最终全量 1740 collected，1738 passed / 0 failed / 0 errors / 2 skipped / 27 warnings（2026-10-02T07:40:39.506290+00:00，pytest 460.28 s，外层 470.170 s）。两项既有 skip 为 M0 缺四项隔离变量和 Windows 无测试符号链接权限，不记作门禁通过。最终 mypy 166 source files 通过（5.259 s）、Ruff 通过（0.154 s）；仅新增 OCR 两份测试，没有修改旧测试及其断言。

2026-10-02T07:49:07.989876+00:00，精确核对零连接/任务标签后清理本批数据库 eduagent_e2_ocr_3614071ec128 与 Redis eduagent-e2-ocr-redis-3614071ec128；原业务库仍 0012_audit_logs。9 项保护文件摘要一致，全部源日志、JUnit、实际 OCR 输出和清理回执保留于忽略缓存。T156 达成并勾选；其他任务状态、标注待办、模型质量/完整导入/打包边界保持。完整交付回执见 validation-report.md 的 T156 节。

## 13. T157 → T158 → T159 具体 TCR（2026-10-02，基线 e1d943a）

登记 UTC：2026-10-02T08:07:16.440823+00:00

用户已确认：pypdfium2 渲染、pypdf 文字提取；ExtractedQuestion 增加可空 order_index 独立列、导入内唯一，新题明确排序，历史未知 null；复用 BaseLLMProvider 分批结构化拆题，携带未完题，答案/解析只摘录原文；沿用进程内后台执行，中断记失败、保留材料后显式重新导入，不新增 Worker/自动恢复。按顺序完成三项，不提前勾选 T160 或教师质量验收。

必要性：当前仅有原文件/页图/暂存/题图基础，没有上传编排、跨页拆题、校正/幂等 commit 或真实校正 UI。新增 tests/unit/ingestion/test_paper_pipeline.py、tests/unit/ingestion/test_paper_extraction.py、tests/contract/test_paper_import_api.py、tests/integration/test_paper_import_flow.py、tests/integration/test_extracted_question_order_migration.py、tests/unit/ui/test_paper_import_view.py。按阶段先行暴露入口缺失再实施，实际源文件、模型输出 DTO、真实 JWT/关系、事务与 UI 持久重读分别验证，不能用模拟调用代替真实模型/渲染验证。

T157：真实文字/扫描/混合/跨页 PDF 和 PNG/JPEG，格式错误、50/51 页、可靠页图/像素和 OCR 默认禁用、每页选择；Provider 替身仅用于可重复错误分支/模型输出，结构化缺陷、来源越界、未完题、零题、后续批次失败保留页与暂存题；原卷 Uploaded 响应、真实后台执行与启动中断标记、实际进度、401/403/异课程/学生原卷拒绝、知识库 Chunk/Embedding 隔离。不硬编码样本题目、教师答案或标注。

T158：PATCH 省略/null/空数组、来源/区域/资产整组校验、题序持久且原题号独立；拒绝理由与终态，单导入锁内批次校验、同事务 Question(Draft)/QuestionAsset/来源/Corrected；非法批次整体回滚、重复 commit 返回原题且不覆盖其后续修订；部分确认与全部拒绝/Ready，缺答案/Rubric/图像条件保持待补全；原文件丢失真实报错。有效图像核对转入只复用当前真实事件，不伪造模型/教师记录。迁移测试覆盖历史 null、正整数/唯一约束、无损升降级边界；旧迁移图只追加新 head，其余断言保持。

T159：原页/结构化字段并排、跨页与顺序、来源框、最多五图及 student_visible 默认 false/显式开关、真实失败说明、明确拒绝和所选批次确认。UI 回调使用真实服务/登录身份与持久重读，页图通过授权读取为内嵌图，禁止公开 Gradio 原文件静态路径；学生不可见入口；导航/登出清理及旧页面保持。修改已有 gradio_app.py/layout_view.py 只追加本批接线，保留用户现有设计系统与题库改动，提交时排除用户原有 diff。

每阶段聚焦；最后既有 pytest tests/ -q、mypy backend/app/、Ruff backend/tests 与隔离 PostgreSQL Alembic check。需要新增/修改已有测试夹具时先追加原因，不删旧断言。日志、baseline、独立数据库/Redis/文件根归属存 .cache/e2-t157-159-20261002；不升级业务库/改 .env，不用 M0 跳过或无教师标注声明完整验收。真实渲染样本和一次真实配置 Provider 调用单独记载，质量评测仍属后续任务。

T158/T159 实际文件补充：HTTP 校正边界独立放入 tests/contract/test_question_correction_api.py；tests/integration/test_paper_import_flow.py 使用独立 PostgreSQL Schema/不同 Session 验证并发确认和锁内重新读取，复核 T157 混合 OCR/零题/分批失败保留记录。新 UI 导航测试使用项目真实 Teacher/Student 枚举值，修正首版夹具的小写值；不改变导航权限规则或旧断言。

真实 PostgreSQL 流程夹具首轮 1 passed / 2 setup errors：metadata.create_all 默认 checkfirst 沿 search_path 发现隔离数据库 public 同名表，未在临时 Schema 建表，导致夹具角色重复；修正为显式在本测试 Schema 建立所有表（checkfirst=False）。这只修正新增夹具隔离，不改变业务逻辑或原断言；不会触及业务数据库。

资源生命周期补充（先记录再测试）：T157 每次导入创建独立异步 Provider，结束后须释放自有 HTTP 客户端。新增 tests/unit/ingestion/test_paper_provider_lifecycle.py，验证关闭自身客户端且不关闭外部注入客户端；BaseLLMProvider 只增加默认无操作 aclose，原 generate_structured 签名与既有适配器兼容。

首轮全量为 1766 passed / 1 failed / 2 skipped：唯一失败是 tests/unit/ui/test_gradio_app.py 的教师导航固定列表尚未包含新增试卷导入入口。按 T159 目标只在教师预期列表相应位置增加 teacher.paper_import，保留学生/管理员完整列表和所有既有断言；该文件原有用户修改先行快照保留，提交时仍按本批差量隔离。浏览器检查同时修正切页尺寸说明、小裁图不强制放大，以及取消“无图”核对恢复未知状态。

本轮最终工作区全量为 1767 passed / 2 skipped / 50 warnings（2026-10-02T09:10:56.681445+00:00，pytest 473.24 s，外层 481.972 s）；mypy 176 source files 和 Ruff/隔离 Alembic check 通过。两项 skip 沿既有 M0 隔离配置/Windows 符号链接限制，不计通过。T158 的暂存 options 为 JSONB，对象键序在 PostgreSQL 中不能保留；已提交 JSON 列方案等待用户确认，未擅自调整。故先独立提交 T157；T158/T159 工作及先行证据保留但不勾选，相关测试与 UI 不进入本次 T157 提交。

独立 T157 暂存快照补验 36 passed / 6 warnings，包含真实 PostgreSQL 失败保留与 0017 升降级；mypy 171 source files 和 Ruff 通过。只在暂存快照移除待决 T158 的接口/DTO/并发测试部分，工作区完整保留，未放宽所提交 T157 测试断言。2026-10-02T09:23:53.045025+00:00 已精确清理本批 DB/Redis，原业务库 0012_audit_logs；只有 T157 标记完成。

## 14. T158 JSON 保序与校正确认收尾 TCR（2026-10-02，基线 c7ca20e）

登记 UTC：2026-10-02T09:35:03.228988+00:00

用户已批准 JSON 列、新增 0018 迁移及历史丢序标记；本轮按“每项完成后停下”只完成 T158，T159 源码保留，不勾选/提交其 UI。现有 Question.options 自 0002 起已是 JSON，ExtractedQuestion.options 在 PostgreSQL 为 JSONB；按真实类型转换，不误报正式题曾全部使用 JSONB。

必要性：先前实现可完成校正/事务，但 JSONB 无法保留对象键序，SQLAlchemy 默认字典比较还会把仅重排当成未变，导致重排不落库且图像核对不失效。新增 tests/integration/test_options_json_migration.py：真实 PostgreSQL 0017→0018，历史对象/数组/空对象/NULL、既有 JSON 正式题、从历史暂存对象转入正式题的标记，目标类型 JSON、独立重读、降级丢序拒绝。0018 检查真实旧列类型，JSONB 非空对象标 false，原 JSON 保留已存顺序，不推造原卷排序。

扩展已有本批 tests/integration/test_paper_import_flow.py 与 tests/contract/test_question_correction_api.py：C/A/D/B 原始对象顺序、新 Session 重读、同值仅换键序 PATCH 后持久、A→B→A 图像上下文失效、待补全 Draft 同事务转入/重复确认、order_preserved 服务只读/保留/传播、非法单题导致指定批次整体拒绝。正式 QuestionService.update 的选项重排与标记另以既有框架聚焦验证；不删旧断言，不放宽验收。

order_preserved 仅描述当前选项序列是否被存储保留，不证明 OCR/LLM/源卷内容准确。历史 false 不因只修改解析、选项省略或相同选项重交而变 true；只有明确不同的选项序列/内容保存成功后更新标记，批次确认复制，重复确认不重写正式题。标记不作为新入库门禁。

修改迁移图测试仅追加 0018，保留所有旧 revision/依赖断言。运行聚焦与最终 pytest/mypy/Ruff/隔离 Alembic check；只对本批专用 DB/Redis/文件根，业务库和 .env 不变。提交快照排除所有 T159 和用户既有 UI/README 变更，先行/最终输出保留 .cache/t158-correction-20261002。requirements 16/16，v2-readiness 36 项仍为后续门禁清单，沿用户本次继续执行授权不改勾选。后置 hooks 按 skill 检查。

首轮新用例 3 failed / 5 passed：新标记响应缺失确为 KeyError；两项迁移用例的关联正式题夹具尚未满足 ck_extracted_question_link，未到目标 revision 检查。实施后聚焦 26 passed / 2 failed，剩余同一新夹具问题。先记录，再将已关联题设 Corrected、未关联题保留 Extracted，符合既有约束；不改业务约束/断言。静态导入与新测试未使用变量同时按规范修正。

最终独立 T158 暂存快照全量：1758 passed / 0 failed / 2 skipped / 43 warnings（UTC 2026-10-02T09:46:09.541689+00:00，pytest 475.77 s、外层 484.769 s）。快照排除用户/T159 UI；mypy 173 source files 无问题（123.377 s），Ruff 通过，隔离 Alembic check 无差异。既有 skip 为 M0 缺四项隔离配置和 Windows 符号链接权限，不计门禁通过。
2026-10-02T09:55:45.503541+00:00 已核对零连接/标签后清理本批 DB eduagent_e2_correction_60f11e107828 和 Redis eduagent-e2-correction-redis-60f11e107828；原业务库仍 0012_audit_logs。13 项保护文件字节相同，仅 T158 勾选，T159 保留等待下一项。

## 15. T159 校正界面与主壳接线收尾 TCR（2026-10-02，基线 6abf901）

- 必要性：T158 的只读 order_preserved 需在校正界面说明历史顺序不可证明；主壳认证包装必须保留上传生成器协议，不能返回未执行的 generator；退出/重新登录应清除新页面的动态课程、记录、题目、页/资产 choices、原页字节与上传文件。T159 不新增字段、迁移、模型选型或图片核对命令。
- 变更边界：继续已有 tests/unit/ui/test_paper_import_view.py 并补主壳行为用例；如修改 tests/unit/ui/test_gradio_app.py，只增加 teacher.paper_import 导航及当前接线所需断言，保留学生/管理员/旧导航、用户 UI 断言。复用现有 files_api、真实 JWT、SQLite 和持久服务，不通过测试特例制造入库成功。
- 覆盖：当前及历史选项标记展示；原样重保存不抹除 false、仅改键序后实际重新打开仍按新序且 true；未知字段 null、跨页/合法和非法边界；题图裁取/移除、图序与学生明确许可；拒绝、显式批次确认、待补全及幂等；文本选项缺内容不得制造字符串 None；完整主壳的上传回调经认证后仍为生成器且两次 yield，真实上传保存/提取；账号切换清空局部动态值/choices/File/HTML。
- 验证计划：先运行暴露上述缺口的聚焦用例，再修复业务接线并聚焦回归；在隔离数据库/Redis/文件根验证真实浏览器校正流程。暂存仅本批成果并从暂存区导出独立快照，完成既有 pytest tests/ -q、mypy backend/app、Ruff。模拟拆题只用于确定性界面夹具，不作为 OCR/模型精度或 T160/T168 验收。
- 保护：初始用户 UI/README/设计系统/题库测试原样保留；只把初始用户快照之后的本批接线差异暂存。requirements 16/16，v2-readiness 36 项未勾选；按本次明确继续 T159 的授权推进，不改清单。
- 实际结果：实施后追加真实结果，不预填通过。

T159 先行 ui_red：5 failed / 5 passed，legacy 提示和两项空文本确实失败；两项主壳测试先卡在新增回调查找夹具对 functools.partial 的 __name__ 假设。修夹具仅改为 getattr 的实际查找，保留 generator、实际持久上传、choices/File/HTML 断言，再执行主壳先行检查，不把夹具错误当业务红灯。

T159 修夹具后的主壳先行：2 failed，分别在 generatorfunction 与动态 choices 清空处真实失败。最小生产修复后聚焦 27 passed / 8 warnings；最终独立暂存快照 1768 passed / 2 skipped / 64 warnings，mypy 176 文件及 Ruff 通过。新增测试的 Ruff 两项导入/kwargs 格式修正不改断言。浏览器真实字段保存、新会话重新读取、明确拒绝和指定题入库、另一个教师账号清空均通过，并只读核对实际 Draft/null/历史 false；固定 Provider 仅用于页面夹具，没有宣称提取精度或教师质量标签。完整主壳回调沿用既有 eduagent-ui 共享队列，未新增并发体系。详见 validation-report.md 本项记录。


## 16. T161–T162 章节生产与显式范围检索 TCR

记录时间：2026-10-02T10:47:13.263821+00:00；基线 fe90ece；本节在本批测试修改前形成。

- 必要性：G01 的章/节字段尚未生产，Markdown 短节跨真实标题合并；检索 Query 未承载章/节/标签，业务调用不能控制范围。依 data-model §11、rag-retrieval 合同与 T161/T162，需证明可信生产、原子替换和 SQL 先过滤。
- 已确认设计：课程级 Chapter；Chunk 可空章/节；metadata 保持 JSON，独立真实教师确认；旧记录未知。用户确认新增显式 retrieval_scope，旧题目分类/提示标签不隐式过滤，阅卷范围存既有任务 payload。T160 与人工质量验收暂缓。
- 变更边界：新增 models/ingestion、KnowledgeBase service/API/UI 与真实 PostgreSQL 迁移/检索行为用例；扩展检索输入、四模式、出题/阅卷/MCP 传递及后台重读。旧无条件检索、来源快照、摄取失败、权限断言保留；Markdown 新真实标题边界断言按本批合同修订，长度/内容完整性不可放宽。用户已有 UI/测试改动不纳入本批提交。
- 输入/预期：scope-001 同名章不合并、目录连续、教师真实身份；scope-002 单独定位/标签确认和未知/空标签；scope-003 原稿分段不跨边界、embedding 失败保留原块/真实失败状态、快照不重写；scope-004 历史 NULL/metadata 原样升级；scope-005 六维交集、非法章/目录及空集；scope-006 范围外高分不挤占 TopK，四模式同范围，JSONB 精确成员；scope-007 显式范围持久重读与旧调用兼容。
- 隔离/替身：本批独立 PostgreSQL 数据库、Redis 容器和缓存文件目录；Provider 固定输出/受控失败只证明业务分流，不证明标注准确率。禁止对业务库迁移/删种子。
- 先行失败：待记录；新增入口缺失的红灯只定位缺口，后续必须执行实际动作、权限和事务断言。
- 验证计划：聚焦新增行为与直接回归，再执行项目既有 pytest、mypy、Ruff；真实浏览器验证教师核对界面。独立提交快照验证排除用户改动。
- 实际执行：not_run；结果、证据、清理与限制在实际执行后填写。

- T161 审查补充（新增断言前）：区分目录重新登记与教师明确确认“只修正小节标题、原顺序及含义不变”的动作；后者保持映射，仅在目录长度/序号不变时允许，不以名称相似自动判断。覆盖真实定位保留与假借改标题重排拒绝。

- T161 最终审查补充（断言前）：文档 A 原稿/切点不能残留至文档 B；切换/重读清空预览和切点，重切分结束刷新真实资料状态并清除旧片段选择。覆盖跨文档切点串用与失效旧块显示。

- T162 最终审查补充（断言前）：进行中任务幂等复用不得静默忽略新检索范围；显式范围语义不同应冲突，旧省略范围轮询/触发保持原兼容。比较章/资料/标签的集合语义和闭区间，复用不覆盖旧 checkpoint，也不再调度。


### T161 实际验证（2026-10-02）

- 先行失败：生产服务入口 3 failed（1.40s）；摄取 13 failed，其中 Markdown 三叶节混成一块是现有真实行为缺陷，其他为新入口未实现；标题修正用例 1 failed（缺少显式语义参数）。字段/迁移 head 两项旧精确断言按新增事实扩展，未削弱旧约束。
- 实现后：t161_complete 143 passed/7 warnings（41.25s）；t161_index 59 passed/7 warnings（21.64s），独立 index 排除 T162 与用户未提交代码；index mypy 180 files 通过，Ruff 全仓通过。最后审查的跨文档切点残留修复追加测试后 t161_ui_context_final 11 passed/1 warning（13.38s），该追加断言首次执行即通过，未伪报先行失败。
- PostgreSQL：0019 原记录 NULL/metadata 原文不变；章节 FK/CHECK/三列索引；同源切点/向量/全文索引原子发布，失败保留原块并真实 Failed，重试成功，历史题目来源快照保留/live FK 置 NULL；两个预载 Ready 会话实际并发时只允许一批重切。
- 浏览器：隔离合成资料通过实际 Chapter UI、真实 JWT 角色账户完成登记、章/节与独立标签确认，刷新与服务重启后读取一致；截图和数据库检查见 docs/evidence/t161-chapter-scope-20261002/。固定 Provider 只证明流程，未替代真实教师内容验收。
- 保护：主 UI 只暂存本批一行 reset；README、题库/布局/设计系统及用户 UI 测试改动均排除；T160 保持未勾选。临时资源待 T162 全量验证后按本批精确标识清理。

- T161 最后更新的独立 index：t161_index_ui_final 13 passed/1 warning（12.69s），覆盖新增跨文档清理与基线主 UI；独立全后端 mypy 180 source files 通过（外层 105.615s）。

### T161–T162 全量回归的合成语料版本补充（修改前）

记录 UTC：2026-10-02T11:47:34.938280+00:00。独立最终快照首轮 1880 collected，1861 passed / 17 setup errors / 2 skipped / 68 warnings（pytest 522.42s，外层 532.316s）。17 项错误共用 test_benchmark_reproducibility 的 manifest 夹具，真实 stderr 为“摄取片段数与标注基准不一致，请重新标注”；原稿当前生产得到 30 个真实叶节块，旧合成参考为 21 个跨节块。不能把该失败当环境跳过，也不能让生产分块回退或放宽精确内容映射。

必要性与范围：保留 benchmark/corpus 的旧原稿、21 块、规则/模型标注及所有历史结果原样；新增同原稿/同查询的独立 heading-v3 合成语料版本，静态参考保存真实当前块，按实际节内容明确登记 AI 规则标签（teacher_verified=false，不宣称教师标注/质量）。初始化 CLI 的默认生产输入改为该新版本，--corpus-dir 仍显式选定原目录；旧离线 Benchmark 入口/JSON/CSV/manifest 格式不变，过时参考仍明确失败。

仅更新 tests/integration/test_benchmark_reproducibility.py 中已失效的精确基数 21→30，并同时核对 reference.chunk_count。UUID 与原文一一映射、实际 PostgreSQL/向量/全文索引、四模式、坏原稿拒绝、隔离清理/可复现指纹及全部原断言保留。已有 17 用例先行失败已记录；修正后先完整运行该模块，再重新运行最终全量。修改不填充 T146/T160 的教师标签，不作为新质量基线或可跨旧版本比较的准确率。

### T162 实际交付及本批最终验证（2026-10-02）

- typed_scope_final 114 passed/1 warning（pytest 55.04s）；范围 Schema/四模式真实 PostgreSQL SQL 前过滤、精确标签、目录/归属与空交集、出题/MCP 和旧入口回归通过。
- grading_scope_prevalidate_green 180 passed/1 warning（32.82s）：真实当前范围触发前校验、包括纯客观答卷；任务 checkpoint 持久保存，新的执行器/Session 重读，旧缺省兼容，坏持久值不改为空值。最初新增 API 夹具 UUID 导入错误单独修正，不算业务先行失败。
- 进行中任务范围复用先行 7 failed/3 passed，修复后 grading_scope_reuse_green 77 passed/1 warning（31.34s）；显式不同范围 409，不写新 checkpoint/不调度；集合乱序/重复同语义可复用，省略范围保持旧行为，显式 {} 不等于省略。
- 新版合成分块映射修正后完整可重复性模块 17 passed（15.54s，外层 17.196s）；保留原文一一对应、非 UUID 文件内键、坏来源拒绝、四模式及隔离/重现断言，原旧语料/结果保持。
- 最终独立暂存快照全量 1880 collected：1878 passed / 0 failed / 0 errors / 2 skipped / 68 warnings；UTC 2026-10-02T11:51:22.339843+00:00，JUnit 519.211s，外层 528.245s。终端真实摘要：1878 passed, 2 skipped, 68 warnings in 519.23s (0:08:39)。
- 最终 backend mypy 181 source files 通过（UTC 2026-10-02T11:35:34.278824+00:00，116.221s）；Benchmark 补修只改静态目录常量/语料与一个预期基数，不改变已检查后端代码。最终 Ruff backend/tests/benchmark_corpus 通过（0.257s）；隔离 PostgreSQL Alembic check 无新增差异（1.551s）。
- 两项既有 skip 为 M0 未提供固定四项隔离变量、Windows 无测试符号链接权限；不作为通过。合成样例、Provider 替身及这些功能检查不证明教师质量/性能，T160/T168/T169 尚未验收。
- 2026-10-02T12:01:11.182310+00:00 核对任务资源身份、零连接后仅清理 eduagent_e3_scope_c770d09d04f4 与 eduagent-e3-scope-redis-c770d09d04f4。原业务库仍 0012_audit_logs，原容器正常；缓存/JUnit/源日志与失败诊断保留，未迁移业务库或修改 .env。浏览器及验证预览进程已关闭。
- 用户已有 README、设计系统/题库/布局/主 UI 测试差异保持未暂存；主壳逐字节仅新增 T161 自有 reset 一行。只勾选 T161/T162，T160 与教师标注待办不改；.specify/extensions.yml 缺失，后置 hook 按技能跳过。


## 17. T163–T164 核验与图片理解持久化 TCR（2026-10-02；基线 cd19853）

- 登记 UTC：2026-10-02T12:25:24.473516+00:00
- 必要性：新增真实核验报告、独立修订失效、整组图理解/教师核对、独立图像模型配置涉及持久化与公共接口；必须验证历史保留、并发/迟到、身份来源、文件权限和错误事实，不能沿用空值制造通过。用户批准独立 VISION_MODEL=deepseek-flash，复用当前端点与凭据，文字配置和 .env 保持原值。
- 范围：新增核心服务/schema/迁移单元与 PostgreSQL 集成测试、授权接口合同测试、Vision 输入/结构化响应/资源生命周期测试，以及导入校正界面核对入口测试。必要时更新既有迁移图断言以反映新增 head，保留旧约束断言。T160 不勾选，测试数据与一次自有合成题图模型调用均不替代教师质量评测。
- 覆盖：A→B→A 修订递增；并发轮次和当前轮；迟到完成保留历史；technical_error 无伪造 checks；真实教师处置不可覆盖机器结论；当前整组可读原图、全图逐项核对、旧证据失效、暂存转正式只引用同组真实证据；学生不获答案/核验/源整页；配置缺省/不支持/文件缺失/传输/模型/输出错误；BaseLLMProvider 原结构化签名保持。
- 验证计划：先新增聚焦用例暴露缺口，再实现；独立数据库/Redis/文件根运行迁移和跨 Session 流程，最终 pytest tests/ -q、mypy backend/app、Ruff backend/tests、Alembic check，另一次实际合成题图调用与真实浏览器交互。环境和日志留 .cache/t163-164-20261002，不迁移业务库，不改教师样本或用户已有改动；收尾只清理本批确切命名资源。

- T163 聚焦实施结果：13 核心单元＋10 真实 PostgreSQL 测试共23 passed（18.95s）；T163 HTTP最终6 passed（19.53s）。新增夹具最初缺 Ready 教学资料/完整输入字段，按实际合同补齐真实 owned chunk 与 Ready 状态后复测，未放宽失败/权限/409断言。直接写入/迁移图/旧题目服务35 passed；0020升级和Alembic check通过。首次核心新用例直接通过，不伪称行为红测。

- T163 独立暂存快照：77 passed / 1 warning（72.83s），Ruff backend/tests通过；root4源文件mypy通过。Ruff首次两处仅导入顺序/Decimal字面量写法，按规则修正，未改断言/行为。T164编排两个真正行为红测分别复现关闭取消遗留running和prepare输入失效stage误判，已修且50联合用例通过；不会把缺模块collection错误或首轮直接通过伪称行为红测。

- 浏览器实际发现生产SessionFactory autoflush=False：图片结束写入在commit前生成view，内部populate_existing重载覆盖未flush JSON，界面/数据库仍running；默认autoflush=True测试未覆盖。先新增生产autoflush=False聚焦回归，覆盖图像结束/人工核对和语义start/finish/处置返回及持久结果；在各写入投影前显式flush自身事实，再commit释放锁。保留真实失败running回执，不修改为成功；旧独立本地T163提交待修复后重新验证并amend，尚未推送。

- T163 autoflush=False 修复后5项全部由红转绿，核心18＋真实PG10＝28 passed；独立T163最终快照82 passed/1 warning（62.41s），提交977460e。默认缺图像配置的真实浏览器调用及持久重读均technical_error，不遗留新running；历史故障证据保留。
- T164聚焦：既有LLM/config联合77 passed；编排/Vision50 passed；HTTP/新旧UI22 passed。取消关闭遗留running、prepare输入失效分类两项行为红测已修复；状态断言未放宽。一次真实合成图调用超时，真实模型来源与原始错误持久保存，无再次付费调用；浏览器没有生成教师标签。最终完整检查采用Git暂存快照，排除全部9项用户既有文件/.env。

- 最终完整暂存快照：1975 passed / 0 failed / 0 errors / 2 skipped / 73 warnings；pytest857.99s（JUnit857.929s、外层872.539s，UTC13:33:04.305481Z）。191源文件mypy通过（201.131s），Ruff通过（0.505s），隔离Alembic check无差异（3.272s）。M0独立配置缺失及Windows符号链接权限两项skip保留，不当作通过。无断言放宽或测试特例。
- 2026-10-02T13:48:26.213425Z精确清理本批DB/Redis；业务库0012_audit_logs未迁移，9项用户文件/.env字节不变，真实浏览器截图/持久重读/一次云调用超时留docs/evidence。只勾选本批T163/T164，T160继续待教师标注；不存在执行后hooks。

## 18. T160 导入技术验收与授权代拟标注 TCR（2026-10-02；基线6cea2ad）

登记UTC：2026-10-02T15:54:20.112159+00:00。用户要求现在执行T160，并授权AI代拟其所需导入标注。现有57条原标注全空；本次单独制作5个正常导入case的AI辅助逐字段草稿，登记时保留真实来源并待用户复核；用户于 16:14:48 UTC 实际确认后冻结为独立新版本。不改原包、developer_review阅卷分数或伪造教师身份/复核时间。T160勾选仍以实际完成证据为准。
- 先运行既有导入/校正/资产合同、真PG集成和UI聚焦用例，原有断言不改。旧stub仅证明业务，不计OCR/LLM质量。
- 已识别必要新覆盖：真PG生产autoflush=False同时PATCH与commit锁竞争，先校正后确认必须读取新值，先确认后编辑必须终态拒绝；注入明确OCRProviderError验证真实错误映射及原卷/已保存页图保留，不能假装空题成功。独立测试文件tests/integration/test_t160_import_acceptance.py；详细先行TCR由审计代理附录登记后才写测试。
- 实际后端使用本批独立DB/Redis/目录启动、关闭并重启，验证持久原卷/页图/题图字节以及学生403；新Session/新Engine不冒充进程重启。使用已授权合成数据与受控拆题时明确technical_selftest，不声称模型质量或教师真值。
- T142正式3轮质量、导入/切页3冷5暖计时与资源结论只能据实际运行；缺标签/真实模型/合法浏览器终点的部分保留待验。只清理本批确切命名资源，不迁移原业务库。全部10项用户文件（含.env/新增阅卷样本改动）受字节保护。
### T160 最小技术验收测试变更申请（先行登记，已执行）

#### 必要性
既有真 PostgreSQL 测试仅证明两个 commit 并发幂等，以及完成后旧 Session 的迟到编辑被拒绝；没有证明 PATCH 持有真实业务锁期间 commit 会等待并读取已提交的新值。既有 OCR 禁用测试不证明调用失败与缺配置被区分。T160 明确要求并发校正/确认与 OCR 失败证据，需补足这两类行为，保留所有既有断言。

#### 文件与范围
新增 tests/integration/test_t160_import_acceptance.py；复用现有 test_paper_import_flow.context/receive/detail 及真实 PDF 样本和本地 PostgreSQL fixture，使用 autoflush=False 以匹配生产 SessionFactory。不改业务代码、旧测试、模型、依赖或契约，不调用云模型、不生成教师标签，不将 fault injection 或结构化 Provider stub 当作真实内容质量。

#### 登记的行为用例（3 项已执行通过）
1. PATCH 已持有同一导入的业务锁、在事务提交边界暂缓；另一个真实数据库 Session 发起 commit。通过 PostgreSQL pg_blocking_pids 观察确实等待该 Session，再释放提交；正式题须消费 PATCH 新题干/分值，原来源一致、只有一份正式题。
2. commit 先完成后，先前已载入旧暂存身份的 Session 执行迟到 PATCH，须 PAPER_STATE_CONFLICT；不改变 Corrected/Ready、正式题及原校正内容。它是与第一例配套的反向顺序，不声称两个请求同时完成。
3. 扫描 PDF 渲染并保存原卷/页图后，已声明就绪的本地 OCR 测试 Provider 在真实调用边界抛 OCRProviderError(OCR_CALL_FAILED)。结果须 Failed/PAPER_OCR_FAILED，保留明确阶段和原错误码，原卷/已保存 PNG 字节可读，无暂存题/无伪造 OCR 文字。另保留原有 OCR_PROVIDER_NOT_READY 回归，区分配置缺失与调用故障。

#### 实际执行与范围边界
本附录先登记到 docs/test-change-record-v2.md，根代理授权后才创建测试。2026-10-02T15:57:10.300679+00:00，在本批隔离 PostgreSQL/持久目录实际运行新增 3 项，3 passed（pytest 13.13s，外层 17.013s，exit 0）。未改业务实现或既有断言，无云调用。原始日志见 benchmark/results/v2/t160-assisted-20261003/technical/t160_added_first.log；原 TCR 计划与执行记录分开保留。


### T160 本批验收口径确认与真实运行授权

2026-10-02T16:14:48+00:00，用户明确确认 AI 代拟/原页对照后的参考，并采用“用户复核的 AI 辅助标注”口径；独立教师标签为 0，不改写原样本包。已授权完整 T142 的质量 3 轮、1/10/50 页导入及三类校正各 3 冷＋1 预热＋5 暖；实际文字模型请求预计约 300 次，不含现有策略重试，用户已知按 token 计费。沿用当前文本模型/端点/凭据，.env 保持原值。实际结果和未知/失败保存原始记录，不通过测试替身证明模型质量。

### T160 本批已执行结果（2026-10-03）

- 既有聚焦 75 passed（JUnit 82.474s，外层 92.331s）加新增 3 passed，共 78；只新增上述独立验收文件，未重写既有断言。实际 PostgreSQL 锁等待、迟到校正及明确 OCR_CALL_FAILED 均通过，配置未就绪与调用故障分开。
- 固定损坏卷/51 页/OCR 未启用/显式 OCR 故障 4/4 通过，0 云调用。真实 create_app/lifespan 两次进程启动、30 项 HTTP 核对及 4 个中断记录恢复通过；minimal Gradio 测试壳、受控夹具明确为技术验收，不称生产 UI/EXE/内容质量。
- 用户确认的 AI 辅助参考为 5 个 case、16 题；真实质量执行 15/15，14 Pending Review、1 Failed，自动严格匹配 43/48、辅助校正 45/48，后置实际 31 次裁图 0 失败、最终关联 45/48。独立教师真值仍为 0，失败 3 题继续计分母，未知字段单列，不改变 T146/T168 要求。
- 导入实际 27 次含 3 预热，24 计时中 12 成功/12 失败，50 页 0/8。实际 83 次性能 HTTP 加 15 次质量 HTTP 共 98 次，total tokens 243347，费用 null；请求 deepseek-chat、响应 deepseek-flash 分开记录，不推断 alias。
- 资源范围旁证证明 12/12 原采样 PID 是 venv redirector，与 27 次业务 worker 不同。原 raw/summary 保留，旁证 app_missing=true、all_component_complete=false，应用峰值/同刻总量/预算结论均 null，不能声明资源完整或达标。
- 浏览器完整执行24计时＋3预热，均真实渲染完成、27张Cua截图；仅2/24低于500ms。UI实际workerPID一致，6次动作各有1个窗口内资源样本、其余缺测保持null，不能判预算通过。四个无起点预检单列。已精确清理隔离资源，原业务库0012_audit_logs与10项用户文件字节不变；无后置hooks。当前 T160 验收未通过，保持 [ ]，不执行 T165；建议先定位收窄提示词与结构化来源合同冲突后另批复验。详情见 docs/evaluation.md、docs/validation-report.md 本批章节和 benchmark/results/v2/t160-assisted-20261003。

## 19. T160 三项修复与完整重验 TCR（2026-10-03；基线 02b6406）

登记 UTC：2026-10-02T17:54:08.462208+00:00。用户要求修复后重验，并已明确允许 T157/T159 的必要局部缺陷修复；保留既有接口、来源检查及已完成任务状态。修复分别提交、推送。原 v1.0 内容及用户已有改动不纳入本批。

- 拆题必要性：实际失败输出的 evidence 任意字符串键符合模型 schema 却被后置业务检查拒绝；另有本批新题错误 updates 和缺答案证据。先在 tests/unit/ingestion/test_t160_extraction_constraints.py 增加真实失败结构的拒绝、合法字段与证据、原页摘录/跨批索引边界回归，再收窄 AI schema/提示词；保留业务来源与失败保留检查，不改 Provider/模型/重试策略。不通过删除错误输出或编造答案制造成功。
- 页面必要性：现有切换重复认证并全量扫描同一导入及文件，需先用当前实际视图分段测量，再以 tests/unit/ui/test_t160_correction_loading.py 验证单次请求复用、选中页/题来源及权限/缺失错误仍传播；必要真实数据库路径使用既有集成夹具。仅针对生产者重复读取与渲染，不采用跨用户全局缓存，不放宽安全和生命周期检查。
- 内存必要性：便携 runner 已含实际 worker PID 握手，但尚无运行证明，必须用零云调用的真实新进程/Windows venv 子进程验证采样对象与测量窗口。若补测试，在 tests/unit/benchmark/test_t160_resource_sampling.py 覆盖握手身份/进程退出/采样缺失与峰值；真实采样脚本保留独立回执。禁止用 redirector 峰值、相邻运行峰值或空值冒充本次应用内存。
- 新回归先运行再实施；首次直接通过则如实登记，不伪称红测。原用例断言保持；只有发现具体不匹配才更新本 TCR 后调整相关测试。
- 本批用户确认门槛：50页计时导入至少6/8成功；UI至少20/24次真实端到端响应小于500ms；资源按v2组件/同时峰值预算实测。冷3、预热1、暖5和质量3轮保持，保存全部失败/慢样本；与原T142全数通过口径分别记录。实际云调用已获本次完整重验授权，仍沿用当前文字Provider与模型配置。
- 继续使用已获用户确认的AI辅助参考版本，不改T146/T168独立教师声明或旧标签；旧T160批原记录保留。隔离DB/Redis/文件根运行，本次10个既有文件含.env先保存字节基线，不迁移业务库。

#### 修复 1 验证回执

- 新增来源约束回归先行运行：11 failed / 3 passed；修复后与既有拆题、Provider、重试策略回归合计 37 passed（8.12s）。
- Ruff、拆题模块 mypy 与 diff 检查通过；真实来源页/摘录与旧题索引校验未放宽，无新增重试策略。
- 原始红/绿回执保存在本批 extraction 目录，最终验收归档随重验报告提交。

#### 修复 3 证据边界补充

- 超时可能发生在 JSON 回执写入中；运行回执改用同目录暂存后原子替换，并增补中断写入不覆盖已有完整回执的回归。失败或未观察 slot 保留原分母，不追加替换运行。

#### 修复 2 浏览器诊断补充

- 真实原生选择预检发现一次切题发送两次相同 reload（两次 queue/join），浏览器 566ms 中目标字段约 334ms 已就绪，但后续重复更新继续触发渲染。追加事件绑定/选择行为回归与原生浏览器分段诊断，以保证一次有效选择仅加载一次、相同值无业务写入，保留原页面和字段完成、交互恢复及稳定双帧终点；不修改计时断言。

#### 修复 3 验证回执

- 先行扩展回归 7 failed / 2 passed；证据原子写入先行 1 failed / 9 passed；最终 10 passed（0.25s）。Ruff、Black、diff 检查通过。
- 真实 Windows venv 探针（UTC 18:07:51–18:08:04）：启动 PID 83904，实际 worker PID 84328，父链验证通过；触页分配 64MiB 后采得工作集增长 64.00390625MiB，基线 3 点、分配后 5 点完整采样，无缺测。探针为 0 云调用/0 SQL，不能代替正式导入资源验收。
- 正式 runner 逐导入记录实际 worker 子树、同时资源峰值、采样完整性和 v2 软预算；进程有限时退出，原始失败/未知保持不变。

#### 修复 2 验证回执

- 读取回归先行 2 failed / 2 passed（首次夹具 JWT 配置失败另行保留）；导航事件回归先行 1 failed；修复后新增和既有用例 22 passed（25.53s），Ruff/diff 检查通过。
- 同输入真实 DB：切题 SQL 62→33，跨页 47→10；回调约 69–72ms / 20–21ms，返回 HTML 字节相同。
- 浏览器诊断证明 Gradio 当前 Dropdown 在选中和 blur 分别发送 input，单次原生动作出现两次 reload。题目和原页导航改用 change，无新增缓存/去重状态；初始程序化载入可触发额外只读同步，保留正常语义。
- 原生鼠标预检 566ms（重复加载）→381.7ms（一次加载），保留相同字段、原页、交互和双帧稳定终点；ArrowDown+Enter 切回第二题，真实题号/题干同步成功。该诊断不计入正式24次测量。

#### T160 修复后的完整协议与收尾

- 最终冻结9c9e79f，全pytest2007 passed/2 skipped（0失败）、mypy191文件、Ruff及隔离schema check通过；模型协议15质量＋27性能，页面27动作（各24计时＋3预热），无覆盖失败的补跑。
- 50页7/8、1/10页16/16、页面24/24，24导入资源窗完整且预算通过。原24/24导入严格门槛仍false，用户6/8口径true。保留cold-3实际业务来源拒绝；质量逐字段差异与AI辅助来源未改。
- 33次真实裁图均成功；故障4/4，实际进程重启30HTTP＋4中断恢复通过。完整回执见本批technical与acceptance-summary.json。
- UI停止助手仅修正等值UTC时间的类型/格式比较，未变计时端点或替换动作；资源短窗保持未知。最初派生汇总遗漏重启文件实际子目录，缺路径诊断保留，显式传参后完整汇总通过；未追加运行。
- 命名隔离资源清理、原业务库版本与10个受保护文件字节核对通过；任务仅勾选T160。


## 20. T165–T167 生成、改编、语义核验与审核界面 TCR（实施前，2026-10-03）

- 授权与范围：连续完成 T165→T166→T167，复用 T161–T164 已确认合同；不运行 T168 教师质量评测、不改阅卷拓扑或历史成绩。关键依赖、父题关系、报告 JSON 与 revision 语义沿既有设计，无新增架构方案。
- 必要性：当前缺目标分值与改编来源生产、真实四项语义模型执行及服务端统一批准门禁；UI 仅凭 Pending Review 允许批准且缺解析/图像/报告与来源展示，必须用行为验证阻止绕过。
- T165：先新增 unit/contract/integration 行为用例，覆盖目标分值不符拒绝；真实资料/章节范围传递；同课程父题、重复/自引/环与并发锁；父题不变、调用中修改冲突；候选/引用/元数据/父子边/复用题图原子提交；新资产身份与隐藏默认、父核对不继承；旧未知来源与 JSON 有序选项保留。迁移验证 RESTRICT、唯一/CHECK 和历史保留。
- T166：先新增聚焦 semantic unit/contract/integration，覆盖实际字段/证据投影、四项检查及证据白名单、Provider/Schema 技术错误保真；running 在外部调用前落库释放锁；最新 revision/轮次与 A→B→A/迟到结果/人工处置失效；业务 failed 原子退回且无伪造教师意见；两个批准入口及通用状态入口均用当前报告；缺依据/图像/字段禁止；真实批准 frozen_at 和合法退回清除、历史 Approved 保留。
- T167：复用用户已有 6 个 UI 文件及 docs/test-change-record-ui-question-bank.md 基底记录，保留原列表/详情/显式编辑/权限/保存失败断言；先扩展 UI/loaders 行为用例，旧仅靠 Pending Review 的批准成功用例必须提供真实当前语义报告/gate，新增失败用例不能以测试开关绕过。覆盖真实详情重读、范围/目标/改编输入、有序选项、解析保存、父题/原卷/教学引用、图像人工核对与报告分项/历史/错误、再提交/核验/批准及保存失败保留编辑。
- 兼容测试调整：原审核成功夹具只补实际新门禁所需持久报告与真实教学证据，保留原状态/来源/意见/事务断言；禁止放宽断言、伪造业务通过或使用生产测试特例。历史读取和既有候选 DTO 状态保持。
- 环境与验证：沿既有 pytest、真实 PostgreSQL/pgvector 隔离库或 schema、可控 Provider 测业务与错误；先红后绿保存结果。逐项聚焦验证、mypy/Ruff及迁移；批末全量回归和实际 Gradio 浏览器操作保留截图。技术受控样本不宣称真实教师/模型质量，真实云调用若需要显著费用另提方案。
- 用户成果：实施前保存现有 10 文件原字节；T167 必要 UI 基底保留后接增量，README/阅卷样本/.env 不提交；独立提交验证不能依赖遗漏的用户 UI 文件。任务仅在验证通过后勾选对应项，其他状态不变。
- 执行状态：待实施；本记录先于本批新增/修改测试。

#### T165 验证记录

- 首轮行为红测为 6 failed / 5 passed；实现后代理聚焦 62 passed，补充删除事务 1 passed。
- 根代理在仅包含 T165 变更的独立暂存源码快照上运行既有生成、来源、审核/不可变性兼容用例：123 passed；新增删除补充 1 passed。
- 独立快照 mypy 6 文件 / Ruff 通过，隔离数据库升级 0021 与 alembic check 通过。真实模型效果留给 T168，未将受控 Provider 结论当准确率。

#### T166 验证结果

- 新核验行为先 RED，最终独立 T166 源快照 173 passed；四项新增 PostgreSQL 事务用例及原十项均通过。
- 原有生成/状态测试仅补真实教学证据、核验报告和受控 Provider 装配，保留状态/来源/数量/失败原断言；没有云调用特例。
- 完整 mypy 195 源文件、backend/tests Ruff 通过；UI 集成由 T167 与最终回归承接。

- T167 运行验收补充：真实浏览器登录暴露全局 outputs 未登记试卷图片核对 reset 返回的两按钮及 Accordion；新增聚焦注册/Gradio 返回值转换回归，必要性是防止登录、退出共用清理回调在序列化时失败，不改变既有清理或业务语义；先复现 1 failed，再修复登记后 test_gradio_app.py 12 passed。

- T167 全量兼容夹具补充（实施前）：tests/integration/test_teacher_setup.py 及 test_paper_import_foundation.py、test_question_asset_service.py 的原批准流程仍无当前核验依据；仅补齐答案/评分标准、真实同课程教学引用与已持久的受控语义报告，带图题先经实际文件读取和当前人工图像核对，再执行语义核验。保留原课程/考试准备、批准时间、批准只读、合法退修与文件不物理删除断言；不改变业务或降低门禁，不把受控报告称作独立教师真值。

- 上述 T167 旧批准夹具聚焦结果：原 3 failed / 5 passed；补齐后 8 passed（t167_legacy_fixtures_complete.log）。图像夹具在实际测试根读取裁剪像素、取得当前 revision/run/check 并经真实 manual_image_check 持久 confirmed 条件后才准备语义报告；统一批准服务按同一真实根检查，原断言保留。

#### 本批全量回归收敛与待决项

- 首轮完整源码回归保留 2059 passed/2 skipped/21 failed/49 errors 原始日志。新统一批准门禁使原 Approved 准备失效，必须补真实教材/当前报告；有图先读真实文件并当前核对，合法退回补真实原因，迁移测试保留旧链并明确新增0021。上述均在 §20 已预登记范围内，没有删除/放宽原业务断言。
- 实施后的精确源码快照逐例重验全部68非演示异常为68 passed，另83/8现有消费者聚焦通过；完整 mypy 197、最终 Ruff通过。
- scripts/demo_seed.py 两项旧成功流程的公共行为/真实调用成本待用户决定，测试与脚本原事实保留，未用批准特例掩盖，T167未勾选。


## 21. T146 AI 授权参考输入的完整性检查（2026-10-03）

- 必要性：新包含61个用例、多种标签结构、原材料引用及JSON/CSV统计，需可重复检查来源、完整覆盖、未知项与可评分分母，防止把AI标签或故障声明当教师真值/真实运行。
- 范围：新增 benchmark/t146/validate_inputs.py 作为样本输入检查入口；不修改业务代码、既有pytest用例、历史结果或断言。
- 覆盖：57个旧ID与4个新增正对照/参与事实、源摘要、JSON/CSV定位、300候选及12组卷见证、评分尾差、正常导入来源页/题序/选项顺序、语义true/false/null和applicable掩码、统计整数分分母及AI身份。
- 限制：检查通过只说明参考输入结构与核算一致，不证明OCR/模型准确率、服务批准、人工核对、性能或EXE交付；独立教师数量仍0。

- 实际验证：61例索引、44语义项（未知/不适用掩码）、300候选与156统计CSV行检查通过；新工具Ruff及mypy通过。未修改旧pytest测试、未运行模型或声称全量回归通过。


## 22. T168 学习项目质量基线 TCR（实施前，2026-10-03）

- 授权与口径：用户批准T168新增必要benchmark评测入口、结果和证据；基准为 AI 辅助 + 开发者审查，独立教师数量0。不改业务代码、其他任务定义、既有断言或历史结果。质量阈值须在真实基线后提出并由用户确认。
- 必要性：当前缺少连接生产语义/图片Provider的可复现三轮质量评测，以及在新参考版本下对已完成导入证据的独立重算；仅Schema通过不能证明题目语义、图片条件或拆题字段正确。
- 覆盖：benchmark/t168/ 下独立入口验证冻结源摘要、case身份、版本及true/false/null/applicable分母；语义保存显式运行上下文并严格投影现有DTO，原题干/答案/Rubric/标签不改，缺答案与依据保留输入阻断；记录实际Provider请求/Prompt/模型/结构化及原始响应、重试、token和错误，不泄露凭据。图片实际读取4份像素、三轮12次调用，条件严格一对一对照，低分辨率未知另计人工移交，故障声明不算云质量成功。
- 导入：只读复用T160同源五案例三轮实测，逐项证明源文件、当前参考标签及相关代码一致，另存原分子/分母、自动/辅助校正阶段、题目身份召回及失败列表。无完整页字符真值时CER/WER留null，不得以OCR自身结果或下游字段充作真值。
- 验证：先dry-run检查33语义slot（30请求/3真实输入阻断）、12图片slot、导入15实测slot及掩码；Ruff/mypy检查新增入口；随后按已批准费用范围运行现有Provider及原重试策略，保留所有slot与失败，不挑选成功重跑。不修改pytest文件，不扩展业务测试矩阵。
- 统计与限制：有标签且适用才进质量分母；拒答/未知/技术错误/输入阻断不算TN或正确，报告覆盖率及TP/FP/TN/FN并保留每轮结果；三轮是同一组样本重复，不伪称独立样本。人工移交和辅助校正不伪称自动正确或像素边界准确。实际基线、受控故障及既有业务验收各自保留证据边界。
- 执行状态：实施前登记；后续补实际结果及阈值决策。


#### T168 实际执行与阈值确认

- 先dry-run33语义槽位（30可调用/3输入阻断）、12图片槽位；随后30语义+12图片真实生产Provider调用完成，42原始响应及token/重试/错误证据保存，未替换失败槽位或修改原标签。
- 新增4个benchmark入口Ruff/mypy聚焦检查；离线重算15历史导入实测、1248原比较差异0、33实际辅助裁图分阶段，根汇总验证固定slot/原图匹配及原分子分母。修复汇总中的不存在fields.json引用为真实fields.csv，无新云请求。
- 可评语义71/81、图片32/33、低清正确移交3/3；自动题干6/48及多项语义误报/覆盖未达确认目标。未知/NA/输入阻断/拒答不算TN，不去除真实synthetic来源说明以制造通过。
- 用户已确认数值目标并保留未达标结论；T168完成测量及登记，整体质量not_met。4入口静态/离线验证不等同业务/全量pytest，既有测试与业务代码未改；AI+开发者学习口径与独立教师0保留。


## 23. T167 演示兼容收尾 TCR（实施前，2026-10-03）

- 授权与选择：用户授权先收尾T167再执行T169；沿此前推荐方案，演示人工题保留待审核，教师在界面补全、选真实教材Chunk、执行当前核验并批准后再重跑发布，不新增脚本自动模型调用或绕过批准门禁。
- 必要性：原scripts/demo_seed.py直接批准缺当前报告的示例题，正确被服务拒绝；scripts/run_demo.ps1固定Demo ready提示会误报新的待审核状态。需在生产者和直接消费者同步如实展示状态。
- 范围：仅demo_seed.py、run_demo.ps1及tests/integration/test_demo_seed.py、tests/contract/test_run_demo.py必要行为调整；文档另存演示流程，不改用户README及原示例题内容、不覆盖已有题状态/考试/答卷/成绩。
- 覆盖：先创建全部题、只将Draft送审；awaiting_teacher_review显示实际题状态/待审ID及已有考试状态，不创建/发布考试；全部真正Approved后继续原幂等考试发布。测试先见待审幂等、真实已摄取Chunk和ContentValidationService/受控Provider核验再经QuestionService教师批准、重跑ready，保留原身份/密码/向量/学生可参加/失败重试及服务调用断言；选择题缺Rubric由测试教师显式补全，不伪造passed报告。人工退修、闭卷或已修改考试继续保留真实生命周期。PowerShell合同保留命令顺序、凭据保护、失败中止，仅修提示语义。
- 验证：聚焦demo集成/PowerShell合同先RED后GREEN、Ruff/mypy；本批末次全量pytest复验当前源码和既有68项夹具修正，记录真实skip/失败，不将旧局部重验冒充全量通过。

#### T167 本批执行结果

- 原业务红测5 failed/8 passed，修正两个直接入口后13 passed；真实教材摄取→当前报告→教师批准→发布，原幂等/密码/学生参加/摄取重试/闭卷保护保留。
- 当前工作区完整2144 passed/1原有用户样本冲突/2 skipped；不改该样本和断言。隔离提交快照完整2145 passed/2原条件skipped，638.11s，源码期间无变动；原冲突用例快照1 passed。
- 全backend mypy199及Ruff通过，隔离迁移0021/check一致，用户文件原字节、业务库0012保持，owned资源清理。日志、精确tree及JUnit见docs/evidence/t167-demo-close-20261003/。

## 24. T169 E3 范围/改编/图像/重核验验收 TCR（实施前，2026-10-03）

- 必要性：现有覆盖主体完整，但逐项单独失败的批准门禁、A→B→A后的旧passed报告失效完整链，以及真实PostgreSQL父题自环/跨课程/环检测仍需聚焦补充；还需保存当前生产服务生成/改编/处置的可追溯运行状态。
- 范围：最小新增tests/integration/test_t169_e3_acceptance.py或在对应现有PG测试追加wrapper；新benchmark/t169业务验收入口及docs证据，复用已有服务/DTO/Provider抽象，不新增业务功能或改写任务定义。新增测试前本TCR先登记。
- 覆盖：四检索模式SQL在Top-K前过滤、缺省/空交集与跨课程/未知章节拒绝复用现有合同；PG复用父题图边界、自环/重复/环/跨课程及同事务；四check逐项fail/needs_review/insufficient_evidence其他pass时拒批，缺依据/不完整输入不制造成功报告；真实当前passed→内容A→B→A→旧报告stale且拒批，再当前核验可恢复。
- 实际业务证据：新owned数据库/持久目录/必要独立Redis，实际迁移/启用合成Teacher/Ready教材Chunk，走生产QuestionGenerationService/QuestionAgent/ContentValidationService生成及父题改编、来源/资产/报告保存、两批准入口和人工处置/修订后重核验；受控Provider明确fixture身份，五类错误/无问题样本的业务门禁与模型质量分开。图像支持/不支持/调用失败和旧核对失效复用现有合同及真实服务验证，不把原图或核对继承为通过。
- 保留断言：任何未解决问题/缺报告/失效/技术失败不得批准；教师处置保留原机器结果和真实执行者/UTC；候选生成与教师审核职责分离；原方案四模式、默认语义、权限与关联事务不降级。受控Provider结果不宣称模型正确率，真实质量引用T168的AI辅助+开发者审查、独立教师0、quality not_met。
- 验证与停止：聚焦新增行为及既有unit/contract/真实PG模块、当前完整pytest/Ruff/mypy、隔离迁移/schema检查；保存JUnit/命令/源码/DB读回与样本分母，缺连接明确未验，不回填伪证。保护用户文件/.env，仅清理本批owned资源。实际云请求0，不扩展已完成T168质量/性能重复协议。

#### T169 本批执行结果

- 新真实PG补验14 passed，覆盖父图、四项各3非通过结论和A→B→A；既有+新增映射126 passed，均对应实际完整提交快照JUnit，不重复计数。
- 可复现生产服务入口r1真实夹具违约失败保留；显式知识点metadata与原字段保持，r2实际通过。10项类型边界错误保留，最小修正后入口mypy/Ruff通过，r3实际12cases/11events/0云调用通过；原字段、失败和机器结论不改写。
- 完整提交快照2145 passed/2原条件skipped，工作区1原用户样本冲突另存；后端mypy199/Ruff/隔离迁移通过，源码未变。原业务库/用户文件保持，owned资源清理。T168质量not_met与AI辅助口径保留；不以受控业务报告冒称模型准确率/教师质量。
- 证据：benchmark/results/v2/t169-20261003/与docs/evidence/t167-demo-close-20261003/；T169仅勾完成状态，其余任务定义不改。

## 25. T170 关联实体与组卷Schema TCR（实施前，2026-10-04）

- 授权：用户连续T170→T174，未决取舍按推荐方案；各项独立提交推送。保留三项用户修改及.env，原业务库只读，迁移/行为验证使用本批owned数据库。
- 必要性：原exam_questions只有双外键，无法承载显式题序/本场分值/评分依据；新增行为须验证原表原地升级及旧关系可读，不允许双写或从当前题库猜历史值。
- 覆盖：模型/Schema金额、严格整数与三题型分布、去重/UTC、SQL NULL与JSON形状；唯一/CHECK/FK删除语义；旧关联数量及Question.created_at/id确定顺序、评分依据保留NULL；安全降级与有新依据/不同题序拒绝丢失；ExamService原创建/追加/删除改为唯一写关联并维护显式连续顺序。
- 兼容夹具：原Exam(questions=...)或secondary原始写入必须改为显式ExamQuestion及确定题序，保留原API/状态/授权/成绩业务断言。迁移head断言随真实新增0022更新。不得用自动默认max序号或写两套关系使旧夹具假成功。
- 验证：先新Schema/模型/迁移行为RED，再实现及真实PG/旧考试服务聚焦；mypy/Ruff/迁移检查。本批最终完整回归使用排除原有用户样本差异的提交快照，原冲突事实已在T169记录，不修改其断言或用户数据。

T170执行结果：新增模型入口RED真实失败后完成；直接回归249 passed、隔离PG迁移1 passed，mypy202/Ruff/Alembic check通过。新增tests/unit/services/test_exam_association_service.py验证旧入口写关联及非草稿未知分值；UI只把null显示为未知。证据见docs/evidence/t170-20261004/。

## 26. T171 历史核对与兼容迁移 TCR（实施前，2026-10-04）

- 必要性：T144证据仅证明部分运行时分值/知识点，不能自动证明发布Rubric/基准/时间。只读清单与显式证据核对、未知新评分拒绝需要验证。
- 覆盖：原关联数、确定题序、NULL未知、旧结果/UTC读回，合法证据核对身份/课程/完整性、非空冲突拒绝、同内容幂等、同场回滚和失败恢复；不改Question.frozen_at或旧结果，不从现题库/模型补历史。只读GET保持，执行门禁覆盖触发/worker/复核写入，完整评分链由T176接续。

- T171具体补验范围：tests/integration/test_exam_history_audit.py、test_exam_scoring_execution_gate.py，tests/unit/services/test_exam_scoring_rules.py、test_exam_execution_boundaries.py。真实PG验证历史读取与源事实，Workflow/Review真实服务+受控运行依赖验证无新评分/写入；非独立教师或云模型质量评测。
- 执行夹具显式提供合成教师核对的完整依据；Snapshot替身显式声明能力，默认不授权。共享旧断言保持；改题型的范围测试同步生成对应合成basis。审查新增必要边界：旧Session行锁刷新、提交回执丢失保持unknown、三个API业务门禁映409，均先记录真实RED再修复。

T171最终结果：64项聚焦通过；直接回归639通过、4夹具失败按真实题型补齐basis后4项重验通过；mypy205/Ruff通过。原始失败和stale/提交未知状态RED均保存，原业务库只读盘点与用户文件校验已记录。证据docs/evidence/t171-20261004/。

## 27. T172 条件组卷与失败意图 TCR（实施前，2026-10-04）

- 必要性：新组卷路由涉及精确条件和失败仍保存意图的双重事务责任。
- 覆盖：同课程已批准且当前依据/图像可用，三题型精确数量、多标签不重复计分、Decimal总分和显式override；合法但未选override不强制；缺题/明确冲突/搜索预算未找到区分；失败保留原关系全部字段但保存要求；无效准入不保存；技术/提交失败未知不假称保存；同场锁、真实PG保存点与迟到失败不覆盖、重启重算及坏JSON显式错误。
- 推荐实现：确定性有界回溯/剪枝，不增加求解依赖，不自动改分或放宽条件；预算耗尽只报策略未找到。受控报告只验证资格边界，不作模型质量通过声明。

T172结果：新PG/HTTP 41 passed，共享回归94 passed；准入锁序、同场并发、缺图、写入失败/提交未知/清理失败保留原错误均验证。原RED和最终JUnit保存docs/evidence/t172-20261004/。

## 28. T173 题序/替换/改分/预览 TCR（实施前，2026-10-04）

- 覆盖：Question.id路由、首尾双向移位与正数临时区间、连续唯一；替换新关联身份保留位置与显式分值，score省略/null区分；实际改分/替换失效评分依据，同值/纯移位保持；最近组卷意图/教师UTC不变，按事实重算缺口；新旧入口/授权原图/教师完整预览与学生答案隔离；旧qid及保护历史拒绝。直接消费者不得再自行按Question创建时间重排。

- 直接消费者补充：原reader测试创建时间排序口径随明确本场题序合同更新为与创建时间/UUID相反的显式关联序；保留答案身份及缺失拒绝断言。新增事务flush后失败恢复、Draft有答卷保护及持久条件缺口发布拒绝，不提前扩展T175完整冻结。

T173结果：PG24+reader1、HTTP22、直接回归91均通过（存在重叠）；原测试路径错误和RED记录保留，5source mypy/Ruff通过。证据docs/evidence/t173-20261004/。

## 29. T174 Rubric准备与教师确认 TCR（实施前，2026-10-04）

- 覆盖：结构化数值或教师明确依据、可加总基准核对、28位Decimal先乘后除末次ROUND_HALF_UP；0.005、正负尾差、未知/定性/非加总；不自动分摊，不覆盖独立默认值；确认key/金额/合计/权限、真实教师UTC原因；同题不同场独立，改分/替换/源标准修订失效，缺依据/未确认发布拒绝。
- UI仅补本场标准所需实际字段/表格/解释/准备确认命令，真实服务回读；追加必要UI/合同/PG验证与实际浏览器证据。完整E4发布并发保护/阅卷统计切换/性能验收仍由T175以后任务承接，不提前勾选。


- T174 实施细化：本场 JSON scoring_basis 增加可空 preparation_id，新准备轮次由服务生成；旧历史未知保留 null。确认匹配真实轮次、当前修订/分值/完整已读 basis；同输入重试幂等，A→B→A 不复用旧确认。复用现有 Question.validation_revision 失效点并清除未保护草稿关联依据，不建新表。
- 兼容测试必要性：两个旧 ExamService 发布用例、两个 demo 发布用例需显式使用真实合成教师进行本场准备/确认；原发布/幂等/参加资格/关闭不重开断言保留。demo 先返回 awaiting_exam_scoring，不伪造教师操作；PowerShell提示新增相同状态断言。新增API/PG/UI测试与实际浏览器证据验证对应动作，金额输入为字符串。

- 全量回归发现新增发布门禁的旧合成夹具缺口：tests/contract/test_grading_api_contract.py::mixed_scenario 直接插入 Approved 题且客观题缺 Rubric，再调用真实发布入口。仅补该生产者的明确合成标准、当前语义核验/批准及本场准备确认，保留原持久评分、失败错误和不留部分结果断言。后续同类失败须逐一定位后登记，不放宽业务校验。
- 真实浏览器通过基准3.00→本场10.00、三个1.00要点独立3.33/+0.01、明确确认3.34/3.33/3.33、实际教师UTC和发布；空白格使用Gradio父单元格+Enter/Tab路径可完整手工输入。

- tests/integration/test_teacher_setup.py 旧完整教师流程在当前题目批准后直接发布，缺T174新增的本场标准步骤。仅在原发布之前通过真实HTTP GET/prepare/confirm补定性标准核对；旧课程/文档/题目批准/最终持久状态断言全部保留。

- 全量还复现两个同源资产事务失败：新增失效 UPDATE 触发 autoflush，资产服务尚未把文件收据返回调用者就提前INSERT失败，导致外层没有失败收据。实现局部修为失效UPDATE使用 no_autoflush，保留原显式flush/commit与收据归属；不改原测试/断言。RED/修复后聚焦及最终全量均保存。

- 最终提交源码快照 tree `7551c2a35dfb7493b0bfb2d2b212bf8be6816c5d` 全量 **2362 passed / 2 skipped / 107 warnings**（861.74s）；两skip为原M0隔离配置和Windows软链接条件。首轮2357 passed/3 failed/2 errors/2 skipped与修复后结果并存。Ruff22变更Python文件、全backend mypy207及隔离Alembic check通过；实际浏览器确认+发布与数据库回读通过，独立教师标注口径未变。完整证据见 `docs/evidence/t174-20261004/`。


## 30. T175 原子发布及全引用冻结 TCR（实施前，2026-10-04）

- 必要性：T174只完成发布必要依据，T175需核对所有直接写入和级联删除入口的真实保护，不能用Approved守卫代替发布/历史冻结。
- 覆盖：发布固定四项本场依据且原子失败；同课程Course→Exam→排序Question/关联锁与最新状态回读；并发发布/退修/内容/资产/父题来源/删除不得竞态放行；Published/Closed/Archived或答卷及历史引用继续保护。
- API错误：保留既有I01六类内容字段QUESTION_APPROVED_IMMUTABLE及题库difficulty/knowledge_points维护；发布引用的原地退修/解析/题图/来源/删除用QUESTION_REFERENCED_IMMUTABLE，考试事实修改用EXAM_PUBLISHED_IMMUTABLE；不通过级联删除解冻，不建完整内容快照。
- 测试：在现有pytest unit/contract/integration框架先记录暴露缺口的行为，再最小修复；真实PostgreSQL并发及事务回滚验证。旧断言保留，若生产夹具因新合同需补实际准备则逐条说明，不改标签或放宽标准。新增/修改文件和RED/GREEN证据在实施中追加。
- 本批仅独占DB/Redis/文件根；原库、两README、开发者阅卷样本及.env原字节保持。T146 AI辅助+开发者审查、独立教师0及T168 not_met不变。T179完整性能/系统验收另行执行。

- T175实际新增 tests/integration/test_exam_lifecycle_freeze.py、test_reference_lifecycle.py 和 contract/test_exam_lifecycle_api.py、test_course_reference_api.py；旧测试未改。新增28+root16与31/24相关回归通过（有重叠），直接回归123pass/1fail代码消除重复锁后45pass；静态208源文件和Ruff通过，详见T175证据README。六个新测试最初误把I01优先错误当成引用错误，已按既定合同修正，旧业务断言保留；静态接入和错误属性问题如实保留。


## 31. T176 固定评分输入与身份 TCR（实施前，2026-10-04）

- 必要性：实际 reader、恢复状态及 repository 尚在读取题库分值/知识点，不能证明两场同题的评分身份和固定依据一致。
- 覆盖：同题两场身份/不同满分/发布标签、选项原序、缺依据拒绝、图片当前核对、Decimal 字符串序列化、v2 检查点往返、显式 v1 缺失保留与未知版本拒绝、恢复固定事实核对、仓储错场拒绝及幂等主键保持。采用现有 pytest 单元/合同/隔离 PostgreSQL 集成框架；新增聚焦测试，不放宽旧断言。
- T176 中间提交保留旧消费者不支持的非等价输入执行门禁；T177 完成消费者后再放行。不新增数据表、迁移或哈希门禁；v1 历史未知不由题库补造。
- 仅使用本批独占数据库、Redis 与文件根，保留原环境及用户未提交改动；AI 辅助基准声明及 T168 not_met 不变。完整系统/性能验收由 T179 承接。

- T176 实施边界补充：工作流 JSONB 会重排 options 对象键，新增 ScoringInput 内部 JSON 编码以有序键值对数组运输，运行时还原 dict；真实 PG 往返验证，公开题目 JSON 接口保持既有形状。工作流载荷升为 v2，旧版本精确断言随版本升级，未知版本拒绝断言保留；v1 新字段缺失保持缺失。
- 实际 AgentInvocation 原未承载调用信封，为接通真实生产者/消费者新增可空 input（旧构造兼容）；新逐题调用生成 AgentInput 固定输入，Workflow 交接前匹配真实 target，不能只新增未使用的 schema 字段。

- 最终 T176 选项运输调整：实际 v1 支持字典/列表/嵌套 JSON 值；保留原合法 SQLite/PG 选项夹具，修新 DTO 适配。对象唯一编码为顶层 ordered_options，原列表/null 仍 options，拒绝歧义及双写；新增原列表、list-of-pairs、空字典及未知历史标记的往返断言。原 tests/unit/grading/test_grading_repository.py 的范围恢复用例仅更新 save_task 生产者，传本轮实际 reader.scoring_inputs，保留原作用域和 worker 消费断言；新版 worker 拒绝旧任务缺输入，不能用当前数据替造。

- tests/contract/test_workflow_api_contract.py 受控 Agent 新增真实调用 input 信封；原“缺 runtime”检查点夹具补本轮真实 reader 输入，以继续只验证其原恢复支撑拒绝断言。另增缺输入/错关联的 HTTP 409 断言，保持两类失败可区分。原合法列表选项及暂停/恢复/教师复核行为断言保留。

- T176 最终验证：输入/仓储 131 passed（含真实 PostgreSQL 6 项、SQLite 16 项及既有回归）；状态/Agent/API 218 passed，两组有重叠不相加。全 backend Mypy 208 源文件通过，最终全 backend/tests Ruff 通过，状态 6 模块最终类型检查通过。实际目标身份、题干/满分兼容投影篡改的 3 项 RED 修复后通过；旧合法选项列表、暂停恢复与复核原断言保留。原始失败、静态检查及最终 JUnit 见 docs/evidence/t176-20261004/。


## 32. T177 统一评分消费、复核与汇总 TCR（实施前，2026-10-04）

- 必要性：T176只接通真实输入，旧消费者仍按浮点/原Rubric评分并拒绝非等价依据；需验证固定本场标准、实际图片、身份和结果上限贯通全部执行及复核写入。
- 覆盖：客观题纯规则零LLM；主观使用已确认要点/满分且不二次缩放；真实已授权原图及已核对条件进入有图能力Provider、失败/不支持明确待处理、资源关闭；原始Decimal先校验上限再ROUND_HALF_UP两位及再次校验，0.005/近上限/非法数字/越界不裁切；同场EQ身份贯通Agent/Reviewer/Workflow/仓储；Confirmed/Modified/Re-grade和原检查点/主键生命周期保持；待复核/失败/缺依据不记零分、发布标签及Decimal汇总/诊断范围保持。
- 采用既有pytest unit/contract/真实隔离PG框架，受控Provider验证真实调用参数及边界，不代表模型质量或独立教师真值。必要旧生产者夹具变更逐项登记；原业务断言不得放宽。仅T177测试/实现与证据，不执行T179性能协议，不增加表。

- T177必要协议测试适配：原structured评分测试将合法字符串“7.0”作非法值，与Decimal金额字符串合同冲突；改为真正非法“七分”，另增精确字符串/Decimal正例，保留其余越界、缺字段和状态断言。原7.34等浮点期望改Decimal(str(expected))仍证明相同实际金额，不放宽精度。

- T177持久身份补充：真实save_single_result可脱离WorkflowRun执行，检查点无法作为每个结果的唯一身份登记。采用GradingResult可空exam_question_id FK/0023迁移，新固定结果保存实际关联，旧NULL不回填；Confirmed/Modified不得漂移，合法重新评分由当前固定输入明确写入。真实PG迁移验证旧行NULL、实际FK及upgrade/check、SQLite模型回归；只迁移本批隔离库。

- tests/unit/services/test_review_service.py 的既有 StubGradingAgent/结果生产者须提供实际 AgentInput 和真实目标 exam_question_id，新协议事实来自当前reader；仅更新producer，原低置信、分数、复核、主键和恢复断言保留。

- T177替换T176中间态门禁：tests/unit/grading/test_scoring_inputs.py同题两场与tests/integration/test_fixed_scoring_input.py真实已核对图的临时EXAM_SCORING_INPUT_NOT_SUPPORTED断言改为require_scoring_ready通过；完整消费者已接通，保留其余真实身份/发布值/图像证据断言和所有未知/失败门禁，不放行缺失依据。

- T177首轮真实HTTP/PG新增6例：3通过/3失败；root新测试误从M4 envelope顶层取state并误用Subjective错误类型，改读既有state段和GradingContextError同时保留精确错误码断言。真实中途缺文件还暴露新helper错误属性message不属于FileStorageError，代码修为原str(error)，原失败日志保留。
- 旧仓储范围worker夹具新fixed结果缺EQID：仅stub用本轮snapshot实际ID重建结果/上下文，不改范围/worker断言；模型精确列清单新增可空EQID及FK RESTRICT验证，原其他列约束保持。

- root PG第二轮5 passed/1失败为新增测试按dict访问ProviderImage；按既有Provider真实对象协议改为类型校验和.value/.mime_type，原字节完全相等及条件/无Base64检查点断言保持。

- T177扩大直接回归169例首轮153 passed/16失败，定位为 contract/test_workflow_api_contract.py 的T176 StubGradingAgent只增加了输入信封但未为新结果填写本场EQ身份。仅其producer从target.scoring_input取真实EQID，score/状态/原暂停恢复与复核断言保持，不放宽消费者。Ruff扩大至所有旧迁移另外发现0001的4项既有类型写法，保留未修改；实际本批检查范围为全backend/tests及新增0023。

- T177最终：核心14新例+直接旧回归共257 passed；仓储/汇总/迁移及直接回归181 passed；复核/Workflow直接169例分次全通过（139不变+30合同重验），其中新增25例；root正式HTTP/真实原图6 passed。组间有重叠不相加。新增图像关闭失败保留原FILE_MISSING，成功后关闭失败明确VISION_PROVIDER_CLOSE_FAILED。全backend Mypy209、全backend/tests/新增0023 Ruff、隔离upgrade/check通过；所有真实RED/夹具/静态失败并存。


## 33. T178 组卷/学生答题/阅卷统一显示 TCR（实施前，2026-10-04）

- 必要性：当前教师缺条件组卷与移位/替换/显式分值界面，学生API/UI仍读题库分值/标签并重排选项，复核未展示同一本场标准、实际检索及原图；这些是已接通评分事实的直接消费者。
- 覆盖：教师条件/失败缺口与intent_saved、真实题序/替换/score省略与null、准备尾差/发布确认保持；学生安全DTO严格同EQ题序/固定score/发布标签/原选项序/只明确允许图片，无答案/解析/标准/源卷/整页图，必要图隐藏或缺失显式阻断；参加资格/时间/保存/提交旧规则保持。复核读取结果已保存身份/满分及同一固定输入、已保存实际检索上下文、原图核对条件；未知旧依据明确显示。
- 图像由实际FileStorage授权读取bytes后内联HTML dataURI，不复制至Gradio公共文件缓存，不生成含JWT下载URL，不从selected状态信任file_id；所有正文说明转义。复核分数输入Number改Textbox以保留原Decimal字符串，相关组件类型/金额运输测试按新合同最小更新；回传已知pending_review_round_id以保持CAS轮次，旧未知为null。
- 采用既有pytest unit/contract/隔离PG；真实本地浏览器操作+持久回读验证代表流程，受控Provider/合成教师输入明确标识，零云调用。最终以排除原用户3个文件改动的精确提交快照运行全量pytest、Mypy/Ruff/迁移check，原文件字节及index保持。T179完整性能/系统协议不在本项宣称通过。必要旧producer更新及真实失败逐项追加，不修改旧行为断言。

- T178用户主动暂停收口（2026-10-04）：保持未勾选/未提交；教师GREEN22 passed，复核GREEN31 passed/2 failed，学生RED7 failed，复核API RED3 failed。各组有重叠，不汇总为完成验收。未执行全量/浏览器/最终静态检查；已停止全部本批Python验证。原工作区、原DB与所有失败证据保留；详见docs/t178-handoff-20261004.md，下一次从该记录继续。

- T178恢复（2026-10-05）：新增学生RED夹具在固定依据后直接改本场score，导致要点仍是旧满分，先修夹具生产者为与实际新score一致的显式确认要点，保留金额/标签/图像断言。复核points须读ScoringBasis.points[].confirmed_points（不是不存在的顶层confirmed_points），作者teacher_id。旧展示金额6.0→"6.00"按Textbox合同适配，其他断言保留。追加未知依据开始/保存/提交阻断、原选项顺序与同场安全HTML展示验证；所有证据使用新标签，保留暂停前失败。

- T178聚焦首轮63通过/2失败：真实文件合同通过create_app(settings=...)注入文件根，而新增SubmissionService依赖仍用了全局root；生产者工厂改为复用get_app_settings，不改文件合同断言或复制文件。该修复同时保证API与文件下载使用同一实际配置。主壳既有resettable_components递归包含HTML/State，新增原图组件无需另建重置机制。

- T178文件合同第二轮64通过/1失败，新增响应200断言并附真实body以定位，不改变原资产列表/永久私有/授权断言；复核loader已验证ScoringInput.assets连续1..n，直接按该真实顺序读图，不给缺失order_index造默认值。

- T178原图子集根因：第3张允许裁图构成单张读取集合，ContentValidationService._read_images要求读取集合序号1..n；学生读取只为此临时子集赋读取序号，原QuestionAsset.order_index、全组核对身份/原图顺序保持，未改真实来源或登记。导入核对先经既有_bound_check验证原事件与正式题绑定，不能再用正式字段名与原暂存字段名强求相等。保留原永久私有裁图合同验证。

- T178合并后全量首轮2536 passed/13 failed/5 errors/2 skipped：失败为T177合同补齐后尚未适配的producer及中间态断言。修改前登记：diagnosis_report_store本地造分前显式生成本场依据和关联ID（不改公共未知历史seed）；teacher_results_ui合成Teacher增加真实角色，定稿前补本场标准与实际EQID；grading_api_contract受控scorer补target.scoring_input真实EQID。保留原诊断/CAS/任务/成绩/UI断言。agent_state的float类型断言改Decimal且保留8.5值；迁移链追加已实施0023，不删旧链；T171非等价临时NOT_SUPPORTED门禁在T177消费者接通后改为固定满分/基准不随题库漂移的显式断言，旧缺依据拒绝及历史分数不变断言保持。只修这些实际失败，不降级业务检查。
- 验证辅助脚本第一次漏写snapshot manifest（循环变量覆盖），r1/r2临时index树及全部2234文件SHA完全一致后补存r1清单；工作区/真实index/保护文件未改。浏览器保存截图的只读EPERM通过受限本地证据接收器保存，不改变应用权限；Playwright fill未触发Gradio键盘事件，真实键入后已答2/2，保留前次明确未答错误。

- T178真实浏览器交卷发现既有confirm_submit返回11项而事件仅绑定8控件，导致服务已提交但前端Column收到interactive参数报错。先补真实Gradio postprocess与真实SubmissionService提交的RED，修事件绑定包含上一题/标记/下一题，保留交卷资格和服务状态；本问题必须修复后重验，不能将已落库当界面通过。

- 全量修复首轮71通过/3失败：新断言误用了ScoringInput.max_score，修为既有effective_score，金额断言不变。交卷RED证明8/11错位；其合成草稿答案原为GRADED，显式改本测试producer为DRAFT再验证真实提交，公共历史seed保持不改。

- T178最终兼容性复查：新学生写入门禁不应要求阅卷ScoringBasis（任务明确保留旧答卷提交规则）。先将本批新未知依据测试修正为原合法开始/保存/提交成功、分值保持未知且阅卷仍拒绝的RED，再删除学生路径多余评分校验；必要题图失败仍阻断。不是放宽新评分或改变旧参加/时窗规则；旧seed和既有拒绝断言不改。

- 旧答卷兼容聚焦首轮33通过/1失败：save_answers原合同返回答案列表，新测试误作答卷DTO读取id；改为核对实际保存答案的题目ID和内容，其余未知分值/阅卷拒绝断言保持。两个误写测试路径的无运行记录保留，不计通过。

- T178复核部分成功边界复查：教师修改已提交但图恢复失败时，M4检查点保留原模型score/reason，数据库已是Modified；它们是合法可变评分，不是固定输入身份。先补真实HTTP modify＋受控恢复成功/失败的显示RED，核对2.345→2.35且同轮已保存检索正文保留。仅Modified跳过原模型score/reason相等门禁，其余本场身份/固定输入/上限/发布标签/检索ID及原错场/未知历史拒绝均保留，不重写检查点。

- T178最终收尾：2557 passed / 2 skipped / 0 failed / 0 errors；pytest 991.147s，外部总耗时 1001.464s；Mypy209文件、Ruff、隔离Alembic check通过。两跳过项明确为M0独占配置缺失/宿主符号链接权限，保留首轮真实失败、中间通过和主动停止回执。最终源码与受测快照同一实现，仅pytest夹具noqa注释/换行AST一致。真实浏览器缺口/换位替换/本场值/原图/发布/提交冻结/复核及实际M4回读证据见docs/evidence/t178-20261004/；只勾选T178，不声明T179性能或模型质量通过。原环境与用户文件保持，独占验证资源已清理。


## 34. T179 条件组卷、分值及发布冻结验收 TCR（实施前，2026-10-05）

- 当前基线615319e；仅执行T179。保留用户两README、开发者阅卷样本和.env原字节，不改T146 AI辅助+开发者审查/独立教师0及T168 not_met。独占数据库、Redis、文件根；零新增云调用。
- 先复用既有pytest unit/contract/真实PG覆盖条件满足/不满足、失败只保存意图、移位替换、同题不同满分、默认/0.005/正负尾差/越界/历史保护、旧API/迁移。新增tests/integration/test_t179_publication_races.py是必要缺口：实际双会话阻塞观测后发布与退回修订/资产写入串行，反向修订先提交不能发布过期依据；核对身份/原内容/关系/文件不变，不用sleep当并发证明。
- benchmark/t179/新增可复現验收入口：沿用T146原300候选与12组卷案例；实际创建受控语义前置及合成账号的审批/图像核对，完整标识作者/教师身份/UTC，绝不把原not_created改成历史批准。业务判定逐项检查持久事实及意图；未知/策略未找到与数学无解分开。
- 诊断不混入正式计划。正式性能1/25/100题×可满足/明确不可满足，各3冷＋1预热＋5暖，共54动作、48计时；同配置同内容新草稿实例、实际300候选、并发1。真实浏览器从提交条件至完整预览或真实诊断渲染；3秒目标、200题仅业务请求边界。真实worker PID、1秒内存采样、组件及同时观察合计、覆盖不足明确未知；不捏造内存峰值或放宽原目标。
- 所有失败/超时留证，不补跑覆盖原编号；修复另立批次。最终真实结果落benchmark/results/v2、docs/validation-report.md及docs/evaluation.md，未达标不勾选T179。新测试变更前登记本TCR，必要实现缺陷修复另记原因与受影响合同。

- T179并发初轮2通过/1失败：发布确已因最新Needs Revision拒绝；新增测试误将“状态退回”当作“内容修改”并要求basis清空。按现有合同，输入未变不重写本场依据；改为同时断言状态Needs Revision、考试仍Draft、关系不变及原basis原样保持。未改业务代码或批准/发布门禁，原失败收据保留。

### T179 性能诊断后的局部修复范围

真实 100 题界面诊断为 11.365 秒；回调剖析显示每道候选重复获取同课程锁、刷新题目及关联、查询最新报告，25 题请求产生约 3772 次 SQL。必要修复限定于组卷当前事务内的批量读取，仍依次锁课程/题目/最新报告、逐题核验真实输入、图片和来源；作用域退出即丢弃，不能跨事务复用旧通过。保留单题命令、公共 API、迁移、200 题边界及业务失败记录。重跑现有 E4 与内容核验回归及完整新性能批次；不修改旧断言或协议目标。

第二轮诊断：批量核验后 1/25/100 题为 0.829/1.815/3.168 秒，100 题仍超标。界面先后两次对每道已选题调用单题读取（每次重复 Course/Question 查询），改为复用课程授权的题目列表查询并显式按关联 ID 过滤，返回时保持实际考试题序，缺失关联继续报错。新增过滤仅用于内部读取，旧接口和单题命令保持。新增真实 PG 用例检查过滤、状态、不存在/空集合及跨课程授权；短动作资源观察改为提交开始时立即启动并并行观测独立组件，保持每秒采样和真实缺测。

正式 v5 批次因代码复核中发现批量核验提前拒绝跨课程历史异常关联而主动终止，保留所有原编号和已完成观测，不宣称完整通过。批量作用域只加载本场课程关联，外课程关联继续由原有逐题 `EXAM_QUESTION_COURSE_CONFLICT` 诊断负责；新增实际 PG 历史异常夹具检查该兼容事实。修复后完整另开 v6，不拼接 v5。

T179收尾：聚焦378通过、最终兼容58通过（重叠），mypy209/Ruff/Black通过；固定v6全部54动作、48正式均业务/3秒/资源通过（最大1571.90ms）。仅清理已落盘且归属/可编辑核对的性能草稿并逐次恢复固定数据库初态，原失败/终止证据不替换。原库及用户文件只读核对相同，T146/T168口径保持。


## 35. T180 教师考情统计（2026-10-05）

必要性：原摘要只有最终平均及待复核题数，不能证明参与分母、发布知识点归因和失败不计零分。新增 tests/contract/test_results_analysis_api.py，复用既有隔离数据库、真实结果仓储及认证 TestClient，不更改旧测试断言。覆盖分配/全体开放、真实草稿参与、最终/复核/失败/缺依据分列、实际分值与零分、多知识点非加总、零观测、同题跨场、历史未知及角色/课程边界。分布按实际最终分值频数，不新设及格阈值；关注给出真实失分或未完成原因，不作模型判断。T184 的完整 UI/SC-013 验收留至其任务，不冒充独立教师标注。

T180 结果：新增11项与旧结果/页面/仓储/汇总聚焦回归合计118通过。两次新增夹具setup失败保留原始记录并修正；不改旧断言。静态类型的 Optional/变量命名两处修正后检查通过。完整 SC-013 对照仍留至 T184。


## 36. T181 本人最终失分与来源推荐（2026-10-05）

用户确认：复用本人最终答卷的上下文授权，开放同课程可信标签的 Ready 教学片段、当前已审核且有真实来源的练习；原文件教师权限及 student_visible/源卷永久私有规则保持。不新增授权表。新增 tests/integration/test_learning_feedback.py，真实 PostgreSQL 隔离空间验证 JSONB 精确标签及授权接口，覆盖当前最终/重评/待复核、真实来源、未知/缺资料/缺题、未审核/待补全/跨课程、原图字节/私有全页、报告过期与 GET 不写库。新增单元测试验证最终失分映射与无标签未知。保留现有诊断生成口径和旧测试，不改变 T146/T168 质量结论；完整师生 UI 和 SC-013 留给 T182–T184。


T181结果：先行16项真实PG和3项单元验证19通过；最终聚焦回归121通过/1既有Starlette弃用警告（79.83s），全backend Mypy209、Ruff/Black6文件通过。首轮13失败是独占DB未准备启动迁移，第二轮10通过/3失败是新增report夹具缺真实结果身份（2项）与PNG被错误标为JPEG（1项）；保留原失败，按生产者与原件读取责任修复，不改旧断言。原类型Optional问题修正后检查通过。真实API/持久结果报告关联、授权片段与练习、原图字节证据另存；受控合成数据和Provider不计模型质量。零连接后独占DB清理，保护文件原字节相同。自动审查曾误判新增测试插入脚本可能截断，拒绝动作未执行，改用唯一文本匹配/保留原测试计数后完成；没有残留审批阻塞。


## 37. T182 教师考情分析页面 TCR（实施前，2026-10-05）

基线15ab0df；仅T182后再T183。必要新增tests/unit/ui/test_results_analysis_view.py及tests/integration/test_teacher_analysis_ui.py：现有页面无法呈现T180分母、分布、失败/缺依据与关注名单，且未展示选中答卷内容。先写目标行为并记录RED，再复用生产加载器/真实结果仓储及Gradio process_api验证展示、零/未知、权限、刷新清除旧选择和关注名单→实际答卷/复核。保留旧测试断言及回调旧字段位置，新增投影不重新计算统计。真实页面操作另存受控合成账号/数据证据，不声明T184完整SC-013或模型质量；保护用户4文件字节、无云调用，独占验证数据库/目录按真实归属清理。

T182收尾：64通过/1既有警告，类型与规范通过；首轮38通过后追加已登记权限边界RED，修复_teacher_summary_loader调用前的角色检查。所有旧断言保留，源语句noqa被Black折行的静态失败修正，行为不变。浏览器真实加载、分母显示及关注项→待复核答卷通过，受控合成数据不算SC-013正式基准。


## 38. T183 学生知识点反馈页面 TCR（实施前，2026-10-05）

必要新增tests/unit/ui/test_results_learning_view.py、tests/integration/test_student_learning_ui.py：原学生页缺自身答案/原图和来源入口，未覆盖T181上下文授权到Gradio的读取/失效。先RED，验证当前最终归因、0/未知、报告Stale/Failed、非最终不造弱点；真实PG/JSONB及Gradio process_api覆盖考试选择、实际答案/原图字节、资料/已审核练习来源与选项序、缺资源、不开放源卷、重评/可见性变化后原链接拒绝及会话重置。保留旧8项回调和旧断言，原主壳首页成绩入口接通完整新增区域，不仅更新表格。

消费者需要非最终处理事实，T181 DTO/API增量提供processing_status/processing_error_code/processing_reason；提取T180已有分类为共用函数保持同规则，以当前真实Workflow/最终结果为源，不让UI推断失败为零分。无新增表/迁移/依赖或模型调用。原始材料、参考基准和T146/T168口径保持；实际合成账号/数据操作证据与系统质量验收分开，T184另行执行。

T183 调试记录：首次聚焦单元检查 29 通过/1 失败，原因为新加载器注入早于视图参数接通；补齐对应参数。新增授权断言按实际 `GradingPermissionError` 常量 `GRADING_PERMISSION_DENIED` 编写，拒绝行为要求不变。

首轮真实PG：16旧项通过/3新增项失败；新增夹具跳过实际考试选项加载，被Gradio前处理按既有合同拒绝。修正为调用真实refresh_student_exams后继续，未放宽选项或授权断言。

扩展真实PG：8通过/2失败。角色拒绝正文未显示已知权限异常，修复显示层保留明确角色提示。退出新增断言误遍历主壳全体HTML（含按合同返回原字符串的公共面包屑），修正为只定位本任务两个详情与来源选择状态，仍要求清空原值。

最终聚焦首轮104通过/1新增断言失败：退出表格保持Gradio初值结构 `{headers,data,metadata}`，应核对data为空；按该真实序列化合同修正断言，未改清理业务逻辑。

105项最终聚焦通过后发现实际无本人答卷的页面可能保留旧M3未就绪占位，新增真实空考试/答卷检查并让当前学习反馈回调清除此旧占位；原8项回调返回不变。此新修改后只复验受影响的学生/首页/接线/教师共享界面，并再次静态检查。

T183收尾：14项新增行为检查全部通过；105项聚焦通过后空态修复60项最终通过，两组重叠不累加。全backend Mypy213及10文件Ruff/Black通过，真实浏览器答案/原图/来源练习查看通过。完整SC-013对照仍由T184完成；受控账号/评分/语义夹具不冒充教师标注或模型质量。独占DB零连接/0schema清理、页面端口关闭与4保护文件相同均登记回执。


## 39. T184 两类分析的冻结参考对照（2026-10-05）

- 必要性：T180–T183 已有业务与 UI 边界用例，但尚未把 T146 冻结统计参考逐项送入真实 PostgreSQL、评分持久服务、授权 API 与展示层，不能以已有断言推定 SC-013 的数字一致。
- 新增：独立运行上下文、冻结 CSV 比较器及参数化合同用例；覆盖两场同题不同分值、多知识点重叠、最终/待复核/失败/缺依据、筛选零样本，逐题学习失分及 UI 分子/分母。补充真实 ReviewService 复核前后查询。
- 保留：旧断言、原参考与未知标签不改；预期来自冻结 CSV/JSON，比较器不调用业务统计生成预期。运行中新增答案/评分候选仅为明确记录的合成执行上下文，不能变成教师真值。原未知参加状态不评价；分箱只作评测投影，不新增业务分箱接口。
- 验收口径：沿用用户批准的 AI 辅助＋开发者审查学习项目口径；独立教师覆盖仍为 0，正式 SC-013 独立教师项留待增强。不得以学习对照结果宣称正式教师质量。
- 回归：既有授权/无资料/未审核练习/题图私有/真实错误与复核测试；不调用云模型。结果待本批实测填写。

- §39 实测：78 passed/0 skipped；150/150可比较项一致，未知4/来源项2另记；最初新夹具认证绑定、主观决策上下文及分箱单位问题已修正，业务断言/业务代码未放宽。真实记录见 docs/evidence/t184-20261005。


## 40. T185 公共 UI 与三类代表页（2026-10-05）

- 必要性：导入页新增显式编辑/取消，须证明打开详情不误写、取消还原持久值、保存后关闭编辑且保留原页；新增公共状态映射须保持错误转义和六类状态可区分。
- 新增：沿用真实导入服务夹具验证注册回调的开始/取消/保存与持久值；六类状态语义及恶意说明转义。旧断言不删不放宽，展示编辑许可继续来自原导入业务状态，底层保存/拒绝/确认校验不改。
- 验证：聚焦已有 UI、导入/结果合同与实际 Gradio 操作，浏览器三页截图（导入校正、教师题库、学生诊断）及操作记录；共享主题/图标不引入新依赖。七类逐页由 T186 完成，三页不宣称 SC-014/EXE 全部完成。

- §40 实测：67 passed/0 skipped、13项既有框架警告，42.42s；7文件Ruff/Black及全backend Mypy213通过。完整生产Gradio＋真实身份＋独占PostgreSQL三页操作/7截图及持久回读已保存，取消未写入、保存真实解析；非教师/云质量验收。清理回执见 docs/evidence/t185-20261005。


## 41. T186 七类 UI 操作验收（2026-10-05）

- 必要性：新增显式编辑/取消需要证明只从当前授权的持久读取恢复值，不提交人工评分决定、不沿用过期选择；复杂页面减少同时展开表单，保留既有回调和状态生命周期。
- 覆盖：注册的取消复核回调重读当前身份的真实加载器、不调用决策；学生会话拒绝并清空旧字段；沿用知识库/考试/出题/阅卷/分析/首页原用例验证绑定，完整工作台逐页实际操作/截图和数据库回读验证新取消/保存。
- 不删除/放宽原断言，不改变审批、评分或持久语义；控制夹具与实际业务运行证据分列，七页UI验收不等于模型质量、性能或EXE交付。

T186 首轮新增RED：缺取消回调2失败；接线后126通过/1新增用语断言失败，实际拒绝为“无权访问阅卷复核”，按原权限合同修正新断言，不改旧断言。临时测试入口输出GBK编码失败已改为UTF-8读取日志，原业务错误保留。

- 实际七页操作发现表格 SelectData 注入使认证参数索引偏移；新增注册表格回调测试，验证注入事件后的真实会话参数仍被认证，并保留事件及记录。只修复包装层索引，不改变授权语义。

- 七页实际批准发现旁栏保留旧状态；新增注册批准回调测试，覆盖写入后从当前授权详情回读并刷新核验旁栏，原审核服务及六元返回合同不变。

T186 最终结果：129 passed / 0 skipped（11项既有警告，40.55s），新四项全部通过；真实浏览器批准后双处状态一致，数据库回读 6→7 Modified / Workflow Completed。后端 mypy 213文件通过，Ruff及 Black 通过。证据 docs/evidence/t186-20261005。

## 42. T187 Windows 单机启动器（2026-10-06）

必要性：新增进程入口涉及外置配置、迁移确认、就绪及进程所有权，旧 Docker 入口测试不能证明这些约束。新增 tests/unit/deployment/test_windows_launcher.py：缺配置不得启动；外置可写数据目录；缺本地模型与缺OCR资源不得宣告就绪；迁移失败不启动；就绪超时关闭仅持有的子进程；正常退出仅停止所属子进程。真实隔离 PostgreSQL 迁移及源码启动由独立运行证据承接。保留旧用例/断言，不以 mock 证明 Windows 系统交付。

T187 增补浏览器打开顺序用例，共8项新增；连同原配置/数据库/Redis共35 passed。实际空库迁移、原API/UI readiness与仅所属进程退出已测；mypy部署2文件、Ruff、Black通过。辅助脚本测量错误已记在运行证据，不放宽生产迁移head核对。

## 43. T188 onedir 构建与实际包冒烟（2026-10-06）

必要性：打包静态/动态模块与外置配置无法由源码测试证明。保留全部既有测试，不新增镜像构建清单的单元测试；实际独立构建环境 pip check、PyInstaller构建、EXE --help/缺配置错误、隔离数据库上的包内迁移与API/UI就绪、实际原页渲染及OCR运行库加载用于验证交付包最小功能。T189完整冷暖重复、故障矩阵及资源预算仍待执行。

T188 结果：实际完整构建脚本成功、111项固定依赖pip check通过；最终包迁移/原API/UI/CSS/JS/PDF/扫描OCR/授权PNG通过；受控空ExtractionBatch仍真实失败，不改模型质量。缺资源与冻结日志编码已修复，源码启动器/原核心35 passed（10.75s），mypy部署2文件、Ruff/Black通过。单次包资源冒烟20.065s不替代T189完整协议；冻结正常退出/资源预算未测。


## 44. T189–T191 真实交付与兼容/闭环验收（2026-10-06，实施前）

必要性：源码和受控冒烟不能证明真实EXE重复启动、资源窗口、退出所有权及完整业务备份。新增独立验收入口/原始回执，复用既有ResourceSampler、隔离PostgreSQL和既有业务服务；不修改旧用例、断言或教师标签。T189固定3首次/5后续及配置/依赖/迁移/模型网络/OCR/重启/退出；仅有实测缺陷时新增对应部署回归。T190运行完整原框架与原Benchmark，T191真实模型课程和隔离恢复；受控故障、AI辅助参考与真实模型明确分列，失败/未知不填成功。正式质量与独立教师缺口保持。

### §44 收尾事实

T190 实际空库 Docker 发现启动恢复检查点依赖迁移、而旧 M0 先等待就绪的循环。仅调整脚本编排为依赖健康→一次性迁移→Backend；复用原 tests/unit/test_m0_smoke_isolation.py、tests/contract/test_docker_demo_config.py、tests/integration/test_m0_smoke.py，没有新增/修改 pytest 用例或断言。最终14单元/契约通过、原集成1/1通过；首次 Docker info 超时 skip/后续观察失败保留。

新增 benchmark/t191 入口只驱动真实授权 API/服务和已批准的合成模型输入，记录显式业务补全与所有原失败，不写替身模型或回填通过状态。实际工作流暂停→带轮次复核→完成→两类分析、20文件/32表隔离恢复通过。观察脚本的旧轮次字段、评分上下文、发布/开放顺序、评分入口以及不存在的总分列错误分别保留；只修正新观察者或另建真实输入，不放宽既有来源/审核/冻结/复核规则，不改旧质量标签。规范化只作用于本批活动入口，历史冻结 entrypoint 不格式化。

原始 pytest 日志/XML 和进度输出包含空行空白或CR字符，git diff --check 对这些原始结果报空白；保留原件，不为格式检查改写已生成证据。活动源码/文档按单独范围检查。

## 45. T189 启动优化与 T168 质量修复重验（2026-10-06，实施前）

必要性：真实 EXE 后续启动 0/5 达标、一个资源窗口因无关 /proc 进程退出缺测，T168 自动提取与语义误报未达标。保持原标签、阈值、原测试及失败证据；仅增加能证明缺陷修复的回归与独立实测。

T189 覆盖：Windows 默认 certifi 文件加载与内存 PEM 加载的完整信任证书集、校验/主机名/协议设置一致；自定义 CA/capath/cadata 不受影响；非法证书不会降级关闭验证；重复安装不叠加包装。只在 Windows 启动器显式安装，不改 Docker/普通应用入口。容器采样排除无关退出进程，真实目标进程无法读取或无目标仍缺测，不放宽原资源完整性和预算判断。启动原合同、退出所有权、缺依赖、迁移错误继续运行既有测试。重建真实冻结包后固定 3 首次/5 后续完整协议；不打开浏览器。

T168 后续先核对首个字段错误与真实来源，再记录必要回归范围；新提示词与原始标签/运行输入分开版本登记，不能通过改标签、扩大正确性比较容忍度或将未知算正确来通过。最终门禁明确区分工作区与提交样本，保留原开发者审查数据。
§45 补充：预检创建但从未调用的 DeepSeek SDK 客户端造成重复 SSL/SDK 初始化。延迟至首次真实请求创建，并在实例构造时保存原凭据/端点/超时快照；空闲关闭不得创建客户端、已关闭实例不可重新开启；调用复用同一客户端，外部注入的客户端仍由调用方释放。Embedding 工厂只在选中对应 Provider 时导入其模块，不改变注册名称、当前配置校验或实际请求。新增回归先证明这些生命周期约束，既有 SDK 请求/模型身份/重试/Trace/图片测试保留。
§45 启动编排决策补充：用户确认冻结包可提前创建只预加载模块/界面的自有工作进程，预检全部成功后通过内部 stdin 信号才启动 Uvicorn；迁移失败不得启动业务服务并清理工作进程。原源码串行入口及其“迁移失败不创建子进程”测试保持；新增冻结路径的失败无信号/无 readiness/无浏览器及停止所有权、成功信号在全部预检之后、工作进程 EOF 不服务的回归。Loopback readiness 使用无代理 opener，仅访问写死的 127.0.0.1，不改变云请求的代理/TLS；回归不允许本机探测经过外部代理。
## 46. T168 既有提取/语义流程质量修复（2026-10-06；测试修改前）

必要性：固定真实响应出现题干前缀/图表混入、题序与JSON选项键序重排，语义四维串项误报。新增内部可选原文定位证据，生产者依据真实页号与唯一字面位置排序；不从原题号大小猜题序、不改变公开DTO/持久字段。覆盖模型倒序与非单调原题号、同页C/A/B/D选项顺序、源页之外/非唯一定位及缺失选项证据拒绝、定位证据不持久化、旧无定位响应保留兼容顺序及assets/source_regions未知。保留原来源/答案/跨页更新等全部断言。

语义修改仅版本化提示词，分别判定答案、题面条件、选项、Rubric，不添加样本ID规则或放宽当前输入/来源/修订合同。原测试验证JSON/当前依据/真实错误，保留；准确性以冻结标签三轮真实模型请求重测，不新增提示词字符串镜像测试。原标签/运行上下文/阈值不改；有争议的SEM-ADAPT-GOOD和NO-BASIS阻断照原口径计量并单列。图片未知保留，不伪造空资产；图片定位裁切不在本批实施。

§46 观测入口补充：SDK改为惰性后，旧评测入口在绑定只读HTTP响应钩子前必须显式取得实际客户端，否则导入入口断言失败、语义/图片原始响应缺测。只改活动benchmark入口，在首次已授权真实请求前调用现有_request_client，保持调用次数、原响应脱敏、生产生命周期和历史冻结脚本；不以缺响应的重跑替代原始证据。

§46 版本断言补充（修改前）：5项实际持久报告回归已通过行为断言但在旧v1提示词版本断言失败；将仅更新该确切预期为question-semantics-v2，继续验证本轮真实版本、身份和结果，不把历史报告重写为v2、不降低来源/拒绝断言。

§46 真实失败补充：首轮新批12/15失败，模型给多个选项复制同一整行原文，真实位置相同。保留独立来源位置约束及首轮失败；新增共享整行证明必须拒绝的行为回归，提示词v4仅要求最小独立摘录，不修改比较规则/标签。重跑仍固定全部15次、失败不移出分母。

§46 根因续查：v4仍将无标签判断选项制造为字典，短语“正确”还出现在评分标准中，定位歧义；部分题保留类型/分值元数据。v5仅明确无标签选项使用原合同支持的列表、字典只用于原标签，题干与类型/分值字段分工。原未知与定位拒绝保持，不加入样本ID或源文字替换。v3/v4全批失败及执行源码保留。

## 47. 最终门禁旧生命周期测试适配（2026-10-06；修改前）

完整提交快照2643 passed/1 failed/15 skipped；唯一失败是旧test_paper_provider_lifecycle仍猴补已移除的模块级AsyncOpenAI名称并假定未使用的构造器即有SDK。T189已批准惰性客户端，业务实现未出现关闭失败。必要修改：在实际openai SDK边界注入替身，经真实generate_structured首次使用后检查自有客户端关闭；原caller-owned不关闭断言和对象身份检查保留，不跳过测试，不恢复空闲客户端的提前构造。未使用不构造/关后不可重开等已有新增回归继续覆盖。针对本次测试接线变化重验原测试及全部直接相关启动/DeepSeek/导入/Embedding合同，不拼接成一次新的全量通过；原完整失败日志保留，业务代码没有后续变化。

## 48. T168 按字段门槛与重验（2026-10-07，修改测试前）

用户明确更改本批质量门槛，不修改标注或文本比较规则。新增benchmark字段判定入口复用import_baseline.compare/aggregate，按12项新阈值读取真实15槽快照，保存失败与原分母，资产未知仅排除门禁而不改事实。必要测试位于tests/unit/benchmark：验证恰达阈值/低于阈值、零可评分母不得通过、关键字段100%不足拒绝、来源未知分母排除但明确缺失仍评、assets不阻断及计划重复/缺失拒绝。此为评测判定边界，不能用全字段AND代替新逐字段门槛，也不写只验证提示词包含某句的测试。

content/knowledge_points只改现有模型提示词：区分明确未标注与未知，不猜知识点；保留跨页真实指令，仅规范不改变数学含义的排版。不改变答案/解析/Rubric来源验证，不修改P0–P4、E1/E2公共接口、迁移或既有测试断言。验证提示词效果依原完整三轮真实Provider基线，不用受控返回代替模型质量。TCR§47的生命周期修复已经在de8551a，沿原测试及全量验证，不重复改测试迎合通过。

§48收尾：新字段判定RED后4项通过；prompt-v6及生命周期聚焦22通过。原PDF依赖恢复后9项原合同通过。最终一次完整提交快照2661 passed / 0 failed / 0 errors / 2 skipped，mypy214源文件/活动Ruff通过；原中断与M0首次下载超时保留，未改M0断言/240秒超时。M0全量前置响应超时跳过后，独立补验1 passed / 0 failed / 0 errors / 0 skipped，分开保存且不加总。真实15槽DeepSeek字段baseline仍因目的地确认待决未执行，质量调用0，新准确率null；不把本地OCR预检或旧v5计数称为新版质量。


## 49. T189 剩余启动耗时优化（2026-10-07，修改测试前）

当前源码实测导入10.124s、完整界面构建2.686s，统一重试模块仅为识别异常却在导入时提前加载完整OpenAI SDK，累计1.585s。将SDK异常类型导入移到真实异常分类边界，保留异常类型、优先级、脱敏、Retry-After和重试预算。新增真实SDK超时/限流/连接/4xx/5xx分类回归；原重试/Provider/Workflow/部署测试及断言保持。性能效果仅依据新冻结包3首次/5后续完整协议，保留旧失败，不降低10秒目标、不使用旧服务或源码耗时替代。
