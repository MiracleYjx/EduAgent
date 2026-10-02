# 评测证据与看板边界

本页说明如何解读现有评测产物；从干净环境复现的命令、隔离 schema、manifest 与人工标签格式见 [Benchmark 复现指南](benchmark.md)。

## 数据和运行身份

检索初始化脚本 `scripts/setup_benchmark_corpus.py` 将语料摄取到独立 PostgreSQL schema，并以真实运行时 document/chunk UUID 生成 `manifest.json`。`scripts/run_retrieval_benchmark.py` 从该 manifest 取片段 ID，不要求数据库预置旧 UUID；`benchmark/corpus/chunks.json` 的旧标识仅供映射标注。结果 JSON/CSV 保存输入指纹、Provider/model/Prompt、检索模式、有效配置与失败状态，便于在同条件下比较。远端模型、网络和硬件仍可能使重复运行的延迟或输出不同。

| 评测 | 输出与单位 | 解释边界 |
| --- | --- | --- |
| 检索 | Recall@5/10、Precision@5/10、MRR、nDCG@10（比例）；p95 latency（ms）；有效样本量 | 对比时需核对相同语料/查询/标注与检索配置，并区分 Provider 差异。 |
| 阅卷 | MAE、RMSE（分）；一致率（比例）；有效教师标签数 | 只有 `teacher_score` 人工标签存在时才计算；无标签为 `null`，不填零或合成“教师分”。 |
| 管道自检 | `evidence_kind=pipeline_selftest`，Provider 标为 `stub` | 证明格式与流程能跑通，不构成模型质量结论。 |

失败运行保留失败原因；缺失指标仍为 `null`。评分结果还区分合成样本与教师标注，不能将合成参考分数称为人工真值。比较不同 Provider 时应使用同一数据集版本、Prompt、检索模式、标签与评分口径；模型或配置变化要在结果元数据中可见。已提交的历史结果不能替代新环境运行证据。

## 现有服务与 UI

`backend/app/services/evaluation_service.py` 在**可信调用者显式授权**后读取 `benchmark/results/` 中的 JSON/CSV，输出按实验、数据集、模型、Prompt、检索模式筛选的结构化对比，缺失数据显示“无数据”，失败项单列。`backend/app/ui/evaluation_dashboard.py` 已有筛选、指标表、图表与详情结构；生产 Gradio 挂载尚未提供评测读取授权，因此入口隐藏，管理员角色也不自动获得 AI 业务读取权限。它不新增评测计算，也不构成可远程任意读取文件的 API。

## 复现入口

在隔离测试数据库与已安装依赖的主机环境中，先按 [Benchmark 复现指南](benchmark.md) 初始化语料并生成 manifest，再用该 manifest 运行检索评测。无教师标签时可运行 `python scripts/run_grading_benchmark.py --mode selftest` 检查管道，但报告质量前须按指南加入真实 `teacher_score` 并使用可用 Provider。实际模型调用可能计费；不要把 selftest 指标与真实 Provider 运行混排为质量排名。

## v2.0 评测协议（T142，v2-evaluation-1）

本节只定义扩展版的输入、标注、执行和证据口径；保留上文 v1.0 的执行器、指标与读取授权。当前未准备标注集、未运行实验，质量阈值为待确认；设计完成不等于准确性、性能、资源或业务门禁通过。[plan.md](../.specify/plan.md) 的既有性能/资源目标保持，SC-010–014 和 Gate 10–19 的业务铁律逐例验收。

### 1. 样本清单与人工真值

T146 在 benchmark/ 准备有版本的样本清单及标注文件。每份清单记录 dataset_version、annotation_version、稳定 case_id、原文件/页图/题图引用、课程/题目对应关系、场景标签、来源、实际标注教师、UTC 标注/复核时间及标注规则版本；源文件摘要只用于核对输入，不能代替内容正确或授权。原稿、题序、选项顺序和缺失信息须保留，不用模型输出生成真值。

执行前固定全量 case_id、分层数量、运行顺序和标注快照，记录计划总数。修正输入或标注须换版本、说明原因，保留旧结果；不能看过输出后删去难例、改标签或挑选运行。下表每个场景至少一例，可由同一样本覆盖多个标签并明确交集；这是覆盖下限，不代表样本量足以推断整体准确率。T146 保存实际数量和完整 ID 清单，未满足覆盖或未固定清单不得作为正式基线。

| 能力 | 必须覆盖的样本及真值 |
| :--- | :--- |
| 导入/校正 | 文字 PDF、含图扫描试卷、试卷图片、混合文字/扫描页、跨页题、选项错序、无答案；标注题目身份、题号、真实来源页、题干/选项及顺序、答案、解析、分值、知识点、题图及其顺序。页中边界可未知为 null，未知理由单列，不能编造框；来源页必须可核对。损坏/超页、OCR 禁用/失败另列业务失败用例，不混入正常内容质量分母。 |
| 语义核验 | 知识库新题与父题改编两条路径；答案错误、条件不足、选项歧义、评分标准不明确各有问题/无该问题样本，另含改编后旧答案失效与依据不足。每题按四类分别标 true/false/null，并保存理由和真实引用；null 是教师无法判定，不是无问题。 |
| 图片理解 | figure/table/diagram、清晰可理解、不清晰应转人工、能力不支持/真实调用失败；标注稳定资产与题目关联、逐条关键条件及依据、应人工核对的原因。未知条件不标成确定值；图片原像素与资产顺序必须可追溯。 |
| 组卷 | 独立保存已审核候选题库/知识点/默认分值快照及教师要求；可满足例提供教师核对的具体组合与有效分值，不可满足例提供可验证的缺题/冲突依据；另含知识点交叠、原题带图、显式改分、人工替换/移位、旧考试无额外约束、失败保留原草稿及重启复核。策略未找到组合与已证明不可满足分开标注。 |
| 成绩/反馈 | 同题两场不同满分、默认分值、0.005 舍入边界及正负尾差；实际答卷混合最终评分、待复核、失败和依据不足。教师独立按本场冻结依据核算参与/提交、成绩分布/均值、逐题及知识点分母；检查解释、复习资料和练习的真实来源。 |
| 交付/文件/UI | 真实 Windows 包的正常与缺依赖/配置/网络/迁移失败，重启可读及同备份集隔离恢复/缺文件；三页代表 UI 与最终七类业务页面逐页操作、截图。故障注入与正常性能工作负载分开记录。 |

标签由教师独立阅读原材料/教学依据后给出，再核对系统输出；不得将模型自信、OCR confidence、机器“通过”或合成参考答案当成人工准确率。存在争议的题/字段/条件先复核，仍未知保留 null 和原因，计入未标注数量；后续教师校正不反向覆盖自动提取快照。客观字段采用规范化比较：仅统一换行符、去除首尾空白，金额按 Decimal 值、知识点按去重精确标签集合、选项/页码/题图按规定顺序比较，不做模糊近似或忽略内部文字错误；语义等价的答案、解析或图片条件由教师依据固定规则说明判定理由。重复识别按一对一匹配，不能让多条输出重复命中同一标注题/条件。

### 2. 机器、配置与工作负载

每批记录实际 Windows/主机 OS、CPU 型号与逻辑核心数、物理内存、磁盘类型、Python/包或 EXE 构建、PostgreSQL/pgvector/Redis 版本及运行方式；代码提交与构建仅用于追溯，不设跨组件版本相等门禁。固定并记录有效 OCR 模型/版本、启用状态、页渲染 DPI/像素尺寸、Provider/模型和可知修订、Prompt 版本及参数、超时/重试配置、并行度、图像预处理、数据库样本规模/索引、网络条件；未知版本如实 unknown，不猜测，不在证据输出连接凭据或密钥。云模型等待及已配置重试都计入业务耗时，记录实际调用次数、失败与可获得费用，不能隐式换 Provider 或 stub。

同一批次固定同一台机器和生效配置，各轮不得换机；桌面主验收机按 plan 的 2–4 vCPU 预算记录实际核心/可用容量，机器总内存与应用常态/峰值预算分别展示；预算不是整机最低配置保证。不同机器/Provider/配置结果分批比较，保留共有输入/标签及差异，不因配置指纹不同阻止计划内的比较。

性能清单固定各输入文件页数/字节数/分辨率/题图数，题库候选数及知识点分布、请求题数、数据库初始状态和并发负载。导入选 1 页、一个固定中间规模和 50 页样本；组卷选 1 题、一个固定中间规模和 100 题，均含可满足与明确不可满足情况并记录真实候选规模。校正选纯文字、带图和跨页切换。50 页/100 题边界必须有实际数据，不拿小样本推断；200 题仅做既有请求边界业务检查，不修改准入。固定中间规模和候选规模由 T146 在运行前写入清单；无对应数据记录未测量。

### 3. 重复次数与冷暖状态

| 运行类别 | 锁定规则 |
| :--- | :--- |
| 业务铁律 | 每个清单用例至少完成一次实际运行并逐例核对前置状态、动作、输出、持久结果及失败原因；并发/重启/冻结/恢复用例按自己的完整步骤执行，不用质量平均值替代。 |
| 模型质量 | 每组固定数据/标签/Provider/Prompt/配置完整运行 3 轮，逐轮使用不同 run_id，保存全部输出；同轮不挑结果重跑。人工校正单独记录每轮实际教师操作和耗时，不伪造三个独立标注集。 |
| 导入、校正、组卷性能 | 每个固定工作负载独立执行 3 次应用冷态、5 次应用暖态；各次使用相同内容的新导入/草稿实例及相同初始状态，避免命中完成记录、幂等响应或上轮人工修改。 |
| EXE 启动性能 | 3 次首次启动使用 3 个独立验收配置/数据副本，均具备同样的迁移前状态和预备外部依赖；5 次后续启动使用已完成初始化的同一配置，每次先正常退出所启动应用再重启。绝不重置用户业务库以造首次状态。 |

应用冷态：重启本次验收应用及其 OCR/模型工作进程，无应用内存缓存；预装依赖和已下载模型资源不删除。应用暖态：同一应用进程先执行一次同规模预热再做 5 次计时运行，预热也保存记录并标 warmup，不混入计划计时次数。原文件/数据初态固定，预热和正式导入使用不同实例。数据库/Redis 均预先就绪，记录是否复用、缓存和索引准备情况；不清理操作系统磁盘缓存，不宣称测得物理磁盘冷启动。校正冷态的页面切换计时仍从操作开始，应用启动单独记录。

EXE 的首次/后续指持久初始化状态；两者都从已退出的应用进程启动，不能将存活服务或已打开旧页计成新启动。首次必要迁移计时，后续迁移版本检查计时；人工填写配置、外部服务安装、首次模型下载、大批历史文件迁移另记，不在计时中伪装已完成。启动测量必须使用真实打包 EXE，源码启动不替代。

计划内超时/失败仍占据该轮/次编号，不用成功补跑替换。修复后另开批次/run_id；需追加实验时记录目的、配置变化和追加次数，与原批分开展示。3 轮和 3/5 次是本协议的重复口径，不宣称统计充分或外推 SLA。

### 4. 计时、资源与失败记录

耗时用同一测量端的单调时钟差，UTC 时间用于追溯，不以跨机器墙钟相减。保存 start_event、end_event、起止 UTC、elapsed_ms、measurement_source、timeout_limit_ms 和结束状态；未到合法终点时记失败/超时及实际观察到的耗时，未知为 null，不能以超时上限充当精确完成耗时。

| 场景 | 起点 → 终点；包含范围 |
| :--- | :--- |
| 导入 | 服务成功接收原文件 → 整卷真实进入 Pending Review，原文件/页图保存、OCR/文字提取、拆题、结构校验与真实 Provider 等待全部计入；教师阅读/校正/确认等待另记。失败止于真实失败事件，不称完成。 |
| 校正切页 | 浏览器发起页切换 → 对应原页图和结构化题目均渲染完成且可交互；保存实际浏览器测量和页面/操作证据，不用接口耗时代替。 |
| 组卷 | 教师提交组卷条件 → 界面可预览完整结果或展示真实未满足诊断；包含请求、选题、事务与渲染，不含临时生成新题。技术失败不能算正确的不可满足响应。 |
| EXE | 真实启动入口 → 本次服务 readiness 成功且浏览器打开本次页面；配置、依赖检查、必要迁移/版本检查均包含。仅子进程启动或端口监听不算就绪；缺任一步即失败。 |

资源从该场景起点至合法终点/失败观察结束，每 1 秒采样一次相关应用子进程、PostgreSQL、Redis 的进程内存（同平台一致的 resident/working-set 口径，记录单位和进程集合）；保存各组件峰值与同一采样时刻合计峰值，OCR 计入 Backend 不重复加。记录采样器/频率、是否覆盖完整时间段，操作系统/浏览器另记；采样峰值是观察值，不保证捕捉采样间隔内的瞬时峰值。缺组件、权限不足或采样中断时资源结论为未测量/证据不完整，不能记零或仅相加各组件不同时间的峰值冒充同时峰值。

逐次对照 plan 既有 <5 分钟、<500 ms、<3 秒、首次 <30 秒/后续 <10 秒及资源预算，记录目标、实测值、是否低于目标和计划次数内的达标次数；目标内但业务失败的运行不能达标。完整通过声明须所有计划运行成功且逐次满足对应目标/预算，有失败、超标或缺测如实保留。报告每种状态下全部记录与成功运行 min/median/max、失败/超时次数，注明有效计时样本数，不能仅凭平均值或小样本 p95 宣称通过；不改变 v1.0 的 p95 指标。质量阈值仍待 T168 确认。

### 5. 分母、指标与业务判定

每轮先记录 planned、attempted、completed、technical_failed、unassessable、unlabeled（按单位说明可交集，不随意相加）。覆盖率为有合法输出且可按真值判定的样本数/计划内有相应人工标签的样本数；失败率为技术失败尝试数/实际尝试数，未执行另记。正常质量样本和有意故障业务用例分表。未标注、未执行及失败不伪装成正确，也不直接当零分、TN 或人工确认成功。

| 指标 | 分子/分母及失败处理 |
| :--- | :--- |
| 拆题完整性 | 一对一正确匹配且边界/来源无误的标注题数 / 全部有标注原题数；失败导入的标注题仍在计划分母。另报漏题、重复、额外题数。未知像素框不影响真实页来源核对，不编造边界。 |
| 字段准确性 | 每字段正确数 / 计划内该字段可评人工标签数；自动与人工校正后分别保存快照。字段有标签但缺输出/提取失败不算正确；原稿确无答案等“已知缺失”可标正确 null，标注未知为不可评并计数。 |
| 语义核验 | 四类分别在有标签且输出可判定的题上计算 TP/FP/TN/FN，正类为“有该问题”；一题多类分别计，不加总成题数。漏报 FN/(TP+FN)、误报 FP/(FP+TN)、检出 TP/(TP+FN)，同时报告对应计划正/负样本、技术失败、无法核验和覆盖率；未输出不能当无问题或 TN。 |
| 图片条件 | 一对一匹配的正确条件数 / 全部输出的可判定条件数（条件正确率）；匹配正确条件数 / 全部可评标注关键条件数（完整性）。错误补充、漏识别、未知条件和整组题图失败另计，失败组已标注条件仍在完整性分母；需要人工核对的分流是否正确逐例记录，教师核对后结果另报。 |
| 组卷 | 可满足用例中一次结果满足全部数量/题型/知识点/Decimal 总分的例数 / 计划可满足例数；技术失败或未找到组合不算满足。已证明不可满足用例中保留原选题事实、保存本次失败意图并诚实显示缺口的例数 / 计划不可满足例数；策略未找到单列。人工调整、当前要求重算与发布资格按独立业务断言判定。 |
| 成绩/诊断/恢复/UI | 按 SC-013/014 逐项对照教师独立核算、冻结数据、备份清单及操作证据，展示有效答卷/题目/文件/页面分母；失败、待复核、依据不足不当作零分或确定知识盲点，历史未知保留未知。 |

分母为零时指标为 null，并记录 no_evaluable_data 和原始计数；TP/FP/TN/FN 全为零不表示准确率 100%。同批先逐轮报告，再用分子/分母汇总 3 轮（注明重复样本，不冒充独立样本扩容）；不能对比例无权平均或跨不同配置/标签版本混算。模型自报置信度不进入准确率分子。

授权、未确认/未补全题隔离、未解决问题不得批准、条件不满足不得发布、来源/图像可追溯、发布/历史冻结及分值一致等铁律按清单逐例检查；一例违反即相应业务门禁失败。质量/性能测量单独展示，不能以质量均值盖过铁律失败，也不能因业务保护通过就宣称 OCR/模型正确。

### 6. 证据格式及阈值确认

未来执行器在 benchmark/results/v2/<batch_id>/ 保存 manifest.json（输入/标签/环境/计划运行清单）、runs/<run_id>.json（本次元数据与逐例原始结果）、cases.csv（每例/轮/阶段）、metrics.csv（逐指标分子分母和值）、timings.csv（逐次起止/耗时/目标/资源）、failures.jsonl（失败阶段/代码/原因/可用中间证据）；写入新目录，不覆盖历史。截图、真实浏览器测量、资源采样、原始 Provider 输出及人工校正记录按相对引用关联。缺失引用或无测量理由必须明确。既有 v1.0 JSON/CSV 保持原格式，引用原产物而不假称当前 UI 已支持新 v2 格式。

| 层级 | 必需字段 |
| :--- | :--- |
| 清单 | protocol_version=v2-evaluation-1、batch_id、dataset_version、annotation_version、case_id/标签/源引用、真实标注者/时间、environment/config、planned_runs、quality_thresholds（待确认时 null）、threshold_status=pending_baseline |
| 单轮/逐例 | run_id、repeat_index、evidence_kind=real_provider/business_acceptance/pipeline_selftest、Provider/model/Prompt 实际信息、输入与标注版本、case_id、stage=automatic/corrected/manual_review/business、状态、真实输出/标签/匹配理由引用、错误码/消息、实际调用次数、UTC/耗时及计时来源；未知字段为 null/unknown 并有原因 |
| 指标/耗时 CSV | batch_id、run_id、case_id（汇总行可空）、stage、metric、unit、numerator、denominator、value、status、reason、evidence_ref；timings 另含 workload/cold_warm、起止事件、elapsed_ms、目标、目标达成与资源完整性。金额采用十进制字符串，时间 ms、内存 MiB，单位显式，null 留空并给原因。 |

T146 准备真实输入与标注；T160 运行导入/校正证据，T168 运行真实质量基线，T179 运行组卷性能及冻结/分值业务验收，T189 运行真实包启动/资源，T190/T191 保存 UI 和系统闭环证据。当前没有上述执行结果，不创建伪造样本、预填效果或新脚本。

T168 依据此协议的原始计数、失败/覆盖率和分层基线提出各质量阈值与理由，由用户确认后登记：指标/阶段/分母、数值/比较符、适用数据/配置、确认者/UTC、引用基线批次；没有真实基线或确认时保持 pending_baseline/pending_confirmation，不声明质量达标。若数据规模不足，明确局限并提出所需样本；不得在看过验收结果后追溯降低阈值或删去失败。更换协议、数据/标签或目标须保留原版本和差异；现有性能/资源目标不因未达到而自动调整。

## T146 样本准备状态（2026-10-01）

按用户确认，本批先准备 [待教师复核样本包](../benchmark/corpus/v2-draft-20261001/README.md)，**T146 保持未勾选**；不改上方 v2-evaluation-1 协议、正式真值要求、质量阈值或 v1.0 历史结果。

实际准备 dataset_version=v2-draft-20261001-1 / annotation_version=teacher-pending-1：57 个唯一 case_id，分层清单和具体动作/源引用；7 份有效 PDF 共 117 页，另含故意损坏文件、试卷 PNG 与 figure/table/diagram；扫描样本为合成原稿栅格化，未伪称实拍扫描。含文字/图像/混合/跨页/错序/无答案与故障、两路径语义四类错误/无问题/旧答案失效、图片转人工/失效、可满足/不可满足组卷及改分/移位、四种评分状态/教师统计输入、七页 UI/真实交付和恢复步骤。

工作负载在草稿中固定：导入 1/10/50 页（3/20/100 题），组卷 1/25/100 题（300 个候选）；200 题只做原准入边界，51 页是拒绝用例。模板保存模型 3 轮、性能 3 冷/5 暖及 EXE 3 首次/5 后续的计划口径，校正纯文字/带图/跨页切页和预热另标；CSV 仅列头，run/环境 JSON 明确 not_run/未知，不写入真实结果目录。

来源为 Codex 新编固定合成原稿和夹具；AI 草稿建议单列，正式教师身份/时间/标签、独立统计表均留空。当前真实教师标注 0，ready_for_formal_baseline=false，quality_thresholds=null。候选 Approved 仅是未来隔离夹具目标，无真实教师批准；同题两场的分值与最终/待复核/失败/缺依据是输入场景，不是已产生的新业务记录。

本次仅核对文件/页数/文字层/渲染、结构/引用/摘要、组合和固定 Decimal 算术。没有模型、OCR、性能、资源、EXE 或恢复实验结果，不声明 SC/Gate 通过。教师补齐独立真值和真实来源后另建/冻结版本，再核对全量覆盖、实际数量/ID及未知项；T160/T168/T179/T189/T190/T191 承接正式执行，T168 真实基线后再提出质量阈值。


## T155 OCR 候选依赖技术评估（2026-10-02）

### 本批范围与证据边界

按用户本次明确指令，T155 仅使用现有扫描件样本进行推理依赖评估，**不等待 T146 教师标注**；T146 保持未完成，教师真值和质量阈值由 T168 承接。本批不填写 CER/WER、字段准确率或教师认可比例，也不宣告正式质量、系统性能或产品 EXE 验收通过。用户已确认首版 RapidOCR＋ONNX CPU；本批只记录选型，适配器和业务依赖交 T156 实施。

输入为 `benchmark/corpus/v2-draft-20261001`，dataset_version=`v2-draft-20261001-1`。扫描件来自合成数学试卷栅格化，未覆盖实拍噪声、倾斜、手写、复杂分式/根式、复杂合并单元格。报告下述成功次数仅表示 SDK 返回可校验文字/区域，不能解释为内容准确率。

### 样本与路由核对

| 文件 | 页数 | 无文字层页 | 原生文字读取中位耗时（3 次，ms） | SHA256 前 12 位 |
| :--- | ---: | :--- | ---: | :--- |
| paper_cross_page.pdf | 2 | 无 | 6.67 | 8c6a18f125a3 |
| paper_mixed.pdf | 2 | [2] | 5.59 | 5c0affa1fb5e |
| paper_scan.pdf | 1 | [1] | 1.08 | 16aaa00c8b41 |
| workload_10_pages.pdf | 10 | 无 | 23.06 | bdca8f730b98 |
| workload_50_pages.pdf | 50 | 无 | 108.39 | 11fceae00e43 |
| workload_51_pages.pdf | 51 | 无 | 111.69 | e37bf3d92a7e |

扫描卷第 1 页和混合卷第 2 页需 OCR；跨页卷与负载卷有原生文字，正常导入应按契约优先读取文字。本批将 1/2/10/50 页全部渲染成图作为额外 OCR 压力试验，不把该耗时等同实际导入耗时。51 页样本读取/渲染已核对，只记录超出 50 页准入上限，不送 OCR，也不声称尚待 T157 实施的业务拒绝已验收。

### 环境和候选

Windows 11 家庭版中文，10.0.26200；AMD Ryzen 5 4600H，6 核/12 逻辑处理器，约 15.37 GiB RAM；CPython 3.13.13 x64。单进程串行，CPU 推理线程 2，未限制 CPU 亲和性、未停止其他本机服务。Poppler `pdftoppm -r 150 -png`，每页 1241×1754 像素；相同 PNG 提供给两个 SDK，其内部缩放/过滤保留各自默认并分别记录，不视为算法公平性基准。

- **A：PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 CPU / PaddleX 3.7.2 / PP-OCRv5_mobile_det + PP-OCRv5_mobile_rec**。显式关闭文档方向、矫正和行方向模块，仅基础 OCR 推理，无训练包及 `all/doc-parser` 扩展。安装解析 72 个分发包（含测量 psutil），site-packages 721.29 MiB；独立模型参数约 20.17 MiB（尚未计配置/结构文件）。基础文字 OCR 与公式/表格结构识别是不同能力组，不能用文字块冒充公式 LaTeX 或表格关系。[官方安装与能力分组](https://www.paddleocr.ai/main/en/version3.x/installation.html)、[3.7.0 发行元数据](https://pypi.org/project/paddleocr/3.7.0/)。
- **B：RapidOCR 3.9.2 / ONNX Runtime 1.30.0 CPU / PP-OCRv5 mobile 检测与识别模型**。通过 SDK 枚举显式选 PP-OCRv5 mobile，避免该版本默认 PP-OCRv6 small 随配置混入；关闭行方向调用，但 SDK 初始化仍加载 PP-OCRv4 分类模型。推理初始环境 23 个包（含 psutil），加入独立打包工具后 site-packages 260.59 MiB。无 PaddlePaddle、CUDA、训练依赖；输出同样是区域文字与置信度，不能自行恢复表格/流程关系。[官方快速开始](https://rapidai.github.io/RapidOCRDocs/main/quickstart/)、[模型与默认版本说明](https://rapidai.github.io/RapidOCRDocs/main/model_list/)、[3.9.2 发行元数据](https://pypi.org/project/rapidocr/3.9.2/)。

NumPy/OpenCV 实测版本不同：A=2.3.5/4.10.0.84（contrib），B=2.5.3/5.0.0.93；完整安装快照保留于本机缓存，均未修改 `pyproject.toml` 或现有应用环境。安装目录体积包含 Python 包及测量工具，并非产品包大小；B 另含 PyInstaller 工具，不能作为两者最小压缩体积的精确比较。

### 真实故障与观察

A 默认 MKL-DNN/oneDNN 配置在首个扫描页即抛出 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`（onednn_instruction.cc:118）；默认配置整批未产生可用 OCR。已保留失败，另行显式 `enable_mkldnn=False` 评估同版模型，此配置可识别扫描及跨页页图；不添加运行时自动降级。

B SDK 首次从 ModelScope 流式下载检测模型时出现 `ChunkedEncodingError / IncompleteRead(0 bytes read, 4819576 more expected)`。后以同一官方 URL 预置模型、校验 SDK 清单 SHA256 后读取成功，未换模型/Provider。离线交付应预置所选模型并验证可读；缺失/坏文件/初始化失败继续明确报错。首次下载和依赖安装不计入页推理耗时。

两者扫描页均返回 26 个文字区域，保留原选项 `C.7 A.5 B.6 D.8`、`cm²`、`2 × 3 + 1` 及图中高/底文字；扫描表格表头出现 `说明 / X / y` 的平面文字顺序，大小写和空格亦有变化。跨页续文与流程节点可识别，但合并同一道题、图形/箭头关系和答案核验仍属于下游拆题/图片理解/教师校正，不能由 OCR 输出自动认定正确。

### 计时、资源及 Windows 打包

每候选成功配置串行完成 65 次页图调用（样本含重复内容，次数不代表独立质量样本），每个输入页仅 1 次；另在扫描页进行 1 次预热＋5 次暖态推理。A 默认配置的 65 次失败独立保留，失败耗时不当作识别性能。坐标按原图像素检查，原生四角区域各点均在 1241×1754 范围内，置信度有限且在 [0,1]，两成功配置的 65 页均通过该技术检查。教师真值未参与，所以不称准确率。

| 指标 | A（显式关闭 MKL-DNN） | B（ONNX CPU） |
| :--- | ---: | ---: |
| 扫描卷，1 页，总推理秒数 | 12.86 | 2.61 |
| 混合卷栅格化，2 页，总推理秒数 | 25.81 | 7.27 |
| 跨页卷栅格化，2 页，总推理秒数 | 12.71 | 4.97 |
| 10 页栅格化压力，总推理秒数 | 69.09 | 15.17 |
| 50 页栅格化压力，总推理秒数 | 351.42 | 73.58 |
| 成功返回/实际页调用数 | 65/65 | 65/65 |
| 扫描页 5 次暖态中位数，秒 | 12.943 | 2.334 |
| 预置模型的新进程初始化，秒（含 SDK 导入） | 25.899 | 1.577 |
| 全批进程 RSS 峰值，MiB（20 ms 采样） | 559.25 | 1039.19 |


B 首轮暖态秒数为 [2.528, 6.480, 5.343, 4.167, 3.955]，与 A 安装尾段重叠，仅保留观察值；安装和 A 推理结束后独立复测为 [2.334, 2.328, 2.305, 2.339, 2.356]，表中使用后者，首轮记录不删除。A 暖态为 [12.848, 13.169, 12.943, 13.103, 12.855]。B 全批完成 UTC=2026-10-02T06:42:54.824584+00:00，A 成功配置完成 UTC=2026-10-02T06:55:03.184601+00:00，B 复测完成 UTC=2026-10-02T06:55:33.783732+00:00；源码基线 fb7f883ff9bdef346d3652c56420b7bec510c6f5，仅用于追溯。

A 首次 49.082 秒初始化含模型下载/网络检查，另有缓存后 25.899 秒；B 首次 2.976 秒仍含分类模型下载，复测 1.577 秒使用全部预置模型。不能将它们当作正式应用冷启动 3 轮统计。RSS 是整个独立进程瞬时观测，包含 SDK、图像处理及评测代码，非持久占用或完整系统预算；未做多并发、长时间运行或性能达标承诺。页总耗时不含渲染、下载、模型初始化、拆题、模型调用、教师校正和持久化。

**Windows 独立 OCR 打包实测**：PyInstaller 6.22.3 / hooks 2026.8 的 onedir 构建完成；目录 254.00 MiB（含 SDK 自带数据，不含外置 v5 模型），EXE 9209733 bytes。清除 PYTHONPATH/PYTHONHOME、PATH 仅制品目录＋System32，HTTP/HTTPS/ALL_PROXY 设为不可用的 127.0.0.1:9，在同机读取预置模型，运行 4.825 秒、退出码 0，扫描页 26 个文字区域的文本序列与 Python 原输出一致。仅此 OCR 冒烟制品，没有构建 EduAgent UI/API/数据库启动器、验证干净 Windows 主机或 onefile；不宣告 T186/T189 完成。

构建过程中提示未安装 TensorRT 和 ONNX 量化模块；本次选 CPU ONNX 推理，EXE 实际推理通过，未为消除无关模块提示引入 GPU/导出依赖。Windows ONNX Runtime 仍有 Visual C++ 2019 runtime 要求，干净机器需核对。[官方 ONNX Runtime 安装条件](https://onnxruntime.ai/docs/install/)。A 本批未做 EXE 构建；官方 PyInstaller 指南需收集 Paddle 动态库、PaddleX 数据及依赖 metadata，列出的已测试组合是 Python 3.10/PaddleOCR 3.1，不能视为本次 3.13/3.7 制品通过。[PaddleOCR 官方打包指南](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/inference_deployment/others/packaging.en.md)。

### 已选模型与复现入口

模型来源为所安装 RapidOCR 3.9.2 的 default_models.yaml 所列 ModelScope v3.9.2 官方资源；手工预置检测/识别文件与 SDK 清单摘要一致，分类文件由 SDK 成功下载，随后同样核对。

| 文件 | bytes | SHA256 |
| :--- | ---: | :--- |
| ch_PP-OCRv5_det_mobile.onnx | 4819576 | 4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae |
| ch_PP-OCRv5_rec_mobile.onnx | 16631306 | 5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5 |
| ch_ppocr_mobile_v2.0_cls_mobile.onnx | 585532 | e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c |


同机原始证据保留在忽略 Git 的 `.cache/t155-ocr-20261002/`：samples.json、native-timings.json、render-timings.json，paddle/rapid/rapid-quick/paddle-no-mkldnn 的 result.json 与 pages.jsonl、错误/构建日志、packaging-result.json、两环境 lock.txt、模型清单与评测脚本。它们是本机临时实验材料，不承诺随 Git 分发或长期保留；本页的输入指纹、版本、原始暖态值、结果与故障摘要随提交保存。不是 v2 正式质量基线目录或 v1 Benchmark 结果格式。

复现关键命令（在隔离 Python 环境执行，模型按上表预置；源码评测脚本本机可用）：

```powershell
uv venv .cache/t155-recheck --python D:\develop\Python\python.exe
uv pip install --python .cache/t155-recheck/Scripts/python.exe rapidocr==3.9.2 onnxruntime==1.30.0 psutil pyinstaller==6.22.3
# 本机完整原始试验
.cache/t155-ocr-20261002/rapid-env/Scripts/python.exe .cache/t155-ocr-20261002/evaluate_ocr.py rapid
.cache/t155-ocr-20261002/paddle-env/Scripts/python.exe .cache/t155-ocr-20261002/evaluate_ocr.py paddle-no-mkldnn
# 独立构建所选 OCR，模型目录外置
.cache/t155-ocr-20261002/rapid-env/Scripts/python.exe -m PyInstaller --onedir --name t155_rapid_smoke --distpath .cache/t155-ocr-20261002/dist --workpath .cache/t155-ocr-20261002/build --specpath .cache/t155-ocr-20261002 --collect-all rapidocr --collect-all onnxruntime --copy-metadata rapidocr --copy-metadata onnxruntime .cache/t155-ocr-20261002/rapid_smoke.py
```

无需本机脚本的最小识别入口（不是业务适配器；输入采用 150 DPI PNG，models 目录含上表 3 个文件）：

```python
from rapidocr import RapidOCR, OCRVersion, ModelType

engine = RapidOCR(params={
    "Global.model_root_dir": "models",
    "Global.use_cls": False,
    "EngineConfig.onnxruntime.intra_op_num_threads": 2,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    "Det.ocr_version": OCRVersion.PPOCRV5,
    "Det.model_type": ModelType.MOBILE,
    "Rec.ocr_version": OCRVersion.PPOCRV5,
    "Rec.model_type": ModelType.MOBILE,
})
out = engine("paper_scan-1.png")
print(out.boxes, out.txts, out.scores)
```

实际接口使用 SDK 枚举，初试字符串值触发 TypeError，修正后才进入模型准备；此配置错误亦保留，不能归为模型识别质量故障。PDF 文字层可靠性与图中缺失文字的判断仍交 T157/T158；这里的字符计数只是路由清点，不把“有任意文字”自动等同整页信息完整。本批是依赖技术试验，不执行 T142 完整应用 3 冷/5 暖、教师标签或 EXE 首次/后续启动协议。


### 推荐与首版决策（已确认）

推荐并按用户本次答复锁定 B 的 CPU ONNX 路线：已完成扫描、混合、跨页和 10/50 页真实识别，依赖体积较小，避免本机 A 默认 oneDNN 兼容故障。B 的实际瞬时 RSS 峰值约 1 GiB，不能宣称与完整后端共存后满足 4 GiB 总预算。A 关闭 MKL-DNN 后可作为另一候选，但不得隐去默认配置失败及耗时成本。

用户于 2026-10-02 明确选择“采用 RapidOCR＋ONNX CPU（推荐）”：首版关键依赖锁定 rapidocr==3.9.2、onnxruntime==1.30.0 CPU，检测/识别明确使用 PP-OCRv5 mobile，不随 SDK 默认升级为 v6；方向分类本轮关闭，但当前 SDK 初始化仍需对应 v4 分类文件。T156 延迟加载且默认关闭 OCR，首次模型准备需明确完成，依赖/模型未就绪与实际调用失败如实区分；不得在故障后自动切到 Paddle/云端。本批不修改 pyproject.toml、不实现 Provider、不新增迁移或测试。正式质量比较等待 T168 教师真值，产品 EXE 与资源验收由 E6/T189 承接。


## T160 用户复核的 AI 辅助验收（2026-10-03）

用户已在本次会话明确确认采用“用户复核的 AI 辅助标注”作为本批 T160 的对照依据，并授权执行 v2-evaluation-1 完整重复协议。此决定仅替代本批 T160 原要求的独立教师真值口径；T146、T168 的独立教师标注要求不变。参考源为原页逐字段阅读后代拟的 5 份、16 题标签，不由本次 OCR/拆题输出回填。

冻结参考见 [标注与原页对照](../benchmark/corpus/t160-assisted-20261003/review.md)，annotation_version=t160-user-authorized-ai-assisted-20261003-1、dataset_version=v2-draft-20261001-1；会话确认 UTC 为 2026-10-02T16:14:48+00:00。保留 AI 作者、用户真实会话确认、教师身份 null、独立教师标签数 0 和 ready_for_formal_baseline=false。结果称为 AI 辅助参考一致性，不称独立教师准确率；质量阈值仍待 T168。原稿没有的答案/解析不推算补写，未测像素框及来源未写的题型保留未知，另报可评与未知分母。

计划固定为 5 个正常 case × 3 轮实际 Provider 导入；1/10/50 页导入各 3 冷、1 预热、5 暖；校正纯文字/带图/跨页各 3 冷、1 预热、5 暖。并行度 1，沿用 deepseek-chat 和既有重试；所有计划内失败、迟到、资源缺测均保留。OCR 仅在隔离验收设置中启用既定 RapidOCR/ONNX CPU 与预置 PP-OCRv5 mobile；不改业务库或 .env。

### T160 本批实际结果与未通过结论

本批已经运行，**T160 验收未通过，任务保持未勾选**。合同及事务保护通过不能抵消真实整卷拆题失败；质量阈值仍为 null。完整原始证据保存在 [T160 结果目录](../benchmark/results/v2/t160-assisted-20261003/)，源码追溯基线为 deepcode / 6cea2adc245998b58ab1fe4cd10e6e6d4e2b092e。旧 T146/T155 记录保留其当时结论，当前本批授权不追溯改变 T146/T168 的独立教师要求。

#### 自动提取、辅助校正与题图关联

固定执行 IMP-TEXT、IMP-SCAN、IMP-IMAGE、IMP-MIXED、IMP-CROSS 各 3 轮，共 15/15 次；14 次真实 Pending Review、1 次 Failed。16 个独立参考题重复 3 轮，固定 48 个题实例分母；严格按原题号与真实来源页一对一匹配，自动 43/48，辅助字段校正后 45/48。失败的 IMP-TEXT 第 3 轮 3 题始终计入分母；IMP-MIXED 第 2 轮的两题来源页差异如实保留于自动快照。文字比较仅统一换行、首尾空白，选项不排序，金额按 Decimal，标签按精确集合；这是参考字段一致性，不能据题干字面差异推算 CER/WER 或语义错误率。

| 字段 | 自动提取 | 辅助字段校正 | 未知排除实例 |
| --- | --- | --- | --- |
| 题干 | 6/48 | 45/48 | 0 |
| 原题号 | 43/48 | 45/48 | 0 |
| 题序 | 43/48 | 45/48 | 0 |
| 选项及键顺序 | 39/48 | 45/48 | 0 |
| 题型 | 40/45 | 42/45 | 3 |
| 分值 | 43/48 | 45/48 | 0 |
| 原文答案 | 43/48 | 45/48 | 0 |
| 原文评分标准 | 30/48 | 45/48 | 0 |
| 原文解析 | 34/48 | 45/48 | 0 |
| 知识点 | 13/48 | 45/48 | 0 |
| 来源页 | 43/48 | 45/48 | 0 |
| 题图关联 | 0/48 | 14/48 | 0 |
| 来源像素框 | null（0 个可评） | null（0 个可评） | 48 |

题型的 3 个未知实例、像素框的 48 个未知实例仅从相应字段分母排除；已知题图关联继续在 48 个题实例分母中，不能因机器未给资产而排除。纯字段校正阶段关联为 14/48，后置使用真实 SourcePage 和资产服务创建 31 张裁图，0 次失败、0 次云调用，最终有序资产类型/来源页关联为 45/48。裁框是另行测量的 AI 辅助操作，未写回未知的参考框，也未伪造教师图像核对或自动批准整卷；原自动、字段校正及后置图像快照分开保存。根代理实际查看 figure/table/diagram 代表裁图，条件完整且未包含答案/整页；此观察不构成独立教师或像素精度统计。

本批参考由用户确认，独立教师真值仍为 0。字段校正通过真实服务执行，原稿缺失的答案/解析保留 null；未从自动结果回填参考标签。详见 [逐轮与字段汇总](../benchmark/results/v2/t160-assisted-20261003/quality/quality-aggregate.json)、[后置图关联](../benchmark/results/v2/t160-assisted-20261003/quality/image-assistance/association-summary.json) 及 [原始失败](../benchmark/results/v2/t160-assisted-20261003/quality/failures.jsonl)。

#### 整卷导入耗时与资源

1/10/50 页输入各执行 3 冷、1 预热、5 暖，共 27 次实际导入，3 次预热单列，24 次计时中 12 次成功、12 次失败；3 次预热也均失败。计时使用服务接受原卷至真实持久终态的单调时钟，含文字/OCR、真实 Provider 等待和结构校验。冷态为新源码进程，暖态复用该组进程，全部串行；不作为 EXE 启动指标。

| 输入 | 冷态成功/计划 | 暖态成功/计划 | 成功耗时冷 min/median/max（秒） | 成功耗时暖 min/median/max（秒） | 业务成功且低于 300 秒 |
| --- | --- | --- | --- | --- | --- |
| 1 页 paper_text | 3/3 | 5/5 | 4.801 / 4.852 / 5.259 | 3.480 / 3.923 / 4.210 | 8/8 |
| 10 页 workload_10_pages | 1/3 | 3/5 | 14.451 / 14.451 / 14.451 | 14.332 / 14.905 / 15.496 | 4/8 |
| 50 页 workload_50_pages | 0/3 | 0/5 | null | null | 0/8 |

15 个失败运行（含预热）均为 PAPER_EXTRACTION_FAILED：12 次 unknown evidence field、2 次 answer update must refer to a previously completed question、1 次 answer/analysis requires an original excerpt。50 页失败观察耗时 10.914–33.269 秒，止于失败，不能当作完成耗时或达标。原文件、已保存页图、实际输出与错误留存；未挑结果重跑、替换 Provider 或降低目标。详见 [逐次计时](../benchmark/results/v2/t160-assisted-20261003/import-performance/timings.csv) 和 [导入汇总](../benchmark/results/v2/t160-assisted-20261003/import-performance/summary.json)。

资源采样范围审计发现 12/12 组记录的 Popen/sample PID 是 Windows venv redirector，27 次业务回执的真实 os.getpid 均属于另一 worker。原 summary 的 complete=true 只证明重定向进程及容器采样，并未覆盖真实应用 worker；以 [resource-scope-audit.json](../benchmark/results/v2/t160-assisted-20261003/import-performance/resource-scope-audit.json) 为范围判定依据：app_missing=true、all_component_complete=false，应用峰值、同刻组件总峰值及预算结果均为 null。不能将不完整观测称为应用/整系统占用或预算通过。PG 为共享容器所有 postgres 进程 RSS，Redis 为专属容器进程 RSS，仍保留原始观测及作用域，不填补缺失应用数据。实际主机为 Ryzen 5 4600H、6 核/12 逻辑处理器、约 15.37 GiB RAM，未限制到 2–4 vCPU；不推断该预算主机或 EXE 性能。

#### 技术保护、模型追溯与待完成项

75 个既有聚焦测试加 3 个新测试，共 78 passed。新测试在真实 PostgreSQL、autoflush=False 下用 pg_blocking_pids 证明 PATCH 持锁时确认等待且消费已提交的新值；另验证确认先完成后的迟到校正拒绝、真实 OCRProviderError 映射并保留原卷/页图。既有并发重复确认、事务回滚、来源及学生许可断言未放宽。另固定故障 4/4 通过：损坏文件、51 页拒绝、OCR 未配置、明确 OCR_CALL_FAILED；0 次云调用。证据见 [聚焦测试](../benchmark/results/v2/t160-assisted-20261003/technical/existing-acceptance.xml)、[新增测试日志](../benchmark/results/v2/t160-assisted-20261003/technical/t160_added_first.log) 和 [故障记录](../benchmark/results/v2/t160-assisted-20261003/technical/fault-cases/summary.json)。

实际 create_app 和完整 lifespan 启动、退出后换进程重新启动，完成 30 项真实 HTTP 核对：教师原卷/页图/暂存与正式资产字节持久一致，学生只读获许可题图，原卷、原页、暂存、隐藏及整页别名继续 403；4 个在途记录明确转 Failed/PAPER_INTERRUPTED，源卷保留。此实验使用 minimal Gradio 测试壳及受控合成夹具，是后端技术验收，不代表生产 UI、真实模型质量或 EXE 启动。见 [重启回执](../benchmark/results/v2/t160-assisted-20261003/technical/restart-receipt.json)。

质量实际 HTTP 尝试 15 次，导入性能 83 次，合计 98 次；实际 usage 为 prompt 181628、completion 61719、total 243347 tokens，费用未知 null。98 个请求体 model 为 deepseek-chat，98 个实际响应 model 记录为 deepseek-flash；配置、请求及响应身份分别保存，不推断 alias，也不据此修改 .env 或冒充另一配置评测。

浏览器纯文字/带图/跨页校正协议已完成，24计时＋3预热均渲染完成，仅2/24低于500ms；详见下方逐组耗时与真实浏览器终点，接口耗时未代替页面计时。当前 T160 不声明 Gate 10/11/13 全通过。建议先定位并收窄 paper-extraction-v1 提示词与结构化来源字段/跨批答案更新合同的冲突，按 TCR 增补必要回归，再执行新的完整 T160 批次；本批不执行 T165，不追溯修改失败证据。

根因定位依据：`backend/app/ai/paper_extraction/schemas.py` 的 `AnswerFields.evidence` 目前声明为任意字符串键字典，提供给模型的 JSON Schema 未约束只能有答案/评分标准/解析三种键；`service.py` 在收到结果后才拒绝额外键。提示词未逐项列出此白名单。因而这些输出可符合结构化 schema，却不符合业务来源校验。两次 updates 还指向当前批新题，而运行时只允许之前已完成的题。下一批应先让生产者提示与 schema 表达既有业务约束，保留来源检查及失败事实，再进行针对性复验。该定位来自现有代码和实际响应；本批未修改业务代码。

#### 校正页面真实浏览器测量与收尾

固定 27 次动作已完成（24 次计时＋3 次预热），27 次均真实加载完成，27 张 Cua JPEG 截图及开始/结束回执保留。正式 24 次只有 2 次低于 500 ms，性能验收失败。计时由浏览器原生目标选择事件的 `performance.now()` 起，到目标原页及字段/图片加载、队列空闲、控件可交互、连续稳定双帧止；不使用 Cua 工具往返时间。浏览器保持可见与焦点，实际状态写入每次回执。

| 场景 | 动作 | 冷态 min/median/max（ms，3 次） | 暖态 min/median/max（ms，5 次） | <500 ms |
| --- | --- | --- | --- | --- |
| 纯文字 | 单页题 2→题 1 | 635.3 / 686.3 / 687.0 | 838.5 / 886.6 / 1109.2 | 0/8 |
| 带图 | 单页题 1→题 2 | 748.2 / 768.1 / 776.7 | 811.2 / 817.2 / 977.4 | 0/8 |
| 跨页 | 同一题 4 的原页 1→原页 2 | 265.0 / 543.2 / 564.6 | 465.4 / 595.7 / 855.1 | 2/8 |

纯文字和带图输入只有一页，因此明确测量题目切换而不虚称跨页。UI 输入来自实际第一轮自动提取快照，经真实服务复制为 27 个新持久身份，标记 `source_backed_replay_for_ui_timing`；没有再次 OCR/模型调用，不属于新的内容质量样本。实际调用 HEAD 的生产校正视图和认证加载器，外层为测量壳，未加载用户未提交主界面改动，不证明七页主界面/EXE 交付。四个观测器预检目录保留并单列排除：脚本挂载位置、未调用函数、含隐藏勾号的选项文本使观测器未触发；其中两次实际目标点击没有合法计时，均不伪造耗时或代入正式分母。修正的是测量脚本，生产界面未改。

UI 资源采样使用服务自己报告的实际 worker PID，各组 PID 一致。1 秒频率下，仅 6 次计时动作有 1 个完整采样周期位于实际动作窗口；其余 18 次计时及 3 次预热无窗口内样本，峰值保持 null。6 个稀疏观察的应用工作集约 250.8–252.2 MiB，同时合计约 440.9–442.4 MiB（数据库为共享 PostgreSQL 容器进程 RSS，含共享页重复计数；浏览器未测）；不代入整组峰值，不据此判资源预算通过。导入批实际应用内存缺测仍独立保留。

详见 [浏览器汇总](../benchmark/results/v2/t160-assisted-20261003/ui-correction-summary/summary.json)、[逐次动作与资源](../benchmark/results/v2/t160-assisted-20261003/ui-correction-summary/attempts.json)、[原始页面证据](../benchmark/results/v2/t160-assisted-20261003/ui-correction/) 和 [总验收回执](../benchmark/results/v2/t160-assisted-20261003/acceptance-summary.json)。2026-10-02T17:31:54.567745+00:00 已停止本批页面 worker/浏览器，仅清理确切隔离 DB/Redis；原业务库仍 0012_audit_logs，10 项用户文件/.env 字节未变，缓存与失败证据保留。后置扩展 hooks 不存在。
