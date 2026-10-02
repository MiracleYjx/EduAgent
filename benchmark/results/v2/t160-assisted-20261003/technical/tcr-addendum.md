# T160 最小技术验收测试变更申请（待根代理登记）

## 必要性
既有真 PostgreSQL 测试仅证明两个 commit 并发幂等，以及完成后旧 Session 的迟到编辑被拒绝；没有证明 PATCH 持有真实业务锁期间 commit 会等待并读取已提交的新值。既有 OCR 禁用测试不证明调用失败与缺配置被区分。T160 明确要求并发校正/确认与 OCR 失败证据，需补足这两类行为，保留所有既有断言。

## 文件与范围
新增 tests/integration/test_t160_import_acceptance.py；复用现有 test_paper_import_flow.context/receive/detail 及真实 PDF 样本和本地 PostgreSQL fixture，使用 autoflush=False 以匹配生产 SessionFactory。不改业务代码、旧测试、模型、依赖或契约，不调用云模型、不生成教师标签，不将 fault injection 或结构化 Provider stub 当作真实内容质量。

## 计划行为用例
1. PATCH 已持有同一导入的业务锁、在事务提交边界暂缓；另一个真实数据库 Session 发起 commit。通过 PostgreSQL pg_blocking_pids 观察确实等待该 Session，再释放提交；正式题须消费 PATCH 新题干/分值，原来源一致、只有一份正式题。
2. commit 先完成后，先前已载入旧暂存身份的 Session 执行迟到 PATCH，须 PAPER_STATE_CONFLICT；不改变 Corrected/Ready、正式题及原校正内容。它是与第一例配套的反向顺序，不声称两个请求同时完成。
3. 扫描 PDF 渲染并保存原卷/页图后，已声明就绪的本地 OCR 测试 Provider 在真实调用边界抛 OCRProviderError(OCR_CALL_FAILED)。结果须 Failed/PAPER_OCR_FAILED，保留明确阶段和原错误码，原卷/已保存 PNG 字节可读，无暂存题/无伪造 OCR 文字。另保留原有 OCR_PROVIDER_NOT_READY 回归，区分配置缺失与调用故障。

## 执行与停止
先登记到 docs/test-change-record-v2.md 再创建测试；在根准备的 T160 隔离 PostgreSQL/持久目录运行新增用例，保存 pytest 原始结果。发现业务缺口只报告事实，不自行改业务实现。等待根授权后才继续测试编辑。