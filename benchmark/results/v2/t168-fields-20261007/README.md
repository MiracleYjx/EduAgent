# T168 授权逐字段基线重跑（2026-10-07）

**执行完成，验收未达标，T168 当前 [ ]。** 基准为 AI 辅助 + 开发者审查，独立教师0；不是独立教师质量。此前待目的地确认状态已由用户本次明确授权解决；旧阶段机器状态保存在 [pre-authorization-status.json](pre-authorization-status.json)，历史提交/结果保留。

## 范围与真实运行

仅发送 benchmark/corpus/v2-draft-20261001 下5份自编合成输入的提取文字至 api.deepseek.com；无真实用户/生产数据、无秘密写入请求正文或公共证据。15个计划槽全部运行，14待校正、1混合卷失败；15次HTTP均200，不把HTTP成功当业务成功。输入45744、输出9933、总55677 tokens；货币费用未返回。请求deepseek-chat，响应全部deepseek-flash，分别记录。原配置/.env、重试及模型依赖不变，没有额外质量批次。

## 按新口径验收

| 字段 | 自动提取准确率 | 确认目标 | 结论 |
|---|---:|---:|---|
| identity | 42/48（87.50%） | ≥100% | 未通过 |
| question_number | 42/48（87.50%） | ≥100% | 未通过 |
| order_index | 42/48（87.50%） | ≥100% | 未通过 |
| source_page_numbers | 42/48（87.50%） | ≥100% | 未通过 |
| question_type | 39/45（86.67%） | ≥100% | 未通过 |
| options | 42/48（87.50%） | ≥100% | 未通过 |
| reference_answer | 42/48（87.50%） | ≥100% | 未通过 |
| score | 42/48（87.50%） | ≥100% | 未通过 |
| content | 35/48（72.92%） | ≥80% | 未通过 |
| analysis | 39/48（81.25%） | ≥80% | 通过 |
| scoring_rubric | 36/48（75.00%） | ≥80% | 未通过 |
| knowledge_points | 40/48（83.33%） | ≥70% | 通过 |

16道参考题×3轮=48实例；题型3个原有未知排除，其余关键字段48分母保持。100%组全部未通过；80%组仅analysis通过；70%组通过。失败的6题按缺失计错。assets待T164自动图像提取流程，不进入T168阈值（既有T164是图片理解/人工核对，自动定位裁切未实现）；source_regions未知排除，全字段一致仅诊断。

content较v5的37.50%提高到72.92%，仍差7.08个百分点；knowledge_points从31.25%提高到83.33%，已达目标。content剩余7个成功匹配差异为OCR内部空格和续题前缀，加失败6题；知识点剩余两项为顿号标签合并及跨页无标签null，加失败6题。scoring_rubric75%不达80%，不能用语义Rubric覆盖77.78%的已知限制豁免它。

混合卷第1轮的原文摘录被模型改写，既有校验真实拒绝。详见[失败来源诊断](rerun-receipt.json)及[全部差异](baseline-v6/mismatches.json)。没有放宽校验、修改标注/比较或使用人工校正代替自动结果。

## 修复与技术回归

prompt-v6只改content数学排版/真实续页指令，以及完整可读原文无显式标签时knowledge_points=[]；来源定位摘录仍须逐字唯一。业务源码cd20d6d，本次运行源码追溯ddeaf26。原标注SHA与runner参考SHA见[环境](environment.json)，未改金标。

同一业务源码全量[2661 passed / 0 failed / 0 errors / 2 skipped](full-regression/receipt.json)，旧生命周期已通过；M0原用例[独立补验1 passed](m0-supplement/receipt.json)，不加总。活动Ruff、backend mypy214文件及原PDF合同9项通过。离线字段评估器原样输出full_regression_still_required=true，因为它不读取测试结果；[顶层回执](rerun-receipt.json)以本次未变业务源码的已有完整回归解析该前提，不改原评估器或原始输出。

## 已知限制

启动15–16秒、语义条件误报16.67%、语义Rubric覆盖77.78%、图片完整性72.73%按本批口径不阻塞T168；原T189后续<10秒目标及未通过状态保持。T168当前未达标来自本表实际字段，不是这些限制。

## 证据与复现

- [真实15槽证据](import-v6/)，包含无凭据HTTP正文/响应、资源采样、自动与辅助校正快照；辅助结果不用于本次自动字段判定。
- [逐字段汇总](baseline-v6/summary.json)、[字段CSV](baseline-v6/fields.csv)、[身份CSV](baseline-v6/identities.csv)、[机器最终回执](rerun-receipt.json)。
- [独占数据库清理](import-v6/cleanup.json)，[用户文件与原库保护](preservation.json)，[72个私有业务文件保存](business-files-preservation.json)；文件SHA不变，共享服务保留。

```powershell
python -m benchmark.t168.repair_import_runner --output <新的结果目录> --ocr-model-dir <锁定PP-OCRv5模型目录>
python -m benchmark.t168.field_acceptance --batch benchmark/results/v2/t168-fields-20261007/import-v6 --thresholds benchmark/results/v2/t168-fields-20261007/thresholds.json --output <新的离线评估目录>
```

第一条会收费外发，用户本次授权只涵盖已执行的这一批，不能直接追加；第二条离线复算且不得覆盖现有输出。现有只读结果可直接核查，不需要重发。下一步先修复T168当前来源/字段失败，再T189性能优化；T169已完成。
