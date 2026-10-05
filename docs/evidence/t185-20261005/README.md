# T185 三页 UI 样板证据（2026-10-05）

本批结论：三页源码运行样板及聚焦回归通过。运行由 AI 会话执行，不冒充教师。T146 基准仍为已批准的 AI 辅助＋开发者审查；独立教师覆盖 0、T168 未达目标结论不变。七页验收由 T186 接续，EXE、性能及模型质量不在本批通过范围。

## 真实运行边界

- Windows 原机、完整生产 `create_gradio_app()`，绑定 127.0.0.1；DEV_MODE 明示，实际 AuthService/JWT 和生产加载器；教师、学生两个独立浏览器会话。
- 独占 PostgreSQL `eduagent_e5accept_8379642c3b48`，真实 Alembic head/pgvector、生产模型和服务；原业务库仍为 `0012_audit_logs`。没有外部云调用。
- 统计答卷/已审核练习是明示合成执行夹具，原页取自既有 `paper_text.pdf` 并真实渲染。拆题/语义 Provider 受控，不用这次页面运行判定模型质量。
- 学生练习 PNG 实际加载成功，naturalSize=8×6，是验证授权字节的微型夹具，不是图像理解/复杂图形显示质量样本。未知答案、评分标准、历史教学来源和未生成诊断全部保留。

## 文件

- `operations.json`：真实会话操作、数据库回读、身份及来源、截图尺寸/SHA256、实际 DOM 样式值。
- `paper-detail.jpg` / `paper-saved.jpg`：原页与结构化详情并排；保存后解析可读、编辑器关闭。
- `question-list.jpg` / `question-detail.jpg` / `question-editor.jpg`：题库列表、待补全只读详情及显式编辑。
- `student-detail.jpg` / `student-source.jpg`：本场最终分值、诊断空态、授权资料/练习及原选项 D/A/B。
- `tests.log` / `tests.xml`：67 passed / 0 skipped，42.42 秒；13 项现有框架弃用/未实现参数警告。
- `static-checks.log`：7 个修改 Python 文件 Ruff/Black 通过；全 backend mypy 213 源文件通过。
- `runtime-source.txt`：原始执行夹具/启动器/截图接收器文本，保存确切操作来源，不是新的生产入口。
- `cleanup.json`：独占库零连接/零测试 schema 后删除，仅关闭本批应用及接收器，端口关闭；两 README、阅卷样本、.env 字节保持。

## 复验

按既有 PostgreSQL 集成测试约定准备独占测试库，设置 DATABASE_URL 指向该库、运行 Alembic upgrade head（勿对原库执行本批试验）。使用项目 Python 环境：

```powershell
python -m pytest tests/unit/ui/test_shared_ui_acceptance.py tests/unit/ui/test_paper_import_view.py tests/unit/ui/test_question_bank_view.py tests/unit/ui/test_results_view.py tests/unit/ui/test_gradio_app.py tests/integration/test_student_learning_ui.py tests/integration/test_teacher_analysis_ui.py -q
python -m mypy backend
```

新增测试必要性及覆盖见 `docs/test-change-record-v2.md` §40；保留旧断言及真正的授权/来源/资产/审核/入库校验。测试与浏览器运行独立记录，数量不累加成系统质量指标。
