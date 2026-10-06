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

## T188 实际构建包

运行 `scripts/build_exe.ps1 -Python <Python路径>`，独立环境按 packaging/requirements-windows.txt 固定依赖；默认输出 .cache/exe-build/dist/EduAgent。当前实际完整执行的输出是 `.cache/t186-t188-20261005/final-build/dist/EduAgent`，整目录交付，EXE与_internal同在。构建清单收集Gradio静态与运行所读组件源码、safehttpx/groovy版本数据、动态后端/Agent模块、Alembic、pypdfium2，以及RapidOCR/ONNX CPU库和字典；移除SDK默认权重，使用已确认外置PP-OCRv5。

[实际构建与运行证据](evidence/t188-20261006/README.md)：PyInstaller6.22.3、Python3.13.13，初始约367MiB，Gradio生成组件存根后的最终目录约369MiB；包内空库迁移/API/UI/静态资源及PDF/扫描OCR/授权PNG读取通过。受控空题目输出仍Failed，不宣称成功导入或真实模型质量。默认包选择云Embedding+LLM重排与可选CPU OCR，降低依赖体积；本地Embedding/Cross Encoder需-WithLocalModels额外构建并实测，运行时不会偷偷切换Provider。

缺配置测试返回1，参数帮助返回0。构建号/哈希仅追溯，实际源文件哈希及dirty状态记录于build-receipt.json，不要求远端组件/数据库与源码提交号相等。资源冒烟退出使用辅助工具按确切归属停止子进程，正常冻结程序Ctrl+C/关闭控制台由T189验证；尚无完整冷暖/内存测量，SC-014交付未宣告通过。

## T189–T191 实际运行与当前交付限制（2026-10-06）

本批以实际-WithLocalModels构建的onedir包运行，外置既有完整BGE权重与锁定PP-OCRv5模型；包约922.57MiB。启动器只检查本地目录存在，实际编码还需完整模型文件：D:/YJX/Cache/huggingface的旧snapshot只有元数据，本批改用仓库.cache/huggingface下已有完整权重，没有自动下载或替换Provider。不要把目录存在、TCP网络检查或readiness称为模型业务可用。

后台验收统一使用 `EduAgent.exe --config <绝对配置路径> --no-browser`，不反复自动打开浏览器。实际冻结包完成原页/裁图读取、OCR、BGE知识库、DeepSeek文字生成/改编/Vision/语义、带图考试、混合工作流评分和复核/诊断；会话委托AI使用真实DEV角色，仅处理自编合成数学题，不能作为独立教师或泛化质量证明。该课程EXE正常Ctrl+C返回0，所属Worker退出；共享PG/Redis保留。

[T189验收](v2.0-exe-acceptance.md)仍未通过：首次3/3<30s，后续0/5<10s（约18.76–20.12s），启动资源7/8窗完整、1窗未知。启动阶段未实际BGE编码，预算不外推到完整本地模型业务峰值。T191使用完整权重的单次就绪19.465s只作运行回执，不替代固定重复协议。

部署前沿用[发布检查表](release-checklist.md)的v2.0新增节；原配置/真实库不自动升级或切换，恢复目标verified也保持停写。当前T168质量、T189启动/资源及工作区原样本契约未通过，**尚不能声明v2.0交付验收完成**；源码、Docker、包构建、业务运行、质量与发布回执分别判断。

## 冻结启动优化后复验（2026-10-06）

当前冻结包会提前启动自有Worker进行模块预加载和界面构建，父进程并行预检外置配置、模型、PG/Redis、网络和迁移；只有全部通过才通过内部stdin发送SERVE并启动Uvicorn。EOF、预检失败或取消时关闭自有Worker，不开放业务端口。源码路径仍按原串行流程，不改迁移失败断言。SDK在首次实际请求创建，并保持构造时配置快照及自有资源释放。使用 --no-browser 后台运行。

最新[实测](v2.0-exe-acceptance.md)：首次3/3<30秒，后续15.20–16.24秒、0/5<10秒；8/8资源采样完整且在预算内，8/8正常退出，8故障均无业务端口。原资源缺测结论作为历史保留；新结果仍不能声明T189或v2.0交付通过。最新包/日志/源码追溯见[构建回执](../benchmark/results/v2/final-gate-retest-20261006/build-receipt.json)。原配置、业务库和恢复环境不自动切换。
