# T183 学生知识点反馈页面证据

基线7631cef，范围仅T183；T182独立提交7631cef已推送。接通本人考试、逐题答案/授权原图/评分解释/失分、当前最终发布标签归因及资料/已审核练习来源查看；使用T181每次重新核对的上下文授权。当前处理状态和错误来自真实结果/最新Workflow，分类复用T180规则。持久诊断的Ready/Stale/Failed与当前确定性事实分开，无模型调用、无新表/迁移/依赖。

验证：105 passed、5既有Gradio/Starlette警告（96.64s）；之后修复无本人答卷时旧M3占位，最终受影响60 passed、5既有警告（34.79s），两组重叠不相加。最终新增14例（11真实PG/Gradio、3纯投影）；全backend Mypy213源文件及10变更Python文件Ruff/Black通过。所有中间RED/夹具/断言失败及静态结果保留；TCR §38登记必要性与修正原因，旧用例/断言不改。

operations.md记录实际浏览器步骤，t183-real-service-feedback.json保存实际持久读模型；界面与数据采用明确合成账号及开发者夹具，不代表教师/模型质量。T146 AI辅助+开发者审查、独立教师0，T168 not_met保持；本次不勾T184，不声明SC-013完整通过。

所有用户既有README.md、README.zh-CN.md、grading_samples.json及.env字节相同；零云调用。学生服务端每次授权读取原件bytes，以内联data URI呈现，不经公共Gradio文件缓存、不携带JWT URL、不开放源卷/答案整页。退出/重评/权限变化的拒绝与清空均实测；本批独占数据库0023零连接、0临时schema后删除，原库未作为清理目标。回执见t183-cleanup-receipt.json。

原始pytest日志包含输出自带的行尾空白，按原样保留。Git文本会按各平台换行转换；为保留测量原件字节另存raw-evidence.zip，sha256.json核对压缩包及其内文件原件，不用于组件锁步或运行门禁。源代码/手写文档的diff whitespace检查通过；生成日志/原始JUnit单独保留。
