# T161 真实标题分块的合成管道参考

版本：synthetic-python-basics-heading-v3-20261002。原稿和查询逐字复用父目录；30 个参考块来自当前生产 parse/clean/chunk，每块属于真实叶节，不再使用原 21 个跨节块。参考键 heading-00 等仅是标注文件内身份，摄取后的 document_id/chunk_id 必须为实际数据库 UUID，仍严格核对每块序号及原文。

annotations.json 是本批按查询与实际节内容明确登记的 AI 规则映射，teacher_verified=false，教师身份/标注时间及 confidence 均未知。它只用于既有真实 PostgreSQL/manifest/四模式/可重复性管道检查，不是教师 Ground Truth，不证明检索准确率，不填充 T146/T160/T168；不会将旧合并块的标签扩散给所有拆分块。复合问题只映射实际提供相关知识的节，不证明原稿覆盖提问全部细节。

旧父目录原稿、chunks.json、annotations.json 和历史 results 全部保留原值。初始化 CLI 默认使用本版本；--corpus-dir 仍支持显式选择其他已经按当前分块复核的输入，原旧参考因粒度不同应明确拒绝。旧离线 Benchmark 的固定 UUID/格式不改；新旧分块的指标不可直接比较。manifest 格式不改，每次输入摘要包含实际内容/标注/分块配置。
