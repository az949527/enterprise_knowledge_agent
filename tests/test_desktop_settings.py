from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.desktop import main as desktop_main


class MemorySettings:
    values: dict[str, object] = {}

    def __init__(self, *_args) -> None:
        self._values = dict(self.values)

    def setFallbacksEnabled(self, _enabled: bool) -> None:
        return None

    def value(self, key: str, default=None, value_type=None):
        value = self._values.get(key, default)
        if value_type is bool:
            return bool(value)
        if value_type is str:
            return str(value)
        return value

    def setValue(self, key: str, value) -> None:
        self._values[key] = value

    def remove(self, key: str) -> None:
        self._values.pop(key, None)

    def sync(self) -> None:
        return None


class DesktopSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @patch.object(desktop_main, "QSettings", MemorySettings)
    def test_save_button_shows_dirty_and_saved_states(self) -> None:
        window = desktop_main.MainWindow()
        try:
            self.assertFalse(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "设置已保存")

            window.embedding_model_input.setText("embedding-test")
            self.assertTrue(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "保存设置")
            self.assertEqual(window.save_feedback.text(), "有未保存的更改")

            window.save_button.click()
            self.assertFalse(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "设置已保存")
            self.assertIn("已保存", window.save_feedback.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
