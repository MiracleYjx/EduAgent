"""Windows inherited-pipe receiver protocol; synthetic grants only."""
import json
import os

import pytest

from backend.app.deployment.browser_pipe import BrowserPipe


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ABI")
def test_actual_pipe_handoff_and_owned_handle_release():
    import msvcrt
    reader, writer = os.pipe()
    # Duplicate because BrowserPipe owns the supplied handle after construction.
    owned = os.dup(writer)
    pipe = BrowserPipe(msvcrt.get_osfhandle(owned))
    os.close(writer)
    secret = "synthetic-one-time-grant"
    try:
        assert pipe("http://127.0.0.1:19492/#code=" + secret)
        with os.fdopen(reader, "r", encoding="utf-8") as stream:
            frame = json.loads(stream.readline())
            assert frame == {"type": "open_settings",
                             "authorization_url": "http://127.0.0.1:19492/#code=" + secret}
            pipe.close()
            assert stream.read() == ""
        assert pipe.stream.closed
    finally:
        pipe.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ABI")
def test_regular_file_cannot_receive_authorization(tmp_path):
    import msvcrt
    path = tmp_path / "not-a-pipe"
    with path.open("wb") as output:
        with pytest.raises(ValueError, match="NOT_PIPE"):
            BrowserPipe(msvcrt.get_osfhandle(output.fileno()))
        assert not output.closed
    assert path.read_bytes() == b""
