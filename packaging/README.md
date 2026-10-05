# EduAgent Windows onedir 包

将整个 EduAgent 目录复制到目标 Windows 主机，双击 EduAgent.exe。console 用于检查步骤与错误，Ctrl+C 结束所属应用进程。不得只复制 EXE 或移除 _internal。

外部先准备 PostgreSQL + pgvector、Redis。将 config.env.example 复制到 %LOCALAPPDATA%/EduAgent/config.env，填写真实配置；不要在安装目录存业务数据/凭据。默认数据在 %LOCALAPPDATA%/EduAgent/storage，日志在其 logs/application.log。可使用 `EduAgent.exe --config <绝对路径> --port 8000`。

本构建支持云 Embedding、原 DeepSeek/LLM重排及可选 RapidOCR+ONNX CPU。OCR 默认关闭；启用时明确预置 T155 的 PP-OCRv5 mobile 三份权重到外部 OCR_MODEL_DIR。SDK 自动附带的 OCRv6 权重不打包。云 API 仍需网络及实际模型/凭据，包不证明模型质量。

源码 `scripts/build_exe.ps1` 使用独立环境、固定 requirements-windows.txt，输出 .cache/exe-build/dist/EduAgent。`-WithLocalModels` 可将 sentence-transformers/Torch 本地推理库加入另一个构建（仍外置权重）；本批仅实际验证默认云 Embedding+OCR 运行库构建，此开关的额外体积与加载需独立实测。选本地 Provider 但无对应库/完整外置目录时明确启动失败，不改为云 Provider。

build-receipt.json 记录实际版本、体积、源码和EXE哈希用于追溯，不作为跨组件版本相等门禁。无需目标机 Python，不带 PostgreSQL/Redis 安装器。首次/后续启动重复次数、故障矩阵、原页/OCR启禁与内存预算由 T189 正式验收，不以构建成功宣称系统交付通过。

构建依据：[PyInstaller spec 文件官方说明](https://pyinstaller.org/en/stable/spec-files.html) 与[冻结运行路径官方说明](https://pyinstaller.org/en/stable/runtime-information.html)。业务验收以本项目运行证据为准。
