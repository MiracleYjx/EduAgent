"""T164：真实模型能力、统一图像传输与结构化输出边界（TCR §17）。"""
from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import BaseModel, ValidationError

from backend.app.ai.llm.base import BaseLLMProvider, LLMMessages
from backend.app.ai.llm.deepseek import DeepSeekProvider
from backend.app.ai.vision.base import (
    ProviderImage,
    VisionFailure,
    VisionImage,
    VisionResult,
)
from backend.app.ai.vision.provider import (
    ProviderVisionUnderstanding,
    create_vision_provider,
)
from backend.app.core.retry_policy import ProviderCallError, RetryPolicy
from tests.unit.settings_helpers import build_test_settings


def image() -> VisionImage:
    stream = BytesIO()
    Image.new("RGB", (80, 60), "white").save(stream, format="PNG")
    return VisionImage.from_bytes(stream.getvalue())


def valid_result() -> dict[str, Any]:
    return {
        "observations": [{"image_index": 1, "kind": "diagram", "description": "直角三角形", "bbox": None}],
        "conditions": [{"image_index": 1, "text": "底边长 3 cm", "evidence_region": None}],
        "unresolved_issues": [],
        "requires_manual_review": False,
        "provenance": None,
    }


class TextProvider(BaseLLMProvider):
    async def generate_structured(self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None) -> BaseModel:
        raise AssertionError("不支持图片时不能发起调用")


class VisionStub(BaseLLMProvider):
    provider_name = "stub"

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.model_name = "fixture-vision"
        self.payload = payload if payload is not None else valid_result()
        self.error = error
        self.calls: list[LLMMessages] = []

    def supports_vision(self) -> bool:
        return True

    async def generate_structured(self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None) -> BaseModel:
        assert model is None
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return schema.model_validate(self.payload)


def understand(provider: BaseLLMProvider, images: list[VisionImage] | None = None) -> VisionResult:
    return asyncio.run(ProviderVisionUnderstanding(provider).understand(
        context={"content": "已授权合成题干", "reference_answer": None},
        images=images if images is not None else [image()],
        task="读取原图条件，不补写答案",
    ))


def test_old_provider_gets_default_false_without_new_abstract_method() -> None:
    assert TextProvider().supports_vision() is False
    with pytest.raises(VisionFailure) as failure:
        understand(TextProvider())
    assert failure.value.code == "VISION_NOT_SUPPORTED"
    assert failure.value.provenance is None


@pytest.mark.parametrize("model,supported", [
    ("deepseek-chat", False), ("deepseek-reasoner", False),
    ("deepseek-v4-pro", False), ("unknown-model", False),
    ("deepseek-flash", True), ("deepseek-v4-flash", True),
    ("deepseek-v4-flash-vision-exp", True),
])
def test_deepseek_capability_comes_from_actual_instance_model(model: str, supported: bool) -> None:
    provider = DeepSeekProvider(build_test_settings(deepseek_model=model), client=object())
    assert provider.supports_vision() is supported


def test_unknown_gateway_does_not_inherit_official_model_capability() -> None:
    provider = DeepSeekProvider(build_test_settings(
        deepseek_model="deepseek-flash", deepseek_base_url="https://gateway.example.test/v1",
    ), client=object())
    assert provider.supports_vision() is False


def test_unconfigured_vision_is_not_ready_without_touching_text_config() -> None:
    settings = build_test_settings(vision_model=None)
    with pytest.raises(VisionFailure) as failure:
        create_vision_provider(settings)
    assert failure.value.code == "VISION_PROVIDER_NOT_READY"
    assert settings.deepseek_model == "deepseek-chat"


def test_independent_vision_instance_uses_explicit_model_and_same_credentials() -> None:
    settings = build_test_settings(vision_model="deepseek-flash")
    provider = create_vision_provider(settings)
    try:
        second = create_vision_provider(settings)
        try:
            assert provider is not second
        finally:
            asyncio.run(second.aclose())
        assert settings.deepseek_model == "deepseek-chat"
        assert provider.describe()["model"] == "deepseek-flash"
        assert provider.supports_vision() is True
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.parametrize("value,expected", [("", None), ("  ", None), (" deepseek-flash ", "deepseek-flash")])
def test_vision_config_normalizes_optional_model(value: str, expected: str | None) -> None:
    settings = build_test_settings(vision_model=value)
    assert settings.vision_model == expected
    assert settings.public_dict()["vision_model"] == expected


def test_all_images_are_sent_in_order_as_actual_base64_with_context() -> None:
    provider = VisionStub()
    first, second = image(), image()
    result = understand(provider, [first, second])
    assert len(provider.calls) == 1
    content = provider.calls[0][-1]["content"]
    assert content[0]["type"] == "text"
    assert "已授权合成题干" in content[0]["text"]
    assert "reference_answer" in content[0]["text"]
    assert len(content) == 5
    assert [base64.b64decode(block["image"].value) for block in content if block["type"] == "image"] == [first.data, second.data]
    assert result.provenance is not None
    assert result.provenance.provider_name == "stub"
    assert result.provenance.model == "fixture-vision"


def test_unreliable_legal_output_is_preserved_for_teacher_review() -> None:
    payload = valid_result()
    payload["unresolved_issues"] = [{"image_indices": None, "message": "模糊条件无法读取"}]
    payload["requires_manual_review"] = True
    result = understand(VisionStub(payload))
    assert result.requires_manual_review is True
    assert result.unresolved_issues[0].image_indices is None
    assert result.conditions[0].text == "底边长 3 cm"


@pytest.mark.parametrize("count", [0, 6])
def test_zero_or_over_five_images_never_call_provider(count: int) -> None:
    provider = VisionStub()
    with pytest.raises(VisionFailure) as failure:
        understand(provider, [image()] * count)
    assert failure.value.code == "VISION_IMAGE_TRANSPORT_UNAVAILABLE"
    assert failure.value.provenance is None
    assert not provider.calls


@pytest.mark.parametrize("bad_image", [
    VisionImage(b"not-an-image", "image/png", 80, 60),
    VisionImage(b"", "image/png", 80, 60),
])
def test_unreadable_image_has_real_transport_failure(bad_image: VisionImage) -> None:
    provider = VisionStub()
    with pytest.raises(VisionFailure) as failure:
        understand(provider, [bad_image])
    assert failure.value.code == "VISION_IMAGE_TRANSPORT_UNAVAILABLE"
    assert not provider.calls
    assert failure.value.provenance is None


def test_actual_bytes_mime_and_dimensions_must_match() -> None:
    real = image()
    provider = VisionStub()
    with pytest.raises(VisionFailure) as failure:
        understand(provider, [VisionImage(real.data, "image/jpeg", 80, 60)])
    assert failure.value.code == "VISION_IMAGE_TRANSPORT_UNAVAILABLE"
    with pytest.raises(VisionFailure):
        understand(provider, [VisionImage(real.data, "image/png", 81, 60)])
    assert not provider.calls


@pytest.mark.parametrize("mutate", [
    lambda payload: payload["conditions"][0].update(image_index=2),
    lambda payload: payload["conditions"][0].update(image_index=True),
    lambda payload: payload["observations"][0].update(bbox=(0, 0, 81, 60)),
    lambda payload: payload["conditions"][0].update(evidence_region={"bbox": [-1, 0, 10, 10]}),
    lambda payload: payload.update(unresolved_issues=[{"image_indices": [2], "message": "未知"}]),
    lambda payload: payload.update(unresolved_issues=[{"image_indices": [], "message": "未知"}]),
    lambda payload: payload.update(unresolved_issues=[{"image_indices": [1], "message": "未知"}], requires_manual_review=False),
    lambda payload: payload.update(provenance={"provider_name": "fabricated", "model": "fake", "model_version": None, "prompt_version": "fake"}),
])
def test_invalid_or_fabricated_output_is_not_success(mutate: Any) -> None:
    payload = valid_result()
    mutate(payload)
    provider = VisionStub(payload)
    with pytest.raises(VisionFailure) as failure:
        understand(provider)
    assert failure.value.code == "VISION_OUTPUT_INVALID"
    assert failure.value.provenance is not None
    assert failure.value.provenance.provider_name == "stub"
    assert len(provider.calls) == 1


def test_provider_failure_preserves_real_code_and_call_provenance() -> None:
    with pytest.raises(VisionFailure) as failure:
        understand(VisionStub(error=ProviderCallError("ProviderTimeout", "LLM Provider 调用超时。", retryable=True)))
    assert failure.value.code == "VISION_CALL_FAILED"
    assert failure.value.cause == "ProviderTimeout"
    assert failure.value.retryable is True
    assert "超时" in failure.value.message
    assert failure.value.provenance is not None


def test_cancellation_retains_actual_failed_call_facts() -> None:
    class CancelledVision(VisionStub):
        async def generate_structured(self, messages: LLMMessages, schema: type[BaseModel], model: str | None = None) -> BaseModel:
            raise asyncio.CancelledError("actual caller cancellation")

    vision = ProviderVisionUnderstanding(CancelledVision())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vision.understand(context={}, images=[image()], task="实际取消"))
    assert vision.call_provenance is not None
    assert vision.call_provenance.provider_name == "stub"


def test_deepseek_converts_uniform_image_block_to_sdk_data_url() -> None:
    provider = DeepSeekProvider(build_test_settings(deepseek_model="deepseek-flash"), client=object())
    encoded = base64.b64encode(image().data).decode("ascii")
    prepared = provider._prepare_messages([{"role": "user", "content": [
        {"type": "text", "text": "JSON 条件"},
        {"type": "image", "image": ProviderImage(encoding="base64", value=encoded, mime_type="image/png")},
    ]}])
    assert prepared[-1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}


@pytest.mark.parametrize("encoding,value", [("local_path", "D:/private/image.png"), ("url", "https://example.test/image.png")])
def test_deepseek_rejects_unimplemented_transport_instead_of_sending_text(encoding: str, value: str) -> None:
    provider = DeepSeekProvider(build_test_settings(deepseek_model="deepseek-flash"), client=object())
    with pytest.raises(ProviderCallError) as failure:
        provider._prepare_messages([{"role": "user", "content": [
            {"type": "image", "image": ProviderImage(encoding=encoding, value=value, mime_type="image/png")},
        ]}])
    assert failure.value.code == "VISION_IMAGE_TRANSPORT_UNAVAILABLE"


def test_text_deepseek_rejects_images_before_sdk_call_even_with_model_override() -> None:
    client = AsyncMock()
    provider = DeepSeekProvider(build_test_settings(), client=client, retry_policy=RetryPolicy(max_retries=0))
    with pytest.raises(ProviderCallError) as failure:
        asyncio.run(provider.generate_structured([{"role": "user", "content": [
            {"type": "image", "image": ProviderImage(encoding="base64", value=base64.b64encode(image().data).decode("ascii"), mime_type="image/png")},
        ]}], VisionResult, model="deepseek-flash"))
    assert failure.value.code == "VISION_NOT_SUPPORTED"
    client.chat.completions.create.assert_not_called()


def test_image_content_is_not_exposed_in_repr() -> None:
    value = image()
    encoded = base64.b64encode(value.data).decode("ascii")
    assert "data=" not in repr(value)
    assert encoded not in repr(ProviderImage(encoding="base64", value=encoded, mime_type="image/png"))


def test_machine_result_does_not_accept_persistent_business_ids() -> None:
    payload = valid_result()
    payload["conditions"][0]["asset_id"] = "fabricated"
    with pytest.raises(ValidationError):
        VisionResult.model_validate(payload)



def test_real_orm_context_preserves_decimal_uuid_and_unknown_fields() -> None:
    page_id = uuid4()
    provider = VisionStub()
    asyncio.run(ProviderVisionUnderstanding(provider).understand(
        context={"score": Decimal("3.50"), "source_page_ids": [page_id], "analysis": None},
        images=[image()], task="真实文字上下文",
    ))
    actual = json.loads(provider.calls[0][-1]["content"][0]["text"])["context"]
    assert actual == {"score": "3.50", "source_page_ids": [str(page_id)], "analysis": None}
