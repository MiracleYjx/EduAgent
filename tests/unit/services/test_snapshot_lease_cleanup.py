"""A final review regression: one failed close cannot strand other leases."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.services.settings.runtime_snapshot import (
    RuntimeSnapshotService,
    SnapshotClientLease,
)


@pytest.mark.parametrize("method", ["release_all", "close", "arelease_all", "aclose"])
def test_release_attempts_every_lease_and_preserves_first_original_error(tmp_path, method):
    service = RuntimeSnapshotService(SimpleNamespace(config=tmp_path))
    snapshot = uuid4()
    calls = []
    first = OSError("synthetic first close failure")
    second = ValueError("synthetic second close failure")

    class Client:
        def __init__(self, name, error=None):
            self.name, self.error = name, error

        def close(self):
            calls.append(self.name)
            if self.error is not None:
                raise self.error

        async def aclose(self):
            self.close()

    for kind, error in (("embedding", first), ("rerank", second), ("subjective_grading", None)):
        service._leases[(snapshot, kind)] = SnapshotClientLease(snapshot_id=snapshot,
            task_kind=kind, client=Client(kind, error), on_release=service._drop_lease)
    arguments = (snapshot,) if method.endswith("_all") else ()
    with pytest.raises(OSError) as failure:
        result = getattr(service, method)(*arguments)
        if method.startswith("a"):
            asyncio.run(result)
    assert failure.value is first
    assert calls == ["embedding", "rerank", "subjective_grading"]
    assert service.active_lease_count == 0
    result = getattr(service, method)(*arguments)
    if method.startswith("a"):
        asyncio.run(result)
    assert calls == ["embedding", "rerank", "subjective_grading"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_release_all_keeps_a_different_snapshot_owned(asynchronous, tmp_path):
    service = RuntimeSnapshotService(SimpleNamespace(config=tmp_path))
    selected, other = uuid4(), uuid4()
    calls = []

    class Client:
        def close(self):
            calls.append("closed")

        async def aclose(self):
            self.close()

    for snapshot in (selected, other):
        service._leases[(snapshot, "embedding")] = SnapshotClientLease(snapshot_id=snapshot,
            task_kind="embedding", client=Client(), on_release=service._drop_lease)
    if asynchronous:
        assert asyncio.run(service.arelease_all(selected)) == 1
    else:
        assert service.release_all(selected) == 1
    assert calls == ["closed"] and service.active_lease_count == 1
    assert (other, "embedding") in service._leases
    service.close()
