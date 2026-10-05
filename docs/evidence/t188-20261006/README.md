# T188 默认 Windows onedir 构建证据

实际 Windows / Python 3.13.13 / PyInstaller 6.22.3 / hooks-contrib 2026.8。固定111项依赖，独立 final-build/runtime，pip check无冲突，由 scripts/build_exe.ps1 完整执行，不用源码运行替代最终包。

构建路径：.cache/t186-t188-20261005/final-build/dist/EduAgent/EduAgent.exe。最终目录回执统计 4144 文件、386877276 bytes（368.955 MiB），不含回执自身；实际版本见 build-receipt.json。源码提交基点 a2a6782f6259ca017b975493d1d539c3c8d9c211，source_dirty=true：T188在该提交后的工作区构建，源文件哈希逐项保存；不是伪称该基点已含T188，不产生运行版本相等门禁。

最终 EXE 在新建空隔离库执行全部迁移到0023_grading_exam_question，/ready与/gradio/均200，真实CSS/JS均200。单次资源冒烟20.065秒，不满足或替代T142的3冷/5暖完整协议。文字/扫描PDF实际渲染为1241×1754；两张PNG授权HTTP200，各157944字节。扫描页实际RapidOCR/ONNX CPU输出312字；confidence保持null，原权重外置且包中无onnx。

本地受控模型返回空题目，两个导入均真实Failed/PAPER_EXTRACTION_FAILED；只证明渲染/OCR/文件/结构调用资源可加载，不证明完整题目导入或模型质量。云调用0，不作为教师独立标注；T146 AI辅助＋开发者审查声明和T168未达标指标保持。清理后隔离数据库0连接且移除；无原业务数据变化。

35项启动器/原核心回归通过；部署mypy2文件、Ruff、Black通过。早期缺资源与编码失败见 failure-notes.md 和逐次JSON。T189完整启动/故障/正常退出/重启/图片/OCR启禁及6GB同时峰值预算仍待执行。默认包包含OCR运行库、云Embedding/LLM路径；本地sentence-transformers/Torch构建开关存在但未实测，不承诺该变体可交付。

初始 build_exe.ps1 输出为4082文件/384799658字节；真实 Gradio 启动生成其组件 .pyi 后，最终目录为4144文件/386877276字节（约369MiB）。回执对应实际最终目录，业务数据始终外置。
