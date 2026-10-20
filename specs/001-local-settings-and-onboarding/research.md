# Research: Local Settings and Onboarding

本文件保留SpecKit 1.0.3 Plan Phase 0输出/Phase 1固定前置入口；决策正文唯一保存在[plan.md §10](plan.md#technical-decisions)，不重复维护。R01–R09均已确认，无NEEDS CLARIFICATION；确认设计不等于运行验证通过。

| 决策 | 正文（Decision / Rationale / Alternatives及来源） |
|---|---|
| R01 配置责任与持久化 | plan §10 R01、§2 |
| R02 Windows DPAPI | plan §10 R02、§10.1、§3 |
| R03 Docker外部Secret（用户选择） | plan §10 R03、§10.1、§3 |
| R04 最小React设置外壳（用户选择B） | plan §10 R04、§8 |
| R05 Bootstrap/业务生命周期 | plan §10 R05、§5 |
| R06 设置权限及Launcher重新授权 | plan §10 R06、§3；settings-api §1 |
| R07 Registry、Probe与持续使用 | plan §10 R07、§2.3/4 |
| R08 RuntimeSnapshot/Embedding历史保护 | plan §10 R08、§6；data-model/两份契约 |
| R09 三环境隔离与默认Fake | plan §10 R09、§7；quickstart §0.3 |

宪章评估、措辞建议和未验证项见plan §10.2；[实现差距/B0](analysis-report.md#implementation-gaps)与当前准入见analysis-report。未来重新运行Plan须读上述正文，不能把这个索引误认作新研究结论或授权重新生成整套规格。
