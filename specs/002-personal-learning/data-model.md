# Data Model — Implemented Foundation / Proposed Remaining Detail

本文件落实 [plan.md](plan.md) 已批准的实体职责、图文、身份/修订/来源及历史边界；T009—T013范围的基础物理映射现已按本轮授权实现，并仅在独立Test验证；其余字段、业务服务、恢复和cutover仍为后续设计。使用现有 SQLAlchemy/PostgreSQL、UUID 和受管文件；空间由环境数据根确定，不新增登录账号、多租户或虚拟教师。

## 1. Entities and Relationships

| 逻辑实体 | 主要字段/关联 | 唯一性及边界 |
| --- | --- | --- |
| Subject | id、name、description | 当前个人空间中的科目；不绑定旧 User/Role |
| Chapter | id、subject_id、parent_id、name、display_order | 父节点属于同科目；层级不得循环 |
| KnowledgePoint | id、subject_id、name、confirmation_state | 稳定 ID；名称不是身份；章节关联独立关系 |
| MaterialSeries / MaterialVersion | id、subject_id、用户确认的系列及版本、default_version_id、document_id | 只有用户确认后新版成为默认；不同出版社不自动归系列 |
| Document / SourceLocation | document_id、purpose、真实 file_id、解析状态；页/区域或段落/对象/片段锚点 | 可选年份/出版社/ISBN不成门槛；不虚构DOCX页码 |
| DocumentChunk | document_version_id、location、text_projection、knowledge_point_ids、embedding_identity | 在现有表/检索层改造；用途、版本、维度/模型身份必须一致 |
| PaperImport / ExtractedQuestion | 导入状态、原文件/页、抽取题序、块内容、缺失/疑点、确认状态、question_id | 批量提交幂等；允许多个来源指向同一正式题身份 |
| Question | id、subject_id、current_revision_id、可选 canonical_id | 稳定练习身份；合并不得产生循环或丢失既有作答 |
| QuestionRevision | question_id、revision、type、stem_blocks、options、answer/rubric、explanation_blocks、difficulty、default_score、knowledge_point_ids、grounding、admission_state | 被练习引用后不可原地覆盖；修改形成新修订并重新判断准入 |
| QuestionOrigin | question_id、document/import/extracted_question/location 或 generation_run_id | 一题多真实来源；题目来源与答案来源独立 |
| DuplicateCandidate | 两个候选身份、差异摘要、pending/same/different、用户决定 | 仅保存需要跨页面继续处理的候选；不将相似度作为同题证明 |
| QuestionAsset / ManagedFile | file_id、真实归属、用途槽位、来源位置、MIME/尺寸/完整性事实 | 文件事实唯一，内容块仅引用；不复制一份可独立修改的路径或公开URL |
| PracticeSession | id、subject_id、确认条件、source_mode、selection_mode、grounding、状态、开始/提交时间 | 用户再次练习生成新 session；不能原地重写旧答案 |
| PracticeItem | session_id、position、question_id、question_revision_id、fixed_max_score、scoring_input_refs、knowledge_point_ids | 同一 session 同一规范题身份只出现一次；分值/依据在开始时固定 |
| Answer | practice_item_id、choice_ids 或 text、独立作答状态（answered/unanswered）、提交状态/时间 | 每 item 一份本次答案；提交后冻结，选择和文字输入互斥，无图片答案；未作答与已作答零分不得用 score 推断 |
| GradeAttempt | answer_id、round、scoring_revision_refs、score/max、reason、confidence、validation_state、执行来源、实际使用的 runtime_snapshot_id、错误/usage | answer+round 唯一；失败不是零分；本地未答零分不伪造模型、置信度或usage；不得覆盖历史轮次 |
| ScoreDispute | answer_id、所争议轮次、原因、待解决/待接受/已解决、accepted_attempt_id | 接受必须引用当前有效重评轮次；重评完成不自动解决 |
| Conversation / Message | subject_id、conversation_id、消息角色/正文、引用、question_revision_id、运行/错误、费用确认条件 | 科目内继续/新建/删除；删除对话不级联删除练习或成绩 |
| OperationRun / Step | operation_id、用途、确认范围、配置事实、状态、各步骤发送/返回事实、usage/安全错误 | Proposed 最小操作记录，不是账单平台；结果未知不能自动再发 |
| WorkflowRun / RuntimeSnapshot | 复用检查点、配置/模型/凭据版本/检索身份 | 引用明确依赖；不回落当前配置伪造旧运行 |

MaterialSeries、DuplicateCandidate 和操作步骤的物理表是否单独建立由实际查询/事务需求决定；不因逻辑实体列出就引入通用框架。现有 Question、Document、资产等表可原位改造；Subject/PracticeSession 是明确的新语义。T006已核对现有迁移依赖；物理映射示例见§7。T007批准职责与历史边界，不构成执行DDL/清理授权；实现不得改变这些已批准边界。

## 2. Ordered Content

ContentBlock 的 Proposed 字段：block_id、type、body、source_location、recognition_state；依类型携带受管 asset_id、formula_text/format、table cells 或 chart data/spec。顺序由列表确定，不能以文件名或模型叙述重建顺序。

- 文本题是 text 块，图文题使用同一结构。
- 题干、每个带稳定 option_id 的选项、解析各自有块列表；图片不能靠一个全题末尾附件列表表示所有槽位。
- 参考答案和 rubric 为评分结构；其题图/公式说明使用同一引用原则。
- Formula 保留识别状态和可核对原内容，格式转写失败不能换成推测公式。
- Chart 必须携明确数据与坐标/单位，前端或受控本地渲染从数据生成；不执行模型返回的任意脚本，也不新增复杂插画生成。
- 字数、嵌套、图数、图片体积限制待实际样例评审，不继承旧 order_index=1..5 作为新要求。
- 搜索文本、模型提示和摘要由块投影；修改正文只修改一个事实源。
- 选择作答保存 option_id 集合；不能依赖会随校正变动的屏幕序号或图片文件名。

## 3. Knowledge, Provenance and Grounding

题目关联多个 KnowledgePoint ID，练习开始固定其使用的关联事实。历史显示可保留当时名称，统计身份不由字符串猜测。知识点建议未确认时不得当作可靠归类；无关联记录在界面明确显示，处理规则进入 UI/契约 Review。

Grounding 采用 uploaded_material 或 general_knowledge：

- 前者关联选定资料版本/片段及实际使用证据；检索不足明确失败，不换通用知识。
- 后者诚实标为通用知识，不造 chunk 或教材引用；使用题目条件和合格的答案/评分依据。
- question_origin（上传原题/AI生成）与 answer_origin（原卷/AI补充）独立；用户校正依据也应保留来源事实，不能转成“原卷标准答案”。
- 新默认教材不重写历史 SourceLocation、QuestionRevision 或 PracticeItem。
- 试卷用途不自动进入教学检索，教材用途不自动抽取全部习题。

## 4. Lifecycle and Transactions

### 4.1 导入和准入

导入状态：uploaded → parsing/extracting → pending_confirmation → committed，错误使用 failed/partial 状态和实际错误原因。具体命名 Proposed；完成度与业务准入分别表达。

题目准入：incomplete / needs_validation / eligible / needs_correction。保存成功不代表 eligible；缺答案、条件或关键图像证据仍待处理。用户批量确认校正只确认选定范围，不伪造未完成核验。同次生成/补答案的一致性判断与服务确定性核验分别记录来源，疑点暂缓；不自动追加模型复核。具体校验字段/疑点编码和质量样例标准待评审。

重复确认事务须同时完成来源关联、规范题身份和暂存题链接；重复请求返回同一结果。已有两边历史的同题合并保留两边 Answer/GradeAttempt，查询按规范身份归并，不能删除一侧记录。

### 4.2 练习和评分

练习状态建议 preview → in_progress → submitted → grading → completed/partial_failed。执行状态与每题评分有效性分离；partial_failed 不能阻挡其他有效题进入诊断。

开始时冻结题目修订、题序、分值、rubric/参考答案和来源，后续题库改动不悄悄影响本次作答。提交只冻结本组答案，不在浏览器逐题泄露解析。

已确认：单选/多选未答零分；空白文字主观题在整组提交时明确提示但允许继续，继续后本地确定性记零且不调用 LLM。保存独立作答状态，未作答与已作答有效零分、评分失败区分；旧“全题非空”约束不得阻断。作答状态是业务事实，字段命名和评分执行来源枚举仍是技术草案，不改变 RuntimeSnapshot 对实际模型运行的约束。

练习报告：作答完成率为已填写有效答案题数/本组题数，与整组提交状态及评分成功与否分开；全部评分确定后本组得分率为各题得分率简单平均，包含未答零分。选择题正确率只对已作答且评分有效的选择题计全对比例；无样本不填0%。本组有失败、争议或未确定评分时只展示已确定部分及范围并提示未完成，不伪造全组结果。

### 4.3 重评与争议

用户提出争议即排除该答案的诊断资格。修正题目或参考答案先形成明确新修订，再生成新 GradeAttempt；原练习依据和原分数保留供对照。成功重评进入待接受，用户接受当前有效轮次后才恢复资格；失败或陈旧接受请求不解决争议。

未争议的初次有效成绩不新增逐题接受步骤。重复提交/重复接受按同一 operation/round 处理；明确重新发起的付费重评才创建新轮次，不把网络重发当新授权。

### 4.4 对话与操作

Conversation 删除仅删除其对话归属内容，不删除引用题目、学习记录或共享资产。模型运行后的引用依然可打开原文；资料缺失时显示真实不可用，不搜索别的路径代替。

Operation 的 Proposed 状态：prepared → confirmed → running → succeeded / failed / partial / outcome_unknown。发送前记录必要步骤事实；超时、断连不证明供应商没有处理，保留已尝试状态。用户重试展示会重复哪些步骤及费用不确定性；不会自动扫描未完成任务重发。原子发布完整片段，已成功结果与半成品分开，半成品不得进入 RAG/练习。

## 5. Diagnosis Projection

不新增可独立修改的“掌握度真值”。从已提交且已作答的 Answer、有效 GradeAttempt、无未解决争议及其他准入条件的状态计算：

1. 每题得分率 = score / 本次 fixed_max_score，再按题简单平均。
2. 每个关联知识点记录同一题的表现，标为“关联题表现”，不是独立测量。
3. 按稳定规范题身份识别首次有效作答；重练单列，重评不是新练习。
4. 最近20道不同题首次有效作答，与此前最多20道比较；显示双方样本数。
5. 至少5道且均值低于60%才“建议强化”，不足5道显示证据不足；恰好60%不触发。
6. 来源等权，可按题目/答案来源筛选；问题记录排除并显示数量。
7. 未作答数单列，不进入知识点表现、20题窗口、5题门槛、首次/重练表现，不直接形成错题或弱点；已作答有效零分仍计入。
8. 未作答不消耗首次有效作答身份；只有未作答记录的题在优先未做题时仍视为未做。无有效样本显示证据不足，不把本组含未答零分的得分率作为知识点表现。

首次有效作答的排序建议按提交时间和稳定 ID；争议解决恢复原作答的时间位置，不用重评时间刷入近期。规范题合并与来源筛选的样本选择必须有例子验证，具体 SQL/缓存方案 Proposed。若使用缓存，必须从真实依赖失效，不能保留过期统计伪装成功。

## 6. Persistence and Recovery Boundary

保留现有迁移链，不删除旧版本脚本再期待 fresh install 自动成功。新迁移须在独立 Test Schema 验证：旧头升级、干净初始化、重跑失败恢复、外键/资源归属与索引身份。

旧业务不迁移、不为遗弃物创建副本；实施时按审批清单清空。配置/凭据/部署设置和新在用历史保留。恢复工具需认识新 owner 类型及关联关系，维护授权不再依赖被删除的业务 Admin。升级前后恢复兼容性必须明确：不能声称一次破坏性旧业务清除可通过 downgrade 找回已删除业务数据。

数据根、凭据和 RuntimeSnapshot 的既有安全约束见 [local-lifecycle.md](contracts/local-lifecycle.md)。本文件不执行任何迁移或清理。

## 7. 物理映射评审方案 — Proposed

T006按当前models/FK及文件/恢复消费者核对。下表是已批准架构内的Proposed物理映射，不是用户逐字段批准的DDL；T006时尚未执行DDL；本轮基础实现以§8为准，未迁入任何旧业务行。

| 现有物理对象 | Proposed落点及处理 | 必要约束/影响 |
| --- | --- | --- |
| courses、users/roles/user_roles | 新subjects；旧角色及课程域在G-C后退役，不创建虚拟用户 | 旧created_by/uploaded_by/confirmed_by及其他User FK逐项解除，设置身份独立 |
| chapters.sections JSON、questions.knowledge_points标签 | 改造chapters指向subjects，新增knowledge_points及chapter_knowledge_points、question_revision_knowledge_points关系 | 稳定知识点ID，同科目关系/章节父链无环；不以名称作为身份 |
| knowledge_bases、documents、document_chunks | 保留文件/用途/索引身份能力，course_id改subject_id；新增material_series、material_versions，版本关联真实document | 保留paper_source不入知识库及knowledge_base用途约束；chunk固定版本，默认版本不覆盖旧引用 |
| questions | 保留稳定题身份并指向subject/current_revision；新增question_revisions | 修订存唯一内容块、选项ID、答案/rubric、来源模式及准入；被引用修订不可原地修改 |
| paper_imports/source_pages/extracted_questions | 保留导入、原页、暂存来源；改subject归属；新增question_origins及需要跨请求保留的duplicate_candidates | extracted_questions.question_id当前unique必须改为多来源可关联同题；事务同时处理canonical身份、来源及暂存链接 |
| question_assets、暂存assets JSON | 沿现有file_id及唯一文件登记；新增question_revision_assets关联固定修订所用资产 | 一个媒体可被多个修订引用，不复制路径/字节事实；块顺序/槽位取代旧全题order_index=1..5限制，删除检查实际在用修订 |
| question_source_chunks、question_generation_metadata | 扩展为修订级依据/生成事实；保留内容快照和真实版本位置 | 当前live_chunk SET NULL仍保留content_snapshot；这不等于原文件仍可打开，缺失须明确不可用 |
| exams/exam_participants/exam_questions、submissions/answers | 新practice_sessions、practice_items、practice_answers；复用固定输入与纯评分逻辑，旧表待G-C退役 | session内规范题唯一、item位置唯一；answer对应item唯一并独立answered/unanswered，不沿用旧考试发布/参与者约束 |
| grading_results/review_records、exam_results/diagnosis_reports | 新grade_attempts、score_disputes；诊断从有效作答投影，旧聚合域待G-C退役 | answer+round唯一，接受引用当前有效轮次；本地零分不伪造模型快照/置信度，失败不记零 |
| workflow_runs、agent_runs | 保留Workflow/检查点/执行追踪，替换旧submission/student/user归属消费者 | 新运行绑定practice/operation及真实runtime_snapshot；不恢复旧被销毁任务 |
| 无现有对应表的教师/操作 | conversations、messages、operation_runs；步骤先置于操作记录的有界结构，不先建通用步骤框架 | 科目对话删除不删成绩；操作幂等及发送未知保留，非账单系统 |

T007架构方向已批准；物理表名和完整DDL仍属实现设计，不据此授权迁移或旧业务销毁。建议保留当前可执行迁移链到0024及以后获批修订，分foundation和cutover；不删除历史迁移脚本，不用同名空表或虚拟角色保持旧服务运行。

### 当前FK与隐式引用清单

以下均为代码事实，箭头是实际FK；括号为ondelete。associations.py的Table也计入，未只扫描ORM类。

- User被courses.created_by、chapters.confirmed_by、documents.uploaded_by、exams.created_by、submissions.student_id、exam_results.student_id、diagnosis_reports.student_id、questions.created_by、question_revision_comments.commented_by、question_validation_results.requested_by、paper_imports.uploaded_by、export_files.created_by、agent_runs.user_id引用（RESTRICT）；exam_participants.student_id及user_roles.user_id为CASCADE，user_roles.role_id→roles为CASCADE。
- Course被chapters/paper_imports/export_files引用（RESTRICT），被knowledge_bases/documents/document_chunks/questions/exams引用（CASCADE）。Document→KnowledgeBase和Chunk→KnowledgeBase/Document/Course为CASCADE；Chunk→Chapter为RESTRICT。删除课程的数据库级联不能替代应用的在用历史保护。
- Exam→Course、ExamParticipant→Exam、ExamQuestion→Exam、Submission→Exam、ExamResult→Exam为CASCADE；ExamQuestion→Question为RESTRICT。Answer→Submission为CASCADE、→Question为RESTRICT；GradingResult→Answer/Submission为CASCADE、→ExamQuestion为RESTRICT；ReviewRecord→GradingResult为CASCADE；DiagnosisReport→ExamResult/Submission为CASCADE。Export→Exam/Submission为RESTRICT。
- PaperImport→Document、SourcePage→PaperImport、ExtractedQuestion→PaperImport/Question、QuestionAsset→Question/SourcePage均RESTRICT；QuestionSourcePaper→源题/派生题均RESTRICT，不是同题多来源关系。
- QuestionGenerationMetadata/QuestionRevisionComment/QuestionValidationResult/QuestionSourceChunk→Question为CASCADE；QuestionSourceChunk.live_chunk_id→DocumentChunk为SET NULL。WorkflowRun→Submission为CASCADE，→Answer/ExamResult为SET NULL；AgentRun→WorkflowRun及QuestionValidationResult→AgentRun为SET NULL。
- source_page_ids、暂存assets/source_regions、image_assessment、scoring_input/checkpoint及runtime_snapshot引用还有JSON/文件级关系，不能仅凭SQL FK完整宣布恢复正确。旧GradingResult对answer_id唯一，无法保留新逐轮重评；旧保护函数question_is_protected/course_has_protected_history依赖Exam/Answer，必须改为检查新历史。

原有与新建在用对象分别处理：旧业务待批准后销毁且不留副本；新修订/教材版本/练习/评分/对话及其文件必须保留。此清单只分析，未清理旧行/文件，未执行迁移或新域恢复。

Practice在逻辑模型中具体表示为PracticeSession/PracticeItem；Dispute具体表示为ScoreDispute，职责与用户批准的Practice/Dispute一致，不引入第二套个人身份或评分事实。

## 8. 本批基础映射与后续边界

0025_personal_foundation在原0024链后增加subjects、knowledge_points、material_series/versions、question_revisions/origins及修订关联、practice_sessions/items/answers、grade_attempts、score_disputes。现有questions/documents/chapters增加个人subject归属，旧course/user字段在个人分支可空、旧域仍有条件必填约束；不迁入、不清除旧行。current_revision与同科目关联由组合FK约束，引用后的修订及关联、开始后的题项、已提交答案和评分历史有数据库保护。

0026_learning_operations独立保存operation_runs，步骤为有界JSON，不另建通用任务/账单平台。operation记录当前environment_id、确认摘要、不可变快照引用、逐步骤实际发送尝试及结果；发送前提交计数，崩溃窗口按未知保留，不把未知当未发送。新操作/评分历史的快照引用接入原可信引用生产者；Lease释放不切换当前配置。

只有操作基础API/DTO/客户端已接入，不代表科目、上传、练习或评分业务已实现。后续故事继续补齐领域校验、媒体及文件owner/恢复映射；不能用基础表存在替代这些验收。新迁移的自动downgrade失败关闭，不销毁新版历史或伪造找回旧数据；若需要降级，须明确其数据处置方案。历史迁移脚本未改。
