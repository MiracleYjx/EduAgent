"""CurrentUser DPAPI and private NTFS DACLs, using only Windows system DLLs.

No machine-scope option, interactive prompt, plaintext backend or environment
fallback exists. Python-owned immutable strings cannot be reliably zeroed;
owned native plaintext buffers are wiped before release. DLLs load lazily so
non-Windows Bootstrap/imports remain possible without pretending DPAPI works.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_PVOID = ctypes.c_void_p
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_ubyte
_MAX_BLOB = 16 * 1024 * 1024
_UI_FORBIDDEN = 1
_DACL_INFORMATION = 4
_PROTECTED_DACL_INFORMATION = 0x80000000
_SYSTEM_SID = "S-1-5-18"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _DWORD), ("pbData", ctypes.POINTER(_BYTE))]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", _PVOID), ("Attributes", _DWORD)]


class _ACL(ctypes.Structure):
    _fields_ = [("AclRevision", _BYTE), ("Sbz1", _BYTE), ("AclSize", _WORD),
                ("AceCount", _WORD), ("Sbz2", _WORD)]


class _ALLOW_ACE(ctypes.Structure):
    _fields_ = [("AceType", _BYTE), ("AceFlags", _BYTE), ("AceSize", _WORD),
                ("Mask", _DWORD), ("SidStart", _DWORD)]


class WindowsCredentialError(RuntimeError):
    """Only stable codes and numeric OS errors; never FormatMessage/SDK text."""

    def __init__(self, code: str, *, stage: str, winerror: int | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.winerror = winerror


def _load_library(name: str) -> Any:
    if sys.platform != "win32":
        raise WindowsCredentialError("WINDOWS_CREDENTIAL_BACKEND_UNAVAILABLE", stage="load")
    # LOAD_LIBRARY_SEARCH_SYSTEM32, not cwd/PATH or a writable application DLL.
    return ctypes.WinDLL(name, use_last_error=True, winmode=0x800)


def _last_error() -> int:
    return ctypes.get_last_error()


def _bind(library: Any, name: str, arguments: list[Any], result: Any) -> Any:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _copy_output(blob: _DATA_BLOB) -> bytes:
    if not blob.pbData or not 0 < blob.cbData <= _MAX_BLOB:
        raise WindowsCredentialError("DPAPI_INVALID_OUTPUT", stage="copy")
    return ctypes.string_at(blob.pbData, blob.cbData)


class WindowsDataProtector:
    """The only production protector: current logon user, non-interactive."""

    def __init__(self) -> None:
        try:
            crypt32, kernel32 = _load_library("crypt32"), _load_library("kernel32")
            pointer = ctypes.POINTER(_DATA_BLOB)
            common = [pointer, ctypes.c_wchar_p, pointer, _PVOID, _PVOID, _DWORD, pointer]
            self._protect = _bind(crypt32, "CryptProtectData", common, _BOOL)
            self._unprotect = _bind(crypt32, "CryptUnprotectData",
                                    [pointer, ctypes.POINTER(ctypes.c_wchar_p), pointer,
                                     _PVOID, _PVOID, _DWORD, pointer], _BOOL)
            self._local_free = _bind(kernel32, "LocalFree", [_PVOID], _PVOID)
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError, ctypes.ArgumentError):
            raise WindowsCredentialError("WINDOWS_CREDENTIAL_BACKEND_UNAVAILABLE", stage="load") from None

    def _transform(self, value: bytes, *, decrypt: bool) -> bytes:
        if not isinstance(value, bytes) or not 0 < len(value) <= _MAX_BLOB:
            raise WindowsCredentialError("DPAPI_INVALID_INPUT", stage="input")
        incoming_buffer = (_BYTE * len(value)).from_buffer_copy(value)
        incoming = _DATA_BLOB(len(value), ctypes.cast(incoming_buffer, ctypes.POINTER(_BYTE)))
        outgoing = _DATA_BLOB()
        stage = "unprotect" if decrypt else "protect"
        try:
            try:
                operation = self._unprotect if decrypt else self._protect
                if not operation(ctypes.byref(incoming), None, None, None, None,
                                 _UI_FORBIDDEN, ctypes.byref(outgoing)):
                    raise WindowsCredentialError("DPAPI_DECRYPT_FAILED" if decrypt else "DPAPI_ENCRYPT_FAILED",
                                                 stage=stage, winerror=_last_error())
                return _copy_output(outgoing)
            except WindowsCredentialError:
                raise
            except (OSError, ValueError, RuntimeError, TypeError, ctypes.ArgumentError):
                raise WindowsCredentialError("DPAPI_OPERATION_FAILED", stage=stage) from None
        finally:
            if not decrypt:
                ctypes.memset(incoming_buffer, 0, len(value))
            if outgoing.pbData:
                if decrypt and 0 < outgoing.cbData <= _MAX_BLOB:
                    ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                if self._local_free(ctypes.cast(outgoing.pbData, _PVOID)):
                    raise WindowsCredentialError("WINDOWS_MEMORY_RELEASE_FAILED", stage="release") from None

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, decrypt=False)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, decrypt=True)


class WindowsFileSecurity:
    """Protected DACL: exactly current process user and SYSTEM, full control.

    secure is used only at creation/write boundaries. verify is read-only and
    refuses inherited, null, broader or otherwise unexpected ACEs; it never
    repairs permissions while serving status/get requests.
    """

    def __init__(self) -> None:
        try:
            advapi32, kernel32 = _load_library("advapi32"), _load_library("kernel32")
            pp = ctypes.POINTER(_PVOID)
            self._local_free = _bind(kernel32, "LocalFree", [_PVOID], _PVOID)
            self._process = _bind(kernel32, "GetCurrentProcess", [], _HANDLE)
            self._close = _bind(kernel32, "CloseHandle", [_HANDLE], _BOOL)
            self._open_token = _bind(advapi32, "OpenProcessToken", [_HANDLE, _DWORD, pp], _BOOL)
            self._token_info = _bind(advapi32, "GetTokenInformation",
                                    [_HANDLE, ctypes.c_int32, _PVOID, _DWORD, ctypes.POINTER(_DWORD)], _BOOL)
            self._convert_sid = _bind(advapi32, "ConvertSidToStringSidW", [_PVOID, pp], _BOOL)
            self._convert_sd = _bind(advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                                    [ctypes.c_wchar_p, _DWORD, pp, ctypes.POINTER(_DWORD)], _BOOL)
            self._set_sd = _bind(advapi32, "SetFileSecurityW", [ctypes.c_wchar_p, _DWORD, _PVOID], _BOOL)
            self._get_sd = _bind(advapi32, "GetNamedSecurityInfoW",
                                [ctypes.c_wchar_p, ctypes.c_int32, _DWORD, pp, pp, pp, pp, pp], _DWORD)
            self._get_control = _bind(advapi32, "GetSecurityDescriptorControl",
                                     [_PVOID, ctypes.POINTER(_WORD), ctypes.POINTER(_DWORD)], _BOOL)
            self._get_ace = _bind(advapi32, "GetAce", [_PVOID, _DWORD, pp], _BOOL)
            self._user_sid = self._current_user_sid()
        except WindowsCredentialError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError, ctypes.ArgumentError):
            raise WindowsCredentialError("WINDOWS_CREDENTIAL_BACKEND_UNAVAILABLE", stage="acl_load") from None

    def _free(self, pointer: _PVOID) -> None:
        if pointer and self._local_free(pointer):
            raise WindowsCredentialError("WINDOWS_MEMORY_RELEASE_FAILED", stage="acl_release") from None

    def _sid_string(self, sid: _PVOID) -> str:
        text = _PVOID()
        try:
            if not self._convert_sid(sid, ctypes.byref(text)):
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="sid", winerror=_last_error())
            if not text:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="sid")
            return ctypes.wstring_at(text)
        finally:
            self._free(text)

    def _current_user_sid(self) -> str:
        token = _HANDLE()
        try:
            if not self._open_token(self._process(), 8, ctypes.byref(token)):  # TOKEN_QUERY
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="token", winerror=_last_error())
            length = _DWORD()
            # TokenUser requires a sized buffer. No fallback to login/env names.
            result = self._token_info(token, 1, None, 0, ctypes.byref(length))
            if result or _last_error() != 122 or not ctypes.sizeof(_SID_AND_ATTRIBUTES) <= length.value <= 65536:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="token_size")
            buffer = ctypes.create_string_buffer(length.value)
            if not self._token_info(token, 1, buffer, length.value, ctypes.byref(length)):
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="token", winerror=_last_error())
            user = ctypes.cast(buffer, ctypes.POINTER(_SID_AND_ATTRIBUTES)).contents
            if not user.Sid:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="sid")
            return self._sid_string(user.Sid)
        finally:
            if token and not self._close(token):
                raise WindowsCredentialError("WINDOWS_HANDLE_RELEASE_FAILED", stage="token_release") from None

    def secure(self, path: Path, *, directory: bool) -> None:
        descriptor = _PVOID()
        inheritance = "OICI" if directory else ""
        sddl = f"D:P(A;{inheritance};FA;;;{self._user_sid})(A;{inheritance};FA;;;SY)"
        try:
            if not self._convert_sd(sddl, 1, ctypes.byref(descriptor), None):
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_build", winerror=_last_error())
            if not self._set_sd(str(path), _DACL_INFORMATION | _PROTECTED_DACL_INFORMATION, descriptor):
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_set", winerror=_last_error())
        except WindowsCredentialError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, ctypes.ArgumentError):
            raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_set") from None
        finally:
            self._free(descriptor)
        self.verify(path, directory=directory)

    def verify(self, path: Path, *, directory: bool) -> None:
        descriptor, dacl = _PVOID(), _PVOID()
        try:
            result = self._get_sd(str(path), 1, _DACL_INFORMATION, None, None,
                                  ctypes.byref(dacl), None, ctypes.byref(descriptor))
            if result or not descriptor or not dacl:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_read", winerror=int(result))
            control, revision = _WORD(), _DWORD()
            if not self._get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)) or not control.value & 0x1000:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNSAFE", stage="acl_verify")
            acl = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
            if acl.AceCount != 2:
                raise WindowsCredentialError("CREDENTIAL_ACL_UNSAFE", stage="acl_verify")
            trustees = []
            for index in range(acl.AceCount):
                address = _PVOID()
                if not self._get_ace(dacl, index, ctypes.byref(address)) or not address:
                    raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_verify")
                ace = ctypes.cast(address, ctypes.POINTER(_ALLOW_ACE)).contents
                if ace.AceType != 0 or ace.AceFlags != (3 if directory else 0) or ace.Mask != 0x1F01FF:
                    raise WindowsCredentialError("CREDENTIAL_ACL_UNSAFE", stage="acl_verify")
                # ACCESS_ALLOWED_ACE SidStart follows ACE_HEADER (4) + ACCESS_MASK (4).
                if address.value is None:
                    raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_verify")
                trustees.append(self._sid_string(_PVOID(address.value + _ALLOW_ACE.SidStart.offset)))
            if sorted(trustees) != sorted([self._user_sid, _SYSTEM_SID]):
                raise WindowsCredentialError("CREDENTIAL_ACL_UNSAFE", stage="acl_verify")
        except WindowsCredentialError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, ctypes.ArgumentError):
            raise WindowsCredentialError("CREDENTIAL_ACL_UNAVAILABLE", stage="acl_verify") from None
        finally:
            self._free(descriptor)
