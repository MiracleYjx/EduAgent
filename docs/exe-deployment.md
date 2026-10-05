# Windows 单机启动（T187）

本入口保留 Docker 原启动路径。Windows 已独立安装 PostgreSQL + pgvector、Redis；云模型仍依赖网络。源码需 Python 3.12+ 与项目依赖，EXE 构建由 T188 承接。

1. 将 config/windows.env.example 复制到 `%LOCALAPPDATA%/EduAgent/config.env`，填写真实数据库、Redis、模型和 JWT 配置。配置文件显式优先于同名继承环境变量；不复制仓库私有 .env。已有数据库升级前按现有备份工具备份。
2. 云 Embedding 使用其自己的实际模型/凭据，须支持既有1024维合同。本地 Embedding/Cross Encoder须显式提供完整外置模型目录及对应可选依赖，不在启动期间自动下载。OCR可关闭；启用时必须选 RapidOCR、PP-OCRv5-mobile 与三份 T155 锁定权重，检查运行库/原权重哈希。
3. 源码运行 `python scripts/launch_windows.py --config <绝对配置路径>`；只监听127.0.0.1，默认8000。`--port` 可调整；`--startup-timeout` 限定应用等待；`--no-browser` 可用于受控验收。
4. 配置→持久目录可写→模型配置→PostgreSQL/pgvector/Redis→必要网络→Alembic upgrade head与实际head核对→启动所属进程→原 `/ready` 与 `/gradio/` 均200→打开浏览器。网络仅TCP/TLS连通探测，无模型token请求，不证明余额/凭据/质量；业务调用仍保留真实失败。
5. 控制台 Ctrl+C 退出，启动器只停止自身 Popen 持有的应用进程；不停止数据库/Redis或其他端口上的程序。关闭浏览器不会结束服务。应用日志在 STORAGE_ROOT/logs/application.log。配置、模型、权限、网络、依赖、迁移或就绪失败打印对应步骤、异常类型与下一步；EXE双击失败保留控制台直到回车。

EXE默认业务数据在 `%LOCALAPPDATA%/EduAgent/storage`。配置和数据不得放在 EXE 的内部资源/安装目录；模型同样外置。首次配置不生成弱密钥、不自动创建教师账号；DEV_MODE仅显式学习演示使用。

## T187实际证据

[evidence/t187-20261006](evidence/t187-20261006/t187-runtime.json)：Windows/Python3.13，独立空数据库迁移到 `0023_grading_exam_question`，生产 FastAPI挂载Gradio的两端点200，返回码0，持有的真实应用进程退出。约19.876秒是单次源码辅助演练总耗时，不能当作冷暖启动指标。模型网络为明确受控本地TCP端点，未调用云模型；此运行使用--no-browser，自动打开晚于就绪由必要单元用例验证。35项配置/数据库/Redis/启动器聚焦测试通过，新增8项、旧用例未修改；静态检查通过。测量脚本曾缺psutil及错误预期缩写修订号，已如实保留说明并按Popen句柄和实际完整修订号核对，不伪造迁移版本。

T188构建包与T189真实Windows重复启动/故障/资源预算另验；本页不宣称SC-014交付已通过。
