# M3 AI 阅卷里程碑复盘报告

复盘日期：2026-09-17  
复盘范围：T046-T064 + B01-B05 修复  
复盘结论：⚠️ 需关注

复盘依据：

- `.specify/memory/constitution.md`：五条战略原则、项目目标与实施约束。
- `.specify/spec.md`：重点核对 FR-029 至 FR-040、US2、US3、SC-004、SC-006、SC-007。
- `.specify/plan.md`：重点核对 §4、§5 状态图、§5.1-§5.4，并对照 §7 的追踪和评测边界。
- `.specify/tasks.md`：T046-T064、M3 检查点；使用 T065-T079、T095/T099/T111/T112 区分后续工作与当前交付。
- `.specify/data-model.md`、`.specify/contracts/agent-workflow.md`。
- `backend/app/services/grading/` 全部模块，以及直接依赖的 `backend/app/services/diagnosis_service.py`、`backend/app/schemas/ai.py`、`backend/app/schemas/grading.py`。
- `backend/app/models/grading_result.py`、`exam_result.py`、`diagnosis_report.py`、`review_record.py`、`agent_run.py`、`workflow_run.py`，及公共基类与直接关联模型。
- `backend/app/api/grading.py`、`backend/app/api/results.py`；`backend/app/ui/results_view.py`、`results_loaders.py`、`results_diagnosis.py`，及 `gradio_app.py` 的结果入口装配。
- `scripts/run_grading_benchmark.py`、`benchmark/corpus/grading_samples.json`、`benchmark/results/grading_selftest-t059.json`、`benchmark/results/grading_summary.csv`；直接依赖的 LLM/Embedding 工厂、检索与重排实现、配置和 `docker-compose.yml`。
- `migrations/versions/0005_grading_results.py`、`0006_diagnosis_reports.py`、`0007_review_records.py`、`0008_workflow_runs.py`、`0009_agent_runs.py`。
- 第七节所列现有测试，以及 M3/B01-B05 相关 Git 提交记录。

代码基线：分支 `deepcode`，HEAD `e2b0594`；开始复盘时工作区无未提交修改。行号以该代码基线为准，以下路径均相对于仓库根目录。T060-T064 是五项建模任务，实际建立 **六个模型**：T060 同时创建 GradingResult 和 ExamResult；本报告覆盖全部六个。

## 一、总体评价

M3 的核心服务已经实现客观题规则评分、主观题检索与结构化评分、置信度决策、整卷汇总，以及最终成绩提交后的诊断生成和只读查询，符合“不依赖 Agent 编排即可运行”的阶段目标。评分职责和数据来源总体清晰，B01-B05 修复加强了权限、事务终态、历史字段保真、诊断并发及真实组件装配，没有引入额外基础设施。现有聚焦测试 **574 项通过、无失败或跳过**，但使用替代模型响应，不能据此宣称真实模型质量或完整系统验收通过。主要待关注项是结果工作台接线不完整、教师统计包含草稿、显式配置未贯穿组件，以及 Benchmark 模型记录和真实质量证据不足。M3 可作为 M4 的服务与持久化基线；本次未确认必须在 M4 启动前修复的高优先级阻断项，LangGraph 恢复应按既有 T073/T074 实现。

## 二、第一性原则符合度

| 原则 | 符合度 | 说明 |
| :--- | :--- | :--- |
| 可运行优先 | ✅ | `docker-compose.yml:3`、`:22`、`:37` 仍只定义 PostgreSQL、Redis、backend。生产装配复用现有数据库、检索和 Provider，后台执行在现有后端内完成（`backend/app/api/grading.py:106`）。没有新增容器、外部向量库或独立任务队列。此结论针对架构与代码，本次未重新执行容器启动验收。 |
| AI 能力优先 | ✅ | 主要复杂度来自评分、检索、结构化校验、置信度和最终结果边界；事务、授权、诊断来源校验直接服务这些目标。未发现需要通用框架才能解决的问题；遗留进度写入类和少量重复样板可以整理（L01/L02，低），但不构成非 AI 业务的明显过度设计。 |
| 模型可替换 | ⚠️ | 评分和诊断依赖 `BaseLLMProvider.generate_structured`，没有直接调用 DeepSeek SDK。新增 Provider 主要需要实现/注册适配器及对应配置，无需改评分算法；但当前只内置注册 DeepSeek（`backend/app/ai/llm/factory.py:91`），并不是把 `LLM_PROVIDER` 改为任意名称就能运行。显式 settings 在部分工厂调用处丢失（M04，中），Benchmark 还把模型身份取自 `deepseek_model`（M06，中）；替换能力尚不能评为完全验证。 |
| 结构化输出铁律 | ✅ | 主观评分走 `generate_structured`、`SubjectiveGradingPayload` 和结果 Pydantic 校验（`backend/app/services/grading/subjective_grader.py:428`、`:494`）；学习建议也走结构化 Schema（`backend/app/services/diagnosis_service.py:361`），LLM 重排同样走结构化生成（`backend/app/ai/retrieval/reranker.py:317`）。存在 `json.loads`（`subjective_grader.py:277`），这是 JSON 反序列化；客观答案的字符串归一/分隔符解析处理的是学生输入。未发现从 LLM 自由文本中猜测、切割分数的正式评分路径。 |
| 可评测原则 | ⚠️ | 三路策略、数据集版本、Prompt 版本、指标定义、失败和结果落盘已实现；real 模式确实装配 M2 检索/重排。当前保存的阅卷结果只有 selftest，四条样本没有教师分数，质量指标如实为 null（M07，中）。模型记录在替换 Provider 时可能失真（M06，中）。因此“实验流程可重复运行”成立，“真实质量对比已完成”不成立。 |

## 三、范围一致性

| 类型 | 发现 | 影响 |
| :--- | :--- | :--- |
| 超出范围 | 未发现实质性新增业务域。决策快照、请求 ID、事务终态和诊断来源时间都是原有结构化、可审计和最终结果合同所需；没有为了 M3 提前建设 Agent 框架。 | B01-B05 主要修复既有合同，不构成新里程碑或技术路线切换。 |
| 缩小范围 | T056 的三个端点及真实评分/落库链已接通；T057 的学生读模型、诊断只读存储与刷新回调已接通，但完整 UI 使用路径仍有缺口（M01/M02，中）。 | T057 可以认定服务和回调已实现，不能据此认定学生/教师所有结果工作台交互均完成。 |
| 状态失真 | `.specify/tasks.md:154` 对学生数据接线标为完成，而 T095/T099/T111/T112（`:366`、`:370`、`:400`、`:401`）仍标“业务待接入”；当前事实是部分业务已经接入、部分选择器和入口更新没有闭合。T059 的 completed 仅表示运行完成。 | 任务说明存在完成粒度混用；详见 M01/M02、M07（中）。本次不修改任务状态。 |
| 阶段边界 | T064 要求持久化 WorkflowRun；真正的图状态、节点、恢复及人工复核分别在 T065、T072-T074、T076-T079（`.specify/tasks.md:175`、`:182`、`:183`、`:184`）。T063 只要求 AgentRun 模型和迁移。 | 未实现 LangGraph 恢复、未产生真实 AgentRun 记录，并不自动构成 M3 少做任务；不能反过来宣称 M3 已实现这些能力。 |

T046-T064 的逐组核对结果：

| 任务 | 当前事实 | 判定 |
| :--- | :--- | :--- |
| T046-T049 | 题型路由、客观规则评分及失败优先测试存在，包含同组答案重复 100 次测试。 | 职责已实现；没有用模型替代客观评分。 |
| T050-T053 | 查询构造、课程范围检索、重排、结构化评分及可配置置信度门禁存在。 | 默认装配链符合设计；显式配置传递问题见 M04（中）。 |
| T054-T055 | 整卷最终性由聚合器决定；诊断只消费最终结果，掌握度由平台计算，建议由 LLM 结构化生成。 | 没有复制评分职责或绕过最终结果门禁。 |
| T056 | 触发/任务状态/单题结果 API、课程归属校验、任务持久化、原地更新评分行、整批提交和主观评分生产装配均存在。 | 已从早期“未就绪边界”补成实际服务；不包含 M4 的恢复接口。 |
| T057 | 学生和教师查询服务共用落库结果，GET 诊断不调用 LLM，学生逐题/错题/诊断刷新已有真实 loader。 | 服务端主体已完成；UI 接线和部分错误/统计语义需补齐（M01-M03、M05，中）。 |
| T058 | 生产服务类组合在真实 PostgreSQL 隔离 schema 上测试；外部依赖替换。 | 有真实事务与组件协作证据，并非全替身测试；覆盖限度见第七节。 |
| T059 | Zero-shot、Vector RAG、Hybrid + Rerank，JSON/CSV、失败统计和 null 指标语义均实现。 | 实验工具已实现，真实质量实验尚未完成（M06/M07，中）。 |
| T060-T064 | 六个 ORM 模型、五个线性迁移和模型/迁移单元测试存在。 | 实体边界具备；模型存在不等于复核服务、Agent 追踪采集或恢复已经实现。 |

B01-B05 的编号在几轮修复中被重复使用。本次同时核对早期边界修复和最终补丁：B01 涵盖课程归属与任务提交/中断保护（`49045ea`、`b00091b`、`80be9bb`）；B02 涵盖评分字段保真与诊断并发（`e8d6c61`、`94621e8`）；B03 涵盖考试题目集合权威与 Benchmark 真实检索（`6836b46`、`58fad20`）；B04 涵盖存储就绪和失败统计（`607cd90`、`1ebbbaf`）；B05 涵盖逐题 DTO、组件回调及空态（`25726db`、`2c01c9b`、后续教师字段补丁）。这些都服务原任务的正确性。修复历史中的 M0 容器隔离改动不计作 M3 新增范围。

## 四、架构一致性

| 对照项 | 核查结论 | 代码与验收证据 |
| :--- | :--- | :--- |
| plan §4 Agent 分工 | M3 提供确定性服务和结构化端口，Agent 的路由/编排职责尚未加入，分工未倒置。 | `backend/app/services/grading/grading_task_service.py:682`、`:688`；M4 任务定义在 `.specify/tasks.md:175`。 |
| plan §5 状态图 | 当前同步管道具备 Load、Classify、Objective/Subjective、Validate、Confidence、Aggregate、Diagnosis 的业务顺序。Reviewer/Re-grade 和图恢复留在 M4，不能把当前 for 循环当作完整 LangGraph。 | `backend/app/services/grading/grading_task_service.py:710`、`:795`；`backend/app/services/grading/subjective_grader.py:428`。 |
| 客观题不调用 LLM | 客观题按 Question.type 路由到 ObjectiveGrader，直接产生结构化结果；不检索、不重排、不调用评分 LLM。最终整卷的诊断建议仍可调用 LLM，这与客观题评分独立。 | `backend/app/services/grading/grading_task_service.py:713`、`:736`；`tests/unit/grading/test_objective_grader.py:613`；`tests/integration/test_grading_pipeline.py:231`。 |
| 主观题标准链路 | 默认执行 Query Construction → Hybrid Retrieval → Rerank → Grading LLM → Pydantic → Confidence；缺题目必要字段或缺检索上下文明确失败。 | `backend/app/services/grading/grading_context.py:527`、`:571`、`:613`；`backend/app/services/grading/subjective_grader.py:447`。参数化装配问题另见 M04（中）。 |
| 低置信度暂停 | 低置信度保留原分数和决策快照，但不计入最终成绩。数据库 WorkflowRun 为 Paused；任务 DTO 为 Completed 表示“本轮自动评分完成”，同时 is_final=false、待复核数大于零。两种状态语义有明确区分。 | `backend/app/services/grading/confidence_policy.py:129`；`backend/app/services/grading/grading_repository.py:93`、`:609`；`tests/integration/test_grading_pipeline.py:317`。 |
| 诊断接受范围 | 聚合器只接受已验证且获接受/人工确认的结果；DiagnosisService 在整卷最终之前返回 Not Ready。正式 Teacher 写入/恢复尚待 M4，当前测试不能证明完整人工复核闭环。 | `backend/app/services/grading/result_aggregator.py:334`；`backend/app/services/diagnosis_service.py:207`；`tests/integration/test_grading_pipeline.py:317`。 |
| Answer → GradingResult → ExamResult → DiagnosisReport | 单题原始字段和置信度决策分开保存；整卷保存汇总事实和 aggregated_at；诊断保存真实 ExamResult 主键和来源汇总时间。读取评分不按当前置信阈值重做历史判断。 | `backend/app/models/grading_result.py:3`；`backend/app/services/grading/grading_repository.py:334`、`:714`；`backend/app/services/grading/diagnosis_report_store.py:137`。 |
| 诊断过期/并发 | GET 比较报告来源与当前 aggregated_at；旧请求保存前锁定成绩行并核对来源，不覆盖新的报告。成绩提交后诊断失败不会回滚已提交成绩。 | `backend/app/services/grading/diagnosis_report_store.py:98`、`:150`；`backend/app/api/grading.py:114`；`tests/integration/test_diagnosis_concurrency.py:29`。 |

SC-004 有 100 次重复评分的测试证据；SC-006 的结构化/范围校验有失败优先覆盖；SC-007 的“低置信度进入待复核、不得提前诊断”已有证据，“教师复核后恢复并生成最终诊断”仍属于 M4 未完成验收，不能跨阶段声明通过。

## 五、代码冗余

数量按本次识别的逻辑重复组统计，不是重复行扫描器分数，也不把 ORM、DTO 和 JSON Schema 的必要边界映射算成重复算法。

| 类别 | 数量 | 示例位置 |
| :--- | :--- | :--- |
| 重复评分逻辑 | 0 组已确认 | ObjectiveGrader 计算客观分，SubjectiveGrader 生成/校验主观分，ResultAggregator 决定纳入和汇总；`backend/app/services/grading/grading_task_service.py:710` 只装配它们。 |
| 重复模型字段 | 0 组需要机械删除；存在有意冗余 | 六个模型已经共用 UUID/时间 Mixin（`backend/app/models/base.py:29`）。submission_id/student_id 是查询与来源字段；score 与 decision_confidence/decision_review_status 是原始结果和当次决策；ReviewRecord 的 original/final 字段是审计前后值。不能因为同名就合并。 |
| 重复 API 代码 | 2 组样板 | 两份错误状态表（`backend/app/api/grading.py:70`、`backend/app/api/results.py:68`，8 个共有映射）和两份 HTTPException 包装（`:155`、`:394`）；见 L02（低）。授权依赖已复用 `require_permission`，资源归属检查分布于各查询入口并不等于可删。 |
| 诊断与汇总重复 | 0 组评分算法；1 处读回再聚合 | 诊断按知识点聚合已确认分数，不重新评分（`backend/app/services/diagnosis_service.py:287`）。仓储读回复用 ResultAggregator 构造明细，再恢复落库总分/时间（`backend/app/services/grading/grading_repository.py:731`）；这是复用而非复制，列表读取成本见 L03（低）。 |
| 未被调用的具体类 | 1 个已确认仓内无调用 | `DatabaseGradingProgressUpdater`（`backend/app/services/grading/grading_task_service.py:621`）仅出现在定义、文档和导出中；生产装配已明确不注入该类（`backend/app/api/grading.py:111`）。其独立事务进度写入重复仓储现有职责，见 L01（低）。不把 GradingProgressUpdater 协议及测试替身误算成该具体类的调用。 |
| 未引用函数/常量 | 未确认其它可删除项 | `AgentRun`、`ReviewRecord` 的生产写入尚待 M4，这是明确的模型任务交付，不能据此视为死代码。本项是仓内引用审查结论，不承诺穷尽反射/动态入口。 |

## 六、复用空间

| 可抽象位置 | 当前重复程度 | 建议 |
| :--- | :--- | :--- |
| T060-T064 模型 | 低，公共主键/时间已抽取 | 继续使用 `backend/app/models/base.py:29` 的两个 Mixin；保留评分、决策、复核和诊断各自生命周期，不增加“大一统结果基类”。 |
| 评分装配 | 已有单一正式组合；同步/异步边界需区分 | 复用 `DefaultScoringPipeline`（`grading_task_service.py:688`）和 `SubjectiveGrader.grade`（`subjective_grader.py:428`）。M4 同步节点可使用同步管道；异步节点直接 await 底层服务或显式卸载同步调用，不能在事件循环内直接调用现有 asyncio.run 包装。先修正 M04 的配置传递，无需另建通用 Pipeline 框架。 |
| API 授权/错误映射 | 权限依赖已共用，错误包装有两份 | 可抽取一个 grading 领域错误转换函数，同时补齐 M05；保留不同资源的归属检查。没有分页实现可供消重，新增分页会改变接口，不作为 M3 既有验收缺失。 |
| 结果读取 | 列表逐条重建完整结果，摘要调用成本偏重（L03，低） | 增加按考试/学生批量读取已落库汇总的仓储查询；详情仍复用完整 DTO 组装。学生一次刷新中的成绩与诊断可以在同一服务入口定位一次 submission，减少重复读取。无需修改评分算法。 |
| UI 状态/显示 | 已共用 empty_state、feedback、status_text；局部取值/空态代码重复 | 只抽取重复的对象/映射取值辅助函数（`backend/app/ui/results_view.py:195`、`backend/app/ui/results_diagnosis.py:47`）。优先补齐 M01/M02 的真实数据和完整组件更新，再处理纯展示消重。 |
| 迁移文件 | 5 份迁移均重复固定枚举构造辅助函数 | 保留 0005-0009 的线性历史。它们固定当时的枚举和约束，不能为了消重导入会变化的应用实现；也不建议合并已进入迁移链的文件。例：`migrations/versions/0005_grading_results.py:79`、`0008_workflow_runs.py:34`。 |

## 七、测试质量

本次执行现有聚焦测试，结果为 **574 passed，0 failed，0 skipped，5 warnings，43.30 秒**。集合包含 M3 相关目录及模型目录的现有回归，不代表全仓测试。未新增或修改测试，PostgreSQL 用例使用临时 schema 并清理；未调用收费模型、未执行开发库迁移或容器部署。

复现命令（仓库根目录，PowerShell）：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -p no:cacheprovider -q tests/unit/grading tests/unit/services/test_diagnosis_service.py tests/unit/models tests/unit/ui/test_results_view.py tests/unit/ui/test_results_loaders.py tests/contract/test_grading_api_contract.py tests/contract/test_results_api_contract.py tests/contract/test_grading_benchmark_contract.py tests/integration/test_grading_pipeline.py tests/integration/test_grading_benchmark_real.py tests/integration/test_diagnosis_concurrency.py
```

| 核查项 | 覆盖事实 | 证据与限制 |
| :--- | :--- | :--- |
| T058 真实路径 | 手工组装真实仓储、快照读取器、默认评分管道、SubjectiveGrader、置信策略、诊断服务和存储，使用真实 PostgreSQL。 | `tests/integration/test_grading_pipeline.py:181`。替换 Retriever、Reranker、Embedding 和两个 LLM Provider；并非完整复用 API 的生产工厂，也不是实模端到端验收。 |
| 客观题确定性/主观题结构化 | 100 次稳定性、Provider 调用数量、字段和决策快照保存/新 Session 重读有断言。 | `tests/unit/grading/test_objective_grader.py:613`、`tests/integration/test_grading_pipeline.py:231`。模型响应来自替身，能验证合同，不能验证判分质量。 |
| 低置信度反向路径 | 待复核题无 effective_score、不进入 final_total_score，WorkflowRun Paused，诊断建议 Provider 调用数为零。 | `tests/integration/test_grading_pipeline.py:317`。未覆盖 Teacher 真实写入与恢复，那属于 T074/T079。 |
| 诊断过期 | 修改来源 aggregated_at 后旧报告返回 Stale。 | `tests/integration/test_grading_pipeline.py:367`。此用例直接修改汇总时间，不是一次完整的重新评分/人工复核操作。 |
| 诊断并发 | 两线程/两事务在 PostgreSQL 上观察实际阻塞，断言新报告不会被旧请求覆盖。 | `tests/integration/test_diagnosis_concurrency.py:29`。这是实际数据库竞争的反向验证。 |
| T059 real 模式 | 保留真实 BGE 适配器、向量/关键词/Hybrid 检索和 LLM Reranker；验证缺语料失败与同一事件循环。 | `tests/integration/test_grading_benchmark_real.py:30`。BGE 编码器和 LLM 响应都被替换，不能称为“真实 BGE 推理和真实 LLM 质量测试”。正式脚本 real 路径使用真实工厂（`scripts/run_grading_benchmark.py:669`、`:685`）。 |
| 回滚/并发触发盲区（M08，中） | SQLite 单元测试有合法行后非法行、flush 后中断和提交回执异常；PostgreSQL 的 T058 失败用例第一条即非法，无中途写入证明；并发触发主要验证替身锁调用。 | `tests/unit/grading/test_grading_repository.py:380`、`:623`；`tests/integration/test_grading_pipeline.py:395`；`tests/unit/grading/test_grading_task_service.py:681`。不能把这些等同于真实 PostgreSQL 的双请求去重与中途写入回滚验收。 |
| UI 盲区（M01/M02，中） | 有真实 Gradio process_api 回调测试，逐题 DTO 显示得到验证。 | `tests/unit/ui/test_results_view.py:285` 在测试中直接填入 Dropdown.choices，跳过生产选项加载；它也没有验证从概览入口切换两次考试后的全部面板一致性。 |
| 迁移证据范围 | 现有迁移测试检查线性链及 upgrade/downgrade 对象定义；本次集成库通过 ORM create_all 建表。 | `tests/unit/models/test_m3_migrations.py:1`、`tests/postgres_helpers.py:52`。因此此次通过不能表述为“0005-0009 已在真实库完成 Alembic 升降级验收”；本次未复跑该操作。 |

在本次相关测试文件中未发现 xfail 或无条件 skip。共享 helper 在 PostgreSQL 或 pgvector 不可用时明确跳过（`tests/postgres_helpers.py:40`、`:47`），本次条件满足、没有跳过。没有当前失败样本可以支持“长期失败”的判断，也没有读取外部 CI 历史，不能推断长期全绿。

5 个 warning 为 1 条 FastAPI/Starlette TestClient 依赖弃用提示和 4 条相同的 User/Role fixture SAWarning（`tests/integration/test_grading_pipeline.py:164`）；记录为本次运行限制，未把 warning 冒充失败或新增验收门槛。真实教师标注和真实模型对比缺口另见 M07（中）。

## 八、M4 就绪度

本节的“未实现”描述后续任务的现状，不等于要求先完成 M4 再允许启动 M4。

| 接口 | 就绪度 | 说明 |
| :--- | :--- | :--- |
| T065 共用状态 | ✅ 可复用业务类型；⚠️ 需建立图状态 | `SubmissionSnapshot`、`GradingTargetAnswer`、`GradingOutcome`、评分/决策 DTO 可作为状态字段来源（`backend/app/services/grading/grading_task_service.py:267`、`:283`、`:710`）。按 T065 增加 workflow_id、当前答案、上下文引用、错误等图状态，无需重写评分服务。 |
| T069 Agent 调用评分 | ✅ 默认服务可用；⚠️ 注意调用方式与配置 | 客观题和聚合器可直接调用；主观底层 `SubjectiveGrader.grade` 已是 async。当前 `build_subjective_scorer`（`backend/app/services/grading/subjective_pipeline.py:90`）和 `DiagnosisRecorder.record`（`diagnosis_report_store.py:279`）使用 asyncio.run，应放在无事件循环的同步线程，或在异步节点改用底层 await。不能认定所有 LangGraph 调用都会失败，也不能无适配地直接调用。配置问题 M04（中）应在按 Agent 选择模型/检索策略前处理。 |
| T073 WorkflowRun 模型 | ✅ 可复用存储字段 | 模型已有 current_node/current_answer_id、JSON checkpoint、pause_reason、retry_count、resumable（`backend/app/models/workflow_run.py:56`）。未见必须另建一套运行模型的依据。 |
| T073 现有 checkpoint 内容 | ⚠️ 不能直接作为 LangGraph 恢复状态 | M3 checkpoint 保存任务统计和答案顺序，结果提交时节点为 aggregate，resumable=false（`backend/app/services/grading/grading_repository.py:563`、`:609`、`:627`）；重启仅把本执行器遗留任务收敛为 Failed（`grading_task_service.py:969`）。这如实表达当前能力，符合 M3；T073 应定义图专用 checkpoint kind/内容，保存节点与游标并实现暂停/恢复，不直接把旧快照标为可恢复。 |
| T074 复核与重新汇总 | ⚠️ 模型/门禁可复用，写服务待建 | ReviewRecord 保存原/最终字段；ResultAggregator 能消费复核状态。Teacher 写入、重评、重新聚合及报告来源更新时间必须在 T074 衔接，不得只改 review_status 就宣布整卷最终。模型入口：`backend/app/models/review_record.py:28`；聚合入口：`backend/app/services/grading/result_aggregator.py:178`。 |
| T072/T076 图与工作流 API | ⚠️ 按计划待实现 | 当前是独立评分后台任务，task Completed 与 Workflow Paused 的映射属于该 API 语义（`backend/app/services/grading/grading_repository.py:93`）。M4 工作流 API 应直接表达图的 Paused/Running 状态，不机械复用此映射。 |
| AgentRun 追踪 | ⚠️ 已有实体，尚未采集 | `backend/app/models/agent_run.py:43` 定义模型，未发现评分生产路径创建 AgentRun；T063 并未要求 Agent 执行。M4 节点应写入实际模型、Prompt 版本、耗时和关联 request/workflow ID，不能把表已存在当成 Trace 已贯穿。 |
| 结果/诊断复用 | ✅ 核心事实可复用；⚠️ 查询展示仍需整理 | 最终结果、原始字段、决策快照和诊断来源校验可以直接复用；M01-M03、M05（中）影响结果工作台及查询一致性，不要求更换评分模型或重做结果表。 |

已知接口问题中，M04 的显式配置传递最直接影响 M4 的组件选择；同步/异步边界和旧 checkpoint 的语义可在 T065/T069/T073 的既有工作中处理。本次没有证据要求把它们升级成新的 M3 高优先级验收门槛。

## 九、问题清单（按优先级）

以下都是复盘发现或建议，不是新增 tasks.md 任务。涉及修改/新增测试的建议，后续若获实施授权，应先按仓库现有 TCR 惯例说明必要性与行为覆盖；本次没有改变测试内容。

### 高优先级（M4 启动前必须修复）

本次未确认此级别的问题。T073/T074 的恢复与复核尚未实现是已规划的 M4 工作，不列为“必须先修复的 M3 缺陷”。

| 编号 | 严重度 | 问题 | 文件位置 | 建议修复方式 |
| :--- | :--- | :--- | :--- | :--- |
| — | — | 无已确认的 M4 启动阻断项 | — | 可以启动 M4；按下列问题的影响范围安排修复。 |

### 中优先级（M4 期间可修复）

| 编号 | 严重度 | 问题 | 文件位置 | 建议修复方式 |
| :--- | :--- | :--- | :--- | :--- |
| M01 | 中 | 教师结果工作台仍无法完成正常查询：课程/考试下拉为空且禁用；生产 loader 要求 exam_id，刷新只替换学生列表，摘要和知识点区域仍取空值。已存在的考试摘要/教师逐题查询能力尚未完整接到工作台。 | `backend/app/ui/results_view.py:657`、`:664`、`:765`；`backend/app/ui/results_loaders.py:94`；`backend/app/api/results.py:175`、`:216` | 沿既有 T099/T112 接入授权课程/考试选项、服务端考试摘要与所选学生的详情/诊断查询；每次切换清理旧选择和复核上下文。教师诊断读取须在查询服务中执行课程归属校验，不在 UI 直接查库。 |
| M02 | 中 | 学生结果入口更新不完整：独立结果页没有授权考试列表加载；概览入口只填当前一项并更新表格/提示，不更新总分、计数、诊断和掌握度，已看过另一考试时可能保留旧面板值，需再次手动刷新。页面还固定显示“等待 M3”和“暂无逐题结果”横幅。 | `backend/app/ui/results_view.py:401`、`:403`、`:432`、`:450`；`backend/app/ui/gradio_app.py:2606`、`:3524`；`tests/unit/ui/test_results_view.py:290` | 各入口统一使用完整面板刷新输出（已有 `refresh_student_panel`），从当前学生授权结果填充考试选项，选择切换时同步刷新全部区域；把空态横幅加入动态输出。后续组件测试覆盖“先看 A 再进 B”的完整输出，不在测试里代填生产应提供的选项。 |
| M03 | 中 | 教师摘要把草稿答卷计为“已提交”：查询只按 exam_id 过滤，随后 submitted_count=len(submissions)。仅开始答题但未提交的 Draft 也会增加已提交数，并使 not_ready=true。当前测试样本均为 Submitted，未覆盖该分支。 | `backend/app/api/results.py:185`、`:195`、`:208`；`backend/app/domain/enums.py:81`；`tests/contract/test_results_api_contract.py:92`、`:305` | 在统计查询中按已提交生命周期筛选 Submitted/Graded/Reviewed；如结果列表保留草稿，单独表达草稿状态，不能计入 submitted_count。按 TCR 增加草稿与已提交混合场景的契约断言。 |
| M04 | 中 | 显式 settings 未贯穿评分组件。SubjectiveGrader.grade 使用传入配置构造部分上下文和置信策略，但 `_generate_payload` 调用无参数工厂；上下文默认 Embedding/Reranker 也读取全局配置。调用方传入另一模型/重排配置时，可能出现阈值/候选数取新配置而实际模型仍用全局配置。 | `backend/app/services/grading/subjective_grader.py:447`、`:488`；`backend/app/services/grading/grading_context.py:558`、`:580`、`:615`；`backend/app/services/grading/subjective_pipeline.py:96`；`backend/app/api/grading.py:124` | 在装配边界用同一 AppSettings 创建并注入评分 Provider、Embedding、Retriever/Reranker 和置信策略，或把该配置显式传入全部工厂；保留调用方已经注入的组件优先级。以局部配置与全局配置不同的场景验证实际调用组件，不只验证参数对象。 |
| M05 | 中 | 结果 API 的未就绪错误语义有遗漏：教师学生详情跳过 ensure_ready；诊断存储的 `DIAGNOSIS_STORE_NOT_READY` 未在 HTTP 映射表列出，会落到默认 500。前者可能直接暴露为未转换的数据库异常，后者不能与其它存储未就绪响应保持一致。 | `backend/app/api/results.py:68`、`:216`、`:394`；`backend/app/services/grading/grading_repository.py:297`；`backend/app/services/grading/diagnosis_report_store.py:62`、`:122` | 教师详情使用同一就绪检查；把诊断存储不可用映射为明确的 503，保留来源错误码。后续契约验证未迁移结果存储与诊断存储不可用两个具体分支，不用空成功结果代替失败。 |
| M06 | 中 | Benchmark 的模型身份来自 DeepSeek 配置，而非实际使用的 Provider：real 模式允许注入/替换 Provider，记录中的 model 却始终取 settings.deepseek_model。更换适配器后记录可能标错模型，破坏可追溯性；当前也未记录评分 Provider 名称。 | `scripts/run_grading_benchmark.py:765`、`:829`、`:860`；`backend/app/ai/llm/factory.py:108` | 在实际组件解析后记录其 provider/model 元数据；显式注入 Provider 时要求对应实验元数据或标为未知，不能代填 DeepSeek 模型名。沿用现有数据集/Prompt 版本和脱敏规则。 |
| M07 | 中 | 缺少真实评分对比证据：四个样本 teacher_score 均为空，仓库只保存 selftest 评分运行。三项质量指标正确返回 null，因此无法评价真实 Zero-shot/RAG/Hybrid+Rerank 的评分一致性或效果。 | `benchmark/corpus/grading_samples.json:5`、`:20`、`:33`、`:46`、`:59`；`benchmark/results/grading_summary.csv:2`；`scripts/run_grading_benchmark.py:868` | 在后续评测工作中提供去标识化教师标注及标注版本，固定同一数据集/Prompt/模型配置执行真实三路实验并保存结果分析。保留 null 的诚实语义，不用合成 reference_score 当教师 Ground Truth；在此之前仅宣称流程验证通过。 |
| M08 | 中 | 部分测试证据强于实际覆盖：T058 的 PostgreSQL“部分写入失败”用例只传入一条越权结果，失败发生在首条写入检查；真实并发触发同一答卷的去重尚未覆盖，已有测试只验证替身收到锁调用。 | `tests/integration/test_grading_pipeline.py:395`、`:420`；`tests/unit/grading/test_grading_repository.py:76`、`:415`、`:623`；`tests/unit/grading/test_grading_task_service.py:681` | 按 TCR 将关键反向验证落到隔离 PostgreSQL：在合法写入/flush 后制造第二处失败并断言原结果、进度、终态均未部分提交；并发触发同一 submission，断言只受理/调度一个进行中任务。复用现有 helper，不扩大到无关压力测试。 |

### 低优先级（M5 可处理）

| 编号 | 严重度 | 问题 | 文件位置 | 建议修复方式 |
| :--- | :--- | :--- | :--- | :--- |
| L01 | 低 | 遗留 `DatabaseGradingProgressUpdater` 无仓内调用，仍维护独立事务进度写入，与仓储原子提交进度重复；模块顶部说明也仍描述该旧路径。 | `backend/app/services/grading/grading_task_service.py:26`、`:621`；`backend/app/services/grading/grading_repository.py:539`；`backend/app/api/grading.py:111` | 后续清理时确认外部接口使用范围，删除未使用的具体实现和过时说明，保留仓储作为结果/进度事务的唯一写入责任方；不为它新增框架或强行合并协议。 |
| L02 | 低 | grading/results 两份错误表和异常组装、results_view/results_diagnosis 的局部取值辅助函数重复，扩展错误码时容易不同步。 | `backend/app/api/grading.py:70`、`:155`；`backend/app/api/results.py:68`、`:394`；`backend/app/ui/results_view.py:195`；`backend/app/ui/results_diagnosis.py:47` | 抽取小范围的领域错误转换与纯显示辅助函数；维持各端点授权及响应合同，使用现有契约测试回归，不建立跨全项目的通用异常体系。 |
| L03 | 低 | 结果列表/摘要逐答卷读取并重建完整评分明细；学生成绩与诊断 loader 各自重复定位答卷和读取成绩。这是可见的重复数据库工作，但本次未测量性能，不能给出容量或延迟结论。 | `backend/app/api/results.py:170`、`:193`、`:283`；`backend/app/services/grading/grading_repository.py:714`；`backend/app/ui/results_loaders.py:61`、`:85` | 优先批量读取持久化的 ExamResult 汇总供列表/统计使用，并在一次学生面板查询中复用已定位的 submission/result；详细评分继续按需加载。分页属于后续接口取舍，不作为本次必修项。 |

## 十、结论

**M3 可以作为 M4 的基线，结论为“需关注”，无需回退核心评分链或重做六个结果/运行模型。** 已确认的问题主要影响查询展示、局部配置替换及评测可信度，应按第九节处理；没有已确认的“必须在 M4 启动前修复”项。

M4 开始设计时应明确复用现有评分 DTO/服务，区分同步包装与 async 调用，并在既有 T065/T073/T074 内定义图状态、可恢复检查点和人工复核事务。当前 WorkflowRun 能承载这些状态，但现有 M3 快照不能直接冒充恢复能力；这属于后续实施内容，不是追加 M3 范围。T059 工具和 574 项测试通过也不等于真实模型效果、完整人工复核闭环、容器运行或发布验收通过。

本次仅生成本报告；未修改业务代码、测试或 `.specify/`，未追加任务、执行 `/speckit.implement` 或 Git 提交。报告交付后停止，等待用户确认。
