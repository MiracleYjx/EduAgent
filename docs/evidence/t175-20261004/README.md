# T175 原子发布与全引用生命周期冻结

发布继续同事务固定四项本场依据，所有现有考试write使用Course首锁、Exam/Question最新行与固定顺序。Question/资产/父题/校正审核入口首锁统一，既有I01内容错误及难度/知识点维护保留；真实发布/历史引用用QUESTION_REFERENCED_IMMUTABLE保护。课程级联不能移除Published/Closed/Archived或历史依据；普通知识库Chunk快照和SET NULL原语义保留。无新增删除公网接口、迁移或内容快照。

新增真实冻结、历史异常、迟到Session和HTTP验证通过：reference_transition_final 28 passed，exam_final 16 passed（含直接旧ExamService回归），exam_contract 31 passed（含旧参加资格），exam_green 24 passed（含T174发布回归）。批次有重复用例，不把计数相加当独立样本。直接内容/资产/来源/KB回归初轮123 passed/1 failed，发现重复Course首锁，代码消除重复并保留原断言；candidate_lock_final 45 passed覆盖原失败与生成/审核/改编。最终全backend Mypy208源文件、backend/tests Ruff通过。

RED含预期缺口与测试口径错误，均保留：原6个已Approved内容/解析预期在确认I01优先后修为原错误码；新错误不覆盖旧I01。父题错误测试使用真实error_code；缓存终态用例改为持有真实缓存对象以避免弱引用被回收。root静态发现旧Submission导入接入未完成，局部修复后真实16用例和静态再次通过。没有改旧业务断言、伪造标注或运行云推理。

本批隔离DB/Redis/文件根复用至T178收尾；原数据库和四个用户文件保持。整批最终完整提交快照回归将在T178后执行，T179性能/系统验收未执行。T146 AI辅助+开发者审查、独立教师0和T168质量not_met不变。
