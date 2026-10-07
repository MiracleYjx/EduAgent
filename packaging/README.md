# EduAgent Windows onedir 包

将整个 EduAgent 目录复制到目标 Windows 主机，双击 EduAgent.exe。console 用于检查步骤与错误，Ctrl+C 结束所属应用进程。不得只复制 EXE 或移除 _internal。

外部先准备 PostgreSQL + pgvector、Redis。将 config.env.example 复制到 %LOCALAPPDATA%/EduAgent/config.env，填写真实配置；不要在安装目录存业务数据/凭据。默认数据在 %LOCALAPPDATA%/EduAgent/storage，日志在其 logs/application.log。可使用 `EduAgent.exe --config <绝对路径> --port 8000`。

本构建支持云 Embedding、原 DeepSeek/LLM重排及可选 RapidOCR+ONNX CPU。OCR 默认关闭；启用时明确预置 T155 的 PP-OCRv5 mobile 三份权重到外部 OCR_MODEL_DIR。SDK 自动附带的 OCRv6 权重不打包。云 API 仍需网络及实际模型/凭据，包不证明模型质量。

源码 `scripts/build_exe.ps1` 使用独立环境、固定 requirements-windows.txt，输出 .cache/exe-build/dist/EduAgent。`-WithLocalModels` 可将 sentence-transformers/Torch 本地推理库加入另一个构建（仍外置权重）；本批仅实际验证默认云 Embedding+OCR 运行库构建，此开关的额外体积与加载需独立实测。选本地 Provider 但无对应库/完整外置目录时明确启动失败，不改为云 Provider。

build-receipt.json 记录实际版本、体积、源码和EXE哈希用于追溯，不作为跨组件版本相等门禁。无需目标机 Python，不带 PostgreSQL/Redis 安装器。首次/后续启动重复次数、故障矩阵、原页/OCR启禁与内存预算由 T189 正式验收，不以构建成功宣称系统交付通过。

构建依据：[PyInstaller spec 文件官方说明](https://pyinstaller.org/en/stable/spec-files.html) 与[冻结运行路径官方说明](https://pyinstaller.org/en/stable/runtime-information.html)。业务验收以本项目运行证据为准。

冻结构建会在独立 work 目录转换 Gradio 的 create_or_modify_pyi，仅跳过运行时的 IDE `.pyi` 文件生成；原安装库与源码运行路径保持原样，组件校验、事件和 API 信息保留。转换工具 scripts/freeze_gradio.py 进入构建回执；当前固定 Gradio 6.26.0，升级该依赖时须重新审查此转换并运行真实组件差分与冻结启动验收。

同一冻结副本还为 get_api_info 建立本次调用的组件 ID 索引，替换逐输入/输出的全表扫描；重复 ID 保留首项，缺失组件仍跳过原端点，API 信息与可见性保持。完整生产工作台双模式逐值差分和原 UI 回归用于核对；升级 Gradio 时连同类型提示转换重新审查。

## 本机配置窗口

首次双击 EXE 缺少默认 config.env 时自动打开配置窗口；之后双击同目录 Configure-EduAgent.cmd，或运行 `EduAgent.exe --configure`。EXE 仍保存到 `%LOCALAPPDATA%/EduAgent/config.env`；`--config <绝对路径>` 可指定外置文件。源码编辑 .env 使用 `python scripts/launch_windows.py --configure --config .env`。

窗口支持连接、模型、OCR、JWT、演示模式与检索参数，密钥遮罩，JWT生成需显式点击。保存保留注释和未管理项，先校验再原子替换；检测到其他程序改动时拒绝覆盖。取消不写文件，编辑模式保存后退出，重启服务生效；首次配置保存后继续原预检。窗口不测试连接、不迁移数据库、不自动下载模型。--no-browser 后台启动及显式缺失配置不自动弹窗。Tcl/Tk 运行资源随包附带，目标机无需安装 Python。


配置注意：DATABASE_URL 中 USER/PASSWORD 必须替换；仅填写文字 Key 不能提供 Embedding。云模式需独立 EMBEDDING_MODEL / EMBEDDING_API_KEY（模型须支持1024维）；本地模式需完整外置目录及包含本地推理库的包。窗口与启动器会直接列出缺失字段，未完成时保留输入、拒绝启动。选择已有数据库还需沿用其配套业务存储目录。
