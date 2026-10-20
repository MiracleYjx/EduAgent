"""Bounded multipart transport: exact 20 MiB boundary and parse-time resource control.

These tests exercise the dedicated personal upload transport directly, so they
need no database and no network. They pin four facts the approved limits depend
on:

- the file part is accepted at exactly 20 MiB and refused one byte later;
- the envelope cap is enforced while the body is still streaming, not after it
  has been buffered;
- the body spool stays in memory for small bodies and rolls over to a real file
  under ``materials/.incoming`` once it passes the spool threshold;
- every failure path leaves no temporary file behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.api.learning_materials import (
    MAX_ENVELOPE_BYTES,
    MAX_METADATA_BYTES,
    MAX_PARTS,
    SPOOL_MEMORY_BYTES,
    UploadTransportError,
    read_material_upload,
)

BOUNDARY = "----eduagent-limit-boundary"
MIB = 1024 * 1024
APPROVED_UPLOAD_BYTES = 20 * MIB
METADATA = json.dumps({"purpose": "study_material"}).encode()


class FakeService:
    """Minimal stand-in for the verified space: only sizes and a storage root."""

    def __init__(self, storage_root: Path, upload_bytes: int = APPROVED_UPLOAD_BYTES) -> None:
        self.storage_root = storage_root
        self.limits = SimpleNamespace(upload_bytes=upload_bytes)


class FakeRequest:
    """Request double that streams caller-controlled chunks and records them."""

    def __init__(self, chunks: list[bytes], *, consumed: list[bytes] | None = None) -> None:
        self.headers = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
        self._chunks = chunks
        self.consumed = consumed if consumed is not None else []

    async def stream(self):
        for chunk in self._chunks:
            self.consumed.append(chunk)
            yield chunk


def multipart_body(
    payload: bytes,
    *,
    metadata: bytes = METADATA,
    filename: str = "big.txt",
    extra_parts: int = 0,
) -> bytes:
    head = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode() + metadata + b"\r\n"
    head += (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    ).encode()
    extra = b""
    for index in range(extra_parts):
        extra += (
            f"\r\n--{BOUNDARY}\r\nContent-Disposition: form-data; "
            f'name="extra{index}"\r\n\r\nx'
        ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    return head + payload + extra + tail


def chunked(body: bytes, size: int = 256 * 1024) -> list[bytes]:
    return [body[offset : offset + size] for offset in range(0, len(body), size)]


def incoming(service: FakeService) -> list[Path]:
    directory = service.storage_root / "materials" / ".incoming"
    return sorted(directory.glob("*")) if directory.is_dir() else []


def read(service: FakeService, request: FakeRequest):
    return asyncio.run(read_material_upload(request, service=service))


def test_file_part_at_exactly_20_mib_is_accepted_and_hashed(tmp_path):
    payload = b"a" * APPROVED_UPLOAD_BYTES
    service = FakeService(tmp_path.resolve())
    filename, parsed, path, size, digest = read(
        service, FakeRequest(chunked(multipart_body(payload)))
    )

    assert filename == "big.txt"
    assert parsed.purpose == "study_material"
    assert size == APPROVED_UPLOAD_BYTES
    assert digest == hashlib.sha256(payload).hexdigest()
    assert path.read_bytes() == payload
    path.unlink()
    assert incoming(service) == []


def test_file_part_one_byte_over_20_mib_is_refused(tmp_path):
    payload = b"a" * (APPROVED_UPLOAD_BYTES + 1)
    service = FakeService(tmp_path.resolve())

    with pytest.raises(UploadTransportError) as error:
        read(service, FakeRequest(chunked(multipart_body(payload))))

    assert error.value.code == "LIMIT_EXCEEDED"
    assert error.value.status == 413
    assert incoming(service) == []


def test_envelope_cap_stops_the_stream_before_the_body_ends(tmp_path):
    service = FakeService(tmp_path.resolve())
    # 25 x 1 MiB: the cap must trip long before the client finishes sending.
    chunks = [b"a" * MIB for _ in range(25)]
    consumed: list[bytes] = []
    request = FakeRequest(chunks, consumed=consumed)

    with pytest.raises(UploadTransportError) as error:
        read(service, request)

    assert error.value.code == "LIMIT_EXCEEDED"
    assert error.value.status == 413
    expected_minimum = (APPROVED_UPLOAD_BYTES + MAX_ENVELOPE_BYTES) // MIB
    assert expected_minimum < len(consumed) < len(chunks)
    assert incoming(service) == []


def test_spool_rolls_over_to_disk_above_the_memory_threshold(tmp_path):
    service = FakeService(tmp_path.resolve())
    payload = b"b" * (2 * MIB)
    body = multipart_body(payload)
    observed: list[list[Path]] = []
    chunks = chunked(body, 512 * 1024)

    class ProbingRequest(FakeRequest):
        async def stream(self):
            for index, chunk in enumerate(self._chunks):
                if index == 3:  # 1.5 MiB already written: past the spool threshold.
                    observed.append(incoming(service))
                self.consumed.append(chunk)
                yield chunk

    assert 3 * 512 * 1024 > SPOOL_MEMORY_BYTES
    filename, _parsed, path, size, _digest = read(service, ProbingRequest(chunks))

    assert filename == "big.txt" and size == len(payload)
    assert observed and observed[0], "spool should have spilled into .incoming"
    path.unlink()
    assert incoming(service) == []


def test_small_body_never_touches_the_disk_spool(tmp_path):
    service = FakeService(tmp_path.resolve())
    payload = b"c" * 1024
    body = multipart_body(payload)
    observed: list[list[Path]] = []

    class ProbingRequest(FakeRequest):
        async def stream(self):
            for chunk in self._chunks:
                observed.append(incoming(service))
                self.consumed.append(chunk)
                yield chunk

    assert len(body) < SPOOL_MEMORY_BYTES
    _filename, _parsed, path, size, _digest = read(service, ProbingRequest([body]))

    assert size == len(payload)
    assert observed and all(item == [] for item in observed)
    path.unlink()
    assert incoming(service) == []


def test_oversized_metadata_part_is_refused_without_leftovers(tmp_path):
    service = FakeService(tmp_path.resolve())
    body = multipart_body(
        b"d" * 32,
        metadata=b"{" + b"x" * (MAX_METADATA_BYTES + 1) + b"}",
    )

    with pytest.raises(UploadTransportError) as error:
        read(service, FakeRequest([body]))

    assert error.value.code == "LIMIT_EXCEEDED"
    assert error.value.status == 413
    assert incoming(service) == []


def test_more_parts_than_allowed_is_refused(tmp_path):
    service = FakeService(tmp_path.resolve())
    body = multipart_body(b"e" * 32, extra_parts=MAX_PARTS)

    with pytest.raises(UploadTransportError) as error:
        read(service, FakeRequest([body]))

    assert error.value.code == "MULTIPART_INVALID"
    assert error.value.status == 422
    assert incoming(service) == []


def test_invalid_metadata_json_creates_no_temp_file(tmp_path):
    service = FakeService(tmp_path.resolve())
    body = multipart_body(b"f" * 32, metadata=b"{not json")

    with pytest.raises(UploadTransportError) as error:
        read(service, FakeRequest([body]))

    assert error.value.code == "REQUEST_INVALID"
    assert error.value.status == 422
    assert incoming(service) == []
