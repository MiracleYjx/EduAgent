# T184 两类分析验收（2026-10-05）

基准为 **AI 辅助＋开发者审查**。沿用 T146 用户已批准的学习项目口径；本次委托按推荐决策执行。独立教师覆盖 0，正式 SC-013 的独立教师一致性项仍待增强；不宣称真实学生质量或模型质量。

## 数字结果

- 冻结 CSV 156 项：150 个可比较项一致，0 差异；4 个原未知参加人数保留未知，2 个原始总体人数只作来源事实，不与筛选视图混用。
- STATS-MIXED / STATS-STARTED：A 平均分 2/满分5，B 平均分4/满分10，Final 2、待复核1、失败1、依据不足1；真正0分独立保留。
- STATS-EMPTY：单独建立选择 S3–S5 的运行视图，提交观测3，不宣称原整场提交人数3；平均分、比率、失分无观测保持 null。
- 分箱为对真实 API 精确分值频数的评测投影，产品继续返回原精确频数。比率按公开 DTO 四位小数对照；原精确分数保留在参考中。
- 学生逐题/知识点失分对照冻结明细；答案来自持久 Answer，未补造原未知答案。复核补充场景：真实 ReviewService/原图恢复前无最终均值，修订6→9后最终均值25，保留原记录。

## 运行与来源

命令：`python .cache/t184-t185-20261005/run_pg_tests.py t184-final tests/contract/test_results_api_contract.py tests/contract/test_results_analysis_api.py tests/integration/test_analysis_review_acceptance.py tests/integration/test_learning_feedback.py tests/integration/test_teacher_analysis_ui.py tests/integration/test_student_learning_ui.py tests/unit/ui/test_results_analysis_view.py tests/unit/ui/test_results_learning_view.py tests/unit/services/test_fixed_review_service.py -q`。78 passed，0 skipped。PostgreSQL 用独占临时数据库、每用例隔离 schema；复核补充用例复用原文件型 SQLite 持久原图夹具，明确不冒充 PostgreSQL 并发证明。

运行夹具使用两场共享 Question、真实 ExamQuestion 本场分值/发布标签、5份 Submitted 和真实评分/Workflow 行。新增合成题干、答案、评分解释与主观决策快照仅支持执行；不写回冻结标签、未知项、原教师身份。云调用0；当前缺资料/缺练习会明确提示，不拿旧来源补齐成功。

## UI 操作证据

`test_teacher_analysis_ui`：真实 Gradio process_api 刷新课程/考试，分布与分子/分母展示；选择实际待复核答卷建立原 Workflow/Answer 上下文；刷新清空旧选中。`test_student_learning_ui`：选择本人考试、展开本人答案和授权原图、打开匹配 Ready 片段/当前已审核练习；缺文件、重评、身份变化拒绝旧链接；学生首页进入反馈与退出清空新详情。完整通过记录在 XML/log，实际答卷ID/答案/状态/错误/数字在六份 JSON；这些是实际服务执行的受控答卷，非在用业务数据。

跨课程、未审核/过期核验、未确认标签、缺资料、源卷整页永远私有、非本人/错角色/未登录拒绝与GET只读在现有真实 PostgreSQL 测试中通过。三类完整页面运行截图由 T185 单独保存；不以此替代七类 UI/EXE 交付验收。
