# T177 评分消费验证

- 核心最终t177_core_cleanup_final：257 passed（14新增）；仓储/汇总最终t177_repo_green_final：181 passed（14新增及真实PG迁移）。
- 复核直接169例分次通过：t177_review_direct_final中139不变通过、30合同14通过16因producer缺EQ失败；补真实EQ后t177_workflow_contract_green全部30 passed，原断言保留。新增固定Reviewer/Service25例通过。
- t177_formal_final正式HTTP/PG及真实读图6 passed，验证本场3.33与题库10.00分离、0.005、手改raw越界/2.345、同PK、实际RAG保存、原件字节/能力不足/中途丢图。
- 最终全backend Mypy209、backend/tests/新增0023 Ruff通过；隔离upgrade/check通过。旧0001迁移的4个既有lint问题未扩展修复。

所有本任务前缀原始RED/GREEN、失败和JSON/日志/XML保留；组间重复，不能把计数相加。新测试的M4 envelope/错误类型/ProviderImage访问及缺document_id夹具问题均登记；实际读图错误属性已修，关闭失败保留原错误另有14项核心覆盖。

仅本批独占数据库/Redis/持久根；原业务库0012/原结果0与保护的用户文件字节不变。零云请求；不是独立教师或平台质量通过证明，T168未达标保持。全量合并回归在T178执行；T179完整性能/系统验收待办。
