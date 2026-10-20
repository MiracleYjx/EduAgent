# Local Settings and Onboarding — 设计审查与架构分析

保留规划审查及原源码审计用于设计追溯；其中源码定位与实现差距属于审计时点，不代表当前运行状态。当前实现进度见 [tasks.md](tasks.md)，所有执行结果在 Codex 对话汇报。

<a id="document-index"></a>
## 文档入口

- [需求](spec.md)、[设计与决策](plan.md)、[数据模型](data-model.md)、[API契约](contracts/settings-api.md)、[运行合同](contracts/runtime-configuration.md)。
- [安全操作与资源规则](quickstart.md)、[测试变更审计](../../docs/test-change-record-local-settings.md)。
- 整理前原始分析保存在现有 [_archive/documents-before-consolidation-20261010T071957Z.zip](_archive/documents-before-consolidation-20261010T071957Z.zip)，不作为当前验收证据。

## Findings：F01–F08规划复审

下表“证据”保留修订前文本定位，行号仅指本轮读取的旧稿；处理后的条款以节标题/任务ID定位。所有HIGH在文档层已修订，尚未执行实现验证。

| ID | 严重度 | 文档路径/条款 | 证据 | 影响 | 处理意见及本轮处理 |
|---|---|---|---|---|---|
| F01 | HIGH | quickstart §0.1；tasks T192；.specify/feature.json | 旧指针为.specify；common.ps1:178–187在设置环境覆盖后默认Save-FeatureJson；普通helper不是只读。 | 技能可能读错旧Feature或未经许可改指针。 | 增加显式绝对路径+PathsOnly+文档存在+指针前后检查；后续每次重复。保留旧指针，本轮已只读验证。 |
| F02 | HIGH | plan §1/5；plan §10 R05；spec FR-019；data-model §8；settings-api §5 | 原plan:75图以Business Ready驱动装配，而FR-019把装配包含在Business Ready内；原§5未严格划分恢复阶段。 | 实现可形成装配循环或把依赖可连误当完整业务就绪。 | 定义Business Prerequisites→装配→原三钩子一次→Business Ready；未装配修复须重启，暂时依赖恢复不重跑钩子。 |
| F03 | HIGH | plan §2.3/4；data-model §2/5/8；settings-api §4.1；runtime §2 | 原plan:106与data-model ProbeResult只规定10分钟，没有接纳事实和持续使用的区别。 | 实现者可能定期降级AI Ready，要求用户重复收费检测。 | 新增activation_validation；TTL仅接纳新目标，未变active/pending保留；读取、计时、重启不自动Probe，目标变更才要新证据。 |
| F04 | HIGH | plan §3；settings-api §1.1；T216/T217/T221/T222 | 原plan:128仅写刷新/重开由启动器授权；实际windows_launcher.py:441–443发送SERVE后关闭stdin，453起只等worker退出。 | 令牌过期后没有实际用户恢复入口；容易被补成公开领码或浏览器持久化。 | 复用Launcher控制窗口与存续自有worker管道，注册确认再开页；过期/刷新/关页/长期重入路径完整，无新服务。 |
| F05 | HIGH | plan §6；data-model §7；settings-api §2/6；runtime §5；T211/T214/T220/T228/T238 | 原data-model:68只有evidence_reference/adopted_at；settings-api表没有查看/预览/确认入口，T214仅泛指接纳。 | 保护要求存在但操作者无法执行，审计和事务责任不清，可能把当前配置当历史。 | 补受控API/UI、逐字段历史证据、人工确认与服务核验界限；旧unknown与接纳身份并列，同事务保存审计，不改向量。 |
| F06 | HIGH | tasks T218/T219/T220/T221/T222/T223/T224；plan §5/8；roadmap | 原T218依赖T208/T210/T217，T219依赖T214，T222依赖T220/T221；反而T220的配置激活接线未显式依赖T215。 | 静态设置被AI/索引链阻塞，同时业务保护门禁在图中没有完整支配激活入口。 | E1拆为最小根应用/权限/基础诊断/外壳；完整装配、初始化及AI接线仍在T220且新增T215前置。任务不增删编号，完整向导移归T223。 |
| F07 | MEDIUM | tasks T192/T203；统一TCR/quickstart §0.2 基线证据分层；quickstart §0.2 | 原T203位于T196–T202修改之后，却要求区分既有/本批失败，没有明确修改前证据采集或不可归因规则。 | 护栏后的结果可能冒充修改前基线，失败被误归因。 | B0修改前取证、B1护栏后当前基线、B2后续回归分别记录；无可比证据标归因未定，不冒险跑旧清理逻辑。 |
| F08 | MEDIUM | checklists/readiness.md 独立复审；analysis-report | 旧analysis-report称功能歧义/未解决规划阻断均为0；24项仅生成，未逐项独立评价。 | 追溯率和内置16/16自检不足以证明本轮实施准入。 | 补24项逐一证据和前后判断：原稿18通过、6需修改；修订后建议通过但不自动勾选，不推断实现已完成。 |

| ID | 严重度/状态 | 证据、影响与处理 |
|---|---|---|
| S0-01 | HIGH，文档已修订 | 原settings-api §1统一要求Origin可能误拒正常同源GET/HEAD。§1.2现区分安全读取与敏感操作：读取缺Origin仅在有效Setup Token+精确Host下允许，非法Origin总拒绝，写/exchange/Probe/诊断必须合法Origin。spec FR-023、plan §3、T216/T217对齐；实际测试not_run。 |
| S0-02 | MEDIUM，pending | S0只见默认eduagent PG/Redis容器；实际Test库/角色/权限/Redis/目录/卷/端口未验证。不能用Development/Demo替代，准入看quickstart §0.3。 |
| S0-03 | LOW，证据限制 | 旧测试/Windows/Docker回执仅原版本；本轮业务测试not_run。T203建立护栏后B1，无法可比时归因未定。 |
| H-01 | LOW，保留历史差异 | docs/exe-deployment.md:41“最新”仍早期未达标数据，与2026-10-07包结果不同；旧项目文档不在本轮修改范围，以版本/包/日期索引看证据，不推断已正式发布。 |

## Task Dependency Review

规划复审前后均为48个唯一任务T192–T239，当时0勾选、0缺失依赖、0环；S0后状态为4完成/44未完成，当前88条依赖、0环，本文档收敛不改变依赖。已对实际“依赖”字段做拓扑检查，不能仅凭编号递增推断。任务ID和原故事可追溯；7项直接依赖改变：

| 任务 | 修订前依赖 | 修订后依赖 | 职责说明 |
|---|---|---|---|
| T218 | T208、T210、T217 | T201、T217 | 根应用/静态/授权与业务导入隔离；完整装配交T220 |
| T219 | T218、T214 | T218 | 基础前置诊断及受保护API，不等待索引或AI |
| T220 | T219、T210 | T219、T210、T215 | 保留完整API/初始化，新增业务装配/AI/索引接纳接线并受T215支配 |
| T221 | T220、T217 | T219、T217 | 持续Launcher授权和自有worker控制可先完成 |
| T222 | T220、T221 | T219、T221 | 最小静态外壳/基础诊断E1；完整向导交T223 |
| T223 | T222、T219 | T222、T220 | 在E1和完整API都具备后接通并验证向导 |
| T224 | T222 | T222、T220 | 完整设置DTO依赖T220，避免对不存在接口造前端事实 |

E1路径：T203 → T216 → T217 → T218 → T219 → T221 → T222。T222前置祖先由27项减为17项，按任务节点数的最长链由18减为14；没有工时估算，不据此声称节省天数。T214及T204–T215均不再是E1前置，T203隔离基线、设置权限测试和真实业务拒绝仍是前置。未接凭据后端的检查显示credential_backend_unavailable/not_evaluated，不用匿名连接或假凭据绕过。

完整向导仍需T210/T215→T220及T222→T223；模型激活/原Workflow/Embedding保护并未从交付移除。完整交付仍由T231–T239证明，E1不等于完整Bootstrap业务装配或可发布包。

**范围审查**：未发现可直接删除而不损失本批保护的整项任务。T211–T215保护热更新/暂停恢复/旧向量，T238保护新增快照/索引审计随备份；不能削掉。T234/T235保留双交付实际证据。T237仍为显式授权的最小真实验证，无授权记not_run而不拖住独立本地/Fake工作。提前外壳及缩短依赖已解决首个可见结果过晚的问题，无需增加后台服务、重建向量或全业务UI迁移。

## Checklist Review

评审结论与标记以[独立复审表](checklists/readiness.md)为准；实施任务状态不代替评审者批准。

<a id="implementation-gaps"></a>
## 实现差距：合并的只读源码审计

来源为原audit.md的A–E取证；全部原件在上方ZIP，语义不因S0文档完成而变成已实现。Confirmed=指定源码/归档支持，Partial=部分机制，Missing=未见目标能力，Contradiction=声明与事实不同，Unknown=需要运行证据。
backend/app为下列core/deployment/ai/services/ui路径的共同前缀；具体当前保护业务仍是课程/资料、Hybrid RAG、OCR/人工校正、生成/改编、审核组卷、考试分值、阅卷复核、LangGraph检查点、分析推荐、文件/一致备份与交付。生产源码存在不证明当前全量已验收；旧contracts及plan G6持续约束。未接生产Supervisor、隐藏评测看板或规则Reviewer不得推广为已具备模型能力。

| 原编号 | 分类/级别 | 源码事实与处理责任 |
|---|---|---|
| A01 | Confirmed | `backend/app/core/config.py:109–118,150–178,327–345`：Pydantic Settings 读取环境与 .env，数据库、Redis、DeepSeek Key/URL/model、JWT 等必填，进程缓存。普通 Python 与 EXE 加载逻辑不同。 |
| A02 | Contradiction | `backend/app/deployment/windows_launcher.py:38–61,411`：外置配置文件覆盖继承环境并送入子进程。不能直接声称当前全入口都是“环境变量最高优先”。 |
| A03 | Partial | `backend/app/core/config.py:290–324` 有安全 public_dict，SecretStr 及隐藏输入错误；这是输出保护，不是加密持久存储。 |
| A04 | Missing | 无统一 SettingsRevision、字段来源、持久 ProviderProfile/TaskBinding 与受控 Probe API。相关配置散落 core/config、工厂与 launcher。 |
| A05 | Partial | `backend/app/core/database.py:51–83`，`ai/llm/factory.py:130`、`embedding/factory.py:193`、`ingestion/ocr/factory.py:37` 有多处全局缓存；仅清一个缓存不足以支持热变更。 |
| B01 | Confirmed / HIGH | `backend/main.py:10` 模块导入即 create_app；`core/app.py:95–103` 创建 FastAPI 前加载全部配置，模型密钥缺失时 HTTP 设置入口也不存在。 |
| B02 | Confirmed | `core/app.py:158–214` 的 /health 是存活，/ready 检查配置、PG SELECT 1、Redis；`database.py:102–125` 不检查迁移 head、表完整性或存储目录。 |
| B03 | Partial | `core/app.py:74–85` 在 lifespan 执行中断阅卷、诊断和试卷导入收敛；Bootstrap 不可在业务依赖未就绪时执行这些业务副作用。 |
| B04 | Confirmed | `deployment/windows_launcher.py:345–377` 无配置时能打开 Tkinter 配置窗口；不能把新需求描述成“项目完全没有图形配置”。当前窗口不是浏览器向导，也不提供新就绪分层。 |
| B05 | Missing / HIGH | `core/security.py:58–77,98–128` 认证依赖要求 DB/JWT；AuthenticationMiddleware 只提取 token，不自动保护新公开接口。新设置权限须独立实现。 |
| B06 | Confirmed | `ui/gradio_app.py:1824–1887,1930–1951`：DEV_MODE 快捷登录仍创建账号并签发 JWT，不是无登录 StudentProfile。教师/学生 UI 模式不等于认证替代。 |
| B07 | Partial | `main.py:17` 普通入口 0.0.0.0，`windows_launcher.py:310` Windows loopback，`docker-compose.yml:66` 主机 loopback 发布；不同入口安全边界不可混同。 |
| D01 | Contradiction / HIGH | `deployment/configuration_editor.py:234,290` 把表单生成明文 env 并原子落盘；掩码密码框不改变持久风险。 |
| D02 | Contradiction / HIGH | `benchmark/t189/windows_acceptance.py:209–230` 将实际 Key 和含密码数据库 URL 写入 .cache 临时 .env；新验收入口必须调整。未读取实际文件秘密。 |
| D03 | Confirmed | `services/trace_service.py:113–146`、`audit_service.py:20–45` 白名单降低错误内容泄露风险，继续复用。 |
| D04 | Partial / MEDIUM | `benchmark/t160/quality_runner.py:419–465` 会存完整成功响应，可能保存自定义服务回显的秘密；尚无历史泄漏证据，不能表述成已泄漏。 |
| D05 | Missing | 尚无 DPAPI/Credential Manager 或 Docker Secret 凭据引用服务；`pyproject.toml:10` 无 pywin32/keyring/cryptography 直接依赖。 |
| D06 | Unknown | 所有真实密钥的当前有效性、远端端点配置、真实输出是否含密钥均未运行验证；本轮不访问秘密值。 |
| E01 | Confirmed | Windows 预检 PG/pgvector/Redis，执行 Alembic 升级、核对版本后允许 worker 服务；只结束自身进程：`windows_launcher.py:154–197,230–255,290–297,424–448`。 |
| E02 | Missing | 数据库和 Redis 安装不在 EXE 内；`packaging/README.md:5,11,21–26`。免手工 .env 不等于免外部依赖。 |
| E03 | Confirmed / Partial | `packaging/EduAgent.spec:8–50` 收 Gradio/迁移资源、onedir；`scripts/build_exe.ps1:13–35` 构建，可选本地模型依赖未全锁定。React 资源收集尚缺。 |
| E04 | Contradiction | `Dockerfile:20–22` 在 HTTP 前构造 Embedding；`docker-compose.yml:45–53` 强制 Demo 云配置且等待依赖健康。新 Bootstrap 需去除该启动阻断，保留业务 /ready 503。 |
| E05 | Partial | `scripts/run_demo.ps1:11–14,36–67` 可指定 env，但默认根 .env；没有统一独立 Demo profile。DEV_MODE 只是开发登录开关。 |
| E06 | Partial / HIGH | `tests/postgres_helpers.py:26–57` 当前 DB 上随机 schema；`tests/integration/test_backup_restore.py:58–80` 同服务器创建临时库。没有强制专属 Test 配置。 |
| E07 | Confirmed | M0 要求 `eduagent-test` 与指定端口：`tests/integration/test_m0_smoke.py:21–45`；`scripts/smoke_m0.ps1:279–310` 有迁移和 readiness 流程。 |
| E08 | Partial | 阅卷 benchmark 默认 selftest：`scripts/run_grading_benchmark.py:917`；检索 --self-test 默认 false：`run_retrieval_benchmark.py:628–632`。当前空参数因旧 UUID 语料在 :681–688 拒绝；显式 manifest 正常路径 :883–905 可真实调用。不能声称默认空命令必污染 public。 |
| E09 | Partial | `setup_benchmark_corpus.py:22` 默认真实 Embedding；`benchmark_corpus.py:177–188,238–244` 随机 schema 和失败清理。旧检索合成分支 :741 的自有种子清理只在 :816–817 正常路径，需收敛到统一 Test 入口。 |
| E10 | Confirmed | `docs/test-change-record-v2.md:7–23,115–117`：每批测试变更先 TCR，代码/测试变化后 pytest、mypy、ruff及相关真实验收；本轮纯规划不执行数据副作用检查。 |

### Provider与运行一致性证据

| 能力 | 分类 | 实际支持、缺口和证据 |
|---|---|---|
| 默认 LLM | Confirmed / Contradiction | `config.py:121` 接受 openai_compatible；`ai/llm/factory.py:91–98` 只内置 deepseek。通用 Adapter 是新工作。 |
| 结构化输出 | Confirmed | `ai/llm/deepseek.py:157–193` 发 json_object 并本地 JSON/Pydantic 校验，不是服务端严格 json_schema。 |
| AI 出题 | Partial | `ai/agents/question_agent.py:541,699,774` 可注入 settings/provider，已有来源记录，无持久任务级覆盖。 |
| AI 阅卷 | Partial | `services/grading/subjective_grader.py:536,629` 从默认模型创建；可复用注入，不必重写 Workflow。 |
| Reviewer | Contradiction | `ai/agents/reviewer_agent.py:10–15,118–175` 是确定性规则，明确不调用 LLM；模型设置应标不适用。 |
| Embedding | Confirmed / Contradiction | `ai/embedding/factory.py:154–161` 注册 bge/huggingface/local/openai_compatible；`config.py:124` 还接受未注册 deepseek。 |
| Rerank | Partial | `ai/retrieval/reranker.py:459` 的 openai_compatible 是 LLMRerankAdapter 别名，并非独立 /rerank 服务；llm/cross_encoder 可用，none 在 HYBRID_RERANK 不合法。 |
| OCR | Confirmed | `ai/ingestion/ocr/factory.py:10`、`rapidocr.py:20,131`：RapidOCR、PP-OCRv5-mobile、三个本地 ONNX 文件，延迟加载；不是任意 OCR 模型插槽。 |
| Vision | Partial / Unknown | `ai/vision/provider.py:41`、`llm/deepseek.py:71`：独立 vision_model，但同 DeepSeek 端点/Key；白名单与 hostname 声明不等于实际远端可用，本轮未实调。 |
| Probe | Missing | `core/retry_policy.py:101` 只有通用 API/网络/超时/限流分类；没有八类配置诊断合同，工厂构造成功不等于连接成功。 |

### 运行一致性

- **Missing / HIGH**：`ai/workflows/grading_workflow.py:227–247` 的 deps.settings 不进入持久状态；`api/workflow.py:503–524` 恢复用当前配置重建，不能保证跨重启原模型。
- **Missing / HIGH**：`core/app.py:96–101`、`models/document_chunk.py:42–64`、迁移 `0004_document_chunks.py:24` 固定 1024 维；`ai/ingestion/service.py:294–348` 无完整 Embedding 来源持久化。同维度也不能证明模型兼容。
- **Partial / MEDIUM**：`llm/deepseek.py:159,223` 请求可覆盖 model，但 describe 来自默认实例；Rerank `reranker.py:322` 使用覆盖，需记录实际请求模型。
- **Partial / MEDIUM**：BaseLLMProvider 有 aclose，但部分出题/阅卷/Rerank 自建实例缺成对释放（`llm/base.py:76`、`question_agent.py:791`、`subjective_grader.py:629`、`reranker.py:254`）。配置服务引入修订实例时必须明确所有者。


<a id="b0-baseline"></a>
## 基线来源与安全追溯

原始静态基线 [evidence/s0-b0-manifest.json](evidence/s0-b0-manifest.json) 与既有机器产物保留，未改写内容。历史结果仅说明对应源码、测试及环境，不能据此断言当前通过或把新发现直接归为历史问题。旧 Feature 指针按 [quickstart §0.1](quickstart.md) 只读解析。

## Analyze一致性规则

按重复、歧义、缺项、宪章、覆盖及不一致进行只读复核；需求映射集中在 tasks，映射不等于运行覆盖。状态、接纳、API/领域事务/UI责任保持既定任务边界。
