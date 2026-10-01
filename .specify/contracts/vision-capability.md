# v2.0 图片理解能力契约

## 范围与依据

对应 [spec.md](../spec.md) FR-043、FR-047、FR-049，[plan.md](../plan.md) §10 及 [data-model.md](../data-model.md) §7.4、§9。
用于上传原卷的图示、表格和条件理解、人工核对，以及原带图题的显示/复用；AI 新出题仍限文字题。
这是目标契约，不宣称当前 Provider、模型、考试或阅卷已经支持图像。

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
| VisionRequest | course_id、question_id 或 extracted_question_id、images、task；业务身份由服务核对，不由模型生成 |
| ImageReference | file_id、source_page_id（可空）、region（可空）、mime_type；绑定本次原题/暂存题的授权文件 |
| ProviderImage | encoding：base64 / url / local_path；value：对应内容；mime_type：真实图像格式 |
| images | 1–5 个题图；保留顺序、原图身份和来源，不把整份含答案原卷直接当作学生题图 |

- 公共请求使用 file_id，不接受任意本机路径或客户端提供的外部 URL。文件服务先核对资源/课程权限和真实图像，服务端取得 ProviderImage。
- Base64、URL 或本地路径由显式配置的 Provider 适配器按其实际支持选择；不要求每家适配器都支持三种。本机路径不会直接传给只能读取网络资源的云模型。
- URL 仅用于该 Provider 实际可访问、经过文件访问授权的传输方式；本机登录链接不能冒充远端可读地址。无可用传输方式返回 VISION_IMAGE_TRANSPORT_UNAVAILABLE。
- 传输内容只含本次允许理解的图像和必要文字条件；日志记录资源标识/实际 Provider 来源，不保存 Base64 全量内容、密钥或含答案源卷访问链接。
- bbox 坐标沿用原图/SourcePage 坐标约定；裁图到原页的映射由服务保存，不能由下游猜测或混用归一化坐标。

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
- VisionResult 为 Pydantic 模型；身份、页码、资产及坐标与请求对应，由服务核对，不信任模型自由生成的 file_id。
- 输出通过结构化校验只证明格式合法；还须核对原图、条件充分性和语义问题，不能凭校验成功直接批准题目。

| VisionResult 字段 | 含义 |
| :--- | :--- |
| observations | 可观察图示/表格内容列表：image_index、kind、description、可获得的 bbox；image_index 对应请求列表 |
| conditions | 识别出的题目条件：image_index、text、evidence_region（可空）；不凭空补充原图没有的数值/关系 |
| unresolved_issues | 不清楚文字、无法辨认条件、矛盾或缺失信息及原因列表 |
| requires_manual_review | bool；有不确定或缺失信息时必须为 true；false 也不代替教师批准 |
| provenance | 服务附加实际 Provider/model/调用来源；Schema 默认 null，不要求模型生成，未知保持未知，不从配置伪造调用成功身份 |

人工核对使用独立结构：status 为 pending / confirmed / unresolved，含教师标识、真实 UTC 核对时间、已确认条件、处理说明。
教师确认不能仅写一个布尔值掩盖 unresolved_issues；记录具体确认/修正条件。理解结果与原图版本对应，不能修改图片条件后沿用旧结果。
理解结果和人工核对必须持久化；当前 QuestionAsset 的字段不是完整理解结果存储。承载方案在实施前同步数据模型，不用 caption/临时 UI 变量代替条件与核对记录。

## 失败与人工接管

| 错误码 | 行为 |
| :--- | :--- |
| VISION_NOT_SUPPORTED | 未声明图像能力；明确返回不支持，保留原图及人工核对入口 |
| VISION_PROVIDER_NOT_READY | 依赖、凭据或配置未就绪；保留真实原因 |
| VISION_IMAGE_TRANSPORT_UNAVAILABLE | 适配器无可用的授权图像传输方式，不偷偷改发文本 |
| FILE_MISSING | 题图丢失；不替换为旧图或其他题图 |
| VISION_CALL_FAILED | 调用失败/超时；保留底层错误及实际阶段 |
| VISION_OUTPUT_INVALID | Pydantic 或请求/输出对应校验失败，不输出成功理解结果 |
| VISION_REVIEW_REQUIRED | 调用成功但条件不足/不可靠；返回结构化问题，不是成功批准 |

- 无能力/理解失败不阻止可靠保存原图；导入可保留 Pending Review，让教师核对原页。自动理解失败不能伪称已理解，也不一定等同于整个试卷解析失败。
- 若可靠人工核对已给出必要条件，题目可继续审核；没有可靠条件时不自动批准或评分为零。
- 图像或相关题干条件变更时，重新核对题干、答案、解析与 Rubric；原图关联受 Approved/发布冻结保护，不能直接覆盖源文件。
- 阅卷使用已核对条件及授权原图；客观题仍走规则评分。主观题输入有图像时先检查 Provider 能力；需要图像但不可用时显式待处理，不以只发 OCR 文字制造成功。

## 访问、排除项与验证

- 预览、学生答题、阅卷及结果展示引用同一题图关系，访问继承课程/考试授权；学生不可因此取得含答案的源卷或别人的答卷。
- 原页只读、题图更新和发布冻结遵循 [file-storage.md](file-storage.md)、[exam-assembly.md](exam-assembly.md)；文件迁移不改变图像内容或身份。
- 不实现图文 Embedding 微调、多模态训练、新题图生成或模型自动发布；不强制更换现有 LLM Provider。
- 验证支持/不支持两类 Provider、不可读图像、坐标映射、无法可靠理解及教师接管；用样本条件识别准确性评测，不用格式校验或模型自信代替。
