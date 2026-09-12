from __future__ import annotations

import ctypes
import os
import stat
import sys
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


class PrivateStoragePermissionError(OSError):
    """Raised when a sensitive path cannot be restricted to its owner."""


_OWNER_DIRECTORY_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
_OWNER_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PrivateStoragePermissionError(f"Refusing to secure symlinked path: {path}")


if sys.platform == "win32":
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL


def _windows_error(message: str, code: int | None = None) -> PrivateStoragePermissionError:
    error_code = int(code if code is not None else ctypes.get_last_error())
    detail = ctypes.FormatError(error_code).strip() if error_code else "unknown Windows error"
    return PrivateStoragePermissionError(error_code, f"{message}: {detail}")


@lru_cache(maxsize=1)
def _windows_current_user_sid() -> str:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _windows_error("Unable to open the current process token")
    try:
        required = wintypes.DWORD()
        _advapi32.GetTokenInformation(
            token, _TOKEN_USER, None, 0, ctypes.byref(required)
        )
        if not required.value:
            raise _windows_error("Unable to size the current user token")
        buffer = ctypes.create_string_buffer(required.value)
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise _windows_error("Unable to read the current user token")
        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        sid_text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise _windows_error("Unable to convert the current user SID")
        try:
            return str(sid_text.value)
        finally:
            _kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        _kernel32.CloseHandle(token)


def _windows_set_private_dacl(path: Path, *, directory: bool) -> None:
    inheritance = "OICI" if directory else ""
    user_sid = _windows_current_user_sid()
    sddl = (
        f"D:P(A;{inheritance};FA;;;SY)"
        f"(A;{inheritance};FA;;;{user_sid})"
    )
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_error("Unable to build a private security descriptor")
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not _advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise _windows_error("Unable to read the private DACL")
        if not dacl_present.value or not dacl.value:
            raise PrivateStoragePermissionError("Refusing to install a missing or null DACL")
        result = _advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise _windows_error(f"Unable to secure private path {path}", result)
    finally:
        _kernel32.LocalFree(descriptor)


def _windows_dacl_sddl(path: Path) -> str:
    descriptor = ctypes.c_void_p()
    result = _advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise _windows_error(f"Unable to inspect private path {path}", result)
    try:
        sddl = wintypes.LPWSTR()
        if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(sddl),
            None,
        ):
            raise _windows_error(f"Unable to serialize the DACL for {path}")
        try:
            return str(sddl.value)
        finally:
            _kernel32.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))
    finally:
        _kernel32.LocalFree(descriptor)


def _windows_dacl_is_private(path: Path) -> bool:
    sddl = _windows_dacl_sddl(path)
    if not sddl.startswith("D:P"):
        return False
    user_sid = _windows_current_user_sid()
    entries = []
    cursor = sddl.find("(")
    while cursor >= 0:
        end = sddl.find(")", cursor)
        if end < 0:
            return False
        fields = sddl[cursor + 1 : end].split(";")
        if len(fields) != 6:
            return False
        entries.append(fields)
        cursor = sddl.find("(", end + 1)
    if not entries:
        return False
    allowed_trustees = {"SY", "S-1-5-18", user_sid}
    trustees = {fields[5] for fields in entries}
    return (
        trustees <= allowed_trustees
        and {user_sid, "SY"} <= trustees
        and all(fields[0] == "A" and fields[2] == "FA" and "ID" not in fields[1] for fields in entries)
    )


def secure_private_directory(path: Path) -> Path:
    private_path = Path(path)
    private_path.mkdir(parents=True, exist_ok=True, mode=_OWNER_DIRECTORY_MODE)
    _reject_symlink(private_path)
    if sys.platform == "win32":
        _windows_set_private_dacl(private_path, directory=True)
    else:
        os.chmod(private_path, _OWNER_DIRECTORY_MODE, follow_symlinks=False)
    return private_path


def secure_private_file(path: Path) -> Path:
    private_path = Path(path)
    if not private_path.is_file():
        raise FileNotFoundError(private_path)
    _reject_symlink(private_path)
    if sys.platform == "win32":
        _windows_set_private_dacl(private_path, directory=False)
    else:
        os.chmod(private_path, _OWNER_FILE_MODE, follow_symlinks=False)
    return private_path


def secure_private_tree(path: Path) -> Path:
    root = secure_private_directory(path)
    for child in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        _reject_symlink(child)
        if child.is_dir():
            secure_private_directory(child)
        elif child.is_file():
            secure_private_file(child)
    return root


def private_path_is_restricted(path: Path) -> bool:
    private_path = Path(path)
    if not private_path.exists() or private_path.is_symlink():
        return False
    if sys.platform == "win32":
        return _windows_dacl_is_private(private_path)
    return stat.S_IMODE(private_path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO) == 0
