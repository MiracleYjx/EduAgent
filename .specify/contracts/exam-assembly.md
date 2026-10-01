# v2.0 条件组卷契约

## 范围与依据

对应 [spec.md](../spec.md) FR-048、FR-049、[plan.md](../plan.md) §12、§13 和 [data-model.md](../data-model.md) §7.6、§9。
新增条件组卷、显式题序和考试内分值，保留 v1.0 创建/选题接口；这是目标契约，非实现或性能验收结论。

## 输入与选题语义

~~~text
POST /api/exams/{id}/assemble
~~~

沿用考试管理权限及课程归属检查；id 是 Exam.id，只允许修改 Draft 且无答卷/历史保护的考试。

| AssemblyRequest 字段 | 类型 / 约束 |
| :--- | :--- |
| course_id | UUID，必须等于考试所属课程 |
| question_count | 正整数，沿用现有考试题目请求最多 200 题的边界；100 题为性能评测规模，不是新增全局上限 |
| type_distribution | [{question_type, count}]；题型去重、count 为正整数，合计等于 question_count |
| knowledge_coverage | [{knowledge_point, min_questions}]；知识点去重，min_questions 为正整数、不超过 question_count |
| total_score | 正数十进制字符串，最多两位小数；表示要求总分，整卷求和不套用单题 Numeric(8,2) 上限 |
| score_overrides | 可选 [{question_id, score}]；题目去重，同课程且已审核，分值为正数、最多两位小数 |

- 扩展首版题型为 SINGLE_CHOICE、TRUE_FALSE、SHORT_ANSWER；不把已有其他枚举自动纳入完整交付。
- 候选必须同课程、Approved，题干/选项/答案/评分标准完整，语义问题已处置，题图及来源可访问；Draft、待补全、未解决核验问题题不能发布。
- 题型数量精确匹配；知识点覆盖表示每个指定知识点至少出现在 min_questions 道所选题中，同一题可覆盖多个知识点，不能因此重复计算题数或整卷分数。
- 知识点匹配按当前课程定义/规范标签精确判断，不能由 Prompt 宣称已经覆盖；空覆盖列表表示教师没有额外覆盖约束。
- 本场分值优先取选中题的 score_overrides，否则使用 Question.score；不修改题库默认分值。未选中题的合法 override 不改变其他题，也不表示必须选该题。
- 不根据总分自动捏造各题分值、按比例均摊或静默放宽题数/题型/知识点；找不到组合时教师调整约束或显式分值。
- 总分用所选题有效本场分值的 Decimal 精确求和，必须与要求相等；请求中的 total_score 不是允许改写题库分值的授权。

## 输出与约束诊断

成功 200 返回 AssemblyResponse，写入 Draft 考试的选题、题序及显式设置分值；不自动发布或批准题目。

| AssemblyResponse 字段 | 语义 |
| :--- | :--- |
| exam_id、current_status | 本场身份和真实 Draft 状态 |
| exam_questions | ExamQuestionView 列表，按 order_index 排序；含 id、exam_id、question_id、order_index、score、effective_score、assets |
| conditions | [{kind, target, actual, satisfied, reason}]；分别显示数量、每种题型、各知识点、总分是否满足 |
| total_score | 当前组合有效满分 Decimal 求和结果，序列化为两位小数字符串 |
| publication_checks | 本场评分依据、尾差确认、题图/答案等发布前尚需处理事项；组卷满足约束不等于可以直接发布 |

- 草稿 score 可为 null，effective_score 此时只作当前预览投影；发布后必须固定 score，见 [exam-scoring.md](exam-scoring.md)。
- assets 仅返回授权题图引用/说明；图片关联来自所选 Question，不能靠生成新图或替换原图满足约束。
- 简单规则筛选是首版方向，不引入独立求解服务。响应区分“候选不足/明确条件冲突”和“当前选题策略未找到满足组合”，后者不能宣称已证明数学上无解。
- 缺题/冲突/未找到有效组合时返回 409、EXAM_ASSEMBLY_UNSATISFIED，detail 包含 code、中文 message、current_status 和 conditions/gaps；保留原草稿选题，不能把半成组合写入并宣称成功。
- gaps 说明题型可用/缺少数量、知识点覆盖缺口、总分差额及已识别冲突；没有得到组合时 actual 或总分差额可以为 null，不能捏造候选总分。
- 非法请求 Schema 使用既有 422 detail；越权/不存在分别沿用 403/404。

~~~json
{
  "detail": {
    "code": "EXAM_ASSEMBLY_UNSATISFIED",
    "message": "简答题仅有 2 道可用，要求为 3 道，请调整条件或补充已审核题。",
    "current_status": "Draft",
    "gaps": [{"kind": "type", "question_type": "SHORT_ANSWER", "required": 3, "available": 2, "missing": 1}]
  }
}
~~~

## 题序、替换与预览

~~~text
PATCH /api/exams/{id}/questions/{qid}
~~~

- qid 明确为当前关联的 Question.id；关联实体自身的 id 作为 exam_question_id 返回，不能混用两个 ID。
- PatchRequest 可包含 order_index、replacement_question_id、score；至少有一项。省略项保持，score=null 仅允许草稿恢复采用题库默认值。
- replacement_question_id 必须同课程、Approved、补全且题图可用，不得已在同场出现；替换保留当前位置和该位置已有本场 score，除非请求显式改 score/null，响应显示替换后的有效分值。
- 调整题序采用移位语义：将目标移到指定 1..题数位置，中间题向前/后移动；事务后连续 1..题数，无重复，不要求客户端自行制造临时重复序号。
- 替换会新建对应 ExamQuestion 关系身份，旧关系仅在可编辑草稿范围移除；被替换题的旧评分依据不移植到新题。
- 改分、替换或影响 Rubric 的内容变化使本场旧评分依据/尾差确认失效，发布前重新核对；题序单独调整不改变题目评分含义。
- 200 返回当前完整 ExamQuestionView 列表、conditions 和 publication_checks。人工调整后可暂存不满足条件的草稿，但明确显示缺口；不能据此发布。
- 预览按同一显式题序、effective_score 和授权题图读取；教师预览可看答案/依据，学生考试接口不返回标准答案/Rubric/含答案源卷。

本轮新增路由不重定义 v1.0 GET 考试/题目接口。条件指标来自最近一次教师明确组卷要求，后续人工调整按同一要求重新计算；这些要求必须持久保存，不仅存于 UI 会话。存储承载实施前补齐模型，当前六实体表本身不包含完整 AssemblyRequest。

## 发布冻结与修订

发布沿用现有 POST /api/exams/{id}/publish，在 v2.0 增加以下原子检查：

1. 选题仍 Approved、同课程、题序连续且唯一，条件满足，答案/Rubric/题图完整、语义问题已处理。
2. 固定 ExamQuestion.score、base_score、published_knowledge_points、scoring_basis；独立舍入的尾差已由教师确认，不能有未知本场评分依据。
3. 事务切换 Exam 为 Published，此时立即冻结本场选题/题序/分值/评分依据及受引用的 Question 内容/题图/父题来源。
4. 发布与退回修订、内容编辑及资产变更共享涉及 Exam/Question 的事务锁及固定顺序，不能在检查后并发改写。

- 发布后组卷/PATCH/删除关联返回 409、EXAM_PUBLISHED_IMMUTABLE；Closed、Archived 或存在 Submission/Answer/评分/复核历史同样保护。
- Approved 未受发布/历史保护的题，修改内容仍须 Approved -> Needs Revision -> Pending Review -> Approved；直接修改六类内容字段沿用 QUESTION_APPROVED_IMMUTABLE。
- 已发布引用题退回修订/内容/解析/题图/来源的原地改写或删除返回 409、QUESTION_REFERENCED_IMMUTABLE，附中文原因及当前状态。
- 受保护题需要新内容时创建派生候选并保留父题关系，重新核验/批准；原考试继续引用原题。本版不引入完整快照/题目版本。
- difficulty、knowledge_points 的题库维护仍允许；历史分析使用 published_knowledge_points，不能随元数据维护改变历史。
- 冻结持续至无合法保留的发布/历史引用；本版无自动解冻，不能借关闭考试或删除单份答卷绕过。

## 原子性与验证

- assemble 全部满足后才事务替换本场草稿关系；失败回滚，不能只追加一部分题。事务内再校验候选状态及文件，避免用过期搜索结果发布。
- 唯一约束 (exam_id, question_id)、(exam_id, order_index) 必须保持；移位/替换采用可保持事务一致性的更新顺序，不放宽数据库约束。
- 同场修改序列化检查；不同考试的分值/题序/评分依据独立。替换或并发发布后，客户端旧 qid 操作明确拒绝，不猜测意图。
- 验证题型/数量/知识点/总分约束满足率、不可满足时无写入、移位/替换/预览、原题带图、发布即冻结和历史保护；性能另测，不在契约中宣称已达标。
