"""TCR-S2-DPAPI-007: Win32 ABI/ownership contract using allocated ctypes doubles."""

import ctypes
import importlib
import struct
from types import SimpleNamespace

import pytest

from tests.support import credential_fakes
from tests.support.credential_fakes import (
    assert_no_secret,
    error_surfaces,
    required_symbol,
)

credential_sandbox = credential_fakes.credential_sandbox
secret_bank = credential_fakes.secret_bank
pytestmark = pytest.mark.usefixtures("credential_sandbox")
USER_SID = "S-1-5-21-1001-1002-1003-1004"


class Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class Win32:
    """OS facts only: no lifecycle, storage, or actual Crypt32 invocation."""

    def __init__(self, module):
        self.module = module
        self.allocations = {}
        self.freed = []
        self.closed = []
        self.calls = []
        self.fail = None
        self.output = b"opaque\x00bytes"
        self.copied_input = None
        self.sddl = None
        self.security_info = None
        self.flags = None
        self.control = 0x1000
        self.ace_flags = 3
        self.trustees = [1, 2]
        self.ace_type = 0
        self.token_sid = ctypes.c_uint32(1)
        self.acl = ctypes.create_string_buffer(struct.pack("<BBHHH", 2, 0, 8, 2, 0))
        self.aces = []
        self.libraries = {name: SimpleNamespace() for name in ("crypt32", "kernel32", "advapi32")}
        for library, callbacks in {
            "crypt32": {"CryptProtectData": self.protect, "CryptUnprotectData": self.unprotect},
            "kernel32": {"LocalFree": self.free, "GetCurrentProcess": lambda: -1, "CloseHandle": self.close},
            "advapi32": {"OpenProcessToken": self.open_token, "GetTokenInformation": self.token_information,
                          "ConvertSidToStringSidW": self.sid_string,
                          "ConvertStringSecurityDescriptorToSecurityDescriptorW": self.convert_descriptor,
                          "SetFileSecurityW": self.set_security, "GetNamedSecurityInfoW": self.get_security,
                          "GetSecurityDescriptorControl": self.get_control, "GetAce": self.get_ace},
        }.items():
            for name, callback in callbacks.items():
                setattr(self.libraries[library], name, Function(callback))

    def allocate(self, value):
        buffer = ctypes.create_unicode_buffer(value) if isinstance(value, str) else ctypes.create_string_buffer(value)
        address = ctypes.addressof(buffer)
        self.allocations[address] = (buffer, value)
        return address

    def assign(self, pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(value)

    def crypt(self, incoming, outgoing):
        blob = ctypes.cast(incoming, ctypes.POINTER(self.module._DATA_BLOB)).contents
        self.copied_input = ctypes.string_at(blob.pbData, blob.cbData)
        output = ctypes.cast(outgoing, ctypes.POINTER(self.module._DATA_BLOB)).contents
        output.cbData = len(self.output)
        address = self.allocate(self.output)
        output.pbData = ctypes.cast(address, ctypes.POINTER(ctypes.c_ubyte))
        return int(self.fail != "crypt")

    def protect(self, incoming, description, entropy, reserved, prompt, flags, outgoing):
        self.calls.append("protect")
        self.flags = flags
        assert description is None and entropy is None and reserved is None and prompt is None
        return self.crypt(incoming, outgoing)

    def unprotect(self, incoming, description, entropy, reserved, prompt, flags, outgoing):
        self.calls.append("unprotect")
        self.flags = flags
        assert description is None and entropy is None and reserved is None and prompt is None
        return self.crypt(incoming, outgoing)

    def free(self, pointer):
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        assert address in self.allocations
        _buffer, value = self.allocations.pop(address)
        self.freed.append((address, ctypes.string_at(address, len(value)) if isinstance(value, bytes) else None))
        return address if self.fail == "free" else None

    def open_token(self, process, access, token):
        assert process == -1 and access == 8
        self.assign(token, 101)
        return int(self.fail != "open_token")

    def close(self, handle):
        self.closed.append(handle.value if isinstance(handle, ctypes.c_void_p) else handle)
        return 1

    def token_information(self, token, kind, buffer, length, returned):
        assert kind == 1
        ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32))[0] = 64
        if buffer is None:
            return 0
        self.assign(buffer, ctypes.addressof(self.token_sid))
        return int(self.fail != "token_information")

    def sid_string(self, sid, output):
        marker = ctypes.cast(sid, ctypes.POINTER(ctypes.c_uint32)).contents.value
        value = USER_SID if marker == 1 else "S-1-5-18" if marker == 2 else "S-1-1-0"
        self.assign(output, self.allocate(value))
        return int(self.fail != "sid_string")

    def convert_descriptor(self, sddl, revision, output, length):
        assert revision == 1
        self.sddl = sddl
        self.assign(output, self.allocate(b"descriptor"))
        return int(self.fail != "convert_descriptor")

    def set_security(self, path, information, descriptor):
        self.security_info = information
        return int(self.fail != "set_security")

    def get_security(self, path, kind, information, owner, group, dacl, sacl, descriptor):
        assert kind == 1 and information == 4
        self.assign(dacl, 0 if self.fail == "null_dacl" else ctypes.addressof(self.acl))
        self.assign(descriptor, self.allocate(b"read-descriptor"))
        return 5 if self.fail == "get_security" else 0

    def get_control(self, descriptor, control, revision):
        ctypes.cast(control, ctypes.POINTER(ctypes.c_uint16))[0] = self.control
        ctypes.cast(revision, ctypes.POINTER(ctypes.c_uint32))[0] = 1
        return 1

    def get_ace(self, acl, index, output):
        if self.fail == "get_ace":
            return 0
        buffer = ctypes.create_string_buffer(struct.pack("<BBHII", self.ace_type, self.ace_flags, 12, 0x1F01FF, self.trustees[index]))
        self.aces.append(buffer)
        self.assign(output, ctypes.addressof(buffer))
        return 1


@pytest.fixture
def native(monkeypatch):
    required_symbol("backend.app.services.settings.windows_credentials", "WindowsDataProtector")
    module = importlib.import_module("backend.app.services.settings.windows_credentials")
    api = Win32(module)
    api.loader = module._load_library
    monkeypatch.setattr(module, "_load_library", lambda name: api.libraries[name])
    monkeypatch.setattr(module, "_last_error", lambda: 122)
    return module, api


@pytest.mark.parametrize("operation", ["protect", "unprotect"])
def test_dpapi_binary_abi_flags_and_output_ownership(native, operation):
    module, api = native
    protector = module.WindowsDataProtector()
    value = "synthetic-汉字\x00tail".encode()
    assert getattr(protector, operation)(value) == api.output
    assert api.copied_input == value
    assert api.flags == 1 and not api.flags & 4
    assert not api.allocations and len(api.freed) == 1
    assert api.freed[0][1] == (b"\x00" * len(api.output) if operation == "unprotect" else api.output)
    blob = module._DATA_BLOB
    assert ctypes.sizeof(blob._fields_[0][1]) == 4
    assert blob.pbData.offset == ctypes.sizeof(ctypes.c_void_p)
    function = getattr(api.libraries["crypt32"], "CryptProtectData" if operation == "protect" else "CryptUnprotectData")
    assert len(function.argtypes) == 7 and ctypes.sizeof(function.restype) == 4
    assert function.argtypes[0]._type_ is blob and function.argtypes[-1]._type_ is blob
    assert api.libraries["kernel32"].LocalFree.argtypes == [ctypes.c_void_p]
    assert api.libraries["kernel32"].LocalFree.restype is ctypes.c_void_p


@pytest.mark.parametrize("operation", ["protect", "unprotect"])
@pytest.mark.parametrize("failure", ["crypt", "copy", "free"])
def test_dpapi_failures_release_allocated_output_and_are_safe(native, monkeypatch, secret_bank, operation, failure):
    module, api = native
    protector = module.WindowsDataProtector()
    api.fail = failure
    if failure == "copy":
        def fail_copy(_blob):
            raise OSError(secret_bank["jwt"])
        monkeypatch.setattr(module, "_copy_output", fail_copy)
    with pytest.raises(RuntimeError) as error:
        getattr(protector, operation)(b"synthetic")
    assert len(api.freed) == 1 and not api.allocations
    if operation == "unprotect":
        assert api.freed[0][1] == b"\x00" * len(api.output)
    assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("operation", ["protect", "unprotect"])
@pytest.mark.parametrize("value", [b"", "not-bytes", None])
def test_invalid_blob_rejected_without_native_call(native, operation, value):
    module, api = native
    with pytest.raises(RuntimeError):
        getattr(module.WindowsDataProtector(), operation)(value)
    assert api.calls == []


@pytest.mark.parametrize("directory", [True, False])
def test_acl_is_protected_current_user_and_system_only(native, tmp_path, directory):
    module, api = native
    security = module.WindowsFileSecurity()
    api.ace_flags = 3 if directory else 0
    security.secure(tmp_path / "synthetic", directory=directory)
    inheritance = "OICI" if directory else ""
    assert api.sddl == f"D:P(A;{inheritance};FA;;;{USER_SID})(A;{inheritance};FA;;;SY)"
    assert api.security_info == 0x80000004
    assert api.closed == [101]
    assert not api.allocations
    assert api.libraries["advapi32"].SetFileSecurityW.argtypes == [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    assert api.libraries["kernel32"].CloseHandle.argtypes == [ctypes.c_void_p]


@pytest.mark.parametrize("failure", ["open_token", "token_information", "sid_string", "convert_descriptor", "set_security", "get_security", "get_ace", "null_dacl"])
def test_acl_errors_fail_closed_and_release_owned_memory(native, tmp_path, failure, secret_bank):
    module, api = native
    api.fail = failure
    with pytest.raises(RuntimeError) as error:
        module.WindowsFileSecurity().secure(tmp_path / "synthetic", directory=True)
    assert api.closed == [101]
    assert not api.allocations
    assert_no_secret(secret_bank, error_surfaces(error.value))


@pytest.mark.parametrize("violation", ["inherited", "everyone", "deny", "inheritance_flags"])
def test_acl_verification_rejects_broader_or_unprotected_permissions(native, tmp_path, violation):
    module, api = native
    security = module.WindowsFileSecurity()
    if violation == "inherited":
        api.control = 0
    elif violation == "everyone":
        api.trustees = [1, 3]
    elif violation == "deny":
        api.ace_type = 1
    else:
        api.ace_flags = 0x13
    with pytest.raises(RuntimeError):
        security.verify(tmp_path / "synthetic", directory=True)
    assert not api.allocations


def test_missing_dll_never_selects_another_backend(native, monkeypatch, secret_bank):
    module, api = native
    def unavailable(_name):
        raise OSError(secret_bank["jwt"])
    monkeypatch.setattr(module, "_load_library", unavailable)
    with pytest.raises(RuntimeError) as error:
        module.WindowsDataProtector()
    assert_no_secret(secret_bank, error_surfaces(error.value))
    assert api.calls == []


@pytest.mark.parametrize("operation", ["protect", "unprotect"])
def test_zero_length_native_output_is_rejected_and_freed(native, operation):
    module, api = native
    api.output = b""
    with pytest.raises(RuntimeError):
        getattr(module.WindowsDataProtector(), operation)(b"synthetic")
    assert len(api.freed) == 1 and not api.allocations


def test_loader_requests_system32_and_last_error_only(native, monkeypatch):
    module, api = native
    calls = []
    # Inspect the real loader helper, not the fixture's replacement.
    real_loader = api.loader

    def loader(name, **kwargs):
        calls.append((name, kwargs))
        return object()

    monkeypatch.setattr(module.ctypes, "WinDLL", loader, raising=False)
    real_loader("crypt32")
    assert calls == [("crypt32", {"use_last_error": True, "winmode": 0x800})]
