# Requirements Review Checklist: Local Settings and Onboarding

**Purpose**: 本批规格/架构质量审查，关注安全、自举、恢复和双交付。
**Created**: 2026-10-10
**Feature**: [spec.md](../spec.md)
**Review Ownership**: 复选框归评审者；[x]只表示需求质量获认可，不表示实现完成。按speckit-checklist生成，全部保留未勾选；正文分析另见analysis-report。

## Completeness

- [ ] CHK001 三环境的全部资源和实际目标校验是否有完整定义？[Completeness, Spec FR-001/002]
- [ ] CHK002 来源优先级、覆盖来源和非法覆盖处理是否清晰？[Clarity, Spec FR-003/005]
- [ ] CHK003 普通配置、凭据和快照的责任是否相互一致？[Consistency, Spec FR-004/006/015]
- [ ] CHK004 Windows与Docker凭据编辑差异是否已由用户决定并记录？[Completeness, Spec Clarifications/FR-007]
- [ ] CHK005 所有现有AI任务的绑定或不适用状态是否明确？[Coverage, Spec FR-009/010]
- [ ] CHK006 自定义兼容服务是否避免以供应商标签推定能力？[Clarity, Spec FR-011]

## Security and Failure

- [ ] CHK007 设置权限、业务身份和同OS用户信任边界是否分开定义？[Consistency, Spec FR-023/024/NFR-001]
- [ ] CHK008 密钥在正常、异常及导出路径中的禁止位置是否完整？[Coverage, Spec FR-008]
- [ ] CHK009 Secret新增、替换、删除、失效与重录的恢复条件是否明确？[Coverage, Spec FR-006/031]
- [ ] CHK010 八类模型错误、未知错误、取消及费用是否定义可区分？[Clarity, Spec FR-012/013]
- [ ] CHK011 草稿失败、并发保存和损坏配置是否有可验证的不覆盖条件？[Measurability, Spec FR-004/005/NFR-003]
- [ ] CHK012 原索引未知、同维不同模型及旧运行未知身份的规则是否明确？[Coverage, Spec FR-016/017]

## Startup and Compatibility

- [ ] CHK013 Setup、Business及逐任务AI Ready是否无相互冒充？[Consistency, Spec FR-018/019/020]
- [ ] CHK014 首次向导的中断、返回、重启与稍后AI配置是否都有要求？[Coverage, Spec FR-021]
- [ ] CHK015 业务阻断是否对应真实依赖而非全局AI布尔值？[Clarity, Spec FR-022]
- [ ] CHK016 “免手工.env”是否与系统服务安装范围区分？[Clarity, Spec FR-027/028]
- [ ] CHK017 快照与热更新/重启的边界是否保护进行中的任务？[Consistency, Spec FR-014/015/016]
- [ ] CHK018 旧登录、Gradio及User/Role的保留和未来退役条件是否明确？[Dependencies, Spec FR-024/Scope]

## Acceptance Quality

- [ ] CHK019 TCR和原业务断言保留是否有明确前置责任？[Dependencies, Spec FR-030/033]
- [ ] CHK020 Fake默认与真实调用授权/评测是否定义完整？[Coverage, Spec FR-029]
- [ ] CHK021 PC尺寸、键盘、无CDN和秘密输入生命周期是否可验证？[Measurability, Spec NFR-005/006]
- [ ] CHK022 成功标准是否分开构建、运行与实际交付验收？[Consistency, Spec SC-008]
- [ ] CHK023 Probe次数/超时和资源释放是否有上限及失败路径？[Measurability, Spec NFR-004/007/SC-012]
- [ ] CHK024 独立当前Feature是否未包含未来无登录全迁移、Rust或Prefix Cache？[Scope, Spec Context and Scope]

## Notes

24项均有明确追溯；这是生成的审查问题，不能据0个勾选声称0项需求满足。无需为本轮纯规划运行实现测试或调用implement。

## 独立复审记录（2026-10-10，规划修订轮）

受用户明确委托逐项评价，采用上文24项原问题，不新增问题、不代替人工勾选。原稿判定18项通过、6项需修改；修订后这6项已有下列条款补足。表中“通过”仅建议需求质量通过，不代表源码/验收通过；24个复选框仍保留[ ]，不能据历史16/16自检自动全勾选。

| 项目 | 修订前 | 本轮建议 | 规划证据 | 独立判断依据 |
|---|---|---|---|---|
| CHK001 | 通过 | 通过 | [plan §7](../plan.md)、[data-model §1](../data-model.md)、T194/T198–T203 | DB/角色、Redis、文件/秘密/输出及实际身份和清理所有权均定义。 |
| CHK002 | 通过 | 通过 | [plan §2.2](../plan.md)、[runtime §1](../contracts/runtime-configuration.md) | 唯一优先级、来源、effective=false及非法高优先值拒绝明确。 |
| CHK003 | 通过 | 通过 | [data-model §2/4/6/9](../data-model.md)、[plan §2/6](../plan.md) | 配置库、秘密边界、不可变运行快照及失败责任清楚；未增第二业务状态源。 |
| CHK004 | 通过 | 通过 | [spec Clarifications](../spec.md)、[plan §3](../plan.md) | 用户已选DPAPI GUI保存及Docker外部Secret，Docker写操作405。 |
| CHK005 | 通过 | 通过 | [plan §4绑定表](../plan.md)、[data-model §3](../data-model.md) | 文字任务继承/覆盖、独立Embedding/Vision/OCR、规则Reviewer均明确。 |
| CHK006 | 通过 | 通过 | [settings-api §4](../contracts/settings-api.md)、[runtime §3](../contracts/runtime-configuration.md) | 注册真实能力，文本测试不推定视觉/向量；json_object仍本地校验。 |
| CHK007 | 需修改 | 通过（修订后） | [plan §3](../plan.md)、[settings-api §1/1.1](../contracts/settings-api.md)、T216/T217/T221 | 原稿分开权限但再授权入口不完整；补持续Launcher通道及会话主体，公开HTTP不领码。 |
| CHK008 | 通过 | 通过 | [spec FR-008/NFR-002](../spec.md)、[data-model §4](../data-model.md)、[统一TCR](../../../docs/test-change-record-local-settings.md) | 正常/错误/日志/浏览器/备份与审计不含秘密；索引证据也仅脱敏摘要。 |
| CHK009 | 通过 | 通过 | [plan §3](../plan.md)、[data-model §4/9](../data-model.md) | 新版本、引用IN_USE、失效重录、DPAPI损坏及外部Secret轮换均有失败保留规则。 |
| CHK010 | 通过 | 通过 | [settings-api §4](../contracts/settings-api.md)、[spec FR-012/013](../spec.md) | 八类结果、unknown、cancel、费用确认、取消不保证退款有独立含义。 |
| CHK011 | 通过 | 通过 | [plan §2.3](../plan.md)、[data-model §9](../data-model.md)、T200/T201/T210 | 预期修订、原子指针、候选秘密写失败及损坏配置显式恢复可验证。 |
| CHK012 | 需修改 | 通过（修订后） | [plan §6](../plan.md)、[data-model §6/7](../data-model.md)、[settings-api §6](../contracts/settings-api.md) | 补索引查看/证据/确认/审计责任与事务；旧Workflow冻结基线和unknown约束保留。 |
| CHK013 | 需修改 | 通过（修订后） | [plan §5.1–5.3](../plan.md)、[data-model §8](../data-model.md)、[settings-api §5](../contracts/settings-api.md) | 补Business Prerequisites，装配不再依赖Business Ready；恢复完成与AI接纳事实分开。 |
| CHK014 | 需修改 | 通过（修订后） | [plan §3/5.2](../plan.md)、[settings-api §1.1](../contracts/settings-api.md)、[quickstart §1/2](../quickstart.md) | 明确刷新/过期重新授权和进度读回，未装配修复后重启；跳过AI保留，未保存秘密不恢复。 |
| CHK015 | 需修改 | 通过（修订后） | [plan §2.3/4](../plan.md)、[settings-api §4.1](../contracts/settings-api.md)、T207/T220/T223 | 原稿未分Probe到期与持续使用；现在仅真实任务依赖阻断，无到期自动收费/全局AI门槛。 |
| CHK016 | 通过 | 通过 | [spec FR-027/028与Assumptions](../spec.md)、[plan §5.2](../plan.md) | 免手写配置不等于安装PG/Redis；迁移需预览目标且不负责建账号。 |
| CHK017 | 通过 | 通过 | [plan §2.4/6](../plan.md)、[runtime §4](../contracts/runtime-configuration.md)、T211–T215 | 新运行用新绑定，旧运行按快照，依赖/本地资源重启且忙不强杀；缺旧凭据不替换。 |
| CHK018 | 通过 | 通过 | [runtime §7](../contracts/runtime-configuration.md)、[plan退役边界](../plan.md#future-roadmap) | 本批JWT/RBAC/User/Role/Gradio保持，未来退役单独批准及等价验收。 |
| CHK019 | 需修改 | 通过（修订后） | [quickstart三层取证](../quickstart.md)、T192/T195/T203 | 具体TCR与旧断言原已定义；补B0/B1/B2避免护栏后基线冒充修改前及误归因。 |
| CHK020 | 通过 | 通过 | [spec FR-029/SC-010](../spec.md)、[runtime §6](../contracts/runtime-configuration.md)、T236/T237 | 默认Fake无真实secret，实调须范围/授权，未跑如实not_run。 |
| CHK021 | 通过 | 通过 | [spec NFR-005/006/SC-011](../spec.md)、[plan §8](../plan.md)、T230 | 两种PC尺寸、键盘/非颜色提示、无CDN、秘密输入清空和禁止持久存储可验证。 |
| CHK022 | 通过 | 通过 | [spec SC-008](../spec.md)、[quickstart §7/8](../quickstart.md)、T231–T239 | 源码/测试/构建/真实Windows与Docker运行/交付/发布分层。E1不冒充完整交付。 |
| CHK023 | 通过 | 通过 | [spec NFR-004/007/SC-012](../spec.md)、[settings-api §4](../contracts/settings-api.md) | 每能力至多1请求、1付费在途、默认15/上限30秒及2秒收尾、取消释放有合同。 |
| CHK024 | 通过 | 通过 | [spec Context and Scope](../spec.md)、[plan后续阶段](../plan.md#future-roadmap) | 无Rust/全React/无登录迁移/Prefix Cache/向量重建；早期展示未删除历史安全任务。 |

本轮没有需要以“证据不足”搁置的需求质量项；真实DPAPI/容器/模型及业务运行证据仍未取得，这属于未来实施验证，不能由本文档评审替代。F01活动路径以及F06任务图另见analysis-report，本清单不假定覆盖所有实施流程风险。
