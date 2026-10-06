# T191 真实课程闭环与同集恢复（2026-10-06）

本任务规定的真实业务执行、恢复和交付证据记录完成；v2.0总体验收未通过，T168质量、T189启动/资源及工作区旧样本契约失败不改。作者是会话委托AI、真实DEV角色及新合成学生，独立教师0；T146/T168的AI辅助＋开发者审查基准未变。

实际运行922.57MiB本地模型onedir EXE，--no-browser，完整既有BGE权重、锁定RapidOCRv5、DeepSeek文字/图像Provider；模型只收到自编合成教材/PDF/题图/答案。没有业务替身、来源伪造、直接SQL状态改写或跳过审核。原模型输出和显式AI补全分别保存；这是业务闭环证据，不能称自动提取质量通过。

## 回执阅读顺序

1. course/：实际PDF页、3题字段/原选项序、真实表格裁图和幂等入库，缺答案不审核；真实BGE资料和Chapter目录。首轮图片题真实语义失败保持。
2. course-continuation/：显式AI业务题面/过程/Rubric补全，当前真实Vision/语义通过；模型文字生成仍保持列表选项简写答案冲突。course-generation-continuation/保留观察者缺Chapter错误。
3. course-final/：实际列表正确选项“C. 7”补全，取得Pass后改错/改回均不能批准，新的真实核验后才Approved；真实parent改编。评分观察者旧base上下文409保持。
4. exam/、exam-complete/、student-grading/：真实比例评分，客观题漏依据发布409、未提前开放图导致学生409、发布后修改图409均保留；失败卷不删除、不解冻。
5. opened-adaptation/、final-exam/：新真实改编，明确补全后在发布前开放已核实475×350表格，再真实Vision/语义/审核；最终两个同题考试10分/5分。student读取原PNG、真实混合提交/评分，旧直接评分入口无Workflow复核409保持；没有将该待复核结果算最终。
6. workflow/：已Graded状态不能补绑Workflow的真实409保持。
7. **workflow-new-student/**：真实管理员创建合成学生、真实密码登录（令牌脱敏）、POST答案，直接启动当前生产Workflow；Paused→带实际轮次Modify→Completed，0/4＋2/6=最终2/10；师生分析/Ready诊断读取成功。旧答卷仍pending，教师统计2提交/1最终/1待复核，最终均分2.00。
8. **export/**：本次真实最终结果CSV，经生产导出服务写入/归属登记；教师原字节一致，学生403；未新增业务导出UI。
9. **storage/**：原停写backup CLI complete、restore CLI verified；32表/20文件已核对，补充观察者exams总分列错误保持，entrypoint冻结。
10. **storage-verification/**：仅修正观察者按ExamQuestion事实读取，原同集备份/恢复不重复。32表全行与20文件一致、C/A/B/D序保持、两场10/5及最终2/10一致；生产文件服务实际重新授权教师、学生裁图、3私有页403。恢复停写标记保留，未激活。
11. cleanup.json：所属EXE正常退出0、源/恢复0连接、原库0012、共享PG/Redishealthy、两README/开发者评分/.env逐字节相同；只删除本批空探针库。完整备份二进制与原字节在.cache，公开仅manifest/验证回执/合成CSV，不提交JWT或密钥。

本批活动入口在 benchmark/t191/；续跑必须显式指定旧回执且保留失败，不把补全覆盖原输出。storage_acceptance.py可对已完成的同一恢复集只读核对，使用--verify-existing-storage，不重新恢复。带图必须发布前开放、所有题型确认本场评分依据、Submitted答卷从Workflow入口启动；早期脚本错误不作为推荐使用方式。

七页UI复用docs/v2.0-ui-acceptance.md及T186真实操作/129项/10截图，业务源码本批未改；本批主要是实际包API/持久事实，未声明本批全部页面点击。EXE完整固定启动证据见T189，仍未通过。详见docs/validation-report.md、docs/release-checklist.md、docs/paper-import.md、docs/file-lifecycle.md、docs/exe-deployment.md；勾选本任务不表示平台质量/性能或发布已获验收。
