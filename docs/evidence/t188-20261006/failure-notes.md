# T188 原始失败与处理

第一次：包内迁移到真实完整 head 成功；UI 导入 safehttpx 时缺 `_internal/safehttpx/version.txt`。第二次诊断保留 source frame / FileNotFoundError.filename，不打印异常参数/凭据。补入 safehttpx 数据；读取 Gradio 元类实际源码发现组件加载会读源码文件，补入 gradio 的 Python 源码资源。随后真实报 groovy/version.txt 缺失，补入其数据。

临时诊断目录补齐后 API/UI 已200，但辅助读取父进程日志遇到 UTF-8 解码失败。冻结 Python 不采用 PYTHONIOENCODING，入口显式 reconfigure UTF-8 后由 build_exe.ps1 完整重建最终目录，再执行 t188-runtime.json。最终包不依赖手工补文件。

诊断编码失败时辅助脚本因尚未取得子PID先结束父进程，留下所属子进程和一个隔离数据库；按完整EXE路径、--child serve与本批bundle-test.env核对后停止，再确认数据库零连接删除。未停止无关服务或终止外部连接。

最终资源导入使用明确的本地受控空 ExtractionBatch，原页/PNG与真实 OCR 已生成/读取；题目阶段真实返回 Failed/PAPER_EXTRACTION_FAILED。其失败是有意的无题目输出，不能改写成成功导入或模型质量通过。辅助脚本最后按确切父子归属停止自己的应用进程，父进程传播 4294967295；这是资源冒烟清理，不是正常 Ctrl+C 系统验收。T187源码正常退出已测，T189冻结正常退出、故障、冷暖/资源仍待测。
