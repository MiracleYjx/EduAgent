# Implementation Plan: EduAgent 个人自主学习

**Branch**: deepcode | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)
**Feature**: 002-personal-learning
**Input**: 既有二十项业务确认、已完成T001—T006证据及本轮用户正式批准的八项架构方向和十项质量原则及开发阶段临时保护/验收方法。
**Status**: 14/87完成（T001—T014）；T007/T008审批及本批基础验收完成，范围止于T014。T015以后、G-C/G-P均不在本轮授权。

**Approval scope（2026-10-19）**：用户已正式批准T007八项架构方向及T008十项质量原则。Approved指职责与行为边界，不表示产品实现或能力验收；物理表名、DTO字段/路径/状态名、具体排序/候选算法、复杂渲染依赖及未批准数值仍按Proposed细节处理。细化不得改变已确认业务规则、公共合同语义、历史/恢复边界；新增实质取舍需另审。T008已通过；本轮仅T008—T014获实施授权，G-C/G-P及禁止T015以后的边界有效。

## Summary

保留 Windows/Docker 本地应用及成熟后端组件，以 React 承载按科目组织的个人学习闭环：用户上传资料/试卷，确认知识组织与原题，按来源和模式练习，整组评分，再查看个人诊断、资料建议和 AI 教师。

已确认的产品规则以 spec.md 为唯一事实源。推荐以 Subject/PracticeSession 表达新业务，复用文件、解析、RAG、评分组件和 RuntimeSnapshot 等共享能力；不通过虚拟教师登录保留旧业务。详细依据与替代方案见 [research.md](research.md)，数据与状态见 [data-model.md](data-model.md)。八项架构方向现已正式批准；具体技术名/DDL等设计映射仍保留Proposed标识，不表示新功能已具备。

## Technical Context

**Language/Version**: 后端声明 Python >=3.12；前端 TypeScript 5.9.3、React 19.3.1，依据 pyproject.toml、frontend/package.json。后台依赖多数为最低版本范围，不能称为全栈精确锁定。
**Primary Dependencies**: FastAPI、SQLAlchemy、Alembic、Pydantic、LangGraph、OpenAI-compatible Provider 抽象；React/Ant Design/Vite；版本以现有声明和 lock 为准，不在本轮升级。
**Storage**: PostgreSQL/pgvector、现有 Redis、环境数据根下的受管文件与配置/凭据存储。没有新增数据库或对象存储服务。
**Testing**: Pytest（unit/contract/integration）、Vitest、TypeScript/API 生成检查、Vite 构建；真实 PG、Windows onedir、Docker 及浏览器体验另行验证。本批已复用现有隔离工具中的Ruff/Mypy；pyproject dev组仍未声明这两项，本轮未安装/升级或改依赖声明。
**Target Platform**: Windows 本地 EXE、Docker 本地发布；不新增云多用户服务。
**Project Type**: Python API/AI 服务 + React SPA + 本地 Launcher。
**Performance Goals**: 开发阶段采用research §7已批准的可配置保护及逐例质量方法；不将其称为生产容量、峰值内存或账单硬上限，不设无统计依据准确率及统一启动/吞吐SLA。
**Constraints**: 一个数据根一个个人空间；无业务登录；设置授权独立；真实来源、历史依据、凭据版本与索引身份保持；无教材抓取、无图片答案、无废弃对象备份；本轮仅实施T008—T014及必要规划维护，不操作Development/Demo或旧业务数据。
**Scale/Scope**: 单人本地学习，支持规格列出的资料、题型和模式。临时文件/解包/解码/批次/并发/字节/时间保护按research §7；模型Token和费用必须另有具体预算及G-P，生产容量仍待代表样例测量，不继承旧实现常量作为新要求。

## Constitution Check

这是设计符合性检查，不是运行验收。

| 原则 | Phase 0 / 设计后判断 | 对应设计 |
| --- | --- | --- |
| 可运行优先 | 无已知设计冲突；交付能力未验证 | 沿用本地 API、PG/Redis、Launcher 和现有前端打包，避免新增常驻基础设施 |
| AI 能力优先 | 无已知设计冲突 | UI 服务于上传、练习、评分、诊断和答疑，不扩展企业后台 |
| 模型可替换 | 保留 | 业务依赖 ProviderRegistry/RuntimeSnapshot/Provider 抽象，不能直接在页面或领域服务调用具体 SDK |
| 核心结果结构化 | 保留 | 题目、评分、诊断建议和 AI 教师引用经 Schema/Pydantic 校验；聊天正文可作为结构化字段展示 |
| 效果可评测 | 保留，效果尚未证明 | TV-001—TV-011 转为有输入、观察结果和退出条件的验证场景；不虚构正确率或模型能力 |
| 实质架构取舍需批准 | T007八项方向已由用户明确批准 | 已批准方向与Proposed实现细节分开；T008数值/质量门禁、G-C/G-P和下一轮实施授权未被豁免 |

未发现必须修改宪章才能研究的方案；未将“草案完成”当作审批完成。详细数值、库选型、迁移处置和质量准入标准未批准前，不声称可执行验收合同已经全部冻结。

## Project Structure

### Documentation (this feature)

~~~text
specs/002-personal-learning/
  spec.md
  checklists/requirements.md
  plan.md
  research.md
  data-model.md
  contracts/
    personal-learning-api.md
    content-and-grading.md
    local-lifecycle.md
  quickstart.md
  tasks.md             带 Spike 和用户评审门禁的任务清单
~~~

tasks.md 按本轮授权由 speckit-tasks 生成；任务拆分可先于技术取舍最终冻结，但依赖未批准方案的实施必须等待相应用户评审门禁。

### Source Code (repository root)

~~~text
backend/app/
  api/                 现有接口；拟增加个人学习资源接口并退出旧业务接口
  models/ schemas/     拟改造题目/资料并引入个人练习及历史实体
  services/            领域编排、评分、诊断、存储和设置
  ai/                  解析/OCR、出题、检索、Vision、Workflow/Provider
  core/ deployment/    Bootstrap、请求边界、Launcher、维护
  ui/                  旧 Gradio；必需行为迁出并核对依赖后删除被替代部分
frontend/src/
  app/ api/ auth/      React 路由与契约；设置授权继续独立
  features/            拟增加科目、资料、题库、练习、诊断与教师对话
migrations/versions/   沿用可执行迁移链，新增受审迁移；本轮不执行
tests/                既有分层；后续受影响断言按 TCR 调整
packaging/ scripts/    现有 Windows/Docker/构建工具
~~~

**Structure Decision — Proposed**: 在现有单体边界内局部重组职责；不新增 BFF、微服务、通用任务框架或第二套文件/凭据系统。拟新增模块的确切名字可在 Tasks 中细化，不据目录名猜测已有能力。

## Architecture and Decisions

| 研究域 | 已批准的架构/行为边界 | 未冻结细节或待验证能力 |
| --- | --- | --- |
| P01 个人数据域 | 独立个人域；稳定科目/知识点、题身份/修订/来源、Practice/Answer/GradeAttempt/Dispute职责，历史保留 | 物理表/字段/迁移DDL为设计映射；无实际迁移，具体清理目标仍走G-C |
| P02 内容与解析 | 统一有序块、受管资产引用；复杂对象保留原内容/定位、人工核对，不静默丢失 | python-docx/有限OOXML与复杂渲染组件未因方向批准而获得完整能力验收或新依赖授权 |
| P03 准入与评分 | 保存与可评分分离，固定依据、空白本地零分、逐轮重评和有效诊断边界 | DTO/实现接线、代表样例、真实评分稳定性与数值标准待实现/质量评审 |
| P04 费用与失败 | 付费确认、真实请求计数、重试约束，未知不当作未发送 | OperationRun具体结构、逐步预算、token/请求/金额规则见research §7批准范围；无付费授权 |
| P05 空间与维护 | 数据根唯一空间；无业务角色登录，设置/凭据/必要本地访问保护独立；保留共享消费者 | Host映射、文件owner字段和恢复格式为实现设计；删除/恢复兼容变更不得越过对应评审 |
| P06 RAG与教师 | 教材版本/历史引用；教师复用RAG、ProviderRegistry、RuntimeSnapshot及客户端生命周期；聊天不改分 | default_llm复用、具体上下文裁剪/引用DTO为实现建议，容量和模型效果未验收 |
| P07 选题与诊断 | 诊断来自有效作答事实；稳定同题身份，未答/问题记录处理与既定统计规则不变 | 无放回/覆盖轮转等为Proposed实现示例，不把未获确认的算法或排序细节写成新产品规则 |
| P08 界面与退役 | React+TypeScript+Ant Design统一业务；替代并回归后退役Gradio；共享能力保留 | 校正布局和模块名可细化；工具声明、双平台运行与资源/性能质量仍需验证 |

### 主链路

上传/解析 → 确认知识组织或校正题目 → 固定题目修订及来源 → 预览候选/不足/费用 → 用户开始与整组提交 → 逐题评分与显式重评 → 有效结果诊断 → 真实材料建议/教师答疑 → 新一轮确认练习。

空白主观题提示后允许继续整组提交、本地记零且零模型调用；独立保留未作答状态。本组全部成绩确定后得分率含未答零分，选择题正确率与知识点诊断只使用已作答有效记录；未答数单列，不占首次/20题窗口/5题门槛，仍视为未做题。这些是已确认产品规则，数据字段和提交协议仍待技术 Review。

每次模型操作通过生产 RuntimeSnapshot 固定配置、凭据版本和上下文，ProviderRegistry 负责真实能力选择。一个用户操作可能包含 OCR、Embedding、Rerank、生成或核验；费用预览不能只算最终 LLM。旧 T237 三次请求及 ¥0.15 是历史一次性授权，不是本产品默认预算。

### 历史与恢复

使用具体题目/教材修订和逐轮评分记录保留在用业务事实；不是为遗弃旧业务建立备份。默认教材更新、题目修正、模型配置激活不改写历史作答依据。备份/恢复资源归属、Workflow 和 RuntimeSnapshot 依赖必须同步适配新域，不能仅删除用户表或旧 owner 分支。

## Review Gates and Proposed Module Locations

T007八项架构方向已获用户正式批准；T008开发保护与质量验收方法已批准，详research §7。审批来源是本轮用户指令，非任务生成或代理推定。

| 门禁 | 任务 | 批准对象及阻塞范围 |
| --- | --- | --- |
| G-A 架构/契约 | T007已完成，依赖T002/T003/T006已满足 | 八项架构方向及其职责/历史/归属/退役边界Approved；示例字段、算法、库依赖不因方向批准自动冻结；新增实质变化仍需评审 |
| G-Q 质量/限额 | T008，依赖T004/T005及G-A | 代表样例、质量判定及文件/页/图/时间/上下文/调用/费用限制；未批准则T009起阻塞，Fake不作为模型效果证据 |
| G-C 具体清理 | T073 | 替代功能验证后的逐对象清单、当前依赖和实际目标所有权；阻塞T074—T079，不授权本轮清理 |
| G-P 真实模型 | T082 | 最小模型验证协议、请求/费用等上限与独立Test凭据；阻塞T083及最终完成，不阻塞独立Windows/Docker验证 |

任务中的拟新增模块落点（位于已批准架构内，名字及细节仍是设计映射）：

- 数据：`backend/app/models/subject.py`、`practice.py`、`question_revision.py`、`question_origin.py`、`score_dispute.py`、`operation_run.py`、`conversation.py`；复用现有题目、文档和评分模型，迁移拆为foundation与cutover两步。
- 服务/API：`backend/app/services/` 的 learning_operations、subject_service、learning_material_service、knowledge_organization、answer_completion、question_identity、practice_selection、practice_service、personal_grading、score_dispute_service、learning_suggestions、conversation_service、teacher_context、teacher_service；API按同领域分文件。具体DTO由契约批准后生成，不新增独立平台。
- 前端：`frontend/src/features/` 下 subjects、materials、paper-import、question-bank、practice、results、learning-diagnosis、teacher；复用有序题块，现有settings/auth边界保留。
- 测试：`tests/spikes/` 仅后续验证使用；其余沿unit/contract/integration层及页面测试。四个Spike测试及其conftest已由DeepCode落盘，现有最小TCR已保留并补充本次必要覆盖；其他拟新增路径仍不代表实现。

## Delivery Slices — Proposed

以下是可体验切片方向；具体依赖见 tasks.md。未获批方案须先完成 Spike 和用户评审，切片列出不代表可越过门禁执行。

1. **个人入口与安全边界**：科目首页、独立设置、个人资源归属；入口不依赖虚拟用户，文件/维护保护仍有效。
2. **资料与知识组织**：先验证格式/图文解析方案，再形成上传、用途、知识点确认和版本引用的真实闭环。
3. **真题与题库**：有序校正、批量确认、答案来源、准入和去重身份；多选贯通导入。
4. **组题与评分**：来源/模式/不足交互、固定输入、整组提交、客观零模型调用、主观及通用知识链、逐轮重评。
5. **诊断与教师**：既定统计窗口及排除规则、材料入口、针对性练习、科目对话和收费确认。
6. **旧业务退出与交付**：依赖核对后删除遗弃对象，无旧业务副本；新域恢复、React 完整首启、Docker 持久化及进程清理。

每个切片都需功能和相关回归证据，不能先删完旧 UI 再以“接口存在”代替实际可用。临时开发过渡不构成长久双业务兼容要求。

## Verification Strategy

见 [quickstart.md](quickstart.md) 的现有命令和未来体验场景。验证分层：确定性单元/契约、Fake HTTP 请求边界、真实 Test PG/Redis、浏览器/Windows/Docker；各层证据不得互相冒充。新测或改测先按仓库 TCR 留最低必要的合同变更原因，不写额外验收报告。

保留旧设置、凭据、RuntimeSnapshot、索引身份及数据恢复的安全断言；多选部分分、教师审批、整卷 final 等确属已批准改变的业务断言必须有明确新预期。旧 Feature 的 48/48 不等于新 Feature 完成，也不复用旧测试数量作为本次结果。数值性能/容量/准确率门槛需先用受控样例实测提出，不把未跑场景记为通过。

## Requirement Coverage

| 需求范围 | 设计落点 | 契约 / 验证 |
| --- | --- | --- |
| FR-001—FR-002；A1 | P01/P05/P08，个人空间/科目/独立设置 | API §1、Lifecycle §1；Q01/Q10 |
| FR-003—FR-008；A2 | P02/P06，资料版本/位置/知识点 | Content §1—§2；Q02/Q03 |
| FR-009—FR-013；A3 | P01/P02/P03，校正/来源/同题身份 | Content §3；Q03/Q04 |
| FR-014—FR-017；A4 | P07/P04，来源与分配/不足确认 | API §3、Content §4；Q05 |
| FR-018—FR-021；A5 | P02/P03，顺序块/图像依据/文本答案 | Content §1、§5；Q03/Q06 |
| FR-022—FR-026；A6 | P03，评分轮次/争议 | Content §5—§6；Q06/Q07 |
| FR-027—FR-030；A7 | P06/P07，逐题统计/资料建议/科目对话 | API §4、Content §7；Q08/Q09 |
| FR-031—FR-033；A8 | P01/P05，清理清单和共享边界 | Lifecycle §3—§4；Q10/Q11 |
| V1/OUT/DEF；A9 | 全部切片受 spec 范围约束 | 禁止自动抓取/图片答案/旧迁移/废弃备份；每次 Review 检查 |
| FR-034；A10 | 全链路及 P04 费用交互 | Q01—Q12，Windows/Docker 实际闭环 |
| BASE/CHANGE/TV；A11 | research 源码证据及验证退出条件 | 研究不等于已通过；Q02/Q03/Q06/Q10—Q12 |
| QD；A12 | T007八项方向Approved，具体未定实现与T008限额Proposed | 已确认业务决定不重问；审批不冒充能力验收，未批准数值/方法不自动冻结 |

## Complexity Tracking

本草案未引入需豁免的宪章违例。复杂性主要来自现有角色/考试耦合和图文解析缺口；建议通过有界适配与明确数据转换处理，不建立通用平台。DOCX 渲染器、新依赖、费用控制持久化和迁移方案是需 Review 的成本，不以“更安全”自动扩展范围。

## Planning Workflow Notes

沿用实际 plan-template 的 Summary、Technical Context、Constitution Check、Project Structure 和 Complexity Tracking 结构。setup-plan.ps1 调用 Get-FeaturePathsEnv 时会持久化旧指针，故按用户更高优先级约束不执行该有副作用入口；使用 check-prerequisites.ps1 -PathsOnly（NoPersist）及 resolve-template.ps1 解析同一模板后，只创建本 Feature 文档。没有修改脚本、旧指针或旧 Feature。未发现 before_plan/after_plan 扩展钩子。

本轮已按授权生成 Tasks，并对完整文档执行只读 Analyze；分析发现的规划问题按用户既有授权修正后复核。先列隔离、无付费的能力 Spike，再列架构与质量/限额用户评审门禁；Proposed 不自动变为 Approved，依赖实施保持阻塞。真实模型效果另需明确调用授权；当前授权仅连续实施T008—T014，不进入T015以后。

## T007 审批记录 — Approved architecture directions

用户在2026-10-19明确批准以下八项方向，完成T007要求的用户裁决与设计回填；T007的Approved范围仅为这些方向，不把未批准的字段名、算法示例、库或限额一并提升为既定要求。

| 批准项 | 已批准方向 | 设计落点 |
| --- | --- | --- |
| A01 | React+TypeScript+Ant Design统一个人业务，替代和回归后退役Gradio，设置安全独立 | P08；API §1；Lifecycle §6 |
| A02 | 环境数据根确定唯一空间，无教师/学生入口及业务登录；Setup/凭据/必要本地访问边界保留 | P01/P05；Lifecycle §1—§2 |
| A03 | 稳定知识点ID、教材系列/版本和真实历史引用，默认切换不改历史 | P01/P06；Data Model §1/§3/§6 |
| A04 | 题干/选项/解析有序内容，媒体受管ID；复杂对象保留原内容与定位并人工核对 | P02；Data Model §2；Content §1 |
| A05 | Question身份、修订、多来源分开；疑似同题用户确认，不凭文字相似自动合并、不覆盖历史 | P01/P07；Data Model §4.1；Content §3 |
| A06 | Practice/Answer/GradeAttempt/Dispute分责，整组提交、空白本地零分、固定依据、主动重评及对照 | P01/P03；Data Model §4.2—§4.3；Content §5—§6 |
| A07 | 正式有效作答事实生成诊断；科目教师复用既有RAG/ProviderRegistry/RuntimeSnapshot/Lease，聊天不改成绩 | P06/P07；Data Model §5；API §4 |
| A08 | 完成替代及共享消费者迁移后按实际清单退出旧业务；遗弃数据不迁移/归档，在用共享配置保留 | P05/P08；Data Model §7；Lifecycle §3—§6 |

T002有限样例通过不代表复杂DOCX、OCR、真实模型效果已通过；这些能力继续按TV和G-Q/G-P验证。具体清理对象/目标仍需G-C，本轮只允许独立Test验证基础DDL，不清理旧业务数据。T008开发保护与方法已批准；仅T008—T014获本轮实施授权，真实模型/生产容量验收不随门禁通过。

## Foundation implementation boundary

本批落地仅为T009—T013数据与操作基础：新增0025/0026迁移、个人归属/稳定身份和历史约束、Snapshot/Registry发送控制，以及OpenAPI同源个人操作DTO/TS客户端。没有提前开发科目首页、资料解析、题库、练习、诊断或教师业务。

LearningLimits保存获批开发值；当前新操作链已接入请求/响应字节、HTTP超时、逐次预算、并发及零自动重试。上传、DOCX解包/图数、PDF批次/解码和OCR软等待的生产接线由T026/T027等对应任务完成；这里只建立共同配置，不能宣称这些解析功能已具备。原生线程必须保持占位的规则不变，不能靠新建线程绕过。旧链不因新配置存在而自动被视为受其约束。
