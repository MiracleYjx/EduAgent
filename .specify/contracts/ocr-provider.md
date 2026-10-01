# v2.0 OCR Provider 契约

## 范围与依据

对应 [spec.md](../spec.md) FR-042、[plan.md](../plan.md) §9、Validation Gate 11；服务入口由 [paper-import.md](paper-import.md) 编排。
只定义可替换的 OCR 推理边界，不引入训练、独立检索服务或必装依赖；当前代码是否实现须另行验证。

## 抽象接口

~~~python
from abc import ABC, abstractmethod
from pathlib import Path

class BaseOCRProvider(ABC):
    @abstractmethod
    async def extract_text(self, image_path: Path) -> OCRResult:
        ...

    @abstractmethod
    def describe(self) -> OCRProviderInfo:
        ...
~~~

OCRResult、OCRRegion、OCRProviderInfo 是 Pydantic 模型；业务层依赖该接口，不直接调用具体 SDK。
PaddleOCR、Tesseract、云端 OCR 是候选 Provider 类型，验证真实扫描件/Windows 推理依赖后选择和锁定；本契约不承诺三类均已实现。

## 输入与结果

- image_path 必须是文件服务已授权、属于当前导入的持久页图；本接口为服务内调用，不允许客户端传任意本机路径。
- 正常路径为按页识别，输入包含单页完整图像。像素尺寸、页号和课程来源由编排携带，不靠 OCR 生成身份。
- 文字 PDF 优先确定性文本提取；扫描或文字层不可靠才调用 OCR。OCR 不负责拆题、答案生成、审核或宣布入库。

| DTO / 字段 | 类型 | 语义 |
| :--- | :--- | :--- |
| OCRResult.text | str | 真实识别文字；空白页可为空字符串，不能以伪造文本替代失败 |
| OCRResult.confidence | Decimal / null | 可获得的总体置信度，规范到 [0,1]；Provider 不提供则 null |
| OCRResult.regions | list[OCRRegion] | 按实际阅读顺序返回；无区域可为空列表 |
| OCRRegion.bbox | tuple[float,float,float,float] | 原图像素坐标 [x0,y0,x1,y1]，有限值且不超出输入页尺寸 |
| OCRRegion.text | str | 区域识别文字，与 bbox 一一对应；不是题目结构 |
| OCRRegion.confidence | Decimal / null | 原始可获得区域置信度规范到 [0,1]，未知为 null |
| OCRProviderInfo.provider | str | 当前实际适配器标识，不能用未调用的全局配置补齐 |
| OCRProviderInfo.model | str / null | 实际模型标识，未知为 null |
| OCRProviderInfo.ready | bool | 配置、依赖和所需推理资源是否就绪；不表示一次远端调用必定成功 |
| OCRProviderInfo.reason | str / null | 未就绪中文原因；已就绪可为 null，不含密钥或本机敏感路径 |

- Provider 负责将原生坐标/置信度转换到统一格式；转换依据不明确时保留未知或明确失败，不能猜测百分比/像素尺度。
- 使用 Pydantic 验证 text、regions、有限坐标及置信度范围；超界、非数值或无法解析响应返回 OCR_OUTPUT_INVALID，不将坏结果当成空白页。
- confidence 只表示供应商识别信号，不能当成实测准确率、自动校正通过证明或题目批准依据。
- 抽象接口只返回识别结果；SourcePage.ocr_text/ocr_confidence、导入状态及错误持久化由导入编排负责。
- 阅读顺序、表格/公式识别不可靠的内容保留原页供人工核对，不以 OCR 文本直接推断题目答案。

## 配置与可选依赖

| 配置 | 默认 / 要求 | 行为 |
| :--- | :--- | :--- |
| OCR_ENABLED | false | 默认不加载 OCR SDK；不阻塞 v1.0 文本服务启动 |
| OCR_PROVIDER | 启用 OCR 时必填 | 选择显式支持的适配器，例如 paddleocr、tesseract、cloud；非法值明确失败 |
| OCR_MODEL | 按所选 Provider 要求配置 | 明确实际识别模型；若 Provider 使用内置模型也须在 describe 说明真实身份 |

- 可选依赖延迟至启用/调用时加载；缺少 SDK、外部识别程序、模型文件或必需凭据时返回 OCR_PROVIDER_NOT_READY。
- OCR_ENABLED=false 且扫描页需要 OCR 时，任务明确失败/提示配置后重新导入；不能假装已识别，不能自动改用另一 Provider。
- 云端 OCR 的认证、网络超时等沿用该适配器明确配置；业务不新增无限重试，也不在 Provider 故障后未经授权切换本地/云端路径。
- 资源和版本在真实样本、所选运行环境验证后锁定；不声称可选依赖在当前 EXE 中已打包。

## 失败语义

Provider 抛出可识别的领域异常，至少含 code、中文 message 及真实底层错误原因；不返回成功 DTO 包裹失败。
编排保留原文件/页图，将错误及阶段保存到 PaperImport；HTTP 表达参考 [paper-import.md](paper-import.md)。

| 错误码 | 含义 |
| :--- | :--- |
| OCR_PROVIDER_NOT_READY | OCR 禁用，或配置/依赖/模型/凭据尚未就绪；不静默降级 |
| FILE_MISSING | 输入页图已丢失；不能用其他页替代 |
| OCR_INPUT_INVALID | 文件不是可识别的页图、尺寸非法或读取失败 |
| OCR_CALL_FAILED | 实际 OCR 调用失败/超时；导入可映射为 PAPER_OCR_FAILED 并保留本码与原因 |
| OCR_OUTPUT_INVALID | 输出未通过结构化校验；导入可映射为 PAPER_OCR_FAILED 并保留本码与原因 |

输入/输出失败与空白页不同；返回 text=""、regions=[] 只用于实际识别为空的页。
不凭失败状态新增自动重试状态机；新的解析尝试遵守 PaperImport 终态和来源保留规则。

## 验证边界

- OCR 未启用或 SDK 未安装时，v1.0 启动与文字资料摄取不受影响。
- 已选择 Provider 的真实扫描页可返回可核对文字、区域和可获得置信度；与标注样本比较字段准确性，不以 describe.ready 替代识别测试。
- 处理缺失图像、空白页、表格/公式、混合 PDF、未知置信度及失败响应；异常必须与真实阶段对应。
- 不修改现有 Embedding/LLM 契约，不把 OCR 当成图片语义理解或教师审核。
