from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.desktop.main import run_desktop


if __name__ == "__main__":
    raise SystemExit(
        run_desktop(
            smoke_test="--smoke-test" in sys.argv,
            ui_test="--ui-test" in sys.argv,
        )
    )
