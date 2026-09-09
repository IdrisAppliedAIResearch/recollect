"""Private pairing material stays in owner-only regular files."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def _path(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    for parent in path.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError("Pairing files cannot use linked parent directories.")
    return path


def _windows_open(path: Path, *, create: bool) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    dword = wintypes.DWORD
    handle_type = wintypes.HANDLE

    def function(library, name, args, result=wintypes.BOOL):
        value = getattr(library, name)
        value.argtypes = args
        value.restype = result
        return value

    close = function(kernel, "CloseHandle", [handle_type])
    free = function(kernel, "LocalFree", [pointer], pointer)
    current = function(kernel, "GetCurrentProcess", [], handle_type)
    open_token = function(
        security, "OpenProcessToken",
        [handle_type, dword, ctypes.POINTER(handle_type)],
    )
    token_info = function(
        security, "GetTokenInformation",
        [handle_type, ctypes.c_int, pointer, dword, ctypes.POINTER(dword)],
    )
    sid_string = function(
        security, "ConvertSidToStringSidW",
        [pointer, ctypes.POINTER(wintypes.LPWSTR)],
    )
    descriptor_from_string = function(
        security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        [wintypes.LPCWSTR, dword, ctypes.POINTER(pointer), ctypes.POINTER(dword)],
    )
    descriptor_dacl = function(
        security, "GetSecurityDescriptorDacl",
        [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer),
         ctypes.POINTER(wintypes.BOOL)],
    )
    set_security = function(
        security, "SetSecurityInfo",
        [handle_type, ctypes.c_int, dword, pointer, pointer, pointer, pointer],
        dword,
    )
    get_security = function(
        security, "GetSecurityInfo",
        [handle_type, ctypes.c_int, dword, ctypes.POINTER(pointer), pointer,
         pointer, pointer, ctypes.POINTER(pointer)], dword,
    )
    equal_sid = function(security, "EqualSid", [pointer, pointer])

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", dword), ("descriptor", pointer),
            ("inherit_handle", wintypes.BOOL),
        ]

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", dword), ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
            ("volume", dword), ("size_high", dword), ("size_low", dword),
            ("links", dword), ("index_high", dword), ("index_low", dword),
        ]

    create_file = function(
        kernel, "CreateFileW",
        [wintypes.LPCWSTR, dword, dword, ctypes.POINTER(SecurityAttributes),
         dword, dword, handle_type], handle_type,
    )
    file_info = function(
        kernel, "GetFileInformationByHandle",
        [handle_type, ctypes.POINTER(FileInformation)],
    )
    token = handle_type()
    if not open_token(current(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = pointer()
    opened = None
    try:
        needed = dword()
        token_info(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not token_info(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        owner_sid = ctypes.cast(buffer, ctypes.POINTER(pointer)).contents
        sid_text = wintypes.LPWSTR()
        if not sid_string(owner_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;;FA;;;{sid_text.value})"
            if not descriptor_from_string(
                sddl, 1, ctypes.byref(descriptor), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            free(ctypes.cast(sid_text, pointer))
        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor, False,
        )
        # The creation DACL is private before the first byte is written. An
        # exclusive, no-follow handle prevents replacement during validation.
        access = 0x80000000 | 0x00020000 | 0x00040000
        if create:
            access |= 0x40000000
        opened = create_file(
            str(path), access, 0, ctypes.byref(attributes) if create else None,
            1 if create else 3, 0x00200000 | 0x80, None,
        )
        if opened == ctypes.c_void_p(-1).value:
            opened = None
            raise ctypes.WinError(ctypes.get_last_error())
        info = FileInformation()
        if not file_info(opened, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & (0x400 | 0x10) or info.links != 1:
            raise ValueError("Pairing files must be regular files without links.")
        stored_owner = pointer()
        stored_descriptor = pointer()
        error = get_security(
            opened, 1, 1, ctypes.byref(stored_owner), None, None, None,
            ctypes.byref(stored_descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        try:
            if not equal_sid(owner_sid, stored_owner):
                raise ValueError("Pairing files must belong to the current user.")
        finally:
            free(stored_descriptor)
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = pointer()
        if not descriptor_dacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        error = set_security(opened, 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
        fd = msvcrt.open_osfhandle(opened, os.O_BINARY | os.O_RDWR)
        opened = None
        return fd
    finally:
        if opened is not None:
            close(opened)
        if descriptor:
            free(descriptor)
        close(token)


def _open(path: str | Path, *, create: bool) -> int:
    target = _path(path)
    if os.name == "nt":
        return _windows_open(target, create=create)
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(target, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Pairing files must be regular files without links.")
        if info.st_uid != os.getuid():
            raise ValueError("Pairing files must belong to the current user.")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(path: str | Path) -> bytes:
    with os.fdopen(_open(path, create=False), "rb") as source:
        data = source.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Pairing files exceed the supported size limit.")
    return data


def write_private(path: str | Path, data: bytes) -> None:
    with os.fdopen(_open(path, create=True), "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def replace_private(path: str | Path, data: bytes) -> None:
    target = _path(path)
    read_private(target)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
    try:
        write_private(temporary, data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
