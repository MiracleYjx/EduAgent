# T168 按字段口径修正与 prompt v6 修复（2026-10-07）

本批基准仍为 **AI 辅助 + 开发者审查**，独立教师标注数量为 0。原标注、运行上下文和比较规则未修改；不把开发者辅助基准称为教师质量。

## 用户确认的门槛

- 100%：identity、question_number、order_index、source_page_numbers、question_type、options、reference_answer、score。
- ≥80%：content、analysis、scoring_rubric。
- ≥70%：knowledge_points。
- assets：待 T164 图像提取流程，不进入本批门槛。现有 T164 实现图片理解与人工核对，自动图像定位/裁切尚未实现。source_regions 原基准未知，继续排除。
- 全字段一致只作诊断，不再作门槛。沿用已知分母与未知计数；分母为零不能通过。所有 5 份试卷 × 3 轮的计划槽须保留，失败导入不能移出分母。

机器阈值：[thresholds.json](thresholds.json)。原确认和旧失败不回写。

## 根因与最小修复

content 原 18/48（37.5%）：OCR 文本失去内部数学排版空格，混合页面积上标差异，以及旧“排除图表标题”指令导致跨页真实题面指令遗漏。paper-extraction-v6 只允许不改变数学含义的排版恢复、保留真实跨页指令；source_anchor/option_sources 仍必须是实际原文的唯一字面摘录。未修改 Schema、来源校验、DTO、迁移或持久化。

knowledge_points 原 15/48（31.25%）：未标知识点的完整原文应为明确缺失 []，旧模型返回了未知 null。v6 要求完整且可读原文无显式标签时返回 []；原文不完整、不可读或待核对则保留 null。只提取显式原标签，不从题干推理或复制整卷主题，不回填历史数据。

旧 v5 的重新计数见 [historical-baseline-new-thresholds.json](historical-baseline-new-thresholds.json)。它没有使用新版 prompt，不是重跑结果；旧 content、knowledge_points 及 scoring_rubric 未达新门槛。

## 当前证据与边界

[local-ocr-preflight.json](local-ocr-preflight.json) 是 5 份真实合成输入的本地解码与原锁定 OCR 预检：7 页，其中 3 页执行 CPU OCR、4 页原生文字。没有调用文字模型，不能据此计算自动拆题字段准确率。原锁定模型恢复记录见 [ocr-restoration.json](ocr-restoration.json)，实际依赖见 [environment.json](environment.json)；项目依赖文件未改。

真实 15 槽质量 baseline 尚未执行：自动审批要求人类直接确认向 api.deepseek.com 发送这 5 份仓库自编合成试卷的提取/OCR 文字。目的地确认问题已提交，当前质量调用数为 0；没有通过本地替身、旧响应或比较归一化制造新版准确率。

TCR §47 的旧生命周期测试适配已在 de8551a 完成，原自有客户端关闭与调用方客户端不关闭断言保留；本批最终一次完整回归 **2661 passed / 0 failed / 0 errors / 2 skipped**，原始证据见 [full-regression](full-regression/receipt.json)。M0原用例在全量因Docker前置超时跳过，独立补验 1 passed / 0 failed / 0 errors / 0 skipped（不加总）；原失败/中断分别保存，不以聚焦检查拼成全量成功。

## 已知限制

本批 T168 不阻塞项：后续启动约 15–16 秒、语义条件误报 16.67%、语义 Rubric 覆盖 77.78%、图片条件完整性 72.73%。原 <10 秒启动目标与 T189 未通过状态保持。语义 Rubric 覆盖不等于导入 scoring_rubric ≥80% 的字段门槛。

## 可复现命令

```powershell
python -m benchmark.t168.repair_import_runner --output benchmark/results/v2/t168-fields-20261007/import-v6 --ocr-model-dir <原锁定PP-OCRv5模型目录>
python -m benchmark.t168.field_acceptance --batch benchmark/results/v2/t168-fields-20261007/import-v6 --thresholds benchmark/results/v2/t168-fields-20261007/thresholds.json --output benchmark/results/v2/t168-fields-20261007/baseline-v6
```

第一条调用已有 DeepSeek 文字模型、按 token 计费，须取得上述目的地确认。第二条只读取真实快照，复用原字段比较规则；不能替代第一条生成真实结果。全量回归使用相同业务源码的提交快照和提交语料，保护工作区既有开发者评分样本，不宣称该受保护样本通过旧“默认无标签”断言。


最终机器汇总：[repair-summary.json](repair-summary.json)。技术回归与新字段质量分别判断：真实baseline未执行，本批质量尚未验收，不新增T168达标声明。
