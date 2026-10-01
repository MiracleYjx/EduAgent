# T146 待教师复核样本包

状态：**draft_pending_teacher；不能作为正式基线**。用户已确认先备齐样本包，T146 暂不勾选。dataset_version=v2-draft-20261001-1，annotation_version=teacher-pending-1，protocol_version=v2-evaluation-1。

由 Codex 在本批创建固定合成原稿/夹具；原文件真实存在，未调用 OCR/LLM 生成运行结果。扫描 PDF 是原稿栅格化后的无文字层文件，**不是实拍扫描采集**。教师身份、标注/复核 UTC 和正式真值均为空；annotation_suggestions.json 只保存 AI 草稿建议，不得用作教师 Ground Truth、填准确率或教师独立统计。

## 1. 文件与用途

| 文件 | 真实内容 |
| :--- | :--- |
| [paper_text.pdf](inputs/paper_text.pdf) | 1 页、3 题；C/A/B/D 原选项顺序、三角形/表格、原稿未附答案/解析；保留源信息，不自动补写。 |
| [paper_scan.pdf](inputs/paper_scan.pdf) | 1 页原稿图像 PDF，需 OCR；全部像素来自本包原稿的 150 DPI 栅格化。 |
| [paper_image.png](inputs/paper_image.png) | 同一原稿的实际图片输入。 |
| [paper_mixed.pdf](inputs/paper_mixed.pdf) | 2 页，第一页文字、第二页图像；题号 1–6；原稿两页各自页脚如实保留。 |
| [paper_cross_page.pdf](inputs/paper_cross_page.pdf) | 2 页、同一第 4 题跨页；真实来源为两页，图为第二页流程图，未知校正边界可保留 null。 |
| [workload_10_pages.pdf](inputs/workload_10_pages.pdf) / [workload_50_pages.pdf](inputs/workload_50_pages.pdf) | 固定中间规模 10 页/20 题、上限 50 页/100 题；1 页工作负载直接使用 paper_text.pdf（3 题）。 |
| [workload_51_pages.pdf](inputs/workload_51_pages.pdf) | 51 页/102 题的超限业务输入，应拒绝而非截断；不计正常质量分母。 |
| inputs/damaged.pdf | 故意截断的损坏输入，解析失败是本文件的设计用途，不是有效可读 PDF。 |
| assets/、assets.json | figure/table/diagram 清晰原图、另生成低分辨率流程图；实际像素尺寸、稳定夹具 UUID 与源页/题号绑定。UUID 不是已登记服务 file_id。 |
| teaching_basis.md、parent_questions.json、semantic_cases.json | 固定教学依据与父题；新题/改编两条路径，四类问题/无问题及旧答案失效/依据不足。 |
| question_pool.json、assembly_cases.json | 300 个待核对候选夹具；实际题型、选项、答案、默认 1.00 分、交叠知识点/题图。Approved 仅为未来隔离夹具的目标状态，无实际教师批准。1/25/100 题性能请求、200 题原准入边界及具体可满足组合/不可满足证明。 |
| scoring_cases.json | 0.005 → 0.01 的 HALF_UP 边界、默认分值、正/负尾差；teacher_confirmed_points 为空，不得自动确认尾差。 |
| statistics_cases.json、feedback_cases.json | 同题两场不同满分，最终/合法零分/待复核/失败/缺依据/未提交，多知识点和推荐权限/资料不足输入。状态为场景标签，非新增业务枚举或真实学生数据。 |
| ui_delivery_cases.json | 三类代表页和七类业务页操作；实际 EXE/备份集/截图待 T188/T191 补齐，不假装已有运行。 |
| cases.json、annotations.json | 57 个固定 case_id、场景/动作/源引用；正式人工标签全部为空。 |
| annotation_suggestions.json | Codex 的算术/字段/问题分类建议；单独保留、teacher_verified=false，禁止默认为正式标签。 |
| teacher_statistics.csv | 独立教师核算的待填表；值、分母、身份和时间为空，不把 AI 建议移入。 |
| source_images.json | 实际 PNG 像素尺寸、作者/来源、渲染 DPI（可知部分），不编造物理扫描设备。 |
| .gitattributes | 本包文字文件固定 LF；PDF/PNG 按二进制保存，跨平台检出保留清单摘要。 |
| templates/ | run JSON、case/metric/timing CSV 头、空 failures.jsonl、环境模板；结构示意，不是实际实验产物。 |
| manifest.json | 版本、完整 ID/分层数量、实际文件字节/页数/摘要、计划重复次数与缺失项；ready_for_formal_baseline=false。 |

## 2. 教师标注操作

1. 独立阅读原 PDF/图片/教学依据；先完成标注，再看系统自动输出。AI 建议可作为待检查内容，不能替代独立判断。
2. 每个 annotations.json 条目填写真实 teacher_id、annotated_at（UTC）、labels 及证据/理由；复核人和复核时间按实际填写，不伪造第二位标注者。仍未知保持 null 并说明原因。
3. 导入 labels 保存逐题：题号/题型/题干、原选项及顺序、原答案/解析/金额/知识点、原文件/真实页号、题图/顺序；source_regions 未知可 null 并说明。已知原稿缺答案用字段 known_missing，未知用 unknown，二者不混成“正确 null”。后续补全答案与原稿提取真值分开。
4. 语义 labels 对 answer_correctness、condition_sufficiency、option_ambiguity、rubric_clarity 四类分别 true/false/null；正类为“有该问题”，逐项理由/依据。父题与本次实际教学依据分别核对。
5. 图片 labels 为逐条关键条件、来源资产/页、可判定性和需要人工核对的原因；未知条件不标确定值。文件存在/模型自信不等于理解正确。
6. 组卷 labels 核对候选资格、课程、实际组合、分值/题型/覆盖和不可满足证据；本包 witness 只是 AI 建议。人工替换/移位、显式改分、旧 NULL 约束及失败后重启按 cases.json 动作核对。
7. 教师独立按 statistics_cases.json 输入计算人数、最终均值/分布、逐题分子/分母、多知识点归属及无有效样本。填写 teacher_statistics.csv，并在对应 annotations 条目绑定该独立核算及真实身份/时间；不能从待验系统或 AI 的统计结果抄写真值。
8. 正/负尾差的最终要点分值与理由须教师真实确认，合计本场满分；null 表示尚未确认，不得自动按第一个要点调整。

规范化仅统一换行、去首尾空白；金额 Decimal、标签去重精确集合、选项/页/题图按顺序；其他语义等价由教师说明。争议先复核，仍不能判定保留未知；一对一匹配，重复输出不能重复命中同一原题。原稿缺失信息、原始选项错序、自动输出快照均保留。

## 3. 固定工作负载与后续执行

- 导入 1/10/50 页，组卷 1/25/100 题，候选 300；中间规模与候选规模在执行前已明确。200 题仅检查旧请求边界，不改成性能验收目标。
- 正常导入和故意损坏/超限/OCR关闭/Provider失败分表；模拟失败只证明业务错误传播，真实 Provider 失败证据另留。
- 模型质量：同一正式教师标签/配置全量 3 轮，不挑难例/失败补跑。
- 导入/组卷性能：每个明确工作负载 3 冷/5 暖；暖态先一次同规模预热并标 warmup。每次新实例/相同初态；数据库/Redis预先就绪，不删除用户或系统缓存。
- 校正选择 IMP-TEXT/IMP-SCAN 与 IMP-CROSS 的纯文字、带图、跨页切换；界面真实渲染计时，不能以接口耗时替代。
- EXE：3 个独立首次状态副本、5 次已初始化后重启，真实包/配置/迁移/readiness/打开页面均须完成。当前 package_ref=null，不开始计时。
- 正式 run_id、environment/config、实际 Provider/模型/Prompt、调用数/超时/费用在执行时真实记录；本包不预填“已调用”信息。
- 本次没有执行这些运行，没有准确率、耗时、资源、Gate 通过记录。M0 构建/配置问题仍按 T143 处理。

## 4. 真值补齐与版本冻结

T146 继续未完成：0 个真实教师标注；教师独立统计未填；物理扫描来源未备齐；真实 Provider/EXE/恢复/浏览器产物属于后续任务。补齐真实教师输入后再核对 T142 全量覆盖、实际数量/ID和未知项；不能仅把 ready_for_formal_baseline 改为 true。

新增真实材料、修正输入/标签或变更清单必须另建数据/标注版本并说明差异，保留此草稿及历史结果；不能看输出后删难例/改标签。固定全量顺序后，按 T160/T168/T179/T189/T190 分阶段执行；质量阈值仍为 pending_baseline，由 T168 基线后供用户确认。

本包 JSON/CSV 是独立输入/证据结构，不是当前业务 API DTO；后续实施批次按已确认合同建立隔离夹具/适配，并先补具体 TCR。图片/候选身份不能当作已入库记录，模拟通过不当真实模型质量。

## 5. 本批核对

7 份有效 PDF、117 页均实际渲染；损坏 PDF 单列预期失败。检查中文字形、图/表/流程、跨页续文和工作负载首尾页；正式质量仍待教师标注。JSON/CSV 结构、全部源引用/摘要、57 个唯一 ID、300 个候选、请求组合/冲突和 Decimal 草稿算术经本地结构检查；这些检查不计作 OCR/语义准确率或系统验收。
