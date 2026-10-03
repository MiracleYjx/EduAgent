# 历史考试盘点与显式核对（T171）

本工具处理已升级到 `0022_exam_question` 的历史考试关联。数据库结构升级应先在隔离副本验证；工具不运行 Alembic、不切换配置、不改原答卷/成绩/复核、不自动重评，也不把当前题库分值、Rubric、知识点或时间写成历史事实。

## 只读盘点

使用现有启用管理员的 JWT，通过 `EDUAGENT_MAINTENANCE_TOKEN` 环境变量传入，令牌不放命令行或报告：

```powershell
python scripts/audit_exam_history.py --report D:\private-review\exam-history-audit.json
python scripts/audit_exam_history.py --exam-id <真实考试UUID> --report D:\private-review\exam-history-one.json
```

默认只读查询当前连接/schema，不枚举或混并其他 schema；T144 中每个历史 schema 应在各自隔离副本明确配置并分别盘点。清单保留关联身份、已迁移的显式题序、评分依据缺项及真实 `GradingResult` 身份/运行时满分/知识点，避免导出学生答案正文。`history_unknown` 表示仍有缺项，进程退出 1；没有评分行不补 0。`fixed_facts_present` 只表示字段已存在，不等于质量或评分链通过。

T170 的默认历史题序来自原显示规则 `Question.created_at, Question.id`；这不是原教师手动排题证据。当时分值、基准、知识点、评分标准有一项未知，继续保持 NULL。评分结果中的 `max_score` 与知识点是运行时证据，不能独自证明完整发布依据。

## 准备真实证据清单

`--apply` 只接受完整的逐场清单。操作者必须同时是启用管理员、教师及该课程创建者，不能替另一课程教师制造确认。人工核对原始文件/备份/可靠记录后，显式填写全部原关联；不增删题目、不采用当前题库值作为替代。以下只是格式示意，不能直接作为真实历史证据：

```json
{
  "format_version": 1,
  "evidence_files": [{"id": "original-record", "path": "records/original.json", "sha256": "原文件的64位小写SHA256"}],
  "exams": [{
    "exam_id": "真实考试UUID",
    "reason": "说明核对了哪些当时记录，如何确定字段含义及处理差异",
    "questions": [{
      "question_id": "该场原题UUID",
      "order_index": 1,
      "score": "5.00",
      "base_score": "5.00",
      "published_knowledge_points": ["真实发布知识点"],
      "scoring_basis": {"kind": "objective", "rounding_mode": "ROUND_HALF_UP", "points": [], "additive": false, "rounding_delta": null, "confirmation": null},
      "evidence_refs": {
        "order_index": ["original-record"],
        "score": ["original-record"],
        "base_score": ["original-record"],
        "published_knowledge_points": ["original-record"],
        "scoring_basis": ["original-record"]
      }
    }]
  }]
}
```

每场 `questions` 必须覆盖全部现存关联，题序连续且不重复。金额是最多两位小数的十进制字符串；知识点未知不能填 `[]`，真实确认无标签才可填空数组。所有字段须引用已列出的实际文件；相对路径相对清单目录解析。文件 SHA-256 只证明本次读取字节与待核对材料一致，不能证明材料是原发布时间的真相；历史真实性及字段对应由当前操作者负责，须保存清单、原材料和最终报告。

清单里的 `confirmation` 只能是 `null`，不能代填他人身份或过去时间。服务权限校验后记录实际操作者、当前 UTC 和本次核对说明；这是当前历史证据核对，不是回填旧批准时间。`Question.frozen_at`、考试/答卷/评分/复核的原时间保持原值。主观标准也使用同一 `ScoringBasis`，完整核对基准/本场要点、独立舍入、加总与尾差；自由文本不自动解析为数值要点。

## 显式应用与失败恢复

```powershell
python scripts/audit_exam_history.py --apply D:\private-review\confirmed-history.json --report D:\private-review\history-apply-001.json
```

对清单各考试按固定顺序加锁，核对同场关系和每项完整依据；整份请求在同一数据库事务提交，任何冲突/异常全部回滚。NULL 可以由真实证据填充；已有不同非空事实拒绝覆盖。没有既有完整标准的历史关联允许按更强证据调整题序，关联身份不变；已有标准的题序冲突拒绝。成功重复执行同一内容返回 `unchanged`，保留首次真实确认时间。

报告路径必须新建，不能覆盖旧报告。CLI 先持久写入 `started` 收据；失败记录真实错误，数据库提交前失败不改历史。若数据库已提交而最终报告落盘失败，报告/终端明确 `report_failed_after_commit` 和 `database_committed=true`：先以新报告路径执行只读审计，再决定显式重试，不能宣称事务已回滚。中断时仅有 `started` 收据也不证明提交成功或失败，先审计当前事实。

完成核对不会自动开启旧评分器对新标准的支持。旧结果仍可读取；完整依据、当前可执行能力及本场标准输入均满足后才能受理新评分/重评。换算后标准及图片条件的完整评分贯通属于后续 T176，不能用本工具的成功回执替代该项验收。

提交回执边界：进入数据库 COMMIT 后发生异常，客户端无法仅凭异常或本地 rollback 证明服务端未提交。工具保留原始异常，并输出 `commit_outcome_unknown`、`database_committed=null`、`EXAM_HISTORY_COMMIT_UNKNOWN`；必须先用新的只读审计报告核对现状，再决定是否显式重试。已在 COMMIT 前拒绝的请求仍为 `database_committed=false`；已经取得成功回执、只是最终报告落盘失败仍为 `true`，三种状态不得混淆。
