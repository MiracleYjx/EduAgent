# EduAgent 发布检查清单

**检查日期**：2026-09-29（Asia/Shanghai）

**检查版本**：`4248288581cc28c469e75a180e1b6d4d1aeb5ac3` + 本次两项迁移测试的 schema 限定修复（提交前验证）；首次失败基线为 `6ff605198c4d622b2aee2b1c92bda92f996f0637`。

**检查范围**：M0-M5；T091.3 全量测试、静态检查与数据库结构检查

当前进度：T091.1、T091.2 已完成；T091.3 经授权限定测试查询 schema 后，全量 0 失败且静态门禁通过。**T091 按已确认范围完成并勾选，满足创建 `v1.0.0-m5-complete` 的门禁条件。** 首次失败记录保留如下，跳过项、警告和已知限制不隐去。

本次已确认的验收范围：Docker Demo 仅验证容器启动与 `/ready` 可达，AI 链路采用 [T089 开发模式实测](validation-report.md)；Workflow 冷启动约 19 秒记录为已知限制，本批不优化。

## 1. 功能验收

下表区分既有验收记录与本轮质量门禁；不将计划存在、任务勾选或自动化测试通过等同于新增的真实环境验收。

| 里程碑 | 状态 | 备注 |
| :--- | :--- | :--- |
| M0 工程骨架 | ✅ 既有验收 | T001-T013 已勾选；本轮独立 M0 容器冒烟跳过，原因见第 3 节 |
| M1 基础业务 | ✅ 既有验收 | T014-T030 已勾选；本轮执行既有回归 |
| M2 RAG | ✅ 既有验收 | T031-T045 已勾选；四种检索实测见 T089，keyword_only 召回为 0 的结果不改写 |
| M3 AI 阅卷 | ✅ 既有验收 | T046-T064 已勾选；真实模型与合成答卷闭环见 T089，不代表教师标注质量达标 |
| M4 Agent + Workflow | ✅ 既有验收 | T065-T079 已勾选；暂停、复核、恢复和诊断见 T089 |
| M5 工程增强 | ✅ 按确认范围收口 | T080-T091 已勾选；全量 0 失败；Docker 与性能等限制继续保留 |
| Phase 6-9 | ⚠️ 不声明全部实现 | Phase 6：5 项完成、6 项部分完成；Phase 7：7 项完成、9 项部分完成；Phase 8：6 项完成；Phase 9：8 项未勾选，Prefix-Cache 仍为计划 |

Phase 6/7 的部分标记沿用任务文件，其中一些对应业务已在后续修复接通；本轮不重新验收或修改这些任务状态。Phase 9 不纳入本次 T091.3 实施范围。

## 2. M5 任务验收

| 任务 | 状态 | 备注 |
| :--- | :--- | :--- |
| T080 MCP 边界 | ✅ | 站内 JWT 工具边界，未接外部标准 MCP 传输 |
| T081 知识工具 | ✅ | 绑定调用者、复用课程授权与检索服务 |
| T082 报告工具 | ✅ | 复用结果与逐答卷诊断，不生成跨考试画像 |
| T083 邮件工具 | ✅ | Noop 实现；未配置返回 not_configured，未接真实 SMTP |
| T084 审计日志 | ✅ 已有实现 | 180 天保留；迁移测试限定 schema 后通过，原失败事实另记于 T091 |
| T085 Agent Trace | ✅ | 复用 AgentRun/WorkflowRun，Trace 默认保留 30 天 |
| T086 评测看板 | ✅ | 读取既有产物，不把 selftest 指标当作真实质量 |
| T087 Docker Demo | ✅ 已有交付 | 一键脚本与幂等种子数据；Docker 验证边界见 T091.2 |
| T088 文档 | ✅ | README、architecture、evaluation、development |
| T089 端到端验证 | ⚠️ 有限验收 | 开发模式真实模型闭环；Docker Demo AI 链路未验证，冷启动未达目标 |
| T090 可观测性文档 | ✅ | Prometheus/Grafana/OTel 仅为可选扩展点 |
| T091 最终门禁 | ✅ 按确认范围通过 | 修复后 1546 passed、0 failed、1 skipped；mypy/ruff 通过；迁移状态检查见首次运行记录 |

## 3. 质量门禁

### T091.3 授权修复后的重验（最终结果）

- 基线与变更：`4248288`，仅为两项迁移测试的表、索引及约束反射查询增加显式 schema 参数；断言和迁移步骤保持不变，不新增测试。业务代码、模型、迁移及 T091.1/T091.2 成果未改动。
- 修复前先形成 [T091.3 TCR](test-change-record-m5.md)。环境仍为 Windows Python 3.13.13 + 已健康的 PostgreSQL/Redis；没有启动 Backend、调用真实模型或启用额外容器环境。
- `run_at`：模型聚焦开始于 2026-09-29T19:30:28.4550020+08:00；全量开始于 2026-09-29T19:31:52.5406987+08:00。检查顺序为模型聚焦 → 两项原迁移测试 → 全量 pytest → mypy → ruff。

| 项 | 结果 | 实测详情 |
| :--- | :--- | :--- |
| `pytest tests/unit/models/ -q` | ✅ | 137 passed；12.22 秒；退出码 0 |
| 两项原迁移测试 | ✅ | 2 passed、7 warnings；2.55 秒；升级/降级及原断言全部通过 |
| `pytest tests/ -q` | ✅ | **1546 passed、0 failed、1 skipped、14 warnings**；308.78 秒；退出码 0 |
| `mypy backend/app/` | ✅ | 140 个文件无问题；退出码 0 |
| `ruff check backend/ tests/` | ✅ | `All checks passed!`；退出码 0 |

唯一跳过项与下方首次运行相同：M0 冒烟缺少四个隔离环境变量，不计为通过。14 次警告为 Starlette 弃用 3 次、Alembic `path_separator` 弃用 7 次、SQLAlchemy `SAWarning` 4 次。新增的 5 次 Alembic 警告来自迁移测试不再提前失败后执行的其余迁移步骤；未屏蔽警告、删改断言或新增测试。

### T091.3 首次运行（修复前历史记录）

- `run_at`：2026-09-29T19:10:48.6929383+08:00（启动全量 pytest）。
- 环境：Windows 主机 Python 3.13.13；现有项目配置；Docker PostgreSQL/pgvector 和 Redis。T091.2 清理后，本轮仅以 `docker compose up -d postgres redis` 启动依赖，确认健康；未启动 Backend。
- 数据：既有测试夹具及临时 PostgreSQL schema；不改开发库迁移，不修改 `.env`、业务代码或测试。测试未配置 M0 专用 Compose 隔离变量，原因与跳过记录如下。
- Provider / 模型 / Prompt：本轮为既有自动化测试及静态检查，未另行运行真实模型 Benchmark；不把测试替身计作真实质量，也不新增外发演示数据授权。
- 执行顺序严格为 pytest → mypy → ruff → alembic current → alembic check。pytest 失败后继续收集其余门禁证据，未为制造通过而重跑或修改实现。
- 结果路径：本节及下方失败诊断；本轮无测试变更，无需新增 TCR。

| 项 | 结果 | 详情 |
| :--- | :--- | :--- |
| `pytest tests/ -q` | ❌ | **1544 passed、2 failed、1 skipped、9 warnings**；316.98 秒；退出码 1 |
| `mypy backend/app/` | ✅ | `Success: no issues found in 140 source files`；退出码 0 |
| `ruff check backend/ tests/` | ✅ | `All checks passed!`；退出码 0 |
| `alembic current` | ✅ | `0012_audit_logs (head)`；退出码 0 |
| `alembic check` | ✅ | `No new upgrade operations detected.`；退出码 0；public 迁移版本与模型结构一致 |

**跳过项（1 项）**：`tests/integration/test_m0_smoke.py::test_m0_smoke_script`。未设置 `COMPOSE_PROJECT_NAME`、`POSTGRES_PORT`、`REDIS_PORT`、`BACKEND_PORT`，测试按既有隔离规则拒绝使用默认项目。该测试的完整入口包括独立项目 `up --build`、迁移及 Redis 检查；本轮未启用这套环境。T091.2 的缓存镜像范围 C 验证是另一份证据，**不替代此项测试，也不计为本项通过**。

**警告（9 次，未屏蔽）**：

- 3 次 Starlette 弃用警告：TestClient 的 httpx 集成 1 次，`HTTP_422_UNPROCESSABLE_ENTITY` 常量 2 次。
- 2 次 Alembic 弃用警告：`alembic.ini` 未声明 `path_separator`，回退旧版路径拆分规则。
- 4 次 SQLAlchemy `SAWarning`：评分管道测试中 User 不在 Session，`Role.users` 关联添加不执行。

### 首次失败项与只读诊断（保留历史）

| 失败测试 | 原始失败事实 |
| :--- | :--- |
| `tests/integration/test_audit_migration.py::test_audit_migration_upgrade_head_and_downgrade` | 第 22 行：升级隔离 schema 至 0011 后，未限定 schema 的 `get_table_names()` 已包含 `audit_logs`，不存在断言失败 |
| `tests/integration/test_question_source_migration.py::test_alembic_upgrade_head_downgrade_and_reupgrade_in_isolated_schema` | 第 114 行：升级隔离 schema 至 0010 后，同类查询已包含 0011 的三张来源表，不存在断言失败 |

两个测试复用 `_empty_isolated_schema()`，连接设置 `search_path=<随机测试 schema>, public`。SQLAlchemy PostgreSQL 方言在未传 `schema` 时，使用 `pg_table_is_visible` 查询搜索路径中的可见表，**并非仅查 current_schema**。因此测试 schema 尚无相应表时，查询可看到 public 的同名表，造成错误的存在性判定。T091.1 补齐 public 三表后，这一测试隔离缺口更易暴露；不应删除开发库表来让测试通过。

独立只读事务作了对照：`transaction_read_only=on`，临时设置 `search_path=pg_catalog, public`；默认 `get_table_names()` 返回的目标交集包含 `audit_logs` 和 0011 三表，而显式 `schema='pg_catalog'` 的目标交集为空。事务随后回滚，未创建、修改或删除对象。实际 `public.alembic_version` 仍为 `0012_audit_logs`，`alembic check` 通过。这支持失败来自测试反射查询边界，而非据此断定生产迁移错误。

**后续处理**：上述修复建议已获用户确认。先补 TCR，再仅调整两项迁移测试的反射查询，显式绑定夹具返回的 schema；全部原断言保留。聚焦与全量重验通过，详见本节最终结果；未修改业务代码或开发库结构。

## 4. 已知限制

| 限制 | 影响 | 建议 |
| :--- | :--- | :--- |
| M0 独立容器冒烟跳过 | 本轮无该入口的通过证据 | 后续在指定隔离项目/端口中验证，不改断言 |
| Docker Demo AI 链路未验证 | 未配置可用云端 Embedding；范围 C 只证明启动和路由可达 | 未来配置云端 Embedding 并单独授权验证 |
| Workflow 冷启动约 19 秒 | T089 两次 POST 为 10.317/19.122 秒，未达 <1 秒目标 | 已接受为技术债；查询 GET 的 50 样本 p95 59.483 ms 已达标，不代表冷启动达标 |
| 教师标签缺失 | 评分质量无教师 Ground Truth；MAE/RMSE/一致率不可量化 | 补充教师标注，不能用合成分数代填质量结论 |
| T091.2 复用缓存镜像 | 非当前源码重新构建或全新环境验证 | 未来在干净环境补验 |
| 无真实邮件发送 | 默认 Noop 不发送邮件 | 未来接入 SMTP 或云邮件适配器 |
| 三容器合计/峰值资源未测 | T089 仅有主机 Python + 两容器快照，不能证明资源上限 | 单独开展同口径资源验收 |
| Phase 6/7 部分标记、Phase 9 未实现计划 | 不能宣称所有追加阶段任务全部完成 | 另行核验状态或安排后续任务，不在本轮改勾选 |

资源与性能只引用 [T089 原始记录](validation-report.md)：PostgreSQL 153.9 MiB、Redis 6.992 MiB、主机 Uvicorn RSS 1798.95 MiB；采样口径不同，不相加冒充三容器合计。T089 `/ready`、普通 CRUD、无模型等待结果查询和 Workflow 查询的 p95 已记录；本轮未重新压测，也未修改既有失败或缺失指标。

## 5. 发布决策

**结论：✅ 可作为已确认范围内的 M0-M5 开发完成版本；⚠️ 演示前须注意第 4 节已知限制。** 两项迁移测试的误失败已修复，全量 0 失败，静态门禁通过；T091 标记 `[X]`，允许提交后创建并推送固定完成 Tag `v1.0.0-m5-complete`，实际发布以 Git 回执为准。此结论不将跳过项记为通过，不声称 Docker AI、峰值资源或冷启动性能已达标，也不将 Phase 9 计划视作实现完成。

## 6. 验证证据索引

- [validation-report.md](validation-report.md)：T089 开发模式端到端、四检索模式、资源与 P95 实测。
- `docs/m0-m4-retrospective.md`：M0-M4 整体复盘；当前是本地未跟踪文档，本轮未纳入提交，远端读者不能据此假定该文件存在。
- [benchmark.md](benchmark.md)：Benchmark 复现流程与标签边界。
- [observability.md](observability.md)：审计、Trace 及可选扩展说明。
- 测试变更记录：[高优先级](test-change-record-high-priority.md)、[P1](test-change-record-p1.md)、[P2](test-change-record-p2.md)、[P3](test-change-record-p3.md)、[P4](test-change-record-p4.md)、[S5](test-change-record-s5.md)、[M5](test-change-record-m5.md)。
- 下列附录保留 T091.1/T091.2 的实际操作记录；其中“尚未执行”等语句属于当时状态，本轮状态以第 3、5 节为准。

## 附录 A：T091.1 修复 public schema 迁移状态偏差

| 记录项 | 值 |
| :--- | :--- |
| run_at | 2026-09-29T18:07:57+08:00（修复后核验记录时间） |
| 验证源码版本 | `25ae040f934fda5126b7919c85abd9b5e59d49af` |
| 环境 | 开发机；Docker 容器 `eduagent-postgres-1`；数据库 `eduagent`；schema `public` |
| 数据库用户 | `eduagent`（容器配置值；原诊断示例中的 `postgres` 角色不存在） |
| 数据集 / 模型 / Prompt | 不适用；本步骤只处理数据库结构，无模型调用 |
| 结果 | 通过；缺失对象已补建，版本与模型结构一致 |
| 证据位置 | 本节；实际数据库状态位于上述本机开发库 |

### 诊断事实

`public.alembic_version` 记录为 `0012_audit_logs`，但以下三表均不存在：

- `question_source_chunks`
- `question_generation_metadata`
- `question_revision_comments`

修复前 `alembic current` 返回 `0012_audit_logs (head)`；`alembic check` 退出码为 1，只报告新增上述三表以及两个索引的差异，无其他结构差异。属于用户定义的情况 A。现有证据证明迁移记录与结构不一致，不能据此确定此前哪一次操作造成了偏差。

### 实际修复方式

直接复用 [0011_question_source_persistence.py](../migrations/versions/0011_question_source_persistence.py) 的 `upgrade()`，通过 Alembic `MigrationContext` / `Operations.context` 绑定当前连接，在一个 PostgreSQL 事务内补建三表、约束和索引。

执行前确认目标数据库为 `eduagent`，设置事务级 `search_path=public`，锁定并核对版本行仍为 `0012_audit_logs`，确认三表均缺失。任一条件不符即停止。事务内记录所有原有表的行数，执行现有迁移定义后核对行数、版本、表清单和关键约束，再提交。

本次没有执行 downgrade、stamp、删表、删数据或历史回填；`0012_audit_logs` 版本记录保持不变。单纯 stamp 不会补建缺表，不适用于本次偏差。现有迁移文件和业务代码未修改，也未新增迁移。

以下为本次定向重放使用的核心方式，前提是已确认上述情况 A；不是日常启动命令，三表存在时不可重放：

```python
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

from backend.app.core.database import create_database_engine

targets = (
    "question_source_chunks",
    "question_generation_metadata",
    "question_revision_comments",
)
revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(
    "0011_question_source_persistence"
)
assert revision is not None
engine = create_database_engine()
try:
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "eduagent"
        connection.execute(text("SET LOCAL search_path TO public"))
        versions = list(connection.scalars(text(
            "SELECT version_num FROM public.alembic_version FOR UPDATE"
        )))
        assert versions == ["0012_audit_logs"]
        for table in targets:
            assert connection.scalar(
                text("SELECT to_regclass(:name)"), {"name": "public." + table}
            ) is None
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.upgrade()
finally:
    engine.dispose()
```

实际执行还在同事务中核对了下表行数和关键约束；全部通过后才提交。数据库状态不会随 Git 推送同步到其他环境；本提交保存的是本机修复记录，其他环境必须先诊断。

### 数据保留核对

原有 21 张表的精确行数在事务前后逐一相等；修复操作只包含建表、建约束和建索引，没有业务 DML。

| 原有表 | 修复前 | 修复后 |
| :--- | ---: | ---: |
| agent_runs | 4 | 4 |
| alembic_version | 1 | 1 |
| answers | 2 | 2 |
| audit_logs | 4 | 4 |
| courses | 4 | 4 |
| diagnosis_reports | 0 | 0 |
| document_chunks | 43 | 43 |
| documents | 3 | 3 |
| exam_participants | 0 | 0 |
| exam_questions | 2 | 2 |
| exam_results | 0 | 0 |
| exams | 1 | 1 |
| grading_results | 0 | 0 |
| knowledge_bases | 3 | 3 |
| questions | 3 | 3 |
| review_records | 0 | 0 |
| roles | 3 | 3 |
| submissions | 1 | 1 |
| user_roles | 7 | 7 |
| users | 8 | 8 |
| workflow_runs | 0 | 0 |

### 修复后验证

| 检查 | 实测结果 |
| :--- | :--- |
| `alembic current` | 退出码 0；`0012_audit_logs (head)` |
| `alembic check` | 退出码 0；`No new upgrade operations detected.` |
| 三表存在 | 独立 `psql` 连接确认均位于 `public`；列数分别为 13、8、5；新表行数均为 0 |
| 主键与外键 | 三表主键完整；题目外键均 `ON DELETE CASCADE`；`live_chunk_id` 外键 `ON DELETE SET NULL`；评论人外键 `ON DELETE RESTRICT` |
| 来源唯一约束 | `(question_id, chunk_id)` 与 `(question_id, source_order)` 均存在 |
| 评论内容约束 | 去除首尾空白后非空且长度不超过 2000 的 CHECK 存在 |
| 两个查询索引 | `ix_question_source_chunks_course_question`、`ix_question_revision_comments_question_commented_at` 的列顺序均正确 |

本步骤无测试代码变更，无需新增 TCR。本节仅记录 T091.1，其他门禁见后续记录。

## 附录 B：T091.2 Docker 容器启动验证（范围 C）

### 环境与验证边界

| 记录项 | 值 |
| :--- | :--- |
| run_at | 2026-09-29T18:59:38.516537+08:00（开始调用 Compose） |
| 工作区源码版本 | `c30a1c4d7600a565fa39f8c6ad57832a1a0cc2cc`；分支 `deepcode` |
| 环境 | Windows 开发机上的 Docker Compose；项目 `eduagent`；默认端口 5432、6379、8000 |
| 实际 Backend 镜像 | `eduagent-backend:latest`；`sha256:700348aa477a7facd5a6cfc6d750201394bc9c2908edbfbce1ff7d196b7e406a` |
| 镜像来源边界 | 复用本地缓存镜像，创建于 2026-09-16T20:14:50+08:00；本次未执行构建，不能据此声明镜像包含上述工作区版本 |
| 初始状态 | PostgreSQL、Redis 已运行且健康；Backend 未运行；已有持久化数据卷 |
| 数据集 / 模型 / Prompt | 不适用；本次不执行 AI 链路或云端 Embedding 调用 |
| 结果及证据位置 | 范围 C 通过；本节记录实际命令与观测结果 |

只在执行命令的 PowerShell 进程中临时设置占位符，未修改 `.env`、Compose 配置或业务代码：

```powershell
$env:DEMO_EMBEDDING_MODEL = "placeholder-model"
$env:DEMO_EMBEDDING_API_KEY = "placeholder-key"
$env:DEMO_EMBEDDING_BASE_URL = "https://example.invalid"
docker compose up -d
```

容器内核验上述三个值均为占位符（只输出匹配布尔值，均为 True）。占位配置只用于启动检查，不证明 Provider 可用；未发起检索、出题、阅卷或模型请求。

### 实测结果

从调用 `docker compose up -d` 开始使用单调时钟计时；健康检查轮询间隔约 2 秒，总等待上限 120 秒。

| 项 | 结果 | 实测证据 |
| :--- | :--- | :--- |
| Compose 启动 | 通过 | 退出码 0；命令耗时 2.342 秒；Backend 重建容器并启动，依赖容器保持运行 |
| PostgreSQL 健康 | 通过 | `eduagent-postgres-1`：`running` / `healthy` |
| Redis 健康 | 通过 | `eduagent-redis-1`：`running` / `healthy` |
| Backend 健康 | 通过 | `eduagent-backend-1`：`running` / `healthy`；后续 `docker ps` 再次确认三者均 healthy |
| `/ready` | 通过 | 主机访问 `http://localhost:8000/ready`：HTTP 200；响应 `{"status":"ok","service":"backend"}` |
| 普通 CRUD 路由可达 | 通过 | 未认证访问 `http://localhost:8000/api/courses`：HTTP 401；响应 `{"detail":"请提供 Bearer 认证凭证。"}` |
| 启动耗时 | 13.512 秒 | 从 Compose 调用至首次观测到 Backend healthy；无超时、无端口冲突 |

此耗时包含 Compose 命令执行和健康状态轮询。依赖容器此前已健康，且镜像与数据卷已存在，**不是首次拉取/构建镜像或空数据卷冷启动耗时**。HTTP 401 只证明路由和未认证拒绝可达，不代表已验证登录后的 CRUD 业务。

### 清理与数据保留

使用相同的进程级占位符执行 `docker compose down`，**未使用 `-v`**，退出码 0。三个 Compose 容器及项目网络已移除；随后 `docker ps` 无运行容器。

`docker volume ls --filter name=eduagent` 确认以下命名卷仍存在，供后续启动重新挂载：

- `eduagent_postgres_data`
- `eduagent_redis_data`

没有删除数据卷或镜像，也未启动主机 FastAPI。两个原有未跟踪文档保持不变。

### 未验证项与后续门禁

- 按决策 C 排除 AI 链路、云端 Embedding 调用和 Docker Demo 完整 AI 端到端验证；相关开发模式实测仍以 [T089 报告](validation-report.md) 为准。
- 本次结论只针对上述实际缓存镜像；未执行当前源码的镜像构建验证。
- 本步骤仅记录运行验证，不修改测试，无需新增 TCR；未重跑 pytest/mypy/ruff，也未采集资源上限或 P95 数据。
- T091 仍未勾选；T091.3 等用户确认后执行，不提前声明最终门禁完成。

## v2.0 最终执行记录（2026-10-06，T189–T191）

当前结论：**v2.0验收未完成，不能据本批记录发布已验收声明。** 本节只追加v2.0状态，前面的v1.0及T091历史记录原样保留。

| 门禁/证据 | 状态与依据 |
|---|---|
| T190 原框架 | 执行完成；2617passed/1原样本契约failed/15初始skip，mypy215，活动Ruff通过；原断言不改。独立补验标准备份/题图15、提交阅卷契约30、M0原集成1项通过，重叠不累计 |
| Docker M0 | 当前镜像空库启动顺序局部修复后，3healthy、ready200、0023head、PONG；原14单元/契约通过。云Embedding仅配置构造，Docker AI业务未验 |
| T160 | 批准重验口径50页7/8、页面24/24；严格原全部导入目标失败、部分质量短窗资源未知仍记录 |
| T168 | 学习项目基线完成，质量not_met；自动导入/语义指标未达确认阈值，不宣称教师质量 |
| T179 | 48正式动作/12业务通过，源码受控资源窗完整；不替代EXE |
| T186 | 七类UI实际操作/129回归/截图和回读完成，本批复用同业务源码，未再次全页点击 |
| T189/Gate17 | 首次3/3目标通过，后续0/5<10s，启动内存1窗未知；未勾选，不宣称SC-014交付通过 |
| T191 真实闭环 | 实际冻结EXE、真实BGE/文字/Vision/OCR，AI显式业务核对、最终混合2/10、真实workflow复核/诊断/师生分析；失败状态和不同本场10/5保持 |
| T191/Gate13恢复 | 停写同集complete→新库/根verified；32表全行/20文件、真实授权、来源/选项/本场值/最终成绩一致；恢复目标未激活 |
| 标注/正式SC-013 | T146 AI辅助＋开发者审查、独立教师0，未知与替换路径保持；本次教师处置为AI会话委托，正式独立教师增强仍未完成 |
| 配置/资源/发布 | 原库和用户4文件未变，所属程序已正常停止；共享PG/Redis保留，未切换恢复配置、未发布正式release |

T190/T191勾选表示执行、闭环恢复及证据说明已完成，不能解除T189未通过或其他质量/兼容未通过项。汇总见 [evaluation](evaluation.md)、[validation](validation-report.md)、[T190](../benchmark/results/v2/t190-20261006/README.md)、[T191](../benchmark/results/v2/t191-20261006/README.md)。

下一批可连续处理T189后续启动性能及完整资源重验→T168按已确认目标修复质量并另立批次→针对新修改/已失败项复验T190/T191最终门禁；保护原基准/失败记录。工作区开发者评分与原默认未标注契约协调须保留真实来源，不能覆盖用户文件或降低旧断言。正式发布和恢复激活另按用户授权，不由任务勾选自动执行。

## 本批修复后最终门禁复验（2026-10-06）

**最终门禁仍未通过，发布条件未满足。** 当前首启3/3达标，后续0/5<10秒；8/8启动资源预算与正常退出、8/8冻结故障门槛通过。T168原AI辅助＋开发者审查质量目标仍not_met（自动全字段0/48、条件误报16.67%、Rubric覆盖77.78%、图片完整72.73%），独立教师0；不改变原阈值或其他任务定义。

全量提交快照2643/1/15（passed/failed/skipped）；唯一旧SDK生命周期测试适配后相关120项通过，未重新宣称全量通过。mypy216/活动Ruff、PG真实备份/题图15项、当前源码M0集成1项通过；隔离测试项目已down、卷保留，原PG/Redis及0012业务库不变。用户两份README、工作评分语料和.env字节保持。已有T191真实闭环与恢复证据保留，恢复目标未激活。本批未重复七类页面操作或再次执行整套T191云业务；当前质量/启动失败足以维持不发布结论。

[本轮汇总和真实失败](../benchmark/results/v2/final-gate-retest-20261006/README.md)。可继续启动剖析与现有提取/语义质量修复；新增自动图片提取流程须另定范围后实施，再针对变化及未通过项复验最终门禁。T189保持未勾选，T168基线完成状态不代表质量达标。
