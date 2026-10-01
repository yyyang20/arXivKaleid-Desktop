# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

from desktop.pipeline import runtime_path


class SecretError(RuntimeError):
    """仅包含固定安全信息，不携带凭据或底层异常正文。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise SecretError("API Key 加密保存仅支持 Windows 当前用户。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    try:
        # 只用 UI_FORBIDDEN；不设置 LOCAL_MACHINE，绑定当前 Windows 用户。
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise SecretError("API Key 加密或解密失败。")
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if result.pbData:
            ctypes.memset(result.pbData, 0, result.cbData)
            kernel32.LocalFree(result.pbData)


class SecretStore:
    def load(self) -> str:
        try:
            path = runtime_path("config", "secret.dat")
            if not path.exists():
                return ""
            return _dpapi(path.read_bytes(), decrypt=True).decode("utf-8")
        except Exception:
            raise SecretError("无法恢复 API Key，请重新输入后保存。") from None

    def save(self, value: str) -> None:
        temporary: Path | None = None
        try:
            path = runtime_path("config", "secret.dat")
            # 先加密，再建立临时文件；磁盘只接触 DPAPI 密文。
            encrypted = _dpapi(value.encode("utf-8"), decrypt=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="secret-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, runtime_path("config", "secret.dat"))
        except Exception:
            raise SecretError("API Key 保存失败，请检查运行目录权限后重新输入。") from None
        finally:
            # 只清理本次创建的密文临时文件，不删除用户已有 Secret。
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
