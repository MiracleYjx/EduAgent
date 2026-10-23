# EduAgent

[English](README.md) | [简体中文](README.zh-CN.md)

基于 React、FastAPI、PostgreSQL/pgvector 和显式 AI 操作的本地个人学习应用。按科目管理自己的资料和试卷，核对题目，开始练习、查看评分与争议，并与有可追溯引用的 AI 教师对话。

界面主要使用中文。个人学习不需要教师/学生业务登录；修改设置仍需本机管理授权。

## 当前状态

个人学习功能于 **2026-10-23 按“带边界验收”口径收尾**。T001–T087 已勾选，其中 T083/T084/T085/T087 按用户裁决接受已知边界。**87/87 是任务勾选数，不代表所有测试或真实模型场景全部通过。** 详见[已知边界](#已知边界)、[需求规格](specs/002-personal-learning/spec.md)和[任务记录](specs/002-personal-learning/tasks.md)。

历史 v2.0 标签对应旧教师/学生版本。其 Gradio 工作台、角色登录和演示账号步骤不是当前入口。Python 包版本仍为 0.1.0，与功能验收及 Git 修订分别追溯。

## 主要能力

- 按科目、资料版本和已确认知识点组织学习。
- 通过适用的资料/试卷路径导入 PDF、DOCX、TXT、Markdown、PNG、JPEG，保留原文件、真实锚点、图文和疑点。
- 校正抽取题目、答案和重复身份，再明确确认是否准入练习。
- 按上传原题、AI 题或混合来源组练习，开始前查看实际覆盖和不足。
- 整组提交后评分；客观题和空白主观题走本地确定规则，AI 评分、重评及补答案需显式确认。
- 保留各轮评分和争议历史；重评不会静默覆盖旧结果，也不会在接受前自动解除争议。
- 查看有证据的诊断和资料建议，打开教师引用原文；聊天不改变正式成绩。

真实效果取决于模型和输入。失败或未知结果如实保留，不会自动重发收费请求制造成功。

## 最快体验：Docker

### Windows 一键启动

双击仓库根目录的 `start-eduagent.cmd`。脚本会检查 Docker Desktop（必要时尝试启动），首次运行自动构建，等待设置服务就绪，然后打开应用首页或一次性设置授权链接。

也可以在 PowerShell 中传入参数：

~~~powershell
.\start-eduagent.cmd -NoBrowser
.\start-eduagent.cmd -Port 8001 -NoBrowser
.\start-eduagent.cmd -Rebuild
.\start-eduagent.cmd -Setup
~~~

`-Rebuild` 用于代码修改后重新构建；`-Setup` 在应用已初始化时仍申请设置授权链接；`-Port`、`-PostgresPort`、`-RedisPort` 可覆盖默认的 8000/25432/26379。脚本会通过 DNS-over-HTTPS 解析 Debian 源并在构建时固定真实 IP，避开 Docker 的 fake-IP DNS；默认 `-DebianMirror deb.debian.org`，也可传入可访问的镜像站域名。

先安装 Git 和带 Compose 的 Docker，并启动 Docker 引擎。该路径在容器中构建前后端，**仅打开设置页不要求宿主机安装 Python、Node.js、BGE 或 OCR 权重**。

请使用新检出的仓库、新 Compose 项目和新数据卷。以下步骤不接管已有部署。

在 PowerShell 中执行：

~~~powershell
git clone --branch main https://github.com/MiracleYjx/EduAgent.git
cd EduAgent

# 端口被占用时改成其他本机空闲端口。
$env:POSTGRES_PORT = "25432"
$env:REDIS_PORT = "26379"
$env:BACKEND_PORT = "8000"

docker compose -p eduagent-local up -d --build
docker compose -p eduagent-local exec -T backend python -m backend.app.deployment.container_control --port 8000
~~~

在本机浏览器打开最后一条命令返回的一次性授权链接。若修改 BACKEND_PORT，container_control 的 --port 也必须相同。首次下载和构建需要网络与时间，不承诺统一启动时长。

向导填写的是**容器内部地址**：

| 配置项 | Docker 应用中填写 |
| --- | --- |
| PostgreSQL 主机 / 端口 | postgres / 5432 |
| PostgreSQL 数据库 / 用户 | eduagent / eduagent，除非你修改了参数 |
| Redis 主机 / 端口 / 库 | redis / 6379 / 0 |
| 业务文件目录 | /app/storage/storage |

25432/26379 是容器外部客户端访问数据库的端口，不能填写成容器的依赖地址。默认 Compose 对 PostgreSQL 使用本机开发 trust 认证，宿主端口只发布到回环；这不是公网多用户部署模板。

核对预览，确认初始化的是**新建空目标**，再按提示激活配置或重启。业务 /ready 为 503 时设置页仍可用；打开页面不会初始化旧数据库，也不会自动调用 AI。

### Docker AI 凭据

Docker 使用只读挂载 Secret，不复用 Windows DPAPI 密文。API Key、凭据密文和模型权重不随仓库或镜像分发。

1. 在仓库外创建自己的私有目录，用编辑器把 Provider Key 保存到 UTF-8 文件 provider-key，不把密钥作为命令行参数。
2. 指定绝对目录及下面的非秘密挂载目录表。聊天与 Embedding 使用不同密钥时，分别增加文件和条目。
3. 带覆盖文件重建后端，再获取新授权链接，在设置中选择挂载凭据。保存、Probe、激活是不同操作；核对范围后才执行真实请求。

~~~powershell
# 改为你自己已经创建的私有目录。
$env:EDUAGENT_SECRETS_DIRECTORY = Join-Path $env:LOCALAPPDATA "EduAgent/github-secrets"
$env:EDUAGENT_SECRET_MOUNTS = '{"provider-key":{"path":"/run/secrets/provider-key","purpose":"provider"}}'

docker compose -p eduagent-local -f docker-compose.yml -f docker-compose.secrets.yml up -d backend
docker compose -p eduagent-local exec -T backend python -m backend.app.deployment.container_control --port 8000
~~~

Secret 文件须满足挂载凭据后端的归属与访问权限检查。管理同一部署时保留原端口、项目名和 Compose 文件组合。缺 Key 或权重时相应能力显示未就绪，不会自动请求。

### 停止和再次启动

~~~powershell
docker compose -p eduagent-local stop
docker compose -p eduagent-local start
~~~

postgres_data、redis_data、storage_data 卷保存持久数据。日常退出不要使用 down -v 或 volume prune。使用 Secret 覆盖文件的部署应保留原环境与覆盖文件选择。

## Windows 源码开发

需要 Python 3.12（项目声明最低 3.12）、与 Docker 构建一致的 Node.js 24、Git，以及 PostgreSQL/pgvector 和 Redis。也可仅用 Docker 提供两个依赖。

在新检出的仓库根目录、PowerShell 中执行：

~~~powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -c packaging/requirements-windows.txt -e ".[dev,ocr]"

$env:EDUAGENT_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
Push-Location frontend
npm ci
npm run build
Pop-Location

$env:POSTGRES_PORT = "25432"
$env:REDIS_PORT = "26379"
docker compose -p eduagent-local up -d postgres redis

$env:EDUAGENT_ENVIRONMENT = "development"
$env:EDUAGENT_DATA_ROOT = Join-Path $env:LOCALAPPDATA "EduAgent/github-local"
$env:EDUAGENT_SETTINGS_UI_ROOT = (Resolve-Path ./frontend/dist).Path
./.venv/Scripts/python.exe scripts/launch_windows.py
~~~

**宿主机应用**向导中，PostgreSQL 填 **127.0.0.1:25432**，Redis 填 **127.0.0.1:26379**，数据库/用户默认仍为 eduagent。请使用自己的空闲数据根，先确认实际初始化目标。Launcher 负责本机授权及自有 worker 的退出。

源码运行必须先构建前端并显式指定 EDUAGENT_SETTINGS_UI_ROOT；GitHub 不自带 frontend/dist。EDUAGENT_PYTHON 保证前端 API 检查使用正确的 Python 环境。

新版入口不隐式导入仓库 .env 或别人的 config.env。.env.example 仅为旧配置兼容参考；需要迁入时在设置页显式导入。

## 模型选择

| 能力 | 使用环节 | 如何准备 |
| --- | --- | --- |
| 远程聊天 / JSON / Vision | 按配置执行出题、补答案、主观评分、教师答疑 | 配置支持的 Provider/模型及自己的受保护凭据 |
| 远程 Embedding | 资料索引、已配置的语义/混合检索 | 单独配置兼容的 Embedding 服务，不能只填写聊天 Key |
| 本地 BGE | 选择本地 Provider 后的索引/检索操作 | 安装 .[rerank-local]，获取兼容权重，设置绝对资源目录 |
| 本地 RapidOCR | 主动识别保留的扫描页或图片 | 安装 .[ocr]，预置三份约定的 PP-OCRv5 mobile ONNX 文件 |

克隆不依赖开发者的模型目录。文字/关键词路径不一定需要本地权重。当前个人出题和教师上下文显式使用关键词检索；评分选择语义/混合模式时才规划相应 Embedding 步骤。

RapidOCR 需要 ch_PP-OCRv5_det_mobile.onnx、ch_PP-OCRv5_rec_mobile.onnx、ch_ppocr_mobile_v2.0_cls_mobile.onnx。[适配器](backend/app/ai/ingestion/ocr/rapidocr.py)核对哈希并使用本地 ONNX CPU 推理，不静默下载替代权重。

Docker 应额外只读挂载模型目录，填写**容器内**绝对资源路径。默认镜像不含 Torch/sentence-transformers，本地 BGE 还需显式准备对应镜像。Windows 默认包同样不包含这些库，除非使用 WithLocalModels 构建。切换 Embedding 身份应走索引校验/重建流程，不能只改路径。

## 构建 Windows EXE

~~~powershell
./scripts/build_exe.ps1 -Python python -BuildRoot .cache/exe-build
~~~

复制生成的**整个 EduAgent 目录**，包含 _internal。EXE 免去目标机 Python/Node 安装，但 PostgreSQL/Redis、个人配置、Key 和可选权重仍在外部。源码仓库不表示已提供 GitHub Release 下载包。详见[打包说明](packaging/README.md)。

## 验证

安装 Python 环境并按前文设置 EDUAGENT_PYTHON 后：

~~~powershell
Push-Location frontend
npm test
npm run build
Pop-Location
~~~

后端测试分布在 tests/unit、tests/contract、tests/integration。涉及 PG/Redis/Docker 的测试须使用经过实例身份核验、归属明确的独立 Test profile 与专用资源，不能指向个人业务库。初始化测试资源前阅读[验证前置条件](specs/002-personal-learning/quickstart.md)和[PG 工具](tests/postgres_helpers.py)。Mock/构建通过不等于真实模型效果或完整浏览器验收。

| 地址 | 含义 |
| --- | --- |
| / | 个人学习界面 |
| /setup、/settings | 首次设置和受保护配置 |
| /health | 进程存活 |
| /setup-ready | 设置及静态资源状态 |
| /ready | 业务就绪；503 不表示设置页不可用 |

## 已知边界

- 开发者验收环境保留4条历史 outcome_unknown；后续独立样例成功不等于原请求已补偿或已确认。新安装不会导入这些历史记录。
- T084/T085 journey 保留 api_passed_browser_not_claimed。截图/UI 回读是局部证据，未宣称完整浏览器闭环或 T083 真实 AI 浏览器入口到结果的路径已通过。
- DOCX 有 T033 有限历史覆盖；T084/T085 没有在 Windows、Docker 两平台重测 DOCX。
- G1 真实 operation 虽完成，但因 ANSWER_ENCODING_INVALID 未准入，oracle_pass=false 保留；带边界验收不把它变成质量通过。
- 账本 schema 未持久化 finish_reason；请求计数争议及历史授权待核实说明仍保留。
- 勾选数不保证准确率、性能、全新机器安装或供应商账单。.cache 本地验收材料不随 GitHub 源码发布。

验收裁决和后续优化入口见 [tasks.md](specs/002-personal-learning/tasks.md)、[spec.md](specs/002-personal-learning/spec.md)、[quickstart.md](specs/002-personal-learning/quickstart.md)。

## 仓库结构

~~~text
backend/       FastAPI、模型、解析、检索、AI、本机管理
frontend/      React、生成 API 类型、前端检查
migrations/    Alembic 迁移历史
packaging/     Windows 构建及已验收运行依赖约束
scripts/       启动、构建、Benchmark、显式维护
tests/         单元、契约、隔离集成与合成样例
specs/         需求、合同、验收边界
benchmark/     历史可复现 Benchmark 输入
~~~

运行数据、.env、凭据、权重、日志、构建包和 .cache 证据不是安装输入。请使用自己有权使用的资料与模型服务。自动搜集教材、图片答案评分、聊天中另建评分流程不属于首版范围。
