# T178 组卷、答题与复核统一显示证据

实际执行：2026-10-04 UTC / 2026-10-05 北京时间；复用前次暂停的独占验证资源。最终全量回归通过，数值与跳过原因如下；原始回执保留。

## 实现范围

- 教师条件组卷、实际条件缺口、指定分值、移位/替换、完整授权题图预览，沿用评分准备/真实确认及发布检查。
- API与学生页面共用安全DTO，题序、金额和发布标签取ExamQuestion；未知历史保持null，旧参加资格/开放时间/提交格式及状态保留。必要图缺失/隐藏/变更明确失败，源卷/含答案整页仍永久私有。
- 复核使用真实固定输入、当前原图核对和已保存M4检索正文；错场/未知来源不补造。Modified已保存但恢复失败时，显示真实教师分数并保留同轮模型输入，不把可变得分/理由当固定身份。
- 交卷绑定补齐上一题/标记/下一题，解决11项返回与8项输出错位；真实服务和Gradio postprocess验证冻结控件与Submitted状态。

## 文件清单

业务：backend/app/ui/exam_view.py、student_exam_view.py、review_view.py、review_loaders.py；backend/app/api/submissions.py、reviews.py；backend/app/services/submission_service.py。

新测试：tests/unit/services/test_student_fixed_exam.py；tests/unit/ui/test_exam_assembly_view.py、test_review_exam_inputs.py；tests/integration/test_review_fixed_input_display.py。

必要旧合同/producer适配：tests/contract/test_grading_api_contract.py、test_question_asset_visibility.py；tests/integration/test_exam_scoring_execution_gate.py、test_teacher_results_ui.py；tests/unit/agents/test_agent_state.py、grading/test_diagnosis_report_store.py、models/test_m3_migrations.py、ui/test_review_view.py。修改前TCR见docs/test-change-record-v2.md §33；保留旧行为断言，不改公共未知历史seed。

合同/记录：.specify/contracts/exam-scoring.md、.specify/tasks.md（只T178勾选）、docs/validation-report.md、docs/test-change-record-v2.md及本目录。

## 真实浏览器记录

- 教师首次按判断/简答各1、总分20组卷；替换并移位、显式3.33后，显示要求20/实际13.33缺口。随后按13.33及指定分值重新组卷，读取真实题序：带图简答10.00、判断3.33。
- 教师准备并确认本场定性/客观标准，记录真实合成Teacher身份、UTC与原因，实际发布。学生看到同序同值与真实320×200原图，未显示答案/Rubric/源卷。
- 原交卷已真实落库但前端输出错位报错，保留原服务器日志。修复版本在新单题合成考试交卷成功，实际Submitted且控件冻结，截图为t178_browser_submitted.png。
- 受控外部Provider执行真实M4/仓储，2.005→2.01、30%置信度进入Paused；复核显示相同原图、真实核对条件、原始/本场标准、已保存模型组合输入与当前授权资料。组合正文由真实片段前缀及实际Chunk正文组成；原辅助脚本把组合正文与裸正文直接相等的失败保留，最终回读验证真实内容与身份。
- 截图：t178_browser_gap.png、t178_browser_teacher_preview.png、t178_browser_published.png、t178_browser_student.png、t178_browser_review.png、t178_browser_submitted.png；步骤见t178_browser_operations.json，实际数据见t178_browser_readback.json。

AI操作明示的合成账号和输入；受控Provider不代表模型质量。零新增云模型请求。保留T146“AI辅助+开发者审查”、独立教师0及T168质量not_met；不宣称T179性能、Docker M0或完整E4系统验收通过。

## 验证口径与真实失败

各聚焦计数有重叠，不相加作为独立质量样本。保留暂停前RED/GREEN、真实全量首轮2536通过/13失败/5错误/2跳过、辅助脚本字段/路径错误、浏览器定位/权限/键盘输入问题及所有后续RED。错误命令未运行用例不计通过。

中间全量t178_full_final为2555 passed/2 skipped；随后旧答卷兼容修复与复核部分成功显示修复需最终源码验收。t178_full_complete被最终复核修复取代，主动停止其独占pytest子进程，4294967295及原日志保留，不计完整通过。

最终源码快照及原始结果以t178_submission_release.json / t178_full_release.json、log、xml为准。最终只有共享pytest夹具F811注释与换行调整，AST与快照完全一致（t178_comment_only_adjustment.json）。原库只读、用户文件保护、清理与Git核验在同目录相应收据中。

任何运行都只作用已命名独占DB/Redis/持久根；用户README.md、README.zh-CN.md、benchmark/corpus/grading_samples.json及.env不纳入业务提交。源码快照的三项用户修改只在临时index取HEAD，不改工作区；正常Git过滤、全文件SHA及真实index未变见源码清单。仅保存哈希，不保存凭据或浏览器控制端点。

## 最终结果

2557 passed / 2 skipped / 0 failed / 0 errors；pytest 991.147s，外部总耗时 1001.464s。Mypy209源文件、Ruff、隔离Alembic check通过。两个跳过项：M0未提供独占Compose项目/PG/Redis/backend端口、Windows未允许创建该测试符号链接。原始回执t178_full_release.json/log/xml为准。453个运行/测试文件比对详见t178_source_verification.json；最终源码仅测试noqa注释/换行与受测快照不同，AST一致。

代表浏览器使用安装的Edge、Gradio/FastAPI及真实独占PostgreSQL；正式M4链使用受控外部Provider。复现时先配置新建独占DATABASE_URL、REDIS_URL、STORAGE_ROOT，再用既有pytest框架运行全量或本项4个新测试文件；不要把默认业务数据库当测试目标。Python3.13.13，数据库迁移head0023；本项没有新增迁移或依赖。缓存辅助脚本执行中的字段/断言错误原样留证，最终验收以仓库测试与最终回执为准。

资源收口见t178_process_cleanup.json及t178_cleanup.json：准确核对所有权后停止本批进程、零数据库连接后移除独占DB/Redis；原库与4份用户文件保持。T179及性能协议尚未执行，T146AI辅助+开发者审查/独立教师0与T168not_met不变。
