from __future__ import annotations

import sys


SUPPORTED_PYTHON = (3, 11)


def require_supported_python() -> None:
    current = sys.version_info[:2]
    if current != SUPPORTED_PYTHON:
        raise RuntimeError(
            "Enterprise Knowledge Agent requires Python 3.11. "
            f"Current interpreter: {sys.version.split()[0]} at {sys.executable}"
        )
