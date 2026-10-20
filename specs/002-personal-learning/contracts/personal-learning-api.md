# Personal Learning API / UI Contract — Approved Boundaries / Proposed DTOs

对应 [spec.md](../spec.md) FR-001—FR-034 和 [plan.md](../plan.md) P01/P04/P05/P08。路径、DTO、状态名均为技术草案；不表示已有接口，不产生实施授权。

**Status**：T007架构职责与行为边界已按2026-10-19用户批准回填；本文件具体路径、DTO、字段、状态名称及实现示例仍为Proposed细节。T008十项硬性质量原则见[research §6](../research.md#6-t008-已批准质量原则--approved)；T008开发临时保护/逐例验收方法现已批准，见research §7；仅T008—T014获本轮实施授权。复杂渲染依赖、真实效果、G-C/G-P不随此批准。

## 1. Entry and Request Boundary

React 的个人首页建议使用 /，按科目进入 /subjects/{subject_id} 下资料、题库、练习和诊断；AI 教师在诊断页展开。具体布局 Proposed，科目首页与 React 统一方向已确认。

建议新资源统一 /api/learning 前缀，现有 /api/settings 保持独立 Setup 授权，不将 settings 客户端的管理 token 强加给日常学习。尚未完成初始化时显示真实配置状态，不能将缺失依赖表现为业务空数据。

已批准：当前空间由环境和数据根解析，不接收客户端任意 owner_id/UserRole；不保留业务登录或教师/学生角色入口，Setup及凭据保护独立。请求和文件边界见 [local-lifecycle.md](local-lifecycle.md)；无业务登录不是无来源/归属检查。

个人学习的 OpenAPI/TypeScript 需要扩展现有生成入口；当前 scripts/export_settings_openapi.py 只导出设置契约。不能手写失配的两份 DTO，不能借导出过程加载真实配置或连接服务。

## 2. Resources and Proposed Operations

下列均相对 /api/learning；没有要求建立一个通用 REST 框架。

| 资源 | 方法/路径建议 | 请求及响应核心 |
| --- | --- | --- |
| 科目 | GET/POST /subjects；GET/PATCH /subjects/{id} | 名称、稳定 ID；只处理当前空间 |
| 知识组织 | GET /subjects/{id}/knowledge；POST /subjects/{id}/knowledge/confirm | 建议章节/知识点、用户修改、真实来源关联和确认状态 |
| 上传 | POST /subjects/{id}/uploads | 文件+purpose，先返回真实接收/验证状态；收费处理另行确认 |
| 资料 | GET /subjects/{id}/materials；GET /materials/{id} | 版本、解析/索引状态、真实定位和可选元数据 |
| 默认版本 | POST /material-series/{id}/default-version | 明确 version_id 及用户确认；不改历史引用 |
| 导入校正 | GET /imports/{id}；PATCH /imports/{id}/questions/{qid}；POST /imports/{id}/confirm | 题块、来源、疑点、修订条件；批量确认不等于自动准入 |
| 题库 | GET /subjects/{id}/questions；GET /questions/{id}；POST /questions/{id}/revisions | 题目/答案独立来源、知识点、难度、准入、完整校正视图 |
| 重复确认 | POST /duplicate-candidates/{id}/resolve | same/different、目标身份与修订条件；保留全部来源/历史 |
| 操作费用 | POST /operations/preview；POST /operations/{id}/confirm；GET /operations/{id} | purpose、范围、绑定模型及派生步骤、估算或未知、实际状态 |
| 缺答案补充 | 由 operations 的 answer_completion purpose 发起 | 必须用户主动；同次返回结构化依据和一致性判断，服务独立核验可确定项；响应仍标 AI 补充，不伪造原卷来源，疑点不自动追加模型复核 |
| 练习准备 | POST /subjects/{id}/practices/preview | 来源、模式、范围、题量、难度、未做优先、grounding；返回覆盖/不足/费用 |
| 开始与作答 | POST /practice-previews/{id}/start；GET /practices/{id}；PUT /practices/{id}/answers | 开始时固定题目修订及分值；作答DTO只有选项或文字 |
| 整组提交 | POST /practices/{id}/submit | 同组提交幂等；空白文字主观题先提示但允许确认继续，本地记零不派发模型；收费评分确认只覆盖实际模型步骤，不逐题即时显示解析 |
| 结果 | GET /practices/{id}/results | 独立已作答/未作答、评分状态及执行来源、真实分数/失败、答案解析、轮次；作答完成率、本组确定/部分得分、选择题有效样本正确率和未答数 |
| 争议 | POST /answers/{id}/disputes；POST /disputes/{id}/regrade；POST /disputes/{id}/accept | 争议理由、明确评分依据/轮次、用户接受当前有效重评 |
| 科目诊断 | GET /subjects/{id}/diagnosis | 已作答有效/问题排除样本、单列未答数、首次/重练、近期对照、来源筛选和真实推荐引用；未答不参与有效表现 |
| 教师对话 | GET/POST /subjects/{id}/conversations；GET/DELETE /conversations/{id}；POST /conversations/{id}/messages | 会话上下文、主动发送、费用条件、回答/引用/不确定/错误 |
| 引用查看 | GET /sources/{id}；受管文件 GET 入口 | 定位真实文件/版本/片段；保留原文件服务路径安全和归属保护 |

纯本地免费操作不制造“收费模型已调用”事实。真实生成、评分、检索或上传处理是否收费依据实际绑定，不因路径名字猜测。

## 3. Preview, Confirmation and Failure

练习预览至少包含：严格筛选后的真题数、未做数、已做数、待核验排除数、实际知识点覆盖、其他题/原错题数，以及可执行的不足处理选项。原题不足和未做不足分别说明。

用户选择减少题量、调整条件、允许重练或确认 AI 补充后才继续；全真题模式不得隐式切 AI。无合格候选不返回伪成功空练习。旧预览所依据候选/版本已变时重新返回差异供确认，不把过期预览强行执行。

收费操作 Proposed DTO 应含操作用途、实际配置身份、预期派生步骤、模型、数据发送范围、输入/输出限制、已知估算与估算状态。数值由获批限制和实际输入产生，未批准前不设虚构预算。confirm 绑定 operation_id 和当前预览事实；不授权其他模型或任意后台工作。

AI 教师每个新会话首次收费发送确认；之后用户主动发送保留提示，不逐次弹窗。模型或费用条件改变重新确认。会话确认不授权自动续发、自动补问或失败后的自动重发。

同一幂等操作重复提交返回已有状态，不再派发；新尝试必须是用户明确动作。状态无法确定时使用 outcome_unknown 而非“未扣费/未调用”。已成功子结果保留，失败不改为零分或旧结果。

超限返回具体限制、当前观察值和用户可采取的自行缩小/拆分动作；超时显示实际完成与失败范围。第一版不增加内置选页分批和自动续处理。PDF 10页指单次处理批次，不得丢弃其余页或谎报整份完成；保留未处理范围，由用户主动决定后续。限额不能因请求失败而自动提高。

## 4. UI Visibility and Teacher Response

- 校正/题库管理可以查看参考答案；练习作答 payload 不携带答案、rubric、解析及仅解析使用的资产，不靠 CSS 隐藏已传数据。
- 全组提交后才展示评分/解析；单题失败与其余有效成绩分开，不制造整组 final 门禁。
- 空白主观题提示须能辨认未填写题目并允许返回补填或继续提交；Proposed DTO 传递待确认题目身份及明确继续意图，不能把空白静默改为已答。具体字段/状态码待契约评审。
- 页面直接使用独立作答状态区分未答和已答零分，不从分数猜测；选择题正确率展示有效已答样本分母，未答数单列；无有效样本显示无样本。作答完成率、本组确定/部分得分与诊断口径遵循内容契约 §5/§7，部分结果不能展示成全组最终成绩。
- 对话回答建议 Schema：answer_body、citations、uncertainties、可选练习建议；正文块与真实引用分别验证。模型只能引用实际提供的资料/题目身份。
- 回答中区分资料依据与模型补充，找不到依据时明确说明；不能自动联网找教材。
- 教师生成“练习建议”只预填正常练习入口，用户确认再开始；对话服务不得有更新成绩或诊断真值的副作用。
- 删除会话不删除共享材料或学习成绩。

## 5. Errors and Concurrency

沿用统一安全错误风格；建议错误类别包含 VALIDATION_FAILED、SOURCE_UNAVAILABLE、INSUFFICIENT_CANDIDATES、CONTENT_NEEDS_REVIEW、OPERATION_CONFIRMATION_REQUIRED、LIMIT_EXCEEDED、REVISION_CHANGED、PROVIDER_FAILED、OUTCOME_UNKNOWN，最终名称在契约评审冻结。

版本条件只保护可变业务内容/当前评分轮次，不把构建号或 Git SHA 当运行相等门禁。并发校正、同题合并、开始练习、重评接受应在责任事务层校验一次，并返回真实冲突。禁止回落到任意新来源、当前模型或替代文件消除失败。

本文件未规定 SSE/WebSocket、自动后台恢复、离线同步或多用户 API，这些不是本版必需能力。

### T043—T049 已实施的个人练习合同

- 预览默认 `source=mixed`（真题优先）、`mode=balanced`、20题、优先未做且不自动引入已做题；`uploaded/ai/mixed`、`random/balanced/weak`均为显式值。科目、知识点、题型、难度和原题文件条件取交集，非法或不属于当前科目的身份拒绝。
- 来源内先按规范身份无放回，均衡使用覆盖较少的知识点，同题多标签只占一个题位并计实际覆盖。强化先取薄弱点其他题，再穿插可用原错题，不预留固定百分比。未做/已做仅来自已提交且当前评分有效、无未解决争议的作答，草稿和未作答不占首次有效身份。
- `POST /subjects/{id}/practices/preview`返回候选/未做两类不足及实际覆盖。减量、允许重练或改条件创建新预览；`POST /practice-previews/{id}/start`只接受当前摘要和本次题目ID对应的合法分值。过期返回409 `PRACTICE_PREVIEW_CHANGED`及增/删/变更身份和新预览，必须重新确认。摘要只比较真实候选事实，不比较Git/构建号。
- 显式生成通过 `POST /practice-previews/{id}/generation-preview`和`/generate/{operation_id}`；所选`uploaded_material`版本严格限定真实片段，本批个人入口使用预览声明的关键词检索，不调用未使用的Embedding/Rerank，不修改其他入口/设置的检索模式。`general_knowledge`没有教材片段或伪造引用。无资料依据明确失败，不回落通用知识。
- 生成计划列出生产者保存的科目/数量/依据/范围摘要、实际模型和费用未知状态；本批为一次LLM响应同时返回题目、答案推导和四项质量判断，B=1。未确认零发送，无自动补发/第二模型；失败和未知状态保留。引用、编码、图表数据、疑点等确定性核验通过才可选入；Fake仅证明流程。
- 开始时固定原始题目、当时规范身份、修订、知识点、题序、分值、依据和私有评分输入；合法原卷分值保留，缺失/AI默认10分，开始前可调整。重复开始幂等，后续题库或资料默认版本变化不替换本次输入。
- `GET /practices/{id}`作答响应不携带参考答案、rubric、解析或仅解析资产；`PUT /practices/{id}/items/{item_id}/answer`只保存该冻结题型的选项或文字草稿，独立记录已答/未答。整组提交与评分属于T050以后，本批不实现。
