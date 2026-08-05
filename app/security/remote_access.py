from __future__ import annotations


# Remote access is disabled until an entry point explicitly enables it.
_REMOTE_ACCESS_ENABLED = False


def set_remote_access(enabled: bool) -> None:
    global _REMOTE_ACCESS_ENABLED
    _REMOTE_ACCESS_ENABLED = bool(enabled)


def remote_access_enabled() -> bool:
    return _REMOTE_ACCESS_ENABLED
