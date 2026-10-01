# v2.0 试卷导入契约

## 范围与依据

对应 [spec.md](../spec.md) FR-041、FR-042、FR-043 和 [plan.md](../plan.md) §8、§9；实体和事务约束引用 [data-model.md](../data-model.md) §7.1–§7.4、§8.1、§13。
这是待实施的 v2.0 契约，不表示接口、OCR 或数据库迁移已经完成；v1.0 知识库接口保持兼容。
知识库资料产生教学依据与检索片段；试卷产生待校正原题，不能因上传或 OCR 成功自动进入知识库索引或可发布题库。

## 输入、输出与授权

- 输入：multipart/form-data 的 `file`、`course_id: UUID`；接受文字 PDF、扫描 PDF、PNG/JPEG 试卷图片，按实际内容验证格式。一份图片文件按一页处理。
- 上传者由认证上下文取得，保存为 `uploaded_by`；客户端不能指定他人身份。沿用课程管理/题库权限，教师只能操作有管理权限的课程。
- 单次任务一份原文件，最多 50 页；超限明确拒绝，不能截断为前 50 页并报告成功。超限时 page_count 不写入模型禁止的值，实际发现页数记录于失败诊断。格式无法解析与 OCR 不可用分别报告。
- 输出逻辑对象：`PaperImport`、`SourcePage[]`、`ExtractedQuestion[]`。首次响应可没有页/题；空列表只表示尚未产生结果，不能代表入库完成。
- 原文件、页图通过 [file-storage.md](file-storage.md) 的授权文件标识访问，不向客户端返回本机绝对路径或要求客户端读取服务器目录。
- 查询返回实际阶段、已产生页/题数量、错误码/中文原因与 UTC 时间；未知页数为 null，不捏造进度百分比。
- 学生不能访问导入管理接口、未校正题或包含答案的原卷；考试题图通过所属考试权限访问。

## 结构化对象边界

所有请求/业务响应经 Pydantic 校验；UUID、枚举、金额、跨课程关系及状态必须明确验证。

| DTO | 主要字段与语义 |
| :--- | :--- |
| PaperImportView | id、course_id、uploaded_by、document_id、original_filename、page_count、status、error_code、error_message、created_at、updated_at、original_file_id、pages、questions；页/题列表可按下列专用接口读取 |
| SourcePageView | id、paper_import_id、page_number、file_id、width、height、ocr_text、ocr_confidence；页号从 1 开始，按页号排序；未走 OCR 的两个 OCR 字段为 null |
| ExtractedQuestionView | id、paper_import_id、source_page_ids、question_type、content、options、reference_answer、scoring_rubric、score、status、correction_notes、extracted_by、extraction_confidence、question_id、created_at、updated_at，以及下述校正扩展 |
| CorrectionPayload | action、题型/题干/选项/答案/Rubric/分值、source_page_ids、correction_notes，以及 question_number、analysis、knowledge_points、source_regions、assets、image_assessment |
| CommitRequest | question_ids：非空、去重的暂存题 UUID 列表；只确认指定题，不默认确认未展示的全部题 |
| CommitResponse | paper_import_id、status、questions：每项 extracted_question_id、question_id、question_status、completion_status；completion_status 为 complete 或 needs_completion，后者保持正式题 Draft |

- `action` 默认为 edit，可为 edit 或 reject；edit 保持 Pending Correction，reject 必须附非空理由并进入 Rejected。客户端不能直接写 Corrected、question_id、上传者或导入状态。
- 省略的 PATCH 字段保持原值；null 仅能清除允许缺失的字段。金额是正数、最多两位小数的十进制字符串；未知可为 null，确认入库前必须由教师明确分值。
- `question_type` 使用既有题型枚举，扩展首版限单选、判断、简答；选项沿用题型现有 JSON 形状，明确顺序和标签，不能排序后改变答案对应。
- `extracted_by` 真实记录 OCR、LLM 或 TEXT；未知置信度为 null，不以 0 或模型自报置信度证明准确。
- 校正扩展：question_number 为原题号（未知为 null）；analysis 为解析（未知为 null）；knowledge_points 为课程内知识点列表（未知为 null）；source_regions 为 [{source_page_id, bbox}]（可靠边界未知为 null）。
- bbox 使用原页像素坐标 [x0,y0,x1,y1]，必须在所属页范围内；跨页来源全部属于本次导入，按页号排列，不允许关联其他导入补位。
- assets 为待转正式题的原图关联列表：file_id、asset_type、source_page_id、region、caption；最多 5 张，必须来自本次导入的可靠文件及原页，不能用客户端任意路径建立关联。
- image_assessment 引用 [vision-capability.md](vision-capability.md) 的理解/人工核对结构；理解失败保留图像和问题，不把 OCR 文本视为图示已理解。
- 校正扩展与暂存题一起持久保存，重新打开仍能读取。T136 已在 [data-model.md](../data-model.md) §13 定义题号、解析、知识点、来源区域和暂存资产的具体承载与转入规则，不能只存在于 UI 内存或塞进 correction_notes。图片理解/人工核对的完整持久结构由 T138 补齐；本契约不宣称目标字段或接口已实施。

## T136：校正持久化与正式题解析边界

字段、JSON Schema 和空值含义以 [data-model.md](../data-model.md) §13 为唯一模型定义：

| 校正字段 | 存储与消费 |
| :--- | :--- |
| question_number / analysis | ExtractedQuestion 独立可空 Text 列；题号保留原文字/前导零，解析独立于答案/Rubric/备注 |
| knowledge_points | 可空 JSONB 规范标签数组；复用既有标签校验，null 表示尚未登记/未知，[] 为明确空列表 |
| source_regions | 可空 JSONB SourceRegion 数组；null 表示可靠边界未知，[] 表示仅页级来源，无局部框 |
| assets | 可空 JSONB StagedAsset 数组，最多 5 项；null 为关联待核对，[] 为不关联题图；顺序和服务分配 id 持久保存 |
| Question.analysis | 正式题独立可空 Text；确认入库按当前暂存值映射，不生成或借用其他字段填充 |

- SourceRegion = {source_page_id, bbox:[x0,y0,x1,y1]}，像素原点为持久原页图左上角，x 向右/y 向下；有限数值、正面积且在页尺寸内。引用页必须在本题 source_page_ids 中并属于本次导入；跨页按页号、同页按明确阅读顺序。OCR/PDF 坐标由生产者转换，未知不能猜归一化比例或虚构整页框。
- 原页已真实核对且其他确认条件满足时，source_regions=null/[] 允许确认；source_page_ids 仍须真实非空。缺题号/解析/知识点不单独阻止入库，缺失保持真实，不把非法框、缺文件或跨导入引用当成合法未知。
- StagedAsset = {id, file_id, asset_type, source_page_id, region, caption}；id 为服务生成 UUID（创建时客户端省略，编辑仅使用本暂存题已有 id），file_id 为服务端稳定授权标识，不是路径。source_page_id 必须在本题页来源中；region 为空或使用同一原页像素 bbox，asset_type 沿用 figure/table/diagram。文件必须可靠且同来源，caption 不代替条件核对。
- 校正响应返回已保存的资产 id/顺序；commit 将 id 沿用为正式 QuestionAsset.id，数组顺序映射 order_index=1..N，尺寸来自实际题图，文件定位/资源映射消费 T137 的统一文件服务。不能把 JSON id 的存在当成正式资产已入库，也不能用含答案整页绕过学生文件访问限制。
- PATCH 省略保持原值，允许缺失的扩展字段可显式 null，数组字段 [] 为整体清空；数组提供时整组替换、完整校验后提交，不隐式合并/截断。页来源变更时必须同次处理不再属于该页集合的区域/资产，否则拒绝。客户端不能指定生产者、正式题/审核状态或他人资产身份。
- commit 前题图关联已核对，无图显式 []，有图为可靠的 1–5 项；assets=null 保持待校正。必要图像条件未确认时可依既有规则保存 Draft/待补全，不能宣称图像理解成功或直接批准。image_assessment 的真正持久承载、与这些稳定资产的关联和失效由 T138 确定。
- 批次确认在原事务中一并保存正式 Question.analysis/知识点、QuestionAsset/图序/真实文件绑定以及 question_id/Corrected；任一失败回滚整批。暂存 null 知识点沿 v1.0 转为正式 []（尚未登记标签），原始未知留在暂存来源；原题号/边界仍通过 imported_extracted_question 回溯，不写成考试题序或重复来源副本。重复 commit 只返回原正式结果，不能覆盖正式题后续编辑。
- 正式 analysis 进入教师详情/编辑/审核，变化或清空递增 §12 的 validation_revision；Approved 直接改解析返回 QUESTION_APPROVED_IMMUTABLE，发布/历史引用后按既有保护拒绝原地变更。解析 null 仍须冻结为当时真实状态，不在学生作答输出中提前公开；历史题新增解析默认为 null，不从答案/备注/Trace 回填，不改变历史批准和结果。
- Corrected/Rejected 的扩展记录为终态原导入依据；后续正式题修订不反写暂存解析/区域/资产。新解析和题图排序遵守审核/发布生命周期，不引入完整题目版本或内容快照。

## 接口

| 接口 | 请求与成功响应 | 约束 |
| :--- | :--- | :--- |
| POST /api/paper-imports | multipart 上传；201 返回 Uploaded 的 PaperImportView，pages/questions 可为空 | 先持久保存原文件，创建 purpose=paper_source 的 Document 和关联任务；响应后由导入编排继续处理，不能返回虚假的 Ready |
| GET /api/paper-imports/{id} | 200 返回任务、页列表及真实进度/失败信息 | 失败任务仍能查看可用原文件/中间结果；记录存在但原文件丢失须额外报告 FILE_MISSING |
| GET /api/paper-imports/{id}/questions | 200 返回暂存题列表与校正扩展 | 只读提取结果；包含逐题状态及已确认的正式题 ID |
| PATCH /api/paper-imports/{id}/questions/{qid} | CorrectionPayload；200 返回更新后的 ExtractedQuestionView | qid 是 ExtractedQuestion.id；校验确实属于 id，Pending Review 导入下的 Pending Correction 题才可编辑/拒绝 |
| POST /api/paper-imports/{id}/commit | CommitRequest；200 返回 CommitResponse | 教师核对后创建/返回正式题；入库确认不等于审核批准 |

新接口的业务错误沿用 I01 风格；请求 Schema 错误保持 FastAPI/Pydantic 既有 422 detail 列表，不改写 v1.0 全局错误协议。

~~~json
{
  "detail": {
    "code": "PAPER_CORRECTION_INCOMPLETE",
    "message": "请先核对题干、题型、选项、分值和来源页，再确认入库。",
    "current_status": "Pending Review"
  }
}
~~~

## 状态机

~~~text
PaperImport:
Uploaded -> Parsing -> Extracting -> Pending Review -> Ready
Uploaded / Parsing / Extracting / Pending Review -> Failed
Pending Review -> Rejected（教师拒绝整卷，或全部题被拒绝）

ExtractedQuestion:
Extracted -> Pending Correction -> Corrected + Question(Draft)
                         \
                          -> Rejected
~~~

- Parsing 确定页面、持久页图和每页解析路径；文字 PDF 优先文本提取，扫描/不可靠文字页走 OCR。混合 PDF 按页选择，不能以部分文字层替代扫描页处理。
- Extracting 执行 OCR/文字提取、拆题、结构化校验；待教师处理前暂存题进入 Pending Correction，导入进入 Pending Review。没有可识别题目明确失败，不把零题任务自动视为 Ready。
- Uploaded 只证明原文件已保存；Document.Ready 只证明原文件元数据就绪；两者均不代表人工校正确认。
- commit 校验题干/题型/选项/来源页/图片关联与分值，允许缺答案或缺 Rubric 的原题入库为 Draft/待补全。不得自动 Approved，也不得自动生成未给出的标准答案。
- 全部暂存题为 Corrected 或 Rejected 且至少一题已入库，才进入 Ready；全部拒绝则 Rejected。已有入库题时不能拒绝整卷并删除正式题，只能处置剩余暂存题。
- Ready、Failed、Rejected 为任务终态；Corrected、Rejected 为暂存题终态。Corrected 之后编辑正式题须走题库修订/审核规则，不反写原导入校正依据。
- 重新解析创建新的任务/Document 尝试，保留失败来源，不自动无限重试、覆盖旧结果或复用同一 Document 违反一对一关联。

## 事务、重复请求与失败

- 原文件可靠落盘后才记录 Uploaded；数据库写入失败不得返回已接收。中间页图/暂存题保留所属任务与真实阶段。
- 同一 PaperImport 的校正/commit 串行化事务检查。指定批次中有不存在、越权、Rejected 或未就绪题时整体拒绝，不出现“部分题入库却整批宣称失败”的响应。
- 正式 Question、来源关联、QuestionAsset、ExtractedQuestion.question_id/Corrected 同事务提交；任一失败回滚该批次，保留已保存原文件。
- 已 Corrected 的题重复 commit 返回既有 question_id，不复制创建；Ready 任务只允许对既有入库结果作同义读取，不能改变终态或增加题目。
- 技术管线失败进入 Failed；校正未完成、非法编辑或 commit 校验失败返回明确业务错误并保持原状态，不把教师可处理的问题变成不可恢复技术终态。

| 错误码 | HTTP / 状态 | 含义 |
| :--- | :--- | :--- |
| PAPER_PARSE_FAILED | 422（接收前）或查询到 Failed | 文件/页面无法解析，保留已可靠保存的材料 |
| PAPER_OCR_FAILED | 查询到 Failed | OCR 调用或结果校验失败；保留底层错误，不伪称文字提取成功 |
| PAPER_EXTRACTION_FAILED | 查询到 Failed | 拆题、结构化或题目生成持久化失败 |
| PAPER_TOO_MANY_PAGES | 422 或查询到 Failed | 超过 50 页，不截断 |
| OCR_PROVIDER_NOT_READY | 查询到 Failed | 扫描页需要 OCR，但依赖/配置未就绪；保留失败阶段，区别 OCR 已调用后失败 |
| PAPER_CORRECTION_INCOMPLETE | 409 | 确认条件未满足，保留 Pending Review |
| PAPER_STATE_CONFLICT | 409 | 非法状态变更或编辑终态暂存题 |
| FILE_MISSING | 404（文件读取）或查询中的诊断 | 数据记录存在，但原文件/页图/题图缺失 |

异步任务错误由 GET 的 200 响应中 status/error_code/error_message 表达，不把 HTTP 200 当成处理成功；认证/授权和不存在资源分别使用既有 401/403/404。
error_message 保留真实步骤和中文可处理原因；不得回传凭据或本机路径。

## 验证与兼容边界

- 样本覆盖文字 PDF、扫描 PDF、混合页、跨页题、无答案题及带图题；校正后再入库，重启后原页与校正结果可读。
- 验证拆题完整性/字段准确性使用标注样本；置信度不等同于实测准确率。页面/图片数量限制不是性能承诺。
- 重复 commit、校正与 commit 并发、原页缺失、OCR 未就绪必须保留真实结果和来源；增加 PATCH 省略/null/[]、已提供框合法性/未知边界、资产身份/顺序、批次回滚、正式解析映射/失效/冻结的后续验证，不改 v1.0 知识库摄取或既有审核验收标准。
- T136 只补设计文档和本任务标记，不新增业务代码、迁移或测试；测试变更先形成 TCR，文档静态检查不等同于运行验收。
