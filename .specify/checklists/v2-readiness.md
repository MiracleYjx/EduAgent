# v2.0 实施前设计质量 Checklist：EduAgent

**Purpose / 目的**：评审 G01–G09、实施依赖及 v1.0 兼容性；检查需求与设计是否明确、完整、可追溯，不代替代码测试。
**Created / 日期**：2026-10-01（Asia/Shanghai）
**Feature / 依据**：[规格](../spec.md)、[计划](../plan.md)、[数据模型](../data-model.md)、[契约](../contracts/)、[既有分析](../../docs/v2.0-analyze-report.md)
**评估基线**：deepcode / c94bc0f；收敛前 tasks.md 最大编号 T133，最高 Phase 9。当前工作区已有用户 UI/README/测试改动，未修改。
**深度与时机**：标准深度，实施前评审；重点为持久承载、跨层依赖、默认调用与历史数据兼容。

**Review Ownership / 归属**：本自定义清单由评审者维护。按本次用户要求提供逐项文档评估；所有新复选框保持未勾选，供评审者确认。
**Marker Semantics / 标记**：`[x]` 仅表示需求质量经评审满足，不表示实现完成。以下“通过/待处理”是本次受托分析结论，不是运行验收或人工签署。

## 完整性：九项设计缺口

- [ ] CHK001 章节身份、课程/资料归属、小节顺序、真实知识点标签及旧未知数据是否具有明确持久映射和写入责任？[Completeness, Gap G01, FR-024/025, rag-retrieval §章节范围限定, data-model §3] — **待处理**：查询闭区间已写明，身份与生产者映射尚未闭合。
- [ ] CHK002 每轮语义核验的分项结论、证据、技术错误、实际执行主体、教师处置及内容变更失效关系是否有持久定义？[Completeness, Gap G02, FR-047, agent-workflow §语义核验节点, data-model §10] — **待处理**：QuestionValidationResult 仅被提及，教师文字意见不能承载机器核验。
- [ ] CHK003 暂存题号、解析、边界、知识点、资产及正式题解析是否有校正、转入和冻结的存储映射？[Completeness, Gap G03, FR-018/028/041/042/049, paper-import §结构化对象边界, data-model §7.3/8.2] — **待处理**：逻辑 DTO 完整不等于持久字段完整。
- [ ] CHK004 稳定 file_id、资源授权、共享文件、导出归属、迁移登记及一致备份清单是否有可恢复的唯一映射？[Completeness, Gap G04, FR-044/045, file-storage §文件标识与访问接口/一致备份与恢复] — **待处理**：ManagedFileView/BackupSet 的物理承载未定，不预设必须新增两张表。
- [ ] CHK005 图片条件、理解问题、人工核对身份/时间/说明及图片变化后的失效规则是否持久关联原资产或暂存图？[Completeness, Gap G05, FR-043/047, vision-capability §结构化调用边界, data-model §7.4] — **待处理**：caption/region 不能替代完整核对记录。
- [ ] CHK006 计划中的 Document 关系、导入终态及“模型/契约尚未更新”说明是否与已确认模型和契约一致？[Consistency, Conflict G06, plan §8/9及数据模型引用, data-model §7.1/8.1, paper-import §状态机] — **待处理**：非空唯一 document_id、Rejected 及阶段说明需要同步。
- [ ] CHK007 计划目录、文件契约和迁移/备份范围是否统一采用独立 storage/exports/？[Consistency, Conflict G07, plan §11及目录树, file-storage §目录与定位] — **待处理**：计划仍列 uploads 子目录或未列 exports。
- [ ] CHK008 教师组卷要求是否明确持久承载、更新语义和重启/替换后的同源复核依据，且不重复存可改写本场分值？[Completeness, Gap G08, FR-048, exam-assembly §题序、替换与预览, data-model §7.6] — **待处理**：Exam/ExamQuestion 未定义完整组卷约束存储。
- [ ] CHK009 性能重复次数、冷暖启动、样本/机器、质量基线和阈值确认时机是否形成可重复的评测协议？[Measurability, Gap G09, plan §v2.0非功能目标, Constitution V] — **待处理**：目标和公式已有，测量协议及基线后阈值待定。

## 依赖与追溯

- [ ] CHK010 任务是否明确文件持久化先于导入、定位先于限定检索、核验先于批准、发布依据先于评分与统计的依赖？[Dependencies, Gap, plan §8–14, tasks Phase 9/T133] — **待处理**：收敛前没有 v2.0 批次及依赖任务。
- [ ] CHK011 FR-041–052、扩展用户场景、SC-010–014 和 Gate 10–19 是否均有后续实施/验证任务，且三页样板不会替代七类最终页面？[Traceability, Gap, spec §版本与验收规则/SC-014, plan §Validation Gates] — **待处理**：设计映射存在，任务映射尚未建立。
- [ ] CHK012 基线复验、历史盘点、测试变更前 TCR、各批次证据与最终真实闭环是否有明确执行任务，而非以旧任务勾选证明完成？[Dependencies, Gap, plan §兼容性目标/Gate 19, tasks T133验证记录, Constitution V] — **待处理**：需补任务并记录既有失败，不改旧状态。
- [ ] CHK013 OCR 选型/版本是否要求真实样本与 Windows 推理验证后确定，且未被误写成所有候选适配器都必做？[Clarity, FR-042, plan §9, ocr-provider §抽象接口/配置与可选依赖] — **通过**：候选及锁定时机明确；选择涉及关键依赖时需用户决定。
- [ ] CHK014 数据库、Redis、模型网络、迁移与 readiness 的启动顺序和退出责任是否明确？[Dependencies, FR-052, plan §14, spec SC-014] — **通过**：本机服务和配置就绪后启动应用，仅停止启动器自有进程。
- [ ] CHK015 新图片生成、完整题目版本/内容快照、独立检索设施和局域网共享是否明确排除或保留为后续选项？[Scope, spec §已确认决策, plan §10/12/14, Constitution I/III] — **通过**：本版边界清楚，不能在任务中自行升级技术路线。

## v1.0 兼容性与一致性

- [ ] CHK016 原 FR/SC 与 v2.0 增量验收是否分开，旧 API 入口、字段语义和默认行为是否明确保留？[Consistency, spec §版本与验收规则, plan §兼容性目标] — **通过**：文档承诺明确，运行兼容须后续验证。
- [ ] CHK017 四种检索模式、融合/重排及来源返回是否保留，章节/知识点字段是否有兼容默认值和 Top-K 前过滤语义？[Consistency, FR-024/025/032, rag-retrieval §输入扩展/SQL强制过滤] — **通过**：同维并集、跨维交集，空新增范围保留原调用。
- [ ] CHK018 文本 Provider、结构化输出和四类 Agent/原阅卷状态图是否保留，新增图片能力是否不会强迫旧 Provider 实现抽象方法？[Compatibility, plan §10, vision-capability §Provider能力声明, agent-workflow §v2.0扩展, Constitution III/IV] — **通过**：supports_vision 默认 False，generate_structured 边界沿用。
- [ ] CHK019 教学文档的知识库关系、导入用途隔离与旧文档默认用途是否明确，试卷是否禁止自动产生知识片段/Embedding？[Consistency, FR-010/041, data-model §8.1, paper-import §范围与依据] — **通过**：knowledge_base 与 paper_source 的用途/状态责任已界定；计划差异另见 G06。
- [ ] CHK020 原教学引用快照及 persisted/history_unknown/no_sources 是否与父题、原卷来源分别表达，旧未知来源是否禁止推测回填？[Compatibility, data-model §8.2/10, question-source-persistence §source_type分类与兼容读取] — **通过**：新关系不改写旧来源语义。
- [ ] CHK021 ExamQuestion 升级是否保留旧关联、可解释题序、真实历史分值/评分证据，未知数据是否仍可真实读取而禁止按现值冒充历史？[Compatibility, FR-049, data-model §7.6/10, exam-scoring §历史兼容与验证] — **通过**：新增字段迁移与人工核对边界明确，不伪填旧成绩。
- [ ] CHK022 Approved 六字段守卫、元数据例外及发布/历史引用保护是否区分，退回修订与删除是否不能绕过冻结？[Consistency, FR-018/049, data-model §9.1, exam-assembly §发布冻结与修订] — **通过**：I01 是基础，发布即冻结还需新增实现。
- [ ] CHK023 同题多场分值、Decimal 精度/ROUND_HALF_UP、教师尾差确认及评分/复核/汇总单一依据是否一致？[Clarity, FR-049, data-model §9.2, exam-scoring §分值来源与发布依据/换算] — **通过**：只在草稿允许取题库默认，发布后使用固定依据。
- [ ] CHK024 OCR 可选性、原 Docker 三容器主路径及 EXE 单机补充路径是否一致，且不默改数据库或移除 Redis？[Compatibility, FR-042/052, plan §9/14, ocr-provider §配置与可选依赖, Constitution I] — **通过**：现有技术边界明确。

## 主流程、异常与恢复覆盖

- [ ] CHK025 文字/扫描/混合 PDF、PNG/JPEG、50 页限制、跨页/错序校正与原页溯源是否均有清晰要求？[Coverage, FR-041/042, spec US1-v2/AC1–2, paper-import §输入/状态机] — **通过**：按页选路径，超限不能截断成功。
- [ ] CHK026 Uploaded、Document.Ready、待校正、确认入库与 Approved 是否区分，缺答案/Rubric 的正式题是否明确保持待补全？[Clarity, FR-041/042, paper-import §状态机, data-model §7.3] — **通过**：入库确认不等于批准或可发布。
- [ ] CHK027 校正/commit 的事务、重复确认、非法批次、技术失败和重新解析来源保留是否定义？[Coverage, FR-041/042, paper-import §事务、重复请求与失败] — **通过**：同批原子提交、返回既有题、失败不伪装业务完成。
- [ ] CHK028 OCR 空白/坏输出、图像不支持/失败/不可靠和人工接管是否区分，并禁止静默换供应商或冒充理解成功？[Coverage, FR-042/043, ocr-provider §失败语义, vision-capability §失败与人工接管] — **通过**：错误与合法缺省清晰。
- [ ] CHK029 父题改编是否要求同课程、无环、真实引用、新候选与重核验，并保留教师批准责任？[Coverage, FR-046/047, question-source-persistence §原题改编, agent-workflow §语义核验节点] — **通过**：不能复制批准状态或以结构化合法替代语义正确。
- [ ] CHK030 组卷是否明确硬约束、无解与策略未找到的区别、失败保留草稿、替换/题序语义及发布前再次核对？[Clarity, FR-048, exam-assembly §输入/输出/原子性] — **通过**：不自动放宽条件或均摊总分，100 题仅性能规模。
- [ ] CHK031 文件缺失、未知、共享删除、学生源卷隔离、迁移失败和同一备份集恢复是否有明确要求？[Coverage, FR-043/044/045, file-storage §继承授权/历史迁移/一致备份与恢复] — **通过**：生命周期规则明确；身份和清单承载另由 G04 补齐。

## 验收标准与非功能要求

- [ ] CHK032 教师统计是否明确定义最终有效样本、分母、发布知识点、多知识点去重总分及无数据/失败状态？[Measurability, FR-050, SC-013, exam-scoring §汇总、分析与学生反馈] — **通过**：数字来自实际答卷，可与教师核算对照。
- [ ] CHK033 学生诊断、逐题解释和复习资料/练习推荐是否限定本人、同课程、真实来源与已审核题，并定义缺资料/待复核展示？[Coverage, FR-051/039/040, SC-013, spec US2-v2/AC2–3] — **通过**：已有诊断可扩展，无需预设新增分析表。
- [ ] CHK034 SC-010–014 是否包含成功、拒绝、重启、历史稳定和恢复场景，并区分需求达标与实际测量？[Acceptance Criteria, spec SC-010–014, plan Gate 10–19] — **通过**：SC 可追溯门禁，现有文档不证明验收通过。
- [ ] CHK035 导入/校正/组卷/启动计时边界、资源峰值及外部模型等待是否定义，是否禁止漏记慢样本与依赖资源？[Measurability, plan §v2.0非功能目标] — **通过**：目标及范围明确；重复次数和阈值仍由 G09 补齐。
- [ ] CHK036 OCR 自动提取与校正结果、语义 TP/FP/TN/FN、图片条件及组卷硬约束评测是否区分，并要求版本化证据及失败记录？[Measurability, plan §可评测目标, Constitution V] — **通过**：指标和真实性要求明确；不以模型置信度证明准确率。

## 评估结论与处理顺序

- 共 **36 项**：受托文档评估 **24 项通过、12 项待处理**。复选框 **0 项勾选**，不是“0 项需求通过”；标记归属与评估计数分开。
- CHK001–009 对应 G01–G09；CHK010–012 是当前缺少 v2.0 任务编排/验证映射，将由下一步 converge 追加任务覆盖，不能因此称设计缺口已修复。
- G01–G05/G08 在各自模型/服务实施前完成最小承载设计，并将实质取舍提交用户决定；G06/G07 同步已确认合同。G01 的闭区间已在检索契约写明，本轮不把旧报告中的“待确认建议”重复当作新决定。
- G04/G07 是 E1 前置；G03/G06 是 E2 前置；G05 是图片核对前置，承载与 G02/G03 协同；G01/G02/G05 是 E3 前置；G08 是 E4 前置。
- G09 的样本、计时、重复次数先于正式评测准备；质量阈值须在真实基线之后由用户确认，不能成为虚构阈值或阻塞不依赖它的 E0 只读盘点的理由。
- E4 必须覆盖考试分值从输入、客观/主观评分到人工复核、汇总和诊断的全链路；E5 依赖该事实源。E6 的三页样板与七类最终页面、EXE 与 Docker 均须有对应验证任务。
- v1.0 兼容性为文档层面的结论，尚未验证运行兼容；T133 记录的全量失败要求 E0 重新核实。T125–T132 保持既有状态，前缀缓存不自动成为 v2.0 必须先完成的阶段。

## Notes / 评审说明

- 本次仅生成清单、受托评估及后续任务；不补写 spec/plan/data-model/contracts，不改变现有代码或测试，不执行 implement。
- `$speckit-implement` 会读取清单状态，但不得替评审者勾选；后续请先复核本清单及设计任务的处理结果。
- `checklists/requirements.md` 使用独立生命周期，本文件不改变它。
- 详细缺口见 [v2.0 分析报告](../../docs/v2.0-analyze-report.md)；后续执行条目见 [tasks.md](../tasks.md) 的新增 Convergence 段。
