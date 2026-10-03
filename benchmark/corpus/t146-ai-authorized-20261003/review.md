# T146 参考标注索引

本表为“基准为 AI 辅助 + 开发者审查”的学习项目参考入口；审查口径由开发者会话确认，不记为独立教师真值或逐题人工重标。

| 用例 | 类型 | 标签文件 |
| :--- | :--- | :--- |
| IMP-TEXT | import | [import_annotations.json](import_annotations.json) |
| IMP-SCAN | import | [import_annotations.json](import_annotations.json) |
| IMP-IMAGE | import | [import_annotations.json](import_annotations.json) |
| IMP-MIXED | import | [import_annotations.json](import_annotations.json) |
| IMP-CROSS | import | [import_annotations.json](import_annotations.json) |
| IMP-DAMAGED | import_fault | [scenario_annotations.json](scenario_annotations.json) |
| IMP-OVERLIMIT | import_fault | [scenario_annotations.json](scenario_annotations.json) |
| IMP-OCR-OFF | import_fault | [scenario_annotations.json](scenario_annotations.json) |
| IMP-OCR-FAIL | import_fault | [scenario_annotations.json](scenario_annotations.json) |
| SEM-GOOD | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-ANSWER | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-CONDITION | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-OPTIONS | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-RUBRIC | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-ADAPT-GOOD | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-STALE | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-NO-BASIS | semantic | [semantic_annotations.json](semantic_annotations.json) |
| IMG-FIGURE | image | [image_annotations.json](image_annotations.json) |
| IMG-TABLE | image | [image_annotations.json](image_annotations.json) |
| IMG-DIAGRAM | image | [image_annotations.json](image_annotations.json) |
| IMG-UNCLEAR | image_fault | [image_annotations.json](image_annotations.json) |
| IMG-UNSUPPORTED | image_fault | [image_annotations.json](image_annotations.json) |
| IMG-FAILURE | image_fault | [image_annotations.json](image_annotations.json) |
| IMG-STALE | image_fault | [image_annotations.json](image_annotations.json) |
| ASM-1-SAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-1-UNSAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-25-SAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-25-UNSAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-100-SAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-100-UNSAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-200-SAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-200-UNSAT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-OVERRIDE | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-EDIT | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-LEGACY | assembly | [assembly_annotations.json](assembly_annotations.json) |
| ASM-FAIL-RESTART | assembly | [assembly_annotations.json](assembly_annotations.json) |
| SCORE-POSITIVE | scoring | [assembly_annotations.json](assembly_annotations.json) |
| SCORE-NEGATIVE | scoring | [assembly_annotations.json](assembly_annotations.json) |
| SCORE-HALF | scoring | [assembly_annotations.json](assembly_annotations.json) |
| SCORE-DEFAULT | scoring | [assembly_annotations.json](assembly_annotations.json) |
| STATS-MIXED | statistics | [statistics_annotations.json](statistics_annotations.json) |
| STATS-EMPTY | statistics | [statistics_annotations.json](statistics_annotations.json) |
| FEEDBACK-SOURCES | feedback | [statistics_annotations.json](statistics_annotations.json) |
| UI-1 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-2 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-3 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-4 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-5 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-6 | ui | [scenario_annotations.json](scenario_annotations.json) |
| UI-7 | ui | [scenario_annotations.json](scenario_annotations.json) |
| DELIVERY-NORMAL | delivery | [scenario_annotations.json](scenario_annotations.json) |
| DELIVERY-FAULTS | delivery | [scenario_annotations.json](scenario_annotations.json) |
| RESTORE-COMPLETE | delivery | [scenario_annotations.json](scenario_annotations.json) |
| RESTORE-MISSING | delivery | [scenario_annotations.json](scenario_annotations.json) |
| PERF-IMPORT-1 | import_performance | [import_annotations.json](import_annotations.json) |
| PERF-IMPORT-10 | import_performance | [import_annotations.json](import_annotations.json) |
| PERF-IMPORT-50 | import_performance | [import_annotations.json](import_annotations.json) |
| SEM-CLEAR-GOOD | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-CLEAR-ADAPT-GOOD | semantic | [semantic_annotations.json](semantic_annotations.json) |
| SEM-CLEAR-OPTIONS | semantic | [semantic_annotations.json](semantic_annotations.json) |
| STATS-STARTED | statistics | [statistics_annotations.json](statistics_annotations.json) |

## 语义可评分参考数量

| 类别 | 有问题 | 无问题 | 未知 | 不适用 |
| :--- | ---: | ---: | ---: | ---: |
| answer_correctness | 2 | 6 | 3 | 0 |
| condition_sufficiency | 3 | 8 | 0 | 0 |
| option_ambiguity | 1 | 1 | 0 | 9 |
| rubric_clarity | 2 | 4 | 5 | 0 |

这是输入标签数量，未运行模型，不是TP/FP/TN/FN。
