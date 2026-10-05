# T181 本人最终失分与来源推荐：工程验证证据

范围：本批T180之后的当前源码，新增只读服务/DTO/答卷上下文授权API，不实施T182/T183界面。原型案例和账号、教学材料、语义Provider及图像为明示合成夹具；AI操作真实生产服务、真实PostgreSQL/JSONB和授权HTTP请求，云调用0，独立教师0。T146 AI辅助+开发者审查及T168 not_met不变。工程测试不充当完整SC-013、质量、性能或EXE验收。

## 最终验证

- `t181-final.log/xml`：121 passed、1既有Starlette弃用警告、79.83s；新增集成16项+单元3项，并复用旧结果、统计、诊断报告、加载器/页面、参加规则、资产可见性和精确检索范围。T180118项和本批121项有重叠，不相加。
- `t181-mypy-final.log`：backend/app全209源文件通过；`t181-ruff-final.log`、`t181-black-final.log`：4所改/新增源文件及2新测试文件通过。
- `t181-feedback-example.json`：实际最终4.00/5.00逐题响应、真实Chunk与QuestionSourceChunk身份、当前Ready诊断与真实ExamResult/聚合时间、资料正文、练习D/A/B原序以及原图返回收据。两个标签的同一1分失分不能合计成2分；掌握度并未低于旧0.60阈值，weak数组保持空。原图返回确为原件PNG字节，响应private/no-store；无JWT、服务器路径或原卷内容。
- `t181-cleanup.json`：迁移head0023、0残留测试schema、0活动连接后删除明确独占数据库；保留共享PostgreSQL和原数据库。4个用户文件字节与开工基线相同。

## 失败与修正记录

1. `t181-pg-first.log/xml`：13项失败，独占数据库尚未初始化正式启动readiness表，不代表业务成功；`pg-migration.log`保存独占数据库真实升级到head的收据，原库未迁移。
2. `t181-pg-second.log/xml`：10通过、3失败；2个新增报告夹具缺合法真实结果ID，修正显式结果身份；PNG响应实际被标为JPEG，根因为PIL复制图像丢失format信息，现依据已验证的原件PNG头判媒体类型，保持原件字节。
3. `t181-pg-third.log/xml`：全部19项通过；后续仅移除未使用错误类型/不可达分支，再运行最终121项兼容回归。
4. `t181-mypy-first.log`保留Optional类型问题，显式收窄和未知图序过滤后`second`4源文件通过，最终全部209文件通过。旧测试与业务断言未放宽。

## 可复现范围

使用项目Python依赖与带pgvector、迁移至head的独占PostgreSQL数据库；DATABASE_URL仅在子进程环境指向独占库，不改.env。运行：

```powershell
python -m pytest tests/integration/test_learning_feedback.py tests/unit/services/test_learning_observations.py tests/contract/test_results_analysis_api.py tests/contract/test_results_api_contract.py tests/unit/services/test_diagnosis_service.py tests/unit/grading/test_diagnosis_report_store.py tests/unit/ui/test_results_loaders.py tests/unit/ui/test_results_view.py tests/unit/services/test_student_fixed_exam.py tests/contract/test_question_asset_visibility.py tests/unit/retrieval/test_retrieval_scope.py -q
python -m mypy backend/app --follow-imports=silent
```

每个PG用例复用现有isolated_postgres_engine创建/清理独占schema。JSON样例由同一实际集成夹具通过TestClient产生，合成题的审批经正式服务及受控语义报告，不能作为真实教师质量样本。SHA256清单仅用于证据文件追溯，不作业务运行门禁。

原始pytest日志/XML中的行尾空格按实际输出保留；源码/测试/说明文档分别进行diff检查，未为消除证据空白而改写原失败。
