# T169 生成、改编与批准门禁业务验收

入口 `e3_acceptance.py` 在操作者预先创建、迁移到当前 head 的隔离 PostgreSQL 数据库和 `.cache` 持久根执行。仅新建本批合成角色/课程/教材/题目，既有业务库、`.env` 和原样本不修改；数据库、环境和清理由操作者管理。

```powershell
# DATABASE_URL / STORAGE_ROOT 已指向本批 owned 隔离资源；不把凭据写入参数。
python benchmark/t169/e3_acceptance.py --expected-database eduagent_e3_acceptance_<本批随机值> --output benchmark/results/v2/t169-<本批日期>/e3-business.json
```

命令严格核对 `DATABASE_URL` 数据库名与参数相等且具有 `eduagent_e3_acceptance_` 前缀，拒绝覆盖既有结果。运行失败保存失败收据并传播原异常；同一结果不能重跑覆盖成功，复验应另存新证据并保留旧记录。环境需要项目运行依赖、真实 PostgreSQL/pgvector、当前迁移和本地文件读写权限；调用方保证数据库及持久目录为本批独占资源；复验保留前次失败记录，使用新业务身份和新结果文件。

真实执行边界为 QuestionGenerationService→QuestionAgent→来源/题目/报告持久化→内容与图像核对服务→教师审核服务。固定检索只返回数据库真实保存的 Chunk；Embedding、Question Provider、四项语义 Provider、Vision Provider 是明确的受控替身。本入口不调用云、不测检索或模型质量、不冒充教师标注。合成教师角色执行的命令仅证明身份归属、状态和证据保存。

T168 已完成 42 次真实模型请求，基准为 **AI 辅助 + 开发者审查**，整体质量仍未达到确认目标。T169 受控 Provider 门禁通过不能代替该语义质量测量，不能据此宣称 SC-011 或整个平台质量通过。

原字段读取 `benchmark/corpus/t168-runtime-20261003/semantic_runtime_inputs.json`，题干、答案、Rubric、选项和分值不改写；结果另存实际业务身份、原 case ID、请求/受控调用记录、持久报告与拒批错误。

| 原样本 | 真实入口与边界 |
|---|---|
| SEM-ANSWER / SEM-CONDITION | 真实新文字生成先因原简短Rubric被结构性退修、自动语义调用0；合成角色显式重新提交原字段并启动语义，独立受控报告答案/条件问题，保留初始拒绝 |
| SEM-STALE | 真实原题改编；原题 x=3 答案7，改编 x=4 沿用7；保存父题；原Rubric先被结构性拒绝，再显式重新提交和核验旧答案，保存独立失败报告，不以 A→B→A 失效测试替代 |
| SEM-RUBRIC | 原 Rubric “看情况给分。”保留；正式人工题显式核验，受控报告评分标准问题 |
| SEM-OPTIONS | 原 A:4/B:4/C:5 与答案 A 保留；重复选项被正式服务预检阻断，模型调用0、成功报告0，不强转列表或制造语义报告 |
| SEM-NO-BASIS | 原题干、缺答案/缺Rubric/缺依据保留；新建题型及1分仅为明示合成技术承载元数据；实际输入阻断 |
| SEM-CLEAR-GOOD / SEM-CLEAR-ADAPT-GOOD | 真实新生成/改编、独立报告；通过仍待审核，真实教师角色审核命令后才冻结 |
| SEM-CLEAR-OPTIONS | 原有序选项对象保持；正式人工题显式核验和审核，不改候选生成的列表协议 |

另测明确的新合成变体：无教学引用的生成、人工补证后显式重核验、A→B→A 修订失效，以及 IMG-FIGURE 原图关联与改编。图像题使用 T146 原图字节/条件和原教学依据，业务题干、答案、标准是明示的新合成上下文，不替换原语义样本。资产共享字节但身份独立、不继承父核对；支持与不支持的图像 Provider 结果都持久化，机器结果不自动成为人工确认，当前图像核对后仍须显式语义核验再审核。

迟到调用、四模式范围过滤、父题图无环和事务失败由本批其他真实 PostgreSQL/合同验证证据承接，不由本入口声称完成。

生成候选的 knowledge_points 必填，因此新增显式合成分类 metadata `["T169 synthetic service fixture"]`；不是原稿提取或基准标签，不改变冻结的七个语义输入字段。首轮缺此字段的真实失败证据保留，后续使用新的结果文件复验。
