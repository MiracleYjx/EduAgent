# Reasonix 三区架构在 EduAgent 中的应用评估

评估日期：2026-09-14  
评估依据：`.specify/memory/constitution.md`（仓库中实际存在的宪法文件；用户指定的 `.specify/constitution.md` 不存在）、`.specify/spec.md`、`.specify/plan.md`、`.specify/tasks.md`、`.specify/contracts/agent-workflow.md`、`.specify/contracts/rag-retrieval.md`、`.specify/data-model.md`。  
评估范围：`backend/app/ai/llm/`、`backend/app/ai/retrieval/reranker.py`、`backend/app/ai/agents/`、`backend/app/ai/workflows/`、`backend/app/services/`、`backend/app/core/config.py`、`scripts/`、相关测试。  
评估结论：⚠️ 部分适用

## 一、总体评价

三区架构适合 EduAgent 中需要重复调用 DeepSeek、且系统规则和输出 Schema 稳定的 Agent 与阅卷 Workflow。当前可运行的 LLM 链路只有 LLM Rerank，已经有固定系统提示词和 Provider 抽象，但没有上下文分区、前缀版本、追加日志约束或缓存命中观测。Question Agent、Grading Agent、Reviewer Agent 和 LangGraph 阅卷 Workflow 当前只有包初始化文件，尚未形成可应用该技术的业务调用链。当前没有实测数据，按请求尾部每次变化且没有前缀指标的事实，整体前缀缓存命中率只能保守估计为 `<20%`；后续收益必须用同条件 Benchmark 验证。

## 二、当前 LLM 调用链路分析

### 2.1 DeepSeek API 调用路径

当前唯一明确的 DeepSeek Chat API 入口是：

1. `backend/app/ai/retrieval/reranker.py:277-300` 的 `LLMRerankAdapter._invoke` 组装两条消息，并调用 `BaseLLMProvider.generate_structured`。
2. `backend/app/ai/llm/factory.py` 根据 `AppSettings.llm_provider` 创建 Provider；默认注册的 `deepseek` Provider 来自 `backend/app/ai/llm/deepseek.py`。
3. `backend/app/ai/llm/deepseek.py:53-86` 执行结构化调用，`_request_json` 在 `backend/app/ai/llm/deepseek.py:88-98` 调用 `AsyncOpenAI.chat.completions.create`，使用 JSON response format。
4. `backend/app/ai/llm/deepseek.py:107-133` 解析 JSON 并通过 Pydantic Schema 校验，符合结构化输出原则。

`backend/app/ai/embedding/providers/openai_compatible.py` 也使用 OpenAI-compatible SDK，但它是可配置的 Embedding Provider，不是当前 DeepSeek Chat 提示链路。代码搜索没有发现业务代码绕过 Provider 直接调用 DeepSeek SDK 的其他路径。`backend/app/services/grading/`、`backend/app/ai/agents/` 和 `backend/app/ai/workflows/` 没有可运行的阅卷或出题调用。

### 2.2 Prompt 组装现状

- LLM Rerank 的不可变候选前缀位于 `backend/app/ai/retrieval/reranker.py:135-139` 的 `_RERANK_SYSTEM_PROMPT`。
- 每次请求的用户消息由 `backend/app/ai/retrieval/reranker.py:305-315` 的 `_build_prompt` 生成，包含查询文本、候选 ID 和候选内容，属于随请求变化的尾部。
- `backend/app/ai/llm/deepseek.py:147-164` 会复制消息；当消息中没有 `json` 文本时，在首部插入固定的 `_JSON_INSTRUCTION`。该规则可重复，但目前没有显式的前缀版本或字节稳定性测试。
- 当前没有应用层维护的 LLM 多轮对话历史，也没有对历史进行重排、截断后再次发送的逻辑。UI 中的 `messages/history` 是导航状态，不是 DeepSeek 请求上下文。
- 当前没有显式 scratch 字段或中间思考传输逻辑。Provider 只读取 `response.choices[0].message.content`，不会把隐藏推理字段或本地临时计划再次发送给模型；但未来 Agent 若把计划写入普通消息，现有接口不会阻止它发送。

### 2.3 前缀缓存命中率判断

仓库没有记录请求前缀、Prompt 版本或 DeepSeek 缓存命中状态，因此无法给出实测比例。按目前只有一次性 Rerank 请求、用户消息包含变化的查询和候选片段、且没有统一 ContextManager 的情况，整体请求前缀命中率估计为 `<20%`。固定系统消息可能让部分 Provider 内部前缀得到复用，但这不是已验证的 DeepSeek 请求级命中率，不能作为性能结论。

## 三、应用场景识别

| 场景 | 对应任务 | 适用性 | 预期收益 | 实现难度 | 采用三区后的预计前缀命中率 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| AI 出题（Question Agent） | T067/T075 | ⚠️ 适用，当前未实现 | 高 | 中-高 | 50%-70% |
| 主观题阅卷（Grading Agent） | T052/T069 | ⚠️ 适用，当前未实现 | 高 | 高 | 40%-60% |
| 阅卷复核（Reviewer Agent） | T070 | ⚠️ 适用 | 中 | 中 | 50%-70% |
| LangGraph 阅卷 Workflow | T072 | ⚠️ 适用 | 高 | 高 | 30%-50% |
| LLM Rerank | T044 | ⚠️ 部分适用 | 中 | 低-中 | 20%-40% |
| 合成 Benchmark 生成 | T011 | ❌ 当前不适用 | 低 | 不追加 | 不适用 |
| 多轮对话辅导（未来扩展） | — | ⚠️ 未来适用 | 高 | 高 | 60%-80% |

以上比例是基于稳定 Prompt 版本、同一模型配置和具有相似请求分布的工程估计，不是实测结果。T132 要求补充对照实验后，才可以把估计替换为项目指标。

### 3.1 AI 出题

适用性为 ⚠️，原因是出题系统规则、工具定义、少样本示例和 Pydantic 输出 Schema 可长期固定，而课程、知识点、难度、题型、数量和检索结果属于请求尾部。检索查询草稿、候选改写、自检和结构校验笔记应放在 `backend/app/ai/agents/question_agent.py` 的易失暂存区，不能作为下一次请求的历史消息。该 Agent 尚不存在，预计缓存收益高但需要先完成 T067/T075，再落实 T128。

### 3.2 主观题阅卷

适用性为 ⚠️，收益高但实现难度高。`backend/app/services/grading/` 和 Grading Agent 可将评分规则、证据引用规范、输出 Schema 和置信度规则固化为不可变前缀；题目、参考答案、评分标准、学生答案和检索上下文组成请求尾部；检索调用、模型结果、校验和重试事件按顺序写入仅追加日志。查询构造、候选筛选、评分理由草稿属于易失暂存区。该场景直接对应 FR-031~FR-037、T052/T069 和 T127。

### 3.3 阅卷复核

Reviewer Agent 的复核规则、允许的 `accept/revise/regrade` 决策和输出 Schema 可固化。原始评分、证据、教师修改和复核结果按事件追加，临时判断和下一步建议在 `backend/app/ai/agents/reviewer_agent.py` 中请求前清空。该场景保持现有复核契约和权限边界，落地任务为 T129。

### 3.4 LangGraph 阅卷 Workflow

适用性为 ⚠️。图的节点契约、路由规则、工具定义和结构化 Schema 适合作为不可变前缀；每个节点的检索、评分、复核、暂停、恢复和重试应作为 WorkflowRun 事件追加，而不是重排历史状态；节点内的路由草稿和临时计划属于易失暂存区。由于状态和检索内容会变化，命中率低于单个 Agent，且必须严格保留 `Pending/Retrieving/Grading/Needs Review/Completed/Failed/Paused` 状态机。落地任务为 T130。

### 3.5 LLM Rerank

适用性为 ⚠️，收益中等。`_RERANK_SYSTEM_PROMPT` 已经具备不可变前缀特征，查询和候选列表必须继续放在请求尾部；Rerank 不需要跨请求对话日志，因而不应人为累积历史。通过 `backend/app/ai/retrieval/reranker.py` 增加前缀指纹和稳定性契约测试即可获得低成本收益。`CrossEncoderRerankAdapter` 是本地模型路线，不经过 DeepSeek，不追加三区改造。落地任务为 T131。

### 3.6 合成 Benchmark 生成

当前 `scripts/generate_synthetic_benchmark.py` 使用固定随机种子和规则 DTO 生成数据，不调用 LLM。引入三区架构不会降低该脚本的成本，反而会增加可复现性变量，因此不追加 T011 改造任务。若未来新增 LLM 生成数据，应单独设计数据集版本、Prompt 版本和种子隔离，再评估是否纳入本阶段。

### 3.7 多轮对话辅导

未来辅导场景天然适合三区：系统教学规则和工具为不可变前缀，每轮用户消息和工具结果按顺序追加，思考草稿每轮清空。该功能不在当前 FR-001~FR-040、plan 或现有里程碑范围内，本次只记录为后续架构约束，不追加任务。

## 四、具体落地方案

### 4.1 LLM Provider 层

在 `backend/app/ai/llm/context.py` 新增 `ContextEnvelope` 和 `ContextManager`，负责：

- 以 Prompt 版本、模型和 Schema 标识构造不可变前缀，并输出稳定的消息序列；
- 只允许向 append-only log 尾部追加事件，禁止修改或重排既有事件；
- 在调用 `generate_structured` 前清除 volatile scratch，禁止 scratch 进入 `LLMMessages`；
- 输出脱敏前缀指纹和上下文元数据供观测使用。

`backend/app/ai/llm/base.py` 的 `generate_structured(messages, schema, model=None)` 保持不变，以消息编译器复用现有 Provider，避免破坏 DeepSeek、测试 Stub 和未来 Provider 的实现。`backend/app/ai/llm/deepseek.py` 继续是唯一 SDK 适配边界，并只负责请求、JSON 解析和 Pydantic 校验。

### 4.2 Agent 层

在 `backend/app/ai/agents/question_agent.py`、`grading_agent.py`、`reviewer_agent.py` 中，将系统规则、工具定义、少样本示例和 Schema 版本化并作为不可变前缀；业务输入、检索结果和上一次工具结果按固定顺序追加；计划、查询草稿、自检和重试说明放入易失暂存区。Agent 不改变 `agent-workflow.md` 的结构化返回和角色职责，也不改变 `backend/app/core` 的认证/RBAC 逻辑。

### 4.3 LangGraph 层

在 `backend/app/ai/workflows/` 的阅卷图中，让节点使用统一 ContextManager 编译请求。WorkflowRun 和检查点只追加事件，恢复时从事件源按原顺序重建业务状态；节点结束后清空 scratch，再进入下一条边。状态机仍以 `plan.md §5` 和 `data-model.md` 为准，低置信度仍进入人工复核，不以缓存命中替代业务校验。

### 4.4 Rerank 与观测层

`backend/app/ai/retrieval/reranker.py` 保留固定 `_RERANK_SYSTEM_PROMPT`，增加前缀版本和指纹记录；候选内容仍由 `_build_prompt` 形成请求尾部。`backend/app/models/` 中现有 AgentRun/Benchmark 记录边界或对应服务应记录模型、Prompt 版本、前缀指纹、命中状态和延迟，但不得记录完整 Prompt 或密钥。`scripts/` 的对照实验应固定数据集、模型、Prompt 和检索模式，并将结果写入 `docs/`。

## 五、收益与成本评估

预期收益集中在 M3/M4 的重复 Agent 调用：固定评分/出题规则可以跨请求复用，追加日志可避免历史重排造成的前缀失效，scratch 隔离可以减少无效 Token 和上下文污染。LLM Rerank 只复用短系统提示，收益有限；Cross Encoder 和当前确定性 Benchmark 不受益。

主要成本是引入上下文编译和事件边界、为 LangGraph 恢复语义增加测试、在 AgentRun/Benchmark 中增加指标，以及对 Prompt 版本变更进行发布管理。若把可变检索结果误放入不可变前缀、对历史做截断重排，或把 scratch 写回日志，缓存命中和阅卷可追溯性都会下降。当前没有真实命中率数据，因此不能承诺费用下降；T132 的同条件实验是收益验收门槛。

## 六、追加的任务清单

本次在 `.specify/tasks.md` 末尾追加 `Phase 9: Prefix-Cache Optimization`，使用未占用的 T125-T132：

| 任务 | 里程碑 | 目标文件 | 依赖/性质 |
| :--- | :--- | :--- | :--- |
| T125 | M3 基础 | `backend/app/ai/llm/context.py`、`tests/unit/ai/test_context_manager.py` | ContextManager、三区边界、接口兼容 |
| T126 | M3 基础 | `backend/app/core/config.py`、`.env.example`、`docs/development.md` | 功能开关，默认关闭 |
| T127 | M3 | `backend/app/services/grading/`、`backend/app/ai/agents/grading_agent.py`、`tests/contract/test_prefix_cache_grading.py` | 主观题阅卷上下文隔离 |
| T128 | M4 | `backend/app/ai/agents/question_agent.py`、`tests/contract/test_prefix_cache_question.py` | Question Agent 稳定前缀 |
| T129 | M4 | `backend/app/ai/agents/reviewer_agent.py`、`tests/contract/test_prefix_cache_reviewer.py` | Reviewer Agent 追加日志与 scratch 隔离 |
| T130 | M4 | `backend/app/ai/workflows/`、`tests/integration/test_prefix_cache_workflow.py` | LangGraph 状态与检查点稳定性 |
| T131 | M3/M4 | `backend/app/ai/retrieval/reranker.py`、`tests/contract/test_prefix_cache_rerank.py` | LLM Rerank 低成本复用 |
| T132 | M5 | `backend/app/ai/llm/`、`backend/app/models/`、`scripts/run_prefix_cache_benchmark.py`、`docs/evaluation.md`、`docs/validation-report.md` | 脱敏指标和可复现实验 |

所有新增任务均为未实现的后续工作；没有修改既有任务勾选状态，也没有改变后端 API 角色校验或 Agent 契约。

## 七、分阶段实施建议

1. **M3**：先完成 `backend/app/ai/llm/context.py`、`backend/app/core/config.py` 和 `.env.example` 对应的 T125/T126，随后在 `backend/app/services/grading/` 与 `backend/app/ai/agents/grading_agent.py` 完成 T127；`backend/app/ai/retrieval/reranker.py` 的 T131 可并行。先用开关关闭验证消息等价性，再开启实验。
2. **M4**：在 `backend/app/ai/agents/question_agent.py`、`reviewer_agent.py` 和 `backend/app/ai/workflows/` 具备可运行实现后，依次完成 T128-T130。每个节点先验证结构化输出和状态机，再验证前缀字节稳定性。
3. **M5**：完成 `scripts/run_prefix_cache_benchmark.py`、`docs/evaluation.md` 和 `docs/validation-report.md` 对应的 T132；只有在固定模型、数据集和 Prompt 版本下得到可复现实验结果，才调整默认开关或宣称成本收益。

## 八、结论与下一步

Reasonix 三区架构对 EduAgent 是部分适用的 Provider/Agent/Workflow 优化，不是当前所有模块都需要的基础设施。建议优先在 M3 主观题阅卷和 Provider 层建立兼容的 ContextManager，再在 M4 扩展到 Question、Reviewer 和 LangGraph；LLM Rerank 作为低成本补充，确定性 Benchmark 和未纳入规格的多轮辅导暂不改造。下一步应按 `.specify/tasks.md` 中 T125-T132 的依赖实施，并用 T132 的实验确认实际缓存命中率后再决定生产启用策略。
