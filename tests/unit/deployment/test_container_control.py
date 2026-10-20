"""T235 restricted container management messages; no socket or Docker mocks."""
from unittest.mock import Mock

import pytest

from backend.app.deployment.container_control import dispatch
from backend.app.deployment.local_management import LocalManagementError


@pytest.mark.parametrize("url", ["http://example.com:8000", "http://127.0.0.1:8000/path",
    "http://127.0.0.1:8000/?x=1", "http://u:p@127.0.0.1:8000", "http://127.0.0.1:8000/#x",
    "file:///tmp/setup", "http://127.0.0.1:99999", "http://127.0.0.1:0"])
def test_invalid_operator_url_cannot_issue_grant(url):
    authority = Mock()
    with pytest.raises(LocalManagementError):
        dispatch(authority, {"type": "open_settings", "settings_base_url": url})
    authority.handle_control_message.assert_not_called()


@pytest.mark.parametrize("message", [[], {}, {"type": "shutdown"},
    {"type": "open_settings", "settings_base_url": "http://127.0.0.1:8000", "extra": True}])
def test_only_fixed_authorization_messages_are_accepted(message):
    authority = Mock()
    with pytest.raises(LocalManagementError):
        dispatch(authority, message)
    authority.handle_control_message.assert_not_called()


def test_valid_loopback_operator_request_reuses_authority():
    authority = Mock()
    message = {"type": "reauthorize", "settings_base_url": "http://127.0.0.1:18000"}
    assert dispatch(authority, message) is authority.handle_control_message.return_value
    authority.handle_control_message.assert_called_once_with(message)
