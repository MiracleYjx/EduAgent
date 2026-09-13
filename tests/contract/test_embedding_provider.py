"""T036 Embedding Provider 契约测试。

自动验证 ``embed_documents()`` / ``embed_query()`` 的输入类型、空输入处理、向量维度、
批量数量一致性，以及切换 Provider 后仍满足同一接口契约；同时覆盖 T035 工厂“未就绪或
配置缺失时返回明确错误、不得伪造实例”的行为。

测试只使用可控测试替身、注入的假本地模型与假异步客户端：不访问网络、不下载模型，
因此可以稳定纳入普通回归。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from openai import OpenAIError

from backend.app.ai.embedding.base import (
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_FAILED,
    EMBEDDING_INVALID_INPUT,
    EMBEDDING_PROVIDER_NOT_READY,
    BaseEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingInputError,
    EmbeddingProviderError,
    EmbeddingProviderNotReadyError,
)
from backend.app.ai.embedding.factory import (
    EmbeddingProviderAlreadyRegisteredError,
    EmbeddingProviderFactory,
    UnsupportedEmbeddingProviderError,
    get_embedding_provider_factory,
    register_embedding_provider,
    reset_embedding_provider_cache,
)
from backend.app.ai.embedding.providers.bge import (
    BGE_DEFAULT_MODEL,
    BGE_QUERY_PREFIX,
    BgeEmbeddingProvider,
)
from backend.app.ai.embedding.providers.local import LocalEmbeddingProvider
from backend.app.ai.embedding.providers.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from backend.app.core.config import AppSettings
from tests.unit.settings_helpers import build_test_settings

CONTRACT_DOCUMENTS = ("第一节课程资料", "第二节课程资料")
CONTRACT_QUERY = "什么是检索增强生成？"
API_KEY = "sensitive-embedding-api-key"


def _run(awaitable: Any) -> Any:
    """同步执行协程，保持与仓库既有异步测试一致的写法。"""

    return asyncio.run(awaitable)


class HashingEmbeddingProvider(BaseEmbeddingProvider):
    """确定性的轻量 Provider：按内容哈希生成固定维度向量。"""

    provider_name = "hashing"
    model_name = "hashing-v1"

    def __init__(self, *, dimension: int = 8, provider_name: str = "hashing") -> None:
        self.provider_name = provider_name
        self.dimension = dimension

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        normalized = self.ensure_documents(documents)
        return self.validate_document_vectors(
            normalized, [self._vector(text) for text in normalized]
        )

    async def embed_query(self, query: str) -> list[float]:
        validated = self.ensure_query(query)
        return self.validate_vector(self._vector(validated))

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index] / 255 for index in range(self.dimension)]


class FakeSentenceTransformer:
    """记录编码输入的假本地模型；维度由 get_sentence_embedding_dimension 提供。"""

    def __init__(self, *, dimension: int = 4, error: Exception | None = None) -> None:
        self.dimension = dimension
        self.encoded: list[list[str]] = []
        self._error = error

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(
        self,
        texts: Sequence[str],
        batch_size: int = 32,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
    ) -> list[list[float]]:
        if self._error is not None:
            raise self._error
        self.encoded.append(list(texts))
        return [
            [float((position + 1) * len(text) % 7) for position in range(self.dimension)]
            for text in texts
        ]


class _EmbeddingItem:
    """模拟 OpenAI 兼容响应中的单条向量。"""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class FakeEmbeddingResponse:
    """模拟 OpenAI 兼容 Embedding 响应。"""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_EmbeddingItem(vector) for vector in vectors]


class FakeAsyncEmbeddings:
    """记录调用参数的假 embeddings 资源。"""

    def __init__(
        self,
        *,
        dimension: int = 4,
        count_override: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.models: list[str] = []
        self._dimension = dimension
        self._count_override = count_override
        self._error = error

    async def create(self, *, model: str, input: list[str]) -> FakeEmbeddingResponse:
        if self._error is not None:
            raise self._error
        self.calls.append(list(input))
        self.models.append(model)
        count = len(input) if self._count_override is None else self._count_override
        vectors = [[0.5] * self._dimension for _ in range(count)]
        return FakeEmbeddingResponse(vectors)


class FakeAsyncClient:
    """只暴露 embeddings 资源的假 OpenAI 兼容客户端。"""

    def __init__(self, embeddings: FakeAsyncEmbeddings) -> None:
        self.embeddings = embeddings


def _cloud_provider(
    *,
    embeddings: FakeAsyncEmbeddings | None = None,
    dimension: int = 4,
) -> OpenAICompatibleEmbeddingProvider:
    """构造注入假客户端的云端 Provider。"""

    return OpenAICompatibleEmbeddingProvider(
        model="contract-model",
        api_key=API_KEY,
        dimension=dimension,
        client=FakeAsyncClient(embeddings or FakeAsyncEmbeddings(dimension=dimension)),
    )


def _provider_variants() -> dict[str, Callable[[], BaseEmbeddingProvider]]:
    """返回参与同一契约校验的 Provider 构造器。"""

    return {
        "hashing": lambda: HashingEmbeddingProvider(),
        "cloud": lambda: _cloud_provider(),
        "local": lambda: LocalEmbeddingProvider(
            model="fake-local-model",
            model_loader=lambda _model: FakeSentenceTransformer(dimension=4),
        ),
        "bge": lambda: BgeEmbeddingProvider(
            model=BGE_DEFAULT_MODEL,
            model_loader=lambda _model: FakeSentenceTransformer(dimension=4),
        ),
    }


@pytest.fixture(params=["hashing", "cloud", "local", "bge"])
def provider(request: pytest.FixtureRequest) -> BaseEmbeddingProvider:
    """为契约测试提供不同实现，验证切换 Provider 后契约不变。"""

    return _provider_variants()[request.param]()


def test_embedding_provider_contract_holds_for_every_provider(
    provider: BaseEmbeddingProvider,
) -> None:
    """所有 Provider 必须满足同一接口契约：类型、数量与维度一致。"""

    vectors = _run(provider.embed_documents(CONTRACT_DOCUMENTS))

    assert isinstance(vectors, list)
    assert len(vectors) == len(CONTRACT_DOCUMENTS)
    for vector in vectors:
        assert isinstance(vector, list)
        assert vector
        assert all(isinstance(value, float) for value in vector)
        assert len(vector) == len(vectors[0])

    query_vector = _run(provider.embed_query(CONTRACT_QUERY))
    assert isinstance(query_vector, list)
    assert query_vector
    assert len(query_vector) == len(vectors[0])

    description = provider.describe()
    assert set(description) == {"provider", "model", "dimension"}
    assert description["provider"] == provider.provider_name
    assert description["model"] == provider.model_name
    assert isinstance(description["model"], str) and description["model"].strip()
    # 契约测试使用的替身与注入模型均已就绪，不得依赖真实云端或模型下载。
    assert provider.is_ready() is True


@pytest.mark.parametrize("documents", [[], ["   ", ""], "不是序列", ["文本", 123]])
def test_embedding_provider_contract_rejects_invalid_documents(
    provider: BaseEmbeddingProvider, documents: Any
) -> None:
    """空输入、单个字符串和非字符串元素都必须返回可识别的输入失败。"""

    with pytest.raises(EmbeddingInputError) as captured:
        _run(provider.embed_documents(documents))

    assert captured.value.error_code == EMBEDDING_INVALID_INPUT
    assert captured.value.retryable is False


@pytest.mark.parametrize("query", ["", "   ", "\n\t", 123])
def test_embedding_provider_contract_rejects_invalid_query(
    provider: BaseEmbeddingProvider, query: Any
) -> None:
    """空查询和非法类型必须返回可识别的输入失败。"""

    with pytest.raises(EmbeddingInputError) as captured:
        _run(provider.embed_query(query))

    assert captured.value.error_code == EMBEDDING_INVALID_INPUT
    assert captured.value.retryable is False


def test_cloud_provider_contract_rejects_batch_count_mismatch() -> None:
    """返回向量数量与输入数量不一致时必须失败，不得静默截断。"""

    provider = _cloud_provider(embeddings=FakeAsyncEmbeddings(count_override=1))

    with pytest.raises(EmbeddingDimensionError) as captured:
        _run(provider.embed_documents(["第一节", "第二节"]))

    assert captured.value.error_code == EMBEDDING_DIMENSION_MISMATCH
    assert captured.value.retryable is False


def test_cloud_provider_contract_rejects_declared_dimension_mismatch() -> None:
    """向量维度与声明维度不一致时必须失败，保证查询与知识片段维度一致。"""

    provider = _cloud_provider(embeddings=FakeAsyncEmbeddings(dimension=3), dimension=4)

    with pytest.raises(EmbeddingDimensionError) as captured:
        _run(provider.embed_query("查询"))

    assert captured.value.error_code == EMBEDDING_DIMENSION_MISMATCH


def test_cloud_provider_contract_batches_without_losing_documents() -> None:
    """批量调用必须覆盖全部输入，并使用同一模型标识。"""

    embeddings = FakeAsyncEmbeddings(dimension=4)
    provider = OpenAICompatibleEmbeddingProvider(
        model="contract-model",
        api_key=API_KEY,
        dimension=4,
        batch_size=2,
        client=FakeAsyncClient(embeddings),
    )

    vectors = _run(provider.embed_documents(["一", "二", "三"]))

    assert len(vectors) == 3
    assert embeddings.calls == [["一", "二"], ["三"]]
    assert set(embeddings.models) == {"contract-model"}


def test_cloud_provider_contract_maps_provider_errors_without_leaking_key() -> None:
    """云端异常必须映射为可识别的 Embedding 失败，且不得泄露密钥。"""

    provider = _cloud_provider(
        embeddings=FakeAsyncEmbeddings(error=OpenAIError("upstream failed"))
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        _run(provider.embed_documents(["课程资料"]))

    error = captured.value
    assert error.error_code == EMBEDDING_FAILED
    assert error.retryable is True
    assert API_KEY not in str(error)


def test_local_provider_contract_uses_injected_model() -> None:
    """本地 Provider 必须使用注入模型编码，并从模型解析向量维度。"""

    model = FakeSentenceTransformer(dimension=6)
    provider = LocalEmbeddingProvider(
        model="fake-local-model", model_loader=lambda _model: model
    )

    vectors = _run(provider.embed_documents(list(CONTRACT_DOCUMENTS)))
    query_vector = _run(provider.embed_query(CONTRACT_QUERY))

    assert [len(vector) for vector in vectors] == [6, 6]
    assert len(query_vector) == 6
    assert provider.dimension == 6
    assert model.encoded == [list(CONTRACT_DOCUMENTS), [CONTRACT_QUERY]]


def test_local_provider_contract_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地依赖缺失时必须明确报告未就绪，而不是返回不可用实例。"""

    import backend.app.ai.embedding.providers.local as local_module

    monkeypatch.setattr(local_module, "sentence_transformers_available", lambda: False)
    provider = LocalEmbeddingProvider(model="BAAI/bge-small-zh-v1.5")

    assert provider.is_ready() is False
    detail = provider.readiness_detail()
    assert detail is not None
    assert "sentence-transformers" in detail


def test_local_provider_contract_maps_model_failure() -> None:
    """注入模型抛出异常时必须映射为 Embedding 失败状态。"""

    provider = LocalEmbeddingProvider(
        model="fake-local-model",
        model_loader=lambda _model: FakeSentenceTransformer(
            dimension=4, error=RuntimeError("model exploded")
        ),
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        _run(provider.embed_documents(["课程资料"]))

    assert captured.value.error_code == EMBEDDING_FAILED


def test_bge_provider_contract_prefixes_query_only() -> None:
    """BGE 只在查询侧添加指令前缀；M3 系列不需要前缀。"""

    model = FakeSentenceTransformer(dimension=4)
    provider = BgeEmbeddingProvider(model_loader=lambda _model: model)

    _run(provider.embed_documents(["课程文档"]))
    _run(provider.embed_query("什么是向量检索？"))

    assert model.encoded[0] == ["课程文档"]
    assert model.encoded[1] == [f"{BGE_QUERY_PREFIX}什么是向量检索？"]

    m3_model = FakeSentenceTransformer(dimension=4)
    m3_provider = BgeEmbeddingProvider(
        model="BAAI/bge-m3", model_loader=lambda _model: m3_model
    )
    _run(m3_provider.embed_query("什么是向量检索？"))

    assert m3_provider.model_name == "BAAI/bge-m3"
    assert m3_model.encoded == [["什么是向量检索？"]]


def test_bge_provider_contract_uses_default_model_when_settings_missing() -> None:
    """未配置 EMBEDDING_MODEL 时 BGE 使用默认模型，而不是抛出空模型错误。"""

    settings = build_test_settings(embedding_model=None, embedding_dimension=None)

    provider = BgeEmbeddingProvider.from_settings(settings)

    assert provider.model_name == BGE_DEFAULT_MODEL
    assert provider.dimension is None


def test_factory_contract_registers_and_rejects_invalid_selection() -> None:
    """工厂必须支持注册、拒绝重复注册与重复注册替换，并拒绝未注册 Provider。"""

    factory = EmbeddingProviderFactory()
    assert factory.supported_providers() == ()

    factory.register("huggingface", lambda _settings: HashingEmbeddingProvider())
    assert factory.supported_providers() == ("huggingface",)
    with pytest.raises(EmbeddingProviderAlreadyRegisteredError):
        factory.register("huggingface", lambda _settings: HashingEmbeddingProvider())
    factory.register(
        "huggingface", lambda _settings: HashingEmbeddingProvider(), replace=True
    )
    with pytest.raises(ValueError):
        factory.register("   ", lambda _settings: HashingEmbeddingProvider())

    provider = factory.create(build_test_settings(embedding_provider="huggingface"))
    assert isinstance(provider, HashingEmbeddingProvider)

    with pytest.raises(UnsupportedEmbeddingProviderError) as captured:
        factory.create(build_test_settings(embedding_provider="deepseek"))
    assert "deepseek" in str(captured.value)
    assert "huggingface" in str(captured.value)


def test_factory_contract_requires_ready_provider() -> None:
    """未就绪的 Provider 必须抛出明确错误，且不得返回任何实例。"""

    factory = EmbeddingProviderFactory()
    factory.register("local", lambda _settings: LocalEmbeddingProvider(model="未安装依赖"))
    settings = build_test_settings(embedding_provider="local", embedding_model="未安装依赖")
    ready, detail = factory.readiness(settings)
    # 当前环境未安装 sentence-transformers，因此该 Provider 必须被判定为未就绪。
    assert ready is False
    assert detail is not None and EMBEDDING_PROVIDER_NOT_READY in detail

    with pytest.raises(EmbeddingProviderNotReadyError) as captured:
        factory.create(settings)
    assert captured.value.error_code == EMBEDDING_PROVIDER_NOT_READY
    assert captured.value.retryable is False
    assert "sentence-transformers" in str(captured.value)


def test_default_factory_contract_exposes_builtin_providers() -> None:
    """默认工厂必须注册 MVP 声明的内置 Provider。"""

    factory = get_embedding_provider_factory()

    assert {"bge", "huggingface", "local", "openai_compatible"} <= set(
        factory.supported_providers()
    )


def test_default_factory_contract_rejects_local_without_model() -> None:
    """本地 Provider 缺少 EMBEDDING_MODEL 时必须明确失败并提示配置项。"""

    factory = get_embedding_provider_factory()

    with pytest.raises(EmbeddingProviderNotReadyError) as captured:
        factory.create(
            build_test_settings(embedding_provider="local", embedding_model=None)
        )

    assert "EMBEDDING_MODEL" in str(captured.value)


def test_default_factory_contract_rejects_cloud_without_credentials() -> None:
    """云端 Provider 缺少模型或密钥时必须明确失败并列出缺失配置。"""

    factory = get_embedding_provider_factory()

    with pytest.raises(EmbeddingProviderNotReadyError) as captured:
        factory.create(
            build_test_settings(
                embedding_provider="openai_compatible",
                embedding_model=None,
                embedding_api_key=None,
            )
        )

    message = str(captured.value)
    assert "EMBEDDING_MODEL" in message
    assert "EMBEDDING_API_KEY" in message


def test_default_factory_contract_creates_ready_cloud_provider() -> None:
    """配置完整的云端 Provider 必须就绪，并能给出可溯源的模型与维度。"""

    provider = get_embedding_provider_factory().create(
        build_test_settings(embedding_provider="openai_compatible")
    )

    assert provider.is_ready() is True
    assert provider.describe()["model"] == "test-embedding-model"
    assert provider.describe()["dimension"] == 8


def test_provider_switch_keeps_same_interface_contract() -> None:
    """同一工厂切换 Provider 后，调用方依赖的接口契约保持不变。"""

    factory = EmbeddingProviderFactory()
    factory.register(
        "huggingface",
        lambda _settings: HashingEmbeddingProvider(dimension=4, provider_name="huggingface"),
    )
    factory.register(
        "local",
        lambda _settings: HashingEmbeddingProvider(dimension=16, provider_name="local"),
    )

    for name, expected_dimension in (("huggingface", 4), ("local", 16)):
        provider = factory.create(build_test_settings(embedding_provider=name))
        vectors = _run(provider.embed_documents(["课程资料"]))
        query_vector = _run(provider.embed_query("查询"))

        assert isinstance(provider, BaseEmbeddingProvider)
        assert provider.provider_name == name
        assert len(vectors) == 1
        assert len(vectors[0]) == expected_dimension
        assert len(query_vector) == expected_dimension
        assert provider.describe()["provider"] == name


def test_register_embedding_provider_extends_default_factory() -> None:
    """向默认工厂注册 Provider 后应可直接创建，并使缓存实例失效。"""

    settings: AppSettings = build_test_settings(embedding_provider="openai_compatible")
    register_embedding_provider(
        "openai_compatible",
        lambda _settings: HashingEmbeddingProvider(dimension=5, provider_name="stub"),
        replace=True,
    )
    try:
        provider = get_embedding_provider_factory().create(settings)
        assert isinstance(provider, HashingEmbeddingProvider)
        assert provider.provider_name == "stub"
        assert provider.describe()["dimension"] == 5
    finally:
        reset_embedding_provider_cache()
