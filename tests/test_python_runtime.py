from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from app.core import runtime
from app.core.runtime import SUPPORTED_PYTHON, require_supported_python


class PythonRuntimeTests(unittest.TestCase):
    def test_project_runs_on_declared_python_version(self) -> None:
        self.assertEqual(sys.version_info[:2], SUPPORTED_PYTHON)
        require_supported_python()

    def test_unsupported_python_is_rejected(self) -> None:
        with patch.object(runtime.sys, "version_info", (3, 8, 5)):
            with self.assertRaisesRegex(RuntimeError, "requires Python 3.11"):
                require_supported_python()


if __name__ == "__main__":
    unittest.main()
