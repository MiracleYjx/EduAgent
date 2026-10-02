"""T161: real source boundaries, candidate evidence and confirmation inputs."""

from __future__ import annotations

from itertools import pairwise
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.ai.ingestion.chunking import chunk_document
from backend.app.ai.ingestion.cleaning import clean_document
from backend.app.ai.ingestion.parsers import parse_document


def test_markdown_short_leaf_sections_never_mix_body_or_overlap() -> None:
    raw = b"# Mechanics\n## Force\nForce body.\n## Energy\nEnergy body.\n# Optics\n## Reflection\nReflection body."
    cleaned = clean_document(parse_document("course.md", raw))
    chunks = chunk_document(cleaned, max_chars=100, overlap_chars=20)
    assert len(chunks) == 3
    assert all(
        not ("Force body." in c.content and "Energy body." in c.content) for c in chunks
    )
    assert all(
        not ("Energy body." in c.content and "Reflection body." in c.content)
        for c in chunks
    )
    assert "Mechanics" in chunks[0].content
    assert "Optics" in chunks[2].content
    for chunk in chunks:
        metadata = chunk.as_metadata()
        assert metadata["heading_level"] == 2
        assert len(metadata["heading_path"]) == 2
        assert "chapter_id" not in metadata
        assert "knowledge_points" not in metadata
        assert "scope_confirmation" not in metadata


def test_heading_source_preview_and_exact_slice_evidence() -> None:
    from backend.app.ai.ingestion.service import prepare_document_source

    document = prepare_document_source(
        "course.md", b"# Chapter\n## Part\n" + b"body " * 50
    )
    section = document.sections[0]
    assert section.heading_level == 2
    assert section.heading_path == ("Chapter", "Part")
    for chunk in chunk_document(document, max_chars=55, overlap_chars=10):
        metadata = chunk.as_metadata()
        source = next(
            s for s in document.sections if s.index == metadata["section_index"]
        )
        assert (
            source.text[metadata["start_char"] : metadata["end_char"]] == chunk.content
        )
        assert len(chunk.content) <= 55


def test_teacher_split_keeps_offsets_in_original_cleaned_section() -> None:
    from backend.app.schemas.chapter_scope import SourceSplit

    document = clean_document(parse_document("course.txt", b"A" * 80 + b"B" * 80))
    chunks = chunk_document(
        document,
        max_chars=50,
        overlap_chars=10,
        source_splits=[SourceSplit(section_index=1, cut_points=[80])],
    )
    assert all(not ("A" in c.content and "B" in c.content) for c in chunks)
    source = document.sections[0].text
    for chunk in chunks:
        assert chunk.content == source[chunk.start_char : chunk.end_char]
    for previous, current in pairwise(chunks):
        if previous.end_char <= 80 and current.start_char >= 80:
            assert previous.end_char == current.start_char == 80


@pytest.mark.parametrize(
    "cut_points", [[0], [160], [161], [-1], [80, 80], [90, 80], [True]]
)
def test_teacher_split_rejects_invalid_real_boundaries(cut_points: list[int]) -> None:
    from backend.app.schemas.chapter_scope import SourceSplit

    document = clean_document(parse_document("course.txt", b"A" * 160))
    with pytest.raises((ValidationError, ValueError)):
        splits = [SourceSplit(section_index=1, cut_points=cut_points)]
        chunk_document(document, source_splits=splits)


def test_teacher_split_rejects_unknown_or_repeated_source_section() -> None:
    from backend.app.schemas.chapter_scope import SourceSplit

    document = clean_document(parse_document("course.txt", b"A" * 160))
    for splits in (
        [SourceSplit(section_index=2, cut_points=[80])],
        [
            SourceSplit(section_index=1, cut_points=[40]),
            SourceSplit(section_index=1, cut_points=[80]),
        ],
    ):
        with pytest.raises(ValueError):
            chunk_document(document, source_splits=splits)


def test_scope_patch_distinguishes_omitted_unknown_and_confirmed_empty() -> None:
    from backend.app.schemas.chapter_scope import ChunkScopeUpdate

    assert ChunkScopeUpdate().model_fields_set == set()
    assert ChunkScopeUpdate(knowledge_points=None).model_fields_set == {
        "knowledge_points"
    }
    assert ChunkScopeUpdate(knowledge_points=[]).knowledge_points == []
    assert ChunkScopeUpdate(
        knowledge_points=["  Force ", "Force", "force", "two  words"]
    ).knowledge_points == ["Force", "force", "two  words"]
    for value in ([" "], [1], "Force"):
        with pytest.raises(ValidationError):
            ChunkScopeUpdate(knowledge_points=value)
    with pytest.raises(ValidationError):
        ChunkScopeUpdate(confirmed_by=uuid4())
    with pytest.raises(ValidationError):
        ChunkScopeUpdate(section_order=True)


def test_chapter_directory_requires_real_contiguous_strict_order() -> None:
    from backend.app.schemas.chapter_scope import ChapterWrite

    chapter = ChapterWrite(
        title=" Mechanics ", sections=[{"section_order": 1, "title": " Force "}]
    )
    assert chapter.title == "Mechanics"
    assert chapter.sections[0].title == "Force"
    for sections in (
        [{"section_order": True, "title": "A"}],
        [{"section_order": 2, "title": "A"}],
        [{"section_order": 1, "title": "A"}, {"section_order": 1, "title": "B"}],
    ):
        with pytest.raises(ValidationError):
            ChapterWrite(title="Chapter", sections=sections)


def test_ingestion_applies_teacher_splits_and_keeps_legacy_custom_chunker() -> None:
    import asyncio

    from backend.app.ai.ingestion.service import IngestionService
    from backend.app.schemas.chapter_scope import SourceSplit
    from tests.unit.ingestion.test_ingestion_service import StubEmbeddingProvider

    payload = b"A" * 80 + b"B" * 80
    result = asyncio.run(
        IngestionService(
            embedding_provider=StubEmbeddingProvider(), max_chars=50, overlap_chars=10
        ).ingest(
            filename="course.txt",
            data=payload,
            source_splits=[SourceSplit(section_index=1, cut_points=[80])],
        )
    )
    assert result.succeeded
    assert all(not ("A" in c.content and "B" in c.content) for c in result.chunks)
    assert all(
        "knowledge_points" not in c.metadata and "scope_confirmation" not in c.metadata
        for c in result.chunks
    )

    calls = []

    def legacy_chunker(
        document,
        *,
        document_id,
        course_id,
        knowledge_base_id,
        original_filename,
        max_chars,
        overlap_chars,
    ):
        calls.append(document)
        return chunk_document(
            document, max_chars=max_chars, overlap_chars=overlap_chars
        )

    for splits in (None, []):
        legacy = asyncio.run(
            IngestionService(
                chunker=legacy_chunker, embedding_provider=StubEmbeddingProvider()
            ).ingest(filename="course.txt", data=payload, source_splits=splits)
        )
        assert legacy.succeeded
    assert len(calls) == 2


def test_heading_only_sibling_is_never_attached_to_another_chapter() -> None:
    from backend.app.ai.ingestion.service import prepare_document_source

    document = prepare_document_source("course.md", b"# First\n# Second\n## Part\nbody")
    assert document.sections[0].text == "First"
    assert document.sections[1].text == "Second\n\nPart\nbody"
    assert document.sections[1].heading_path == ("Second", "Part")
    chunks = chunk_document(document)
    assert chunks[0].content == "First"
    assert "First" not in chunks[1].content
