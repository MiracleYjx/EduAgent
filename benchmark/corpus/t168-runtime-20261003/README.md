# T168 语义质量基线：显式运行上下文

本目录补齐已获开发者确认的 synthetic 运行元数据，使用当前 **AI 辅助 + 开发者审查（学习项目口径）** 参考；不是独立教师标注、真实课程数据库登记或教师批准记录。根参考版本与分项原版本分别记录在 JSON 中，原标注与题干、答案、评分标准保持。

`semantic_runtime_inputs.json` 的 `cases[*].input` 使用生产 `SemanticValidationInput` Schema，`context_provenance` 保存各字段的真实来历，不发送标签、期望结果或标注理由给待评模型。

- 原8个简化案例没有正式题型：本次 benchmark 作者显式声明 SHORT_ANSWER；原题内含 A/B/C 的 SEM-OPTIONS 声明 SINGLE_CHOICE。这是已授权的 synthetic 元数据选择，不宣称原稿已有题型。
- SEM-GOOD、SEM-ANSWER、SEM-CONDITION、SEM-OPTIONS、SEM-ADAPT-GOOD、SEM-STALE 的满分来自原 Rubric 唯一明示的 5.00 或 2.00；逐例保留原文摘录。
- SEM-RUBRIC 的满分 **5.00 是本次 benchmark 作者新声明的 synthetic 运行元数据**，不是原稿摘录。原“看情况给分。”逐字保留；未补写得分规则或改动问题标签。
- SEM-OPTIONS 的 A/B/C 取值和题序从题干明示选项原样结构化；原题干仍完整保留。其余原简化案例的解析未知，保留 null，不生成解析。
- 三个 SEM-CLEAR-* 对照已有题型、满分、选项、解析，原样投影。
- 教学依据读取 `benchmark/corpus/v2-draft-20261001/teaching_basis.md` 全文；所有 UUID 是可追溯的 synthetic fixture 身份，`location.database_record=false`，不伪造数据库登记的真实 Chunk 或课程。
- SEM-NO-BASIS 的答案、Rubric 与教学依据原来缺失，继续保留；`input=null` 明确阻断，每轮记录 input_not_ready，不能调用云模型或计作 TN。

输出仅允许 `.cache/` 或 `benchmark/results/` 下的新建目录，既有 slot 会被拒绝，不能覆盖旧证据。执行前核对根参考接受口径、root/component 版本与本运行上下文所列源快照；模型 Prompt、Schema、DeepSeek 适配器及重试源码摘要仅用于真实追溯，不建立跨组件发布版本相等门禁。

执行3轮＝33计划案例运行，30次计划语义调用＋3次输入阻断；实际网络请求包括既有 Provider 重试，单独记录，不能用计划数充当实测数。生产 Prompt、Schema、Provider 与重试策略保持，不涉及业务持久化、人工处置或批准。

```powershell
# 本地预检，默认不初始化 Provider、不访问云模型
.cache/e2-t157-159-20261002/runtime/Scripts/python.exe benchmark/t168/semantic_runner.py --runtime-inputs benchmark/corpus/t168-runtime-20261003/semantic_runtime_inputs.json --output .cache/t168-20261003/semantic_preflight_runtime_new

# 仅在批准真实模型运行后添加 --execute；结果应使用新批目录，保留旧证据
```

指标按四类分别统计：fail 为阳性、pass 为阴性；needs_review/insufficient_evidence 单列无法确定，不伪造问题结论。原44项中9项不适用、8项未知不计混淆矩阵，每轮27项可评分参考标签；输入阻断、技术失败和未执行保留计划分母与覆盖率，不计 TN。3轮分别展示，重复运行不能冒充更多独立样本。

如需正式独立教师增强：另建参考版本，由真实教师依据原材料逐项确认身份、UTC、标签及理由；保留本版本及输出，用新参考版本重新评分或重新运行并明确差异，不回写本次标签、身份或历史结果。
