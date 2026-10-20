# EduAgent Windows onedir 包

将整个 EduAgent 目录复制到目标 Windows 主机，双击 EduAgent.exe。console 用于检查步骤与错误，Ctrl+C 结束所属应用进程。不得只复制 EXE 或移除 _internal。

首次启动不要求模型、数据库或 JWT 已配置；Launcher 在回环地址打开设置页并保留控制窗口。设置可达不等于业务就绪：按向导配置 PostgreSQL + pgvector、Redis 和业务目录，预览并确认初始化后重启应用。未配置 AI 的能力继续显示未就绪，不自动调用模型。

配置与 DPAPI CurrentUser 密文位于外置环境目录（默认 `%LOCALAPPDATA%/EduAgent/development` 下的 config 和 credentials），已有业务 storage 保留原兼容路径。不要复制其他用户的密文当作可用凭据；解密失败需在设置页重录。不要把数据目录放在安装包内。旧 `.env` 只通过设置页显式导入，`--config` 不再装载明文配置。

本构建支持云 Embedding、原 DeepSeek/LLM重排及可选 RapidOCR+ONNX CPU。OCR 默认关闭；启用时明确预置 T155 的 PP-OCRv5 mobile 三份权重到外部 OCR_MODEL_DIR。SDK 自动附带的 OCRv6 权重不打包。云 API 仍需网络及实际模型/凭据，包不证明模型质量。

源码 `scripts/build_exe.ps1` 使用独立环境、固定 requirements-windows.txt，每次输出新的 .cache/exe-build/build-<id>/dist/EduAgent，失败不覆盖前一包。`-WithLocalModels` 可将 sentence-transformers/Torch 本地推理库加入另一个构建（仍外置权重）；默认构建包含云 Embedding+OCR 运行库，此开关的额外体积与加载需独立实测。选本地 Provider 但无对应库/完整外置目录时明确启动失败，不改为云 Provider。

build-receipt.json 记录实际版本、体积、源码和EXE哈希用于追溯，不作为跨组件版本相等门禁。目标机无需 Python 或 Node；Node 仅在构建期安装锁定依赖并校验 API/TypeScript，设置页资源位于 `_internal/settings-ui`。Windows DPAPI 使用系统 DLL 和 Python ctypes，不引入密钥框架。包不带 PostgreSQL/Redis 安装器。首次/后续启动重复次数、故障矩阵、原页/OCR启禁与内存预算由 T189 正式验收，不以构建成功宣称系统交付通过。

构建依据：[PyInstaller spec 文件官方说明](https://pyinstaller.org/en/stable/spec-files.html) 与[冻结运行路径官方说明](https://pyinstaller.org/en/stable/runtime-information.html)。业务验收以本项目运行证据为准。

冻结构建会在独立 work 目录转换 Gradio 的 create_or_modify_pyi，仅跳过运行时的 IDE `.pyi` 文件生成；原安装库与源码运行路径保持原样，组件校验、事件和 API 信息保留。转换工具 scripts/freeze_gradio.py 进入构建回执；当前固定 Gradio 6.26.0，升级该依赖时须重新审查此转换并运行真实组件差分与冻结启动验收。

同一冻结副本还为 get_api_info 建立本次调用的组件 ID 索引，替换逐输入/输出的全表扫描；重复 ID 保留首项，缺失组件仍跳过原端点，API 信息与可见性保持。完整生产工作台双模式逐值差分和原 UI 回归用于核对；升级 Gradio 时连同类型提示转换重新审查。

## 本机设置入口

双击 EXE、同目录 Configure-EduAgent.cmd，或运行 `EduAgent.exe --configure`，均使用新的设置页。控制窗口提供打开设置、重新授权和退出；授权码只经本机控制通道交给浏览器，不写日志。`--no-browser --no-window` 可用于受控自动验证；退出只停止本 Launcher 创建的 worker，浏览器关页不会退出应用。

升级前按项目备份流程保护业务数据和配置，秘密单独管理；不导出明文 env 作为回退。包的构建身份只用于追溯，实际初始化、重启、DPAPI、恢复及退出仍需分别验收。
