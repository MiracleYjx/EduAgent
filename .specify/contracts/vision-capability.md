# v2.0 图片理解能力契约

## 范围与依据

对应 [spec.md](../spec.md) FR-043、FR-047、FR-049，[plan.md](../plan.md) §10 及 [data-model.md](../data-model.md) §7.4、§9、§12、§15。
用于上传原卷的图示、表格和条件理解、人工核对，以及原带图题的显示/复用；AI 新出题仍限文字题。
这是目标契约，不宣称当前 Provider、模型、考试或阅卷已经支持图像；T138 仅补持久承载与衔接。

## Provider 能力声明

~~~python
class BaseLLMProvider:
    def supports_vision(self) -> bool:
        return False
~~~

- 是默认方法，不增加迫使既有文本 Provider 重写的抽象必实现方法；文本调用保持原接口。
- 能力针对实际配置模型与适配器，而不是供应商名称。配置 DeepSeek 或其他模型不支持图像时返回 VISION_NOT_SUPPORTED，不硬编码品牌例外。
- supports_vision() 为能力声明，不保证网络/凭据/本次模型响应成功；不得从一次文本调用成功推断图像能力。
- 未配置、未就绪、能力不支持、调用失败、理解不可靠是不同状态，不静默改成文本调用或自动换供应商。

## 图片输入与传输

| 对象 | 结构与约束 |
| :--- | :--- |
| VisionRequest | course_id、question_id 或 extracted_question_id（二者恰一）、images、task；业务身份由服务核对，不由模型生成 |
| ImageReference | asset_id、file_id、source_page_id（可空）、region（可空）、mime_type；绑定本题的真实暂存图/QuestionAsset，顺序按本题整组题图 |
| ProviderImage | encoding：base64 / url / local_path；value：对应内容；mime_type：真实图像格式 |
| images | 1–5 个题图；保留顺序、原图身份和来源，不把整份含答案原卷直接当作学生题图 |

- 公共请求使用 file_id，不接受任意本机路径或客户端提供的外部 URL。文件服务核对资源/课程权限和真实图像，服务端取得 ProviderImage；id/file_id/页来源必须与本题真实资产一致。
- 一次持久理解针对本题当前整组图片，调用前捕获 ImageInputRefs 和 context_revision，不通过只选一张图绕过其他必要图示；暂存 assets=null 或未知图序须先校正。无题图不发起伪造空调用。
- 首版文字上下文按模型 §15.2 纳入题型、题干、选项、答案、Rubric、解析、基准分值、图 caption 和真实导入来源定位；未知字段保持 null，不把未关联内容送入模型。
- Base64、URL 或本地路径由显式配置的 Provider 适配器按其实际支持选择；不要求每家适配器都支持三种。本机路径不会直接传给只能读取网络资源的云模型。
- URL 仅用于该 Provider 实际可访问、经过文件访问授权的传输方式；本机登录链接不能冒充远端可读地址。无可用传输方式返回 VISION_IMAGE_TRANSPORT_UNAVAILABLE。
- 传输内容只含本次允许理解的图像和必要文字条件；日志记录资源标识/实际 Provider 来源，不保存 Base64 全量内容、密钥或含答案源卷访问链接。
- bbox 沿原图/SourcePage 像素坐标约定；裁图到原页的映射由生产者保存。有源页但无法可靠映射时 evidence_region=null，不猜框；无源页使用实际资产原图坐标，不混用归一化坐标。

## 结构化调用边界

沿用已有签名，不新增绕过校验的自由文本图片接口：

~~~python
result = await provider.generate_structured(
    messages=messages,
    schema=VisionResult,
    model=model,
)
~~~

- Vision 应用服务检查能力、解析授权图像并构造消息。messages 兼容既有纯文本消息；图片消息含 text/image 内容块，image 携带 ProviderImage。
- Provider 负责把统一消息内容转换为具体 SDK 的 Base64/URL/路径参数，业务不直接调用供应商 SDK；文本 Provider 收到图像必须明确拒绝。
- VisionResult 为 Pydantic 模型；页/资产/坐标与本次实际输入对应，由服务核对，不信任模型自由生成的持久 file_id/asset_id。
- 输出通过结构化校验只证明格式合法；还须核对原图、条件充分性和语义问题，不能凭校验成功直接批准题目。

| VisionResult 字段 | 含义 |
| :--- | :--- |
| observations | 可观察图示/表格内容：image_index、kind、description、可获得的 bbox；image_index 从 1 开始，对应本次输入列表 |
| conditions | 识别出的条件：image_index、text、evidence_region（可空）；不凭空补原图没有的数值/关系 |
| unresolved_issues | 不清楚文字、无法辨认条件、矛盾或缺失信息及真实原因；可可靠获得的图片索引须与输入相符，整组/未知归属保持未知 |
| requires_manual_review | bool；有不确定或缺失信息时必须 true；false 也不代替教师核对/批准 |
| provenance | 服务附加实际 Provider/model/调用来源；默认 null，模型不生成，未调用/未知不得从配置伪造 |

模型输出与服务持久结构有明确责任边界：服务按 image_index 绑定真实 asset_id，并为条件/问题生成 condition_id/issue_id；问题的持久形状为 {issue_id,asset_ids或null,message}。这些附加身份不作为模型必答字段，完整 Schema 以模型 §15 为准。

## T138：持久承载与人工核对

- ExtractedQuestion.image_assessment 与 Question.image_assessment 为可空、受校验 JSONB；同一 ImageAssessment 保存 context_revision、runs、manual_checks、imported_review。跨图结果只在题目保存一份，通过稳定资产身份关联，不新增结果表或在每个资产复制一份。
- ImageUnderstandingRun 在真实归属校验后、能力/传输检查前登记 running（未取得的尺寸/格式保持 null），保存启动修订号、run_no、输入引用、实际执行组件/发起者、Provider 来源与 UTC；结束为 completed 或 technical_error。completed 只是合法输出，不等于条件已确认。技术错误保存原 code/message/stage/retryable/cause，不填空成功条件或伪造已调用模型。
- 原机器结果、requires_manual_review、问题和错误在轮次结束后不可改写；人工解释/修正只追加 ImageManualCheck。短期 AgentRun/Trace 不能替代持久业务事实。
- 人工命令 ImageManualCheckRequest = {expected_context_revision,expected_run_no,expected_check_no,status,confirmed_conditions,image_findings,issues,issue_resolutions,explanation}；客户端条件/新问题不指定持久 id，服务生成并返回；可引用真实 source_condition_id/已登记 issue_id。完整约束见模型 §15.4，不新增本任务范围外的 HTTP 路由。
- status 为 confirmed/unresolved；每张图须有 conditions_confirmed/no_conditions_needed/unresolved 及具体理由，确认条件绑定 asset_id 和真实证据区域（未知可 null）。无额外必要条件也须说明，不能用空数组或单个布尔值替代核对。
- 服务附加当前认证管理教师 teacher_id 和真实 checked_at（UTC）；explanation 非空。问题须逐项解决或明确未解决，后续核对须逐项处理核对时已登记的同一上下文机器问题及未解决教师问题；原模型问题/技术失败保留，教师否定模型问题也须说明依据。
- pending 为无当前核对时的投影，不生成虚假教师/时间。最新核对 unresolved 不回退旧 confirmed；当前修订的最新调用仍 running 时不能确认未知输出。可靠人工核对可独立于模型完成，保留未调用/失败事实，不能把它宣称成机器成功或语义核验 passed。
- 请求仅提交人工命令，不接受完整 ImageAssessment、runs、provenance、教师身份/时间、计数或 imported_review 的替换/清空。expected_* 与锁内当前计数不符明确返回 IMAGE_ASSESSMENT_STALE，不能把旧页面的核对静默套到新输入。

## T138：失效、转入与批准

- 图像/顺序/定位或实际文字上下文变化同事务递增 context_revision，旧记录保留为 stale；改动后改回原值也不能恢复旧核对。已确认条件变化不递增图像输入计数，但按模型 §12/§15 推进 Question.validation_revision，使旧语义报告失效。
- 新图像轮次使旧核对不能覆盖新输出；当前核对按最新 check_no、当前 context_revision 和当前最大 run_no 判定。迟到结果只补齐其原轮次的实际结束事实，不替代当前条件或推进题目状态。
- 调用启动/完成、图片/文本编辑、人工核对、入库和批准在既有业务锁内核对输入/状态；外部调用期间释放锁。不另建锁系统、成功缓存布尔值或完整题目版本。
- commit 仅在同图、同序、同定位、同实际上下文且当前 confirmed 时，在正式题建立 ImportedImageReview 绑定原暂存核对事件；保留原教师/核对 UTC，bound_at 只表示关联时间。正式题的 runs/manual_checks 初始为空，不伪装成新调用/新核对。
- 不适用或未核对时保留可靠题图与原历史，入库仍是 Draft/needs_completion。绑定不能代替正式题语义核验；后续正式输入/调用/核对变化后旧绑定失效，不反写原暂存证据。重复 commit 不覆盖正式题后来核对。
- G02 语义核验只读消费当前核对，模型调用记录属于 QuestionValidationResult；预览/考试/阅卷只读使用冻结核对，阅卷自身调用沿既有 GradingResult/Trace 规则。它们不自动新建图片理解轮次，避免消费证据的操作使证据自身失效。
- QuestionValidationResult 的题图 Evidence 保存实际已确认条件与 image_review_ref（owner_kind、owner_id、check_id、binding_id），绑定真实持久事件；caption、模型布尔值和临时 UI 状态不能作批准依据。
- 必要图片条件未经当前可靠核对不得批准。Approved/发布及历史引用保护覆盖新调用、人工核对和条件变更；受保护原题不能就地改核对依据，改编创建新候选重新核对。
- Corrected/Rejected 暂存题不接受新调用或人工核对；终态前已登记的 running 轮次只允许补齐真实历史结束事实，不改冻结证据或转入正式条件。历史 image_assessment 保持 null，不从 caption/OCR/备注/Trace 回填；既有 Approved/发布状态和结果不改写，展示历史核对未知。

## 失败与人工接管

| 错误码 | 行为 |
| :--- | :--- |
| VISION_NOT_SUPPORTED | 未声明图像能力；明确返回不支持，保留原图及人工核对入口 |
| VISION_PROVIDER_NOT_READY | 依赖、凭据或配置未就绪；保留真实原因 |
| VISION_IMAGE_TRANSPORT_UNAVAILABLE | 适配器无可用的授权图像传输方式，不偷偷改发文本 |
| FILE_MISSING | 题图丢失；不替换为旧图或其他题图，不能完成可靠人工确认 |
| VISION_CALL_FAILED | 调用失败/超时；保留底层错误及实际阶段，实际取消同样保存真实原因 |
| VISION_OUTPUT_INVALID | Pydantic 或请求/输出对应校验失败，不输出成功理解结果 |
| VISION_REVIEW_REQUIRED | 调用已完成但条件不足/不可靠，或当前必要条件未核对；保留问题，不是成功批准 |
| IMAGE_ASSESSMENT_STALE | 409；人工请求计数已过期/输入变更，保留历史与当前状态并提示重新核对 |
| VISION_STATE_CONFLICT | 409；非可操作题状态、当前调用未结束等冲突；不伪造结束或绕过冻结 |

业务操作错误沿既有 I01 detail.code/message/current_status 风格；请求 Schema 错误保持 Pydantic 422，认证/授权/不存在保持 401/403/404。已登记轮次的执行错误持久化后查询可返回 200，但 outcome/error 表达真实失败，不把 HTTP 200 当作理解成功。

- 无能力/理解失败不阻止可靠保存原图；导入可保留 Pending Review，让教师核对原页，不一定等同于整个试卷解析失败。
- 若可靠人工核对已给出必要条件，题目可继续语义核验和审核；没有可靠条件时不自动批准或评分为零。图片/条件变更同步重新核验题干、答案、解析与 Rubric。
- 阅卷使用已核对条件及授权原图；客观题仍走规则评分。主观题需要图像时先检查 Provider 能力；不可用时显式待处理，不以只发 OCR 文字制造成功。

## 访问、排除项与验证

- 预览、学生答题、阅卷及结果展示引用同一题图关系，访问继承课程/考试授权；学生不可因此取得含答案源卷、核对 JSON 或别人的答卷。
- 原页只读、题图更新、发布冻结和统一 file_id 遵循 [file-storage.md](file-storage.md)、[exam-assembly.md](exam-assembly.md)；物理迁移不改变图像内容/身份或核对资格。
- 不实现图文 Embedding 微调、多模态训练、新题图生成或模型自动发布，不强制更换现有 LLM Provider。
- 后续验证支持/不支持两类 Provider、多图对应、不可读图像/坐标映射、非法输出、真实教师接管、条件/问题与 UTC、A→B→A/迟到调用、语义失效、转入差异/重复/回滚和发布冻结；样本识别准确性评测不以格式校验或模型自信代替。
- T138 仅补设计与本任务标记，不新增业务代码、迁移或测试；测试变更须先形成 TCR，文档静态检查不等同于运行验收。
