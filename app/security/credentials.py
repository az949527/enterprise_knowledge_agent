from __future__ import annotations

import base64
from pathlib import Path
import subprocess
import sys


DEFAULT_SERVICE = "EnterpriseKnowledgeAgent"

# 兜底平台（Linux 等）使用的本地混淆文件。这不是密码学加密，
# 只避免明文落盘；Windows/macOS 使用系统凭据库。
_FALLBACK_KEY = b"EnterpriseKnowledgeAgent::local-secret-v1"
_FALLBACK_FILE = "secrets.bin"
_FALLBACK_SUBDIR = "enterprise_knowledge_agent"

_WINDOWS_AVAILABLE = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _advapi32.CredWriteW.argtypes = [
            ctypes.POINTER(_CREDENTIALW),
            wintypes.DWORD,
        ]
        _advapi32.CredWriteW.restype = wintypes.BOOL
        _advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        _advapi32.CredReadW.restype = wintypes.BOOL
        _advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        _advapi32.CredDeleteW.restype = wintypes.BOOL
        _advapi32.CredFree.argtypes = [ctypes.c_void_p]
        _advapi32.CredFree.restype = None

        _CREDENTIAL_TYPE_GENERIC = 1
        _CRED_PERSIST_LOCAL_MACHINE = 2
        _WINDOWS_AVAILABLE = True
    except (ImportError, OSError, AttributeError):  # pragma: no cover - 依赖探测
        _WINDOWS_AVAILABLE = False

# 惰性探测：Windows 凭据管理器在部分环境（CI 服务会话、无交互登录、
# 远程桌面锁定等）不可用，CredWriteW 会失败。首次使用探测一次，
# 不可用时整个进程回退到本地混淆文件，避免应用崩溃。
_WINDOWS_PROBED = False
_WINDOWS_OK = False


def get_backend_name() -> str:
    if sys.platform == "win32" and _WINDOWS_AVAILABLE and _windows_backend_usable():
        return "windows_credential_manager"
    if sys.platform == "darwin":
        return "macos_keychain"
    return "fallback_file"


def get_secret(service: str, account: str) -> str:
    if not service or not account:
        return ""
    return _read_credential(service, account)


def set_secret(service: str, account: str, value: str) -> None:
    if not service or not account:
        return
    if value:
        _write_credential(service, account, value)
    else:
        delete_secret(service, account)


def delete_secret(service: str, account: str) -> None:
    if not service or not account:
        return
    _delete_credential(service, account)


def _read_credential(service: str, account: str) -> str:
    if sys.platform == "win32" and _WINDOWS_AVAILABLE and _windows_backend_usable():
        return _windows_read(service, account)
    if sys.platform == "darwin":
        return _macos_read(service, account)
    return _fallback_read(service, account)


def _write_credential(service: str, account: str, value: str) -> None:
    if sys.platform == "win32" and _WINDOWS_AVAILABLE and _windows_backend_usable():
        _windows_write(service, account, value)
    elif sys.platform == "darwin":
        _macos_write(service, account, value)
    else:
        _fallback_write(service, account, value)


def _delete_credential(service: str, account: str) -> None:
    if sys.platform == "win32" and _WINDOWS_AVAILABLE and _windows_backend_usable():
        _windows_delete(service, account)
    elif sys.platform == "darwin":
        _macos_delete(service, account)
    else:
        _fallback_delete(service, account)


def _windows_backend_usable() -> bool:
    global _WINDOWS_PROBED, _WINDOWS_OK
    if _WINDOWS_PROBED:
        return _WINDOWS_OK
    _WINDOWS_PROBED = True
    try:
        _windows_write(DEFAULT_SERVICE, "__probe__", "probe")
        _windows_delete(DEFAULT_SERVICE, "__probe__")
        _WINDOWS_OK = True
    except OSError:
        _WINDOWS_OK = False
    return _WINDOWS_OK


# ---------- Windows Credential Manager ----------

def _windows_target_name(service: str, account: str) -> str:
    return f"{service}\\{account}"


def _windows_write(service: str, account: str, value: str) -> None:
    import ctypes
    from ctypes import wintypes

    blob = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(blob)
    credential = _CREDENTIALW()
    credential.Type = _CREDENTIAL_TYPE_GENERIC
    credential.TargetName = _windows_target_name(service, account)
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        buffer,
        ctypes.POINTER(wintypes.BYTE),
    )
    credential.UserName = account
    if not _advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(ctypes.get_last_error(), "Windows Credential Manager 写入失败")


def _windows_read(service: str, account: str) -> str:
    import ctypes

    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not _advapi32.CredReadW(
        _windows_target_name(service, account),
        _CREDENTIAL_TYPE_GENERIC,
        0,
        ctypes.byref(pointer),
    ):
        return ""
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return blob.decode("utf-8", errors="replace")
    finally:
        _advapi32.CredFree(pointer)


def _windows_delete(service: str, account: str) -> None:
    # 凭据不存在时 CredDeleteW 返回 False，这里不视为错误。
    _advapi32.CredDeleteW(_windows_target_name(service, account), _CREDENTIAL_TYPE_GENERIC, 0)


# ---------- macOS Keychain ----------

def _macos_write(service: str, account: str, value: str) -> None:
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a", account,
            "-s", service,
            "-w", value,
            "-U",
        ],
        check=True,
        capture_output=True,
    )


def _macos_read(service: str, account: str) -> str:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a", account,
            "-s", service,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


def _macos_delete(service: str, account: str) -> None:
    subprocess.run(
        [
            "security",
            "delete-generic-password",
            "-a", account,
            "-s", service,
        ],
        capture_output=True,
    )


# ---------- 兜底：本地混淆文件 ----------

def _fallback_path() -> Path:
    return Path.home() / ".config" / _FALLBACK_SUBDIR / _FALLBACK_FILE


def _obfuscate(value: str) -> str:
    data = value.encode("utf-8")
    key = _FALLBACK_KEY
    repeated = (key * (len(data) // len(key) + 1))[: len(data)]
    masked = bytes(left ^ right for left, right in zip(data, repeated))
    return base64.b64encode(masked).decode("ascii")


def _deobfuscate(text: str) -> str:
    data = base64.b64decode(text.encode("ascii"))
    key = _FALLBACK_KEY
    repeated = (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(left ^ right for left, right in zip(data, repeated)).decode("utf-8")


def _fallback_read(service: str, account: str) -> str:
    import json

    path = _fallback_path()
    if not path.exists():
        return ""
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entry = store.get(service, {}).get(account)
    if not entry:
        return ""
    try:
        return _deobfuscate(entry)
    except (ValueError, UnicodeDecodeError):
        return ""


def _fallback_write(service: str, account: str, value: str) -> None:
    import json

    path = _fallback_path()
    store: dict[str, dict[str, str]] = {}
    if path.exists():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            store = {}
    store.setdefault(service, {})[account] = _obfuscate(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")


def _fallback_delete(service: str, account: str) -> None:
    import json

    path = _fallback_path()
    if not path.exists():
        return
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if service in store:
        store[service].pop(account, None)
        if not store[service]:
            store.pop(service, None)
        path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
