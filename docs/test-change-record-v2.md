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
