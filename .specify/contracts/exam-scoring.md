# v2.0 考试内分值与评分契约

## 范围与依据

对应 [spec.md](../spec.md) FR-049 至 FR-051、FR-028/FR-039/FR-040 的扩展；引用 [plan.md](../plan.md) §12、§13 及 [data-model.md](../data-model.md) §7.6、§9、§10。
沿用 v1.0 规则评分、结构化主观评分和教师复核；只增加本场固定分值/标准的统一语义，不把设计当作已实现。

## 分值来源与发布依据

~~~text
草稿预览：
effective_score = ExamQuestion.score（非 null）或 Question.score

发布事务：
ExamQuestion.score = effective_score
ExamQuestion.base_score = 发布时核对的 Question.score
固定 published_knowledge_points 和 scoring_basis

已发布评分 / 复核 / 汇总：
只使用固定 ExamQuestion.score 和本场 scoring_basis
~~~

- 同题在不同考试可以有不同 score；不修改 Question.score，也不因调整考试 A 影响考试 B。
- 仅草稿允许 score=null。已发布关联缺少固定依据返回 EXAM_SCORING_BASIS_MISSING，不能动态回退当前题库分值制造成功。
- score/base_score 为正数，Numeric(8,2)，不超过 999999.99；输入最多两位小数，拒绝超精度、NaN/Infinity、非法负数，不能静默替教师舍入输入。
- 金额传输为十进制字符串，内部 Decimal；满分、基准和发布知识点以 ExamQuestion 列为唯一事实源，不在 scoring_basis 存可独立改写副本。
- 发布依据缺失、未知历史满分或未完成教师尾差确认时不能按新标准发布/重评；历史迁移规则见下文。

## Rubric 结构化与换算

- 题库原始 Rubric 仍为受冻结保护的 Question.scoring_rubric；数值要点必须来自结构化校验及教师核对。
- 不用正则/字符串替换原 Rubric 的数字决定得分，不凭空给定性标准、重叠要点或门槛条件编造权重。
- 明确可加总 Rubric 先核对基准要点合计等于 base_score；非加总/定性标准保持真实语义，由教师确认本场对应方式。
- 中间计算至少 28 位 Decimal 精度，先乘本场 score 再除 base_score，最后一次量化；比例仅作解释，不先保存两位比例再计算。

~~~python
from decimal import Decimal, ROUND_HALF_UP, localcontext

with localcontext() as context:
    context.prec = 28
    default_points = (
        base_points * exam_score / base_score
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
~~~

scoring_basis 的 Pydantic 结构引用数据模型，不保存完整题干、选项或图片快照：

| 字段 | 语义 |
| :--- | :--- |
| kind | objective / subjective，与题型路由一致 |
| rounding_mode | 固定 ROUND_HALF_UP |
| points | [{key, label, base_points, default_points, confirmed_points}]；key 唯一，金额为两位字符串，有限非负且不超对应满分 |
| additive | bool，明确是否允许加总，不能猜测 |
| rounding_delta | 可加总标准中 score - sum(default_points)，有符号两位字符串；非加总/定性为 null |
| preparation_id | 当前准备轮次 UUID，由服务生成；历史未记录为 null，不补造。轮次与题目修订/本场上下文用于拒绝迟到确认 |
| confirmation | null 或 {teacher_id, confirmed_at, reason}，由真实有权限教师填写，保留 UTC 时间/处置理由 |

- default_points 保留独立四舍五入的真实结果；confirmed_points 保存教师最终明确采用的本场要点值，不能把人工调整伪称自动舍入。
- 无尾差且数值标准可加总时，confirmed_points 可等于 default_points；有尾差必须展示差额，要求教师明确处置并验证最终合计等于 score。
- 例：基准 3.00，三个 1.00 要点改为 10.00，默认 3.33/3.33/3.33，尾差 +0.01；教师可选择 3.34/3.33/3.33，并记录确认。
- 不自动把尾差加到某要点、按序分摊或隐藏 9.99。教师确认发生在发布之前；未确认返回 RUBRIC_ROUNDING_UNCONFIRMED，不等到学生提交后才临时改变标准。
- 定性标准 points=[] 不等于无标准或零分标准；保留原文字标准、基准/本场满分关系与教师核对。无法确认对应语义返回 RUBRIC_REVIEW_REQUIRED。
- 正式评分使用 confirmed_points 及已核对本场标准，不二次乘比例；改变草稿满分/题目/Rubric 后旧确认失效，重新核对发布。

## 评分输入与输出边界

ScoringInput 使用 Pydantic 结构，包含以下真实输入；业务标识由服务组装，不能由模型生成或从 Prompt 文本反解析。

| 字段 | 语义 |
| :--- | :--- |
| exam_id、exam_question_id、question_id | 定位唯一考试关联，核对属于本场及课程 |
| submission_id、answer_id、student_id | 真实答卷与答案归属，学生答案对应本题 |
| question_type、question_content、options | 受发布冻结保护的原题内容/题型/选项 |
| reference_answer、source_rubric | 受保护原答案/文字 Rubric |
| effective_score、base_score、scoring_basis | 固定本场满分、发布基准及已核对本场标准 |
| student_answer | 实际提交内容，不用参考答案替代空答 |
| course_context、source_references | 同课程可追溯检索依据，遵循既有 RAG 空上下文/失败规则 |
| assets、verified_image_conditions | 原题授权图像及与当前原图对应的已核对条件；没有图像可为空 |

- 客观题用既有确定性匹配规则，正确给本场 effective_score，错误/合法空答按现有规则；不得依赖 LLM 判断或套用题库默认分值。
- 主观题经既有 Provider.generate_structured/Pydantic 边界，使用本场标准与上限；图像输入/人工核对参见 [vision-capability.md](vision-capability.md)。
- 需要图示但无法可靠获得条件、模型失败、资料依据不足与学生答错分别表达；缺依据/失败不输出伪造 0 分。
- 评分结果沿用现有结构化结果及生命周期，评分服务显式关联本场输入/满分/依据，返回得分、理由/要点、来源和复核状态，不改写 v1.0 已有结果字段含义。
- 分数用 Decimal 量化到两位（ROUND_HALF_UP），并检查原始/最终分数均在 [0,effective_score]；越界返回 GRADING_SCORE_OUT_OF_RANGE，不裁切制造成功。
- 教师复核读取同一固定依据和满分，保留原结果与真实修改意见；不能在复核中临时改变本场满分或原题 Rubric。

## 汇总、分析与学生反馈

- 整卷满分=sum(本场有效 score)，最终得分=sum(本场最终有效题分)；Decimal 求和，不逐项回取 Question.score。
- 未完成、失败、依据不足、待复核单独计数；整卷存在未完成必要评分/复核时，不宣称最终成绩或把该题当零分填入平均分。
- 逐题得分率=有效最终得分之和/对应本场满分之和；平均分/成绩分布注明最终有效答卷数，无有效样本显示暂无数据。
- 知识点统计使用 published_knowledge_points 及对应题目/有效人数/满分分母；一题归属多个知识点可分别展示，不能重复累计成整卷总分。
- 教师关注名单给出实际失分/未完成原因；学生解释/薄弱知识点来自本人真实答卷与最终评分。
- 复习资料/练习推荐必须同课程、可访问且有真实来源；未审核/待补全题不能作正式练习，缺资料/题目明确说明。
- 学生结果中可显示允许查看的本题评分解释及原题图，不能因此开放含答案源卷或其他学生评分。

## 并发与错误传播

- 独立评分身份以 exam_question_id + submission_id + answer_id 为依据，沿用现有流程幂等/检查点语义；不得只以 question_id 缓存/复用另一场评分。
- 重复评分/复核遵循既有生命周期，不无限重试、不重复累计得分；保存时核对输出确属当前答卷和固定标准。
- 分值换算只写本场草稿/发布关联，不改共享 Question；不同考试并发不能覆盖对方标准。
- 题目/关联发布冻结覆盖内容、图像及评分依据；历史保护见 [exam-assembly.md](exam-assembly.md)。

| 错误码 | 行为 |
| :--- | :--- |
| EXAM_SCORING_BASIS_MISSING | 本场固定满分/基准/依据缺失或未核对，拒绝按当前题库推断 |
| EXAM_SCORING_INPUT_NOT_SUPPORTED | 固定依据完整，但当前评分输入不能完整表达本场分值、结构化标准、知识点或图片条件；明确拒绝执行 |
| RUBRIC_ROUNDING_UNCONFIRMED | 尾差尚未由教师确认，阻止发布 |
| RUBRIC_REVIEW_REQUIRED | 原标准含糊、定性对应或权重尚未核对 |
| GRADING_SCORE_OUT_OF_RANGE | 原始或量化分数越界，保留原错误/待处理，不截断 |
| VISION_NOT_SUPPORTED / VISION_REVIEW_REQUIRED | 图像能力/条件不可用，保留原图并明确待处理 |
| FILE_MISSING | 本场必需图像缺失，不替换为其他文件 |

异步评分失败按现有 Answer/GradingResult/Workflow 状态及错误传播，不新造成功状态；同步业务拒绝沿用 detail 的错误码、中文提示和 current_status。

## 历史兼容与验证

- v1.0 的实际答案、分值、评分及复核证据保留；迁移只按真实证据落实历史本场依据，未知明确标记。
- 不能用当前 Question.score/Rubric/知识点冒充历史发布值，也不能未经核对按新标准改写既有成绩；未收敛历史考试保留真实读取并拒绝声称完整 v2.0 评分依据。
- 验证同题跨考试不同满分、客观题给分、主观题换算/复核/汇总一致、0.005 半分边界、正负尾差及教师确认、失败不记零分、并发身份隔离。
- 不引入 RLHF/奖励模型、多模态训练、复杂学生能力模型或完整内容快照；不修改现有三个待扩展契约的 v1.0 正文。

### 历史执行与读取边界（T171 实施）

- 上述两种评分依据业务阻断在阅卷、工作流与复核 API 返回 HTTP 409，保留原错误码。查询旧结果、任务与运行状态不要求重新授权评分；复核详情满分读取既有 GradingResult.max_score。
- 只有固定依据经校验且现有评分器能完整消费时允许执行；仅“JSON 非空”不构成核对通过。后台重读、工作流恢复和写入复核同样检查，不能通过已排队或旧暂停记录绕过。
- 实施过程中的旧输入兼容仅允许本场/基准/题库满分相等、发布知识点相同、题序一致且无未传入图片的场景；客观题确定性单要点满分、主观题已确认的原文定性标准。其他完整依据保持可读，等待完整评分输入链接通，不能静默用旧默认值计算。
- 历史盘点与显式证据回填入口见 docs/exam-history-reconciliation.md；未知项保持 SQL NULL，本次核对 UTC 不充当历史批准时间。提交回执不明时报告 unknown，重新审计实际状态，不假定回滚。


### 本场标准准备与确认接口（T174）

教师沿用考试管理权限，路径 qid 为 Question.id；所有写入仅允许 Draft 且无答卷历史。

- `GET /api/exams/{id}/questions/{qid}/scoring-basis`：返回关联身份、原 Rubric、题目修订号、题库满分、显式/有效本场满分、可空基准、当前 basis 及 editable。题库当前满分不冒充已核对基准；未知历史有效满分保留 null。
- `POST .../scoring-basis/prepare`：教师提交 expected_question_validation_revision、expected_effective_score、expected_base_score、expected_basis（当前完整 basis 或初始 null）、additive 和实际核对的 points[{key,label,base_points}]。题型决定 objective/subjective；客观题使用确定性满分要点，主观题数值由明确输入产生，定性标准保持空 points。服务器以 Decimal 计算独立默认值/有符号尾差，并保存基准和新的 preparation_id。相同真实输入重试保留已有轮次与确认；不同结构的准备请求须匹配当前完整已读 basis，迟到请求不得覆盖另一轮准备或同轮次新确认。
- `POST .../scoring-basis/confirm`：提交上述已读上下文、preparation_id、expected_basis、逐 key 的 confirmed_points[{key,points}] 和真实处置理由。服务器校验完整要点集合、金额与加总语义，记录实际教师及 UTC；页面轮次或内容过期明确拒绝，不代替其他教师的确认。

源题相关修订复用现有 validation_revision，并在同事务清除仍可编辑草稿的 base_score/scoring_basis；实际改分或替换同样失效。准备后修改再改回原值会产生新轮次，不能用旧页面提交通过。纯题序变化或分值同值写入保持依据。历史已有答卷或非 Draft 关联不因此被改写。

草稿准备保留 score 的显式/缺省意图，不提前固定发布知识点。发布前统一检查实际条件、当前题目资格与评分依据；通过后同事务固定有效 score 及 published_knowledge_points。旧 Published 缺失依据仍报告缺失，不从当前题库补写。T175 将接续所有直接入口的完整发布冻结，T176/T177 接续评分输入消费。


### 固定评分输入运输与恢复（T176 实施）

- DatabaseGradingSubmissionReader 从实际 ExamQuestion 身份、题序及固定列组装 ScoringInput；未知历史字段保持缺失，执行明确阻断。独立单题目标的兼容分值/标签由同一固定输入派生，不再动态读取题库满分/知识点。
- ScoringInput 的金额为 Decimal，JSON 输出十进制字符串；运行态 options 保留原字典或列表形状与原序；内部检查点 JSON 对字典唯一保存顶层 ordered_options 有序键值对数组（移除 options），列表/null 仍用 options 原形。解码拒绝双字段或重复键并严格还原，避免 JSONB 重排，也不与合法列表或业务选项键混淆。历史 order_preserved=false 如实保留，本轮运输保序不宣称原卷顺序已恢复。Question/ExtractedQuestion 的 JSON 列及公开题目 options 形状保持既有合同。
- Agent 调用信封及 Workflow 当前题槽/整卷输入集合保存同一关联与真实内容、固定标准、资产及当前核对引用；图片二进制只在实际调用期读取，不进入检查点或本机路径字段。实际检索前 course_context/source_references 保持 null，之后可填本轮真实同课程依据。
- 新 Workflow 业务载荷为 version=2；显式读取 version=1，缺失固定事实不补造，拒绝旧版本声称携带新字段及未知版本。恢复重新读取真实答卷，核对完整固定事实和选项顺序，不以 question_id 复用他场结果，也不以源码版本/哈希作业务门禁。
- T176 中间阶段保留既有非等价标准/图片执行阻断；T177 消费本场标准和图像后解除对应限制。历史结果仍按原结果证据读取，不重写旧成绩。


### 完整评分消费边界（T177 实施）

- 在 T176 固定输入之上消费本场已确认要点、分值、发布标签及真实核对图片；T171 的等价旧输入临时门禁不再用于完整新输入。未知/不完整历史、当前图片失败及固定事实不符仍拒绝执行。
- 评分 DTO 的 score/max_score 为 Decimal，JSON 金额为字符串；解析真实 JSON 数字使用 Decimal，原始值先检查有限及本场区间，再 ROUND_HALF_UP 两位后复查。评分及教师改分越界不裁切、失败不补零。题库 QuestionCandidate.score 的既有语义不变。
- 新评分结果由服务指定真实 exam_question_id，保存在既有 GradingResult 可空列；旧 NULL 保留未知，不从当前关联补造。单题/整卷/检查点/复核的结果归属均核对真实关联与本场上限，Confirmed/Modified/Re-grade 保持原生命周期及主键。
- 实际检索成功后保存本次 course_context/source_references，失败保持真实状态；固定事实比较仅允许这一检索增补。题图评分读取授权原件字节和当前核对条件，使用已配置图像能力 Provider，不再次把理解调用当作核对，也不以 OCR/替代图制造成功；字节/凭据/文件系统路径不写检查点。
- 完成消费者实现和受控边界验证不代表云模型质量通过，T168 AI辅助+开发者审查的未达标结论以及 T179完整性能/系统验收分开记录。


### 组卷、答题与复核显示边界（T178 实施）

- 教师组卷页复用既有组卷/关联调整/评分准备确认服务，成功或失败后重新读取实际题序、显式分值、有效满分、保存的意图与条件缺口。失败回执的 intent_saved 三态保持；页面改动清除旧发布检查和评分准备快照。教师完整预览使用本场真实题序与授权原图，不生成公共缓存路径或带凭据文件链接。
- 学生 GET /api/submissions/exams/{id} 与 Gradio 答题共享 StudentExamDetail 安全投影：分值及总分来自固定 ExamQuestion.score，知识点来自 published_knowledge_points，题序来自 order_index；旧未知分值为 null，不能取当前题库满分补造。选项保留 JSON 原形与键序，order_preserved=false 明示历史原序未知。投影不含参考答案、解析、Rubric、评分依据、源页定位或服务器路径。
- 学生资产仍执行 student_visible 及源卷/含答案整页永久私有规则。原图读取核对实际授权文件、字节和尺寸；确认为作答必要的图被隐藏或永久禁止时返回 EXAM_REQUIRED_IMAGE_UNAVAILABLE，文件缺失/变更等保留真实 FILE_* 错误；HTTP 为409。可选教师图不因必要图缺失被自动公开。图片二进制只用于当前授权页面内的 data URI，不进入学生公开JSON。
- 学生开始/保存/交卷保留既有资格、开放时窗、答案格式、状态和幂等规则；必要题图失败明确阻断写入。旧答卷缺本场评分依据仍可按旧规则提交，显示未知，不替代既有新评分入口的 EXAM_SCORING_BASIS_MISSING 门禁。交卷服务成功后前端按对应控件冻结作答、保存、导航、标记并关闭确认区。
- 教师复核按实际 GradingResult.exam_question_id 及最新真实 M4 检查点核对本场身份、固定输入和该结果，匹配后才展示本轮保存的检索正文/来源。未知旧结果保持未知，错场或失配的检查点不显示为当前依据；数据库既有评分值仍原样读取。当前可访问资料正文与本轮实际检索输入分别说明。
- 复核显示同场原图、真实教师/UTC核对条件、原始标准、本场已确认标准/要点、基准满分与本场满分。金额用十进制文本传给既有决策接口，并携带当前复核轮次；人工修改、确认和恢复生命周期保持。

教师Modified已保存而图恢复失败时，保留原模型得分/理由的检查点仍可提供同轮固定输入与检索正文；得分/理由差异不能当作错场。当前得分与教师结论取真实数据库及复核记录，工作流仍保持真实待恢复状态，不伪造恢复成功。


### 教师考情读模型（T180 实施）

- 既有 `GET /api/results/exams/{id}/summary` 增量提供分配资格人数、已参加、草稿、未开始、最终分值频数、逐题及知识点统计和关注明细，保留原字段含义、教师角色及课程归属边界。资格人口按当前启用学生及真实 ExamParticipant；无分配沿用全体开放（含无角色旧账号）。资格人数不表示当前时间窗已开放；参加为真实答卷学生去重，保留历史停用或已撤销分配者的观测，不凭页面访问猜测参与。
- 已提交只含 Submitted/Graded/Reviewed；分布和平均分的分母为当前真实最终答卷，合法零分参加。分布按实际分值频数，没有新增及格阈值。无最终观测为暂无数据/空分布及 null 平均。
- 逐题统计仅使用整卷最终条目，并核对真实 exam_question_id、题序、固定满分及发布知识点一致；旧身份/固定依据未知不从当前题库补造，最终历史总分保持原证据，归因排除数可见。得分率按实际分子/满分分母计算，输出四位小数；无观测的金额/比例为 null。多标签题在每标签完整归属，显示实际题目、去重有效学生及有效答案数，知识点各行不可加总成整场得分/失分。
- 待复核、失败、缺依据与尚未完成按当前真实结果/最新执行错误单列；失败不变零分。`insufficient_evidence_submission_count` 也包含最终历史答卷无法完整归因的数量，可能与 final_count 重叠，不能把这些字段相加冒充互斥总人数。成功最终结果不被旧失败执行记录覆盖。关注名单只描述真实最终失分、缺依据、未提交/未开始或待处理原因，并携带学生/答卷/答案/本场关联身份，缺最终结果时 final_lost_score=null。
- 查询不写库、不调用模型；师生分析页面接线和完整 SC-013 对照分别留至 T182–T184。
